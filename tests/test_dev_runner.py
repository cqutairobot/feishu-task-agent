"""Cross-platform native development backend supervisor tests."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.config import DatabaseSettings, ManagementWebSettings
from app.dev_runner import (
    DevelopmentBackendStack,
    DevelopmentStackError,
    FrontendRuntime,
    backend_service_specs,
    ensure_port_available,
    full_service_specs,
    resolve_frontend_runtime,
)
from app.main import _run_dev, _run_dev_backend, main


class FakeProcess:
    next_pid = 90_000

    def __init__(self, *, return_code: int | None = None) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.return_code = return_code
        self.stdout = io.StringIO("")
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if self.return_code is None:
            self.return_code = 0
        return self.return_code

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9


class DevelopmentBackendStackTest(unittest.TestCase):
    def test_fixed_topology_uses_current_python_interpreter(self) -> None:
        specs = backend_service_specs("/test/python")

        self.assertEqual(
            [item.name for item in specs],
            [
                "listener",
                "detection-worker",
                "reminder-worker",
                "notification-worker",
                "management-api",
            ],
        )
        self.assertEqual(
            specs[0].arguments,
            ("/test/python", "-u", "-m", "app", "listen"),
        )
        self.assertEqual(specs[1].arguments[-2:], ("worker", "--forever"))

    def test_full_topology_adds_frontend_with_its_own_working_directory(
        self,
    ) -> None:
        root = Path("/test/project")
        specs = full_service_specs(
            FrontendRuntime("/test/node", "/test/npm", (22, 13, 0)),
            project_root=root,
            python_executable="/test/python",
        )

        self.assertEqual(len(specs), 6)
        self.assertEqual(specs[-1].name, "management-web")
        self.assertEqual(specs[-1].arguments, ("/test/npm", "run", "dev"))
        self.assertEqual(
            specs[-1].working_directory,
            root / "management-web",
        )

    def test_occupied_management_port_is_rejected_before_start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        try:
            with self.assertRaises(DevelopmentStackError):
                ensure_port_available("127.0.0.1", port)
        finally:
            server.close()

    def test_one_child_exit_stops_every_other_backend(self) -> None:
        children: list[FakeProcess] = []

        def create_process(arguments: tuple[str, ...], **_: object) -> FakeProcess:
            failed = arguments[-1] == "management-server"
            process = FakeProcess(return_code=3 if failed else None)
            children.append(process)
            return process

        def stop_process(process: FakeProcess) -> None:
            process.return_code = 0

        runner = DevelopmentBackendStack(
            project_root=Path.cwd(),
            popen_factory=create_process,
            sleeper=lambda _: None,
        )
        with (
            patch("app.dev_runner._request_graceful_stop", stop_process),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = runner.run()

        self.assertEqual(exit_code, 3)
        self.assertEqual(len(children), 5)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_ctrl_c_stops_all_backends_and_returns_success(self) -> None:
        children: list[FakeProcess] = []

        def create_process(*_: object, **__: object) -> FakeProcess:
            process = FakeProcess()
            children.append(process)
            return process

        def stop_process(process: FakeProcess) -> None:
            process.return_code = 0

        runner = DevelopmentBackendStack(
            project_root=Path.cwd(),
            popen_factory=create_process,
            sleeper=MagicMock(side_effect=KeyboardInterrupt),
        )
        with (
            patch("app.dev_runner._request_graceful_stop", stop_process),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = runner.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(children), 5)
        self.assertTrue(all(child.poll() == 0 for child in children))


class FrontendRuntimeTest(unittest.TestCase):
    def test_valid_runtime_requires_manifest_and_installed_dependencies(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "management-web"
            frontend.mkdir()
            (frontend / "package.json").write_text("{}", encoding="utf-8")
            (frontend / "node_modules").mkdir()
            result = subprocess.CompletedProcess(
                args=("/test/node", "--version"),
                returncode=0,
                stdout="v22.13.1\n",
                stderr="",
            )
            with (
                patch(
                    "app.dev_runner.shutil.which",
                    side_effect=["/test/node", "/test/npm"],
                ),
                patch("app.dev_runner.subprocess.run", return_value=result),
            ):
                runtime = resolve_frontend_runtime(root)

        self.assertEqual(runtime.node_version, (22, 13, 1))
        self.assertEqual(runtime.npm_executable, "/test/npm")

    def test_old_node_version_is_rejected(self) -> None:
        result = subprocess.CompletedProcess(
            args=("/test/node", "--version"),
            returncode=0,
            stdout="v20.19.0\n",
            stderr="",
        )
        with (
            patch(
                "app.dev_runner.shutil.which",
                side_effect=["/test/node", "/test/npm"],
            ),
            patch("app.dev_runner.subprocess.run", return_value=result),
            self.assertRaises(DevelopmentStackError),
        ):
            resolve_frontend_runtime(Path("/unused"))

    def test_missing_node_modules_has_install_instruction(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "management-web"
            frontend.mkdir()
            (frontend / "package.json").write_text("{}", encoding="utf-8")
            result = subprocess.CompletedProcess(
                args=("/test/node", "--version"),
                returncode=0,
                stdout="v22.13.0\n",
                stderr="",
            )
            with (
                patch(
                    "app.dev_runner.shutil.which",
                    side_effect=["/test/node", "/test/npm"],
                ),
                patch("app.dev_runner.subprocess.run", return_value=result),
                self.assertRaisesRegex(DevelopmentStackError, "npm ci"),
            ):
                resolve_frontend_runtime(root)


class DevelopmentBackendCliTest(unittest.TestCase):
    def test_main_routes_full_dev_command(self) -> None:
        with patch("app.main._run_dev", return_value=0) as run:
            self.assertEqual(main(["dev"]), 0)

        run.assert_called_once_with()

    def test_main_routes_dev_backend_command(self) -> None:
        with patch("app.main._run_dev_backend", return_value=0) as run:
            self.assertEqual(main(["dev-backend"]), 0)

        run.assert_called_once_with()

    def test_preflight_migrates_once_then_runs_supervisor(self) -> None:
        database_context = MagicMock()
        database_context.__enter__.return_value = SimpleNamespace()
        manager = MagicMock()
        manager.run.return_value = 0
        management = ManagementWebSettings(enabled=True)

        with (
            patch("app.main.load_settings"),
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-dev-test.db", echo=False
                ),
            ),
            patch("app.main.load_detection_settings"),
            patch("app.main.load_detection_worker_settings"),
            patch("app.main.load_task_llm_settings"),
            patch("app.main.load_task_settings"),
            patch("app.main.load_lifecycle_settings"),
            patch("app.main.load_reminder_settings"),
            patch("app.main.load_reminder_worker_settings"),
            patch(
                "app.main.load_management_web_settings",
                return_value=management,
            ),
            patch("app.dev_runner.ensure_port_available") as port_check,
            patch(
                "app.main.open_database_runtime",
                return_value=database_context,
            ) as database,
            patch(
                "app.dev_runner.DevelopmentBackendStack",
                return_value=manager,
            ),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = _run_dev_backend()

        self.assertEqual(exit_code, 0)
        port_check.assert_called_once_with("127.0.0.1", 8000)
        database.assert_called_once()
        manager.run.assert_called_once_with()

    def test_disabled_management_api_fails_before_spawning(self) -> None:
        with (
            patch("app.main.load_settings"),
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-dev-test.db", echo=False
                ),
            ),
            patch("app.main.load_detection_settings"),
            patch("app.main.load_detection_worker_settings"),
            patch("app.main.load_task_llm_settings"),
            patch("app.main.load_task_settings"),
            patch("app.main.load_lifecycle_settings"),
            patch("app.main.load_reminder_settings"),
            patch("app.main.load_reminder_worker_settings"),
            patch(
                "app.main.load_management_web_settings",
                return_value=ManagementWebSettings(enabled=False),
            ),
            patch("app.dev_runner.DevelopmentBackendStack") as stack,
            redirect_stderr(io.StringIO()),
        ):
            exit_code = _run_dev_backend()

        self.assertEqual(exit_code, 2)
        stack.assert_not_called()

    def test_full_dev_preflight_adds_frontend_and_port_check(self) -> None:
        runtime = FrontendRuntime("/test/node", "/test/npm", (22, 13, 0))
        specs = tuple(backend_service_specs("/test/python"))
        stack = MagicMock()
        stack.run.return_value = 0
        with (
            patch("app.main._prepare_development_backends") as backend_check,
            patch(
                "app.dev_runner.resolve_frontend_runtime",
                return_value=runtime,
            ),
            patch("app.dev_runner.ensure_port_available") as port_check,
            patch("app.dev_runner.full_service_specs", return_value=specs),
            patch(
                "app.dev_runner.DevelopmentServiceStack",
                return_value=stack,
            ) as stack_type,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = _run_dev()

        self.assertEqual(exit_code, 0)
        backend_check.assert_called_once_with("dev")
        port_check.assert_called_once_with(
            "127.0.0.1", 3000, service_name="management frontend"
        )
        stack_type.assert_called_once_with(
            specs=specs,
            description="development services",
        )
        stack.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
