"""Cross-platform supervision for native development services."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
from threading import Thread
import time
from typing import Callable, IO, Sequence


MINIMUM_NODE_VERSION = (22, 13, 0)


class DevelopmentStackError(RuntimeError):
    """Raised when the native development stack cannot start."""


@dataclass(frozen=True, slots=True)
class DevelopmentServiceSpec:
    """One supervised development process."""

    name: str
    arguments: tuple[str, ...]
    working_directory: Path | None = None


@dataclass(frozen=True, slots=True)
class FrontendRuntime:
    """Validated executables and version for the management frontend."""

    node_executable: str
    npm_executable: str
    node_version: tuple[int, int, int]


@dataclass(slots=True)
class RunningService:
    """A child process and its output forwarding thread."""

    spec: DevelopmentServiceSpec
    process: subprocess.Popen[str]
    output_thread: Thread | None


def backend_service_specs(
    python_executable: str | None = None,
) -> tuple[DevelopmentServiceSpec, ...]:
    """Return the fixed five-process Python backend topology."""

    python = python_executable or sys.executable
    prefix = (python, "-u", "-m", "app")
    return (
        DevelopmentServiceSpec("listener", (*prefix, "listen")),
        DevelopmentServiceSpec(
            "detection-worker", (*prefix, "worker", "--forever")
        ),
        DevelopmentServiceSpec(
            "reminder-worker", (*prefix, "reminder-worker", "--forever")
        ),
        DevelopmentServiceSpec(
            "notification-worker",
            (*prefix, "task-notification-worker", "--forever"),
        ),
        DevelopmentServiceSpec(
            "management-api", (*prefix, "management-server")
        ),
    )


def full_service_specs(
    frontend: FrontendRuntime,
    *,
    project_root: Path | None = None,
    python_executable: str | None = None,
) -> tuple[DevelopmentServiceSpec, ...]:
    """Return all five backends plus the npm management frontend."""

    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    return (
        *backend_service_specs(python_executable),
        DevelopmentServiceSpec(
            "management-web",
            (frontend.npm_executable, "run", "dev"),
            working_directory=root / "management-web",
        ),
    )


def resolve_frontend_runtime(
    project_root: Path | None = None,
) -> FrontendRuntime:
    """Validate Node, npm, version, manifest, and installed dependencies."""

    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    frontend_root = root / "management-web"
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise DevelopmentStackError(
            "Node.js and npm are required; install Node.js 22.13 or newer"
        )
    try:
        result = subprocess.run(
            (node, "--version"),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DevelopmentStackError(
            f"could not execute Node.js at {node}"
        ) from exc
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip()
    )
    if match is None:
        raise DevelopmentStackError(
            f"could not parse Node.js version: {result.stdout.strip()!r}"
        )
    version = tuple(int(part) for part in match.groups())
    if version < MINIMUM_NODE_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_NODE_VERSION[:2])
        found = ".".join(str(part) for part in version)
        raise DevelopmentStackError(
            f"Node.js {minimum} or newer is required; found {found}"
        )
    if not (frontend_root / "package.json").is_file():
        raise DevelopmentStackError(
            f"management frontend is missing at {frontend_root}"
        )
    if not (frontend_root / "node_modules").is_dir():
        raise DevelopmentStackError(
            "management frontend dependencies are missing; run "
            "`cd management-web && npm ci` first"
        )
    return FrontendRuntime(node, npm, version)


def ensure_port_available(
    host: str,
    port: int,
    *,
    service_name: str = "management API",
) -> None:
    """Fail before spawning children when a required port is occupied."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        raise DevelopmentStackError(
            f"{service_name} address {host}:{port} is unavailable; "
            "stop the existing process or change its configured port"
        ) from exc
    finally:
        probe.close()


class DevelopmentServiceStack:
    """Start, monitor, and stop a fixed set of development children."""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        specs: Sequence[DevelopmentServiceSpec] | None = None,
        description: str = "backend services",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        sleeper: Callable[[float], None] = time.sleep,
        shutdown_timeout: float = 8.0,
    ) -> None:
        self.project_root = (
            project_root or Path(__file__).resolve().parents[1]
        ).resolve()
        self.specs = tuple(specs or backend_service_specs())
        self.description = description
        self._popen = popen_factory
        self._sleep = sleeper
        self.shutdown_timeout = shutdown_timeout
        self.running: list[RunningService] = []
        self._shutdown_requested = False
        self._shutdown_signal: int | None = None

    def run(self) -> int:
        """Block until interrupted or until any required child exits."""

        previous_handlers = self._install_shutdown_signal_handlers()
        try:
            self._start_all()
            print(
                f"All {len(self.specs)} {self.description} are running. "
                "Press Ctrl+C once to stop all of them.",
                flush=True,
            )
            while not self._shutdown_requested:
                for service in self.running:
                    return_code = service.process.poll()
                    if return_code is not None:
                        print(
                            f"[{service.spec.name}] exited unexpectedly "
                            f"with code {return_code}; stopping all services.",
                            file=sys.stderr,
                            flush=True,
                        )
                        return return_code if return_code != 0 else 1
                self._sleep(0.25)
            print("\nStopping all development services...", flush=True)
            return 0
        except KeyboardInterrupt:
            print("\nStopping all development services...", flush=True)
            return 0
        except (OSError, ValueError) as exc:
            raise DevelopmentStackError(
                f"could not start development services: {exc}"
            ) from exc
        finally:
            self._stop_all()
            self._restore_shutdown_signal_handlers(previous_handlers)

    def _install_shutdown_signal_handlers(
        self,
    ) -> tuple[tuple[int, signal.Handlers], ...]:
        """Turn process termination into the same supervised cleanup path.

        Each child starts in its own process group so a force-terminated
        supervisor would otherwise leave listeners and workers orphaned. The
        handlers only set a flag; the main polling loop performs all cleanup.
        """

        installed: list[tuple[int, signal.Handlers]] = []
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, self._request_shutdown)
            except (OSError, ValueError):
                # ``signal.signal`` is restricted to the main interpreter
                # thread. Tests or embedded callers may legitimately run the
                # supervisor elsewhere, where KeyboardInterrupt still works.
                continue
            installed.append((signum, previous))
        return tuple(installed)

    @staticmethod
    def _restore_shutdown_signal_handlers(
        handlers: tuple[tuple[int, signal.Handlers], ...],
    ) -> None:
        for signum, previous in handlers:
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                continue

    def _request_shutdown(self, signum: int, _frame: object) -> None:
        self._shutdown_requested = True
        self._shutdown_signal = signum

    def _start_all(self) -> None:
        child_environment = os.environ.copy()
        child_environment.setdefault("PYTHONIOENCODING", "utf-8")
        child_environment.setdefault("PYTHONUNBUFFERED", "1")
        for spec in self.specs:
            working_directory = spec.working_directory or self.project_root
            popen_options: dict[str, object] = {
                "cwd": str(working_directory),
                "env": child_environment,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if os.name == "nt":
                popen_options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_options["start_new_session"] = True
            process = self._popen(spec.arguments, **popen_options)
            thread = None
            if process.stdout is not None:
                thread = Thread(
                    target=_forward_output,
                    args=(spec.name, process.stdout),
                    name=f"{spec.name}-output",
                    daemon=True,
                )
                thread.start()
            self.running.append(RunningService(spec, process, thread))
            print(
                f"[{spec.name}] started (pid={process.pid})",
                flush=True,
            )

    def _stop_all(self) -> None:
        active = [item for item in self.running if item.process.poll() is None]
        for service in reversed(active):
            _request_graceful_stop(service.process)

        deadline = time.monotonic() + self.shutdown_timeout
        for service in reversed(active):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                service.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                service.process.kill()

        for service in self.running:
            if service.output_thread is not None:
                service.output_thread.join(timeout=1.0)
        if self.running:
            print("All development services stopped.", flush=True)


# Backward-compatible name for the already documented backend-only command.
DevelopmentBackendStack = DevelopmentServiceStack


def _forward_output(name: str, stream: IO[str]) -> None:
    """Make interleaved child output attributable to one service."""

    try:
        for line in stream:
            print(f"[{name}] {line}", end="", flush=True)
    finally:
        stream.close()


def _request_graceful_stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
    except (AttributeError, OSError, ProcessLookupError):
        process.terminate()
