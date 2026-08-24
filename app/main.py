"""Command-line entry point for the Feishu Task Agent."""

from __future__ import annotations

import argparse
from datetime import timezone
import json
import math
import platform
import sys

from app.agent.context import TaskDetectionContext
from app.config import (
    SettingsError,
    load_detection_settings,
    load_detection_worker_settings,
    load_database_settings,
    load_lifecycle_settings,
    load_management_web_settings,
    load_reminder_settings,
    load_reminder_worker_settings,
    load_settings,
    load_task_llm_settings,
    load_task_settings,
)
from app.database.runtime import open_database_runtime
from app.identity.aliases import AliasBinding, AliasError, AliasRepository


def runtime_summary() -> str:
    """Return a non-sensitive summary used by the local smoke test."""

    return (
        "Feishu Task Agent local runtime is ready. "
        f"Python {platform.python_version()}"
    )


def _prepare_development_backends(command: str) -> None:
    """Validate shared settings, port, and migrations before spawning."""

    from app.dev_runner import ensure_port_available

    load_settings()
    database_settings = load_database_settings()
    load_detection_settings()
    load_detection_worker_settings()
    load_task_llm_settings()
    load_task_settings()
    load_lifecycle_settings()
    load_reminder_settings()
    load_reminder_worker_settings()
    management_settings = load_management_web_settings()
    if not management_settings.enabled:
        raise SettingsError(
            f"MANAGEMENT_WEB_ENABLED must be true for {command}"
        )
    ensure_port_available(
        management_settings.bind_host,
        management_settings.port,
    )
    # Complete migrations before several processes open the same SQLite file.
    with open_database_runtime(database_settings):
        pass


def _run_dev_backend() -> int:
    """Validate configuration, migrate once, and supervise five backends."""

    from app.dev_runner import DevelopmentBackendStack, DevelopmentStackError

    try:
        _prepare_development_backends("dev-backend")
        print("Configuration and database preflight passed.", flush=True)
        return DevelopmentBackendStack().run()
    except (DevelopmentStackError, OSError, SettingsError, ValueError) as exc:
        print(f"Development backend failed: {exc}", file=sys.stderr)
        return 2


def _run_dev() -> int:
    """Supervise all five Python backends and the management frontend."""

    from app.dev_runner import (
        DevelopmentServiceStack,
        DevelopmentStackError,
        ensure_port_available,
        full_service_specs,
        resolve_frontend_runtime,
    )

    try:
        _prepare_development_backends("dev")
        frontend = resolve_frontend_runtime()
        ensure_port_available(
            "127.0.0.1",
            3000,
            service_name="management frontend",
        )
        specs = full_service_specs(frontend)
        print(
            "Configuration, database, Node.js, npm, dependencies, and ports "
            "preflight passed.",
            flush=True,
        )
        return DevelopmentServiceStack(
            specs=specs,
            description="development services",
        ).run()
    except (DevelopmentStackError, OSError, SettingsError, ValueError) as exc:
        print(f"Development stack failed: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu-task-agent")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("check", help="check the local runtime")
    commands.add_parser("db-status", help="show non-sensitive database counts")
    commands.add_parser("listen", help="listen for Feishu messages")
    commands.add_parser(
        "dev",
        help="start all five backends and the management frontend",
    )
    commands.add_parser(
        "dev-backend",
        help="start and supervise all five native development backends",
    )
    llm_check = commands.add_parser(
        "llm-check", help="check the configured task-detection model"
    )
    llm_probe = llm_check.add_mutually_exclusive_group()
    llm_probe.add_argument(
        "--probe",
        action="store_true",
        help="also detect a built-in fictional conversation",
    )
    llm_probe.add_argument(
        "--batch-probe",
        action="store_true",
        help="detect two tasks in a built-in fictional conversation",
    )

    worker = commands.add_parser(
        "worker", help="run the durable task-detection Worker"
    )
    worker_mode = worker.add_mutually_exclusive_group(required=True)
    worker_mode.add_argument(
        "--once",
        action="store_true",
        help="claim and process at most one ready job",
    )
    worker_mode.add_argument(
        "--forever",
        action="store_true",
        help="poll continuously until interrupted",
    )
    worker.add_argument(
        "--job-id",
        type=int,
        help="claim only this exact ready job",
    )

    reminder_worker = commands.add_parser(
        "reminder-worker", help="send durable task reminders"
    )
    reminder_worker_mode = reminder_worker.add_mutually_exclusive_group(
        required=True
    )
    reminder_worker_mode.add_argument(
        "--once",
        action="store_true",
        help="claim and send at most one due reminder",
    )
    reminder_worker_mode.add_argument(
        "--forever",
        action="store_true",
        help="poll continuously until interrupted",
    )
    reminder_worker.add_argument(
        "--reminder-id",
        type=int,
        help="claim only this exact due reminder (with --once)",
    )

    notification_worker = commands.add_parser(
        "task-notification-worker",
        help="send durable private task notifications",
    )
    notification_worker_mode = notification_worker.add_mutually_exclusive_group(
        required=True
    )
    notification_worker_mode.add_argument(
        "--once",
        action="store_true",
        help="claim and send at most one due task notification",
    )
    notification_worker_mode.add_argument(
        "--forever",
        action="store_true",
        help="poll continuously until interrupted",
    )
    notification_worker.add_argument(
        "--notification-id",
        type=int,
        help="claim only this exact due task notification (with --once)",
    )

    queue_parser = commands.add_parser(
        "queue", help="inspect or manage detection jobs"
    )
    queue_commands = queue_parser.add_subparsers(
        dest="queue_command", required=True
    )
    queue_cancel = queue_commands.add_parser(
        "cancel", help="atomically cancel exact queued jobs"
    )
    queue_cancel.add_argument(
        "--job-id",
        type=int,
        action="append",
        required=True,
        help="queued job ID; repeat for multiple jobs",
    )
    queue_cancel.add_argument(
        "--reason",
        required=True,
        help="audit reason stored with the cancellation",
    )

    task_materialize = commands.add_parser(
        "task-materialize",
        help="materialize one exact successful detection run",
    )
    task_materialize.add_argument(
        "--run-id",
        type=int,
        required=True,
        help="successful detection run ID",
    )

    task_list = commands.add_parser(
        "task-list",
        help="list unfinished tasks from one exact chat",
    )
    task_list.add_argument("--chat-id", required=True, help="Feishu chat ID")
    task_list.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum tasks to return, 1-100 (default: 20)",
    )

    reminder = commands.add_parser(
        "reminder", help="plan or inspect durable task reminders"
    )
    reminder_commands = reminder.add_subparsers(
        dest="reminder_command", required=True
    )
    reminder_sync = reminder_commands.add_parser(
        "sync", help="idempotently synchronize reminder plans"
    )
    reminder_sync.add_argument(
        "--task-id",
        type=int,
        help="synchronize one exact task instead of all tasks",
    )
    reminder_list = reminder_commands.add_parser(
        "list", help="list every reminder audit row for one task"
    )
    reminder_list.add_argument(
        "--task-id", type=int, required=True, help="task ID"
    )
    reminder_probe = reminder_commands.add_parser(
        "probe",
        help="send a non-mutating delivery test for one task",
    )
    reminder_probe.add_argument(
        "--task-id", type=int, required=True, help="task ID"
    )

    task_context = commands.add_parser(
        "task-context",
        help="inspect the chat-isolated input for task detection",
    )
    task_context.add_argument("--chat-id", required=True, help="Feishu chat ID")
    task_context.add_argument(
        "--message-id",
        required=True,
        help="trigger message at the end of the context window",
    )
    task_context.add_argument(
        "--limit",
        type=int,
        default=30,
        help="maximum number of messages (default: 30)",
    )

    task_detect = commands.add_parser(
        "task-detect",
        help="run one structured task-detection call",
    )
    task_detect.add_argument("--chat-id", required=True, help="Feishu chat ID")
    task_detect.add_argument(
        "--message-id",
        required=True,
        help="trigger message at the end of the context window",
    )
    task_detect.add_argument(
        "--limit",
        type=int,
        default=30,
        help="maximum number of messages (default: 30)",
    )

    lifecycle_detect = commands.add_parser(
        "lifecycle-detect",
        help="read-only detection of updates to existing tasks",
    )
    lifecycle_detect.add_argument(
        "--chat-id", required=True, help="Feishu group chat ID"
    )
    lifecycle_detect.add_argument(
        "--message-id",
        required=True,
        help="trigger message at the end of the context window",
    )
    lifecycle_detect.add_argument(
        "--limit",
        type=int,
        default=30,
        help="maximum number of chat messages (default: 30)",
    )

    private_lifecycle_detect = commands.add_parser(
        "private-lifecycle-detect",
        help="read-only detection for one private task-code command",
    )
    private_lifecycle_detect.add_argument(
        "--message-id",
        required=True,
        help="stored P2P trigger message",
    )
    private_lifecycle_detect.add_argument(
        "--task-code",
        required=True,
        help="the single task code cited by the private message",
    )
    private_lifecycle_detect.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum number of private messages (default: 20)",
    )

    alias_parser = commands.add_parser(
        "alias", help="manage verified chat member aliases"
    )
    alias_commands = alias_parser.add_subparsers(
        dest="alias_command", required=True
    )

    alias_set = alias_commands.add_parser("set", help="bind a name to a member")
    alias_set.add_argument("--name", required=True, help="name used in the chat")
    alias_set.add_argument(
        "--message-id",
        help="use the chat and sender from this stored message",
    )
    alias_set.add_argument("--chat-id", help="Feishu chat ID")
    alias_set.add_argument("--open-id", help="Feishu user Open ID")
    alias_list = alias_commands.add_parser(
        "list", help="list configured aliases for a chat"
    )
    alias_list.add_argument("--chat-id", required=True, help="Feishu chat ID")

    alias_resolve = alias_commands.add_parser(
        "resolve", help="resolve a chat name to an Open ID"
    )
    alias_resolve.add_argument("--chat-id", required=True, help="Feishu chat ID")
    alias_resolve.add_argument("--name", required=True, help="name used in the chat")

    chat_admin_parser = commands.add_parser(
        "chat-admin", help="manage chat-scoped task administrators locally"
    )
    chat_admin_commands = chat_admin_parser.add_subparsers(
        dest="chat_admin_command", required=True
    )
    chat_admin_grant = chat_admin_commands.add_parser(
        "grant", help="grant one verified group member task-admin access"
    )
    chat_admin_grant.add_argument("--chat-id", required=True)
    chat_admin_grant.add_argument("--open-id", required=True)
    chat_admin_revoke = chat_admin_commands.add_parser(
        "revoke", help="revoke one member's task-admin access"
    )
    chat_admin_revoke.add_argument("--chat-id", required=True)
    chat_admin_revoke.add_argument("--open-id", required=True)
    chat_admin_list = chat_admin_commands.add_parser(
        "list", help="list active administrators for one group"
    )
    chat_admin_list.add_argument("--chat-id", required=True)

    management_parser = commands.add_parser(
        "management", help="inspect the authorized Phase 7A read model"
    )
    management_commands = management_parser.add_subparsers(
        dest="management_command", required=True
    )
    management_chats = management_commands.add_parser(
        "chats", help="list groups administered by one actor"
    )
    management_chats.add_argument("--actor-open-id", required=True)
    management_dashboard = management_commands.add_parser(
        "dashboard", help="show one authorized group's task summary"
    )
    management_dashboard.add_argument("--actor-open-id", required=True)
    management_dashboard.add_argument("--chat-id", required=True)
    management_tasks = management_commands.add_parser(
        "tasks", help="list tasks from one authorized group"
    )
    management_tasks.add_argument("--actor-open-id", required=True)
    management_tasks.add_argument("--chat-id", required=True)
    management_tasks.add_argument("--status", action="append", default=[])
    management_tasks.add_argument("--owner-open-id")
    management_tasks.add_argument("--query")
    deadline_filter = management_tasks.add_mutually_exclusive_group()
    deadline_filter.add_argument("--missing-deadline", action="store_true")
    deadline_filter.add_argument("--with-deadline", action="store_true")
    management_tasks.add_argument(
        "--deadline-before", help="timezone-aware ISO 8601 timestamp"
    )
    management_tasks.add_argument("--limit", type=int, default=50)
    management_tasks.add_argument("--offset", type=int, default=0)
    management_task = management_commands.add_parser(
        "task", help="show one authorized task with evidence and audit"
    )
    management_task.add_argument("--actor-open-id", required=True)
    management_task.add_argument("--chat-id", required=True)
    management_task.add_argument("--task-id", type=int, required=True)
    commands.add_parser(
        "management-server",
        help="run the loopback-only read management HTTP service",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a local check or start the Feishu WebSocket listener."""

    args = _build_parser().parse_args(argv)
    command = args.command or "check"

    if command == "check":
        print(runtime_summary())
        return 0

    if command == "llm-check":
        return _run_llm_check(
            probe=args.probe,
            batch_probe=args.batch_probe,
        )

    if command == "dev":
        return _run_dev()

    if command == "dev-backend":
        return _run_dev_backend()

    if command == "worker":
        return _run_detection_worker(args)

    if command == "reminder-worker":
        return _run_reminder_worker(args)

    if command == "task-notification-worker":
        return _run_task_notification_worker(args)

    if command == "management-server":
        return _run_management_server()

    if command in {
        "db-status",
        "alias",
        "chat-admin",
        "management",
        "queue",
        "task-context",
        "task-detect",
        "lifecycle-detect",
        "private-lifecycle-detect",
        "task-materialize",
        "task-list",
        "reminder",
    }:
        try:
            database_settings = load_database_settings()
            task_settings = (
                load_task_settings()
                if command == "task-materialize"
                else None
            )
            reminder_settings = (
                load_reminder_settings()
                if command in {"task-materialize", "reminder"}
                else None
            )
        except SettingsError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        with open_database_runtime(
            database_settings,
            task_settings=task_settings,
            reminder_settings=reminder_settings,
        ) as runtime:
            if command == "alias":
                return _run_alias_command(args, runtime.aliases)
            if command == "chat-admin":
                return _run_chat_admin_command(
                    args, runtime.chat_administrators
                )
            if command == "management":
                return _run_management_command(args, runtime.management)
            if command == "queue":
                return _run_queue_command(args, runtime.detection_queue)
            if command == "task-context":
                return _run_task_context(args, runtime)
            if command == "task-detect":
                return _run_task_detect(args, runtime)
            if command == "lifecycle-detect":
                return _run_lifecycle_detect(args, runtime)
            if command == "private-lifecycle-detect":
                return _run_private_lifecycle_detect(args, runtime)
            if command == "task-materialize":
                return _run_task_materialize(args, runtime.tasks)
            if command == "task-list":
                return _run_task_list(args, runtime.tasks)
            if command == "reminder":
                return _run_reminder_command(
                    args,
                    runtime.reminders,
                    task_repository=getattr(runtime, "tasks", None),
                )
            counts = runtime.repository.counts()
            print("SQLite message store is ready.")
            print(f"chats: {counts.chats}")
            print(f"users: {counts.users}")
            print(f"messages: {counts.messages}")
            print(f"aliases: {counts.aliases}")
            print(f"detection_jobs: {counts.detection_jobs}")
            print(f"detection_runs: {counts.detection_runs}")
            print(f"tasks: {counts.tasks}")
            print(
                "task_materializations: "
                f"{counts.task_materializations}"
            )
            print(f"task_reminders: {counts.task_reminders}")
        return 0

    try:
        settings = load_settings()
        database_settings = load_database_settings()
        detection_settings = load_detection_settings()
        reminder_settings = load_reminder_settings()
        lifecycle_settings = load_lifecycle_settings()
        management_web_settings = load_management_web_settings()
        lifecycle_llm_settings = (
            load_task_llm_settings()
            if lifecycle_settings.private_writes_enabled
            else None
        )
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # Keep the SDK import out of the smoke-check path.
    from app.feishu.directory import DirectoryService, FeishuDirectoryProvider
    from app.feishu.receiver import start_listener

    with open_database_runtime(
        database_settings,
        detection_settings,
        reminder_settings=reminder_settings,
        lifecycle_settings=lifecycle_settings,
        management_web_settings=management_web_settings,
        lifecycle_administrator_open_ids=settings.task_admin_open_ids,
        lifecycle_allowed_chat_ids=settings.allowed_chat_ids,
    ) as runtime:
        chat_administrators = getattr(
            runtime, "chat_administrators", None
        )
        directory = DirectoryService(
            FeishuDirectoryProvider(settings),
            runtime.repository,
        )
        lifecycle_detector = None
        lifecycle_commands = None
        management_commands = None
        card_actions = None
        if settings.task_card_actions_enabled:
            from app.tasks.card_actions import TaskCardActionProcessor

            card_actions = TaskCardActionProcessor(
                runtime.tasks,
                runtime.lifecycle_mutations,
                administrator_open_ids=settings.task_admin_open_ids,
                allowed_chat_ids=settings.allowed_chat_ids,
                chat_name_refresher=directory.refresh_chat_name,
                chat_administrators=chat_administrators,
            )
        if lifecycle_llm_settings is not None:
            from app.agent.context import TaskDetectionContextBuilder
            from app.agent.provider import OpenAICompatibleTaskDetector
            from app.lifecycle.context import (
                PrivateLifecycleDetectionContextBuilder,
            )
            from app.lifecycle.private_commands import (
                PrivateLifecycleCommandProcessor,
            )

            lifecycle_detector = OpenAICompatibleTaskDetector(
                lifecycle_llm_settings
            )
            lifecycle_commands = PrivateLifecycleCommandProcessor(
                runtime.tasks,
                PrivateLifecycleDetectionContextBuilder(
                    TaskDetectionContextBuilder(
                        runtime.repository,
                        runtime.aliases,
                    ),
                    runtime.aliases,
                ),
                lifecycle_detector,
                runtime.lifecycle_mutations,
                administrator_open_ids=settings.task_admin_open_ids,
                allowed_chat_ids=settings.allowed_chat_ids,
                context_limit=lifecycle_settings.context_limit,
                chat_administrators=chat_administrators,
            )
        management_auth = getattr(runtime, "management_auth", None)
        if management_web_settings.enabled and management_auth is not None:
            from app.management.commands import ManagementCommandProcessor

            management_commands = ManagementCommandProcessor(
                management_auth,
                public_base_url=management_web_settings.public_base_url,
            )
        try:
            start_listener(
                settings,
                runtime.ingestion,
                directory,
                runtime.aliases,
                runtime.tasks,
                lifecycle_commands,
                card_actions,
                chat_administrators,
                management_commands,
            )
        except KeyboardInterrupt:
            print("\nFeishu listener stopped.")
        finally:
            if lifecycle_detector is not None:
                lifecycle_detector.close()
    return 0


def _run_alias_command(
    args: argparse.Namespace, repository: AliasRepository
) -> int:
    """Execute an alias subcommand without requiring Feishu credentials."""

    try:
        if args.alias_command == "set":
            if args.message_id:
                if args.chat_id or args.open_id:
                    raise AliasError(
                        "use either --message-id or both --chat-id and --open-id"
                    )
                sender = repository.sender_for_message(args.message_id)
                chat_id = sender.chat_id
                open_id = sender.open_id
            else:
                if not args.chat_id or not args.open_id:
                    raise AliasError(
                        "--chat-id and --open-id are required without --message-id"
                    )
                chat_id = args.chat_id
                open_id = args.open_id

            binding = repository.bind(
                chat_id,
                open_id,
                args.name,
            )
            print("Alias saved.")
            _print_binding(binding)
            return 0

        if args.alias_command == "list":
            bindings = repository.list_for_chat(args.chat_id)
            if not bindings:
                print(f"No aliases configured for chat {args.chat_id}.")
                return 0
            for binding in bindings:
                _print_binding(binding)
                print()
            return 0

        binding = repository.resolve(args.chat_id, args.name)
        if binding is None:
            print(
                f'No confirmed member matches "{args.name}" in chat '
                f"{args.chat_id}."
            )
            return 1
        _print_binding(binding)
        return 0
    except AliasError as exc:
        print(f"Alias error: {exc}", file=sys.stderr)
        return 2


def _run_chat_admin_command(args: argparse.Namespace, repository: object) -> int:
    from app.management.access import ChatAdministratorError

    try:
        if args.chat_admin_command == "grant":
            result = repository.grant(args.chat_id, args.open_id)
            payload = {
                "action": result.action,
                "changed": result.changed,
                "administrator": _json_compatible(result.administrator),
            }
        elif args.chat_admin_command == "revoke":
            result = repository.revoke(args.chat_id, args.open_id)
            payload = {
                "action": result.action,
                "changed": result.changed,
                "administrator": _json_compatible(result.administrator),
            }
        else:
            payload = {
                "chat_id": args.chat_id,
                "administrators": _json_compatible(
                    repository.list_chat(args.chat_id)
                ),
            }
    except (ChatAdministratorError, ValueError) as exc:
        print(f"Chat administrator command failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _run_management_command(args: argparse.Namespace, api: object) -> int:
    from datetime import datetime

    from app.management.queries import (
        ManagementAccessDenied,
        ManagementQueryError,
    )

    try:
        if args.management_command == "chats":
            result = api.list_chats(args.actor_open_id)
        elif args.management_command == "dashboard":
            result = api.dashboard(args.actor_open_id, args.chat_id)
        elif args.management_command == "tasks":
            deadline_before = (
                None
                if args.deadline_before is None
                else datetime.fromisoformat(args.deadline_before)
            )
            missing_deadline = (
                True
                if args.missing_deadline
                else False
                if args.with_deadline
                else None
            )
            result = api.list_tasks(
                args.actor_open_id,
                args.chat_id,
                statuses=args.status,
                owner_open_id=args.owner_open_id,
                query=args.query,
                missing_deadline=missing_deadline,
                deadline_before=deadline_before,
                limit=args.limit,
                offset=args.offset,
            )
        else:
            result = api.task_detail(
                args.actor_open_id, args.chat_id, args.task_id
            )
    except (ManagementAccessDenied, ManagementQueryError, ValueError) as exc:
        print(f"Management read failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            _json_compatible(result),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_management_server() -> int:
    from app.feishu.directory import DirectoryService, FeishuDirectoryProvider
    from app.management.web import run_management_server

    try:
        feishu_settings = load_settings()
        database_settings = load_database_settings()
        web_settings = load_management_web_settings()
        reminder_settings = load_reminder_settings()
        with open_database_runtime(
            database_settings,
            reminder_settings=reminder_settings,
            management_web_settings=web_settings,
        ) as runtime:
            directory = DirectoryService(
                FeishuDirectoryProvider(feishu_settings),
                runtime.repository,
            )
            try:
                run_management_server(
                    web_settings,
                    runtime.management_auth,
                    runtime.management,
                    runtime.chat_administrators,
                    runtime.lifecycle_mutations,
                    runtime.management_task_creation,
                    runtime.chat_settings,
                    directory.refresh_strict,
                )
            except KeyboardInterrupt:
                print("\nManagement read server stopped cleanly.")
    except (OSError, SettingsError, ValueError) as exc:
        print(f"Management server failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _run_queue_command(args: argparse.Namespace, queue: object) -> int:
    from datetime import datetime, timezone

    from app.agent.queue import DetectionQueueError

    try:
        results = queue.cancel_jobs(
            tuple(args.job_id),
            reason=args.reason,
            cancelled_at=datetime.now(timezone.utc),
        )
    except DetectionQueueError as exc:
        print(f"Queue error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "cancellations": [
                    {
                        "job_id": item.job_id,
                        "changed": item.changed,
                        "status": item.status.value,
                        "cancelled_at": item.cancelled_at.isoformat(),
                        "reason": item.reason,
                    }
                    for item in results
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_task_materialize(
    args: argparse.Namespace, repository: object
) -> int:
    from datetime import datetime, timezone

    from app.tasks.repository import TaskMaterializationError

    try:
        result = repository.materialize_run(
            args.run_id,
            materialized_at=datetime.now(timezone.utc),
        )
    except (TaskMaterializationError, ValueError) as exc:
        print(f"Task materialization failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "detection_run_id": result.detection_run_id,
                "already_materialized": result.already_materialized,
                "candidate_count": result.candidate_count,
                "created_task_count": result.created_task_count,
                "reused_task_count": result.reused_task_count,
                "task_ids": list(result.task_ids),
                "materialized_at": result.materialized_at.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_task_list(args: argparse.Namespace, repository: object) -> int:
    from app.agent.context import SHANGHAI_TZ

    try:
        page = repository.list_open_tasks(
            args.chat_id,
            limit=args.limit,
        )
    except ValueError as exc:
        print(f"Task list failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "chat_id": page.chat_id,
                "total_count": page.total_count,
                "tasks": [
                    {
                        "id": task.task_id,
                        "task_code": task.public_code,
                        "title": task.title,
                        "owner_name": task.owner_name,
                        "owner_open_id": task.owner_open_id,
                        "assignees": [
                            {
                                "name": member.name,
                                "open_id": member.open_id,
                                "position": member.position,
                            }
                            for member in task.responsible_members
                        ],
                        "deadline": (
                            None
                            if task.deadline is None
                            else task.deadline.astimezone(
                                SHANGHAI_TZ
                            ).isoformat()
                        ),
                        "status": task.status.value,
                        "confidence": task.confidence,
                    }
                    for task in page.tasks
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_reminder_command(
    args: argparse.Namespace,
    repository: object,
    *,
    task_repository: object | None,
) -> int:
    from app.agent.context import SHANGHAI_TZ
    from app.feishu.reminder_sender import (
        FeishuReminderSender,
        ReminderDeliveryError,
    )

    try:
        if args.reminder_command == "sync":
            result = (
                repository.sync_all()
                if args.task_id is None
                else repository.sync_task(args.task_id)
            )
            print(
                json.dumps(
                    {
                        "tasks_scanned": result.tasks_scanned,
                        "task_statuses_changed": (
                            result.task_statuses_changed
                        ),
                        "reminders_created": result.reminders_created,
                        "reminders_cancelled": result.reminders_cancelled,
                        "active_reminders": result.active_reminders,
                        "synced_at": result.synced_at.isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.reminder_command == "probe":
            if task_repository is None:
                raise ValueError("task repository is unavailable")
            task = task_repository.get_task(args.task_id)
            if task is None:
                raise ValueError(f"task {args.task_id} does not exist")
            settings = load_settings()
            from uuid import uuid4

            receipt = FeishuReminderSender(settings).probe(
                task,
                probe_key=uuid4().hex,
                private_chat_id=repository.find_private_chat_id(
                    task.owner_open_id
                ),
            )
            print(
                json.dumps(
                    {
                        "task_id": task.task_id,
                        "message_id": receipt.message_id,
                        "receive_id_type": receipt.receive_id_type,
                        "receive_id": receipt.receive_id,
                        "private_error_code": receipt.private_error_code,
                        "formal_reminder_plan_changed": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        reminders = repository.list_for_task(args.task_id)
        print(
            json.dumps(
                {
                    "task_id": args.task_id,
                    "reminders": [
                        {
                            "id": reminder.reminder_id,
                            "kind": reminder.kind.value,
                            "deadline_snapshot": (
                                reminder.deadline_snapshot.astimezone(
                                    SHANGHAI_TZ
                                ).isoformat()
                            ),
                            "scheduled_for": (
                                reminder.scheduled_for.astimezone(
                                    SHANGHAI_TZ
                                ).isoformat()
                            ),
                            "status": reminder.status.value,
                            "attempt_count": reminder.attempt_count,
                            "max_attempts": reminder.max_attempts,
                            "sent_at": (
                                None
                                if reminder.sent_at is None
                                else reminder.sent_at.astimezone(
                                    SHANGHAI_TZ
                                ).isoformat()
                            ),
                            "feishu_message_id": (
                                reminder.feishu_message_id
                            ),
                            "delivery_receive_id_type": (
                                reminder.delivery_receive_id_type
                            ),
                            "delivery_receive_id": (
                                reminder.delivery_receive_id
                            ),
                            "last_error_code": reminder.last_error_code,
                            "cancel_reason": reminder.cancel_reason,
                        }
                        for reminder in reminders
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ReminderDeliveryError, SettingsError, ValueError) as exc:
        print(f"Reminder command failed: {exc}", file=sys.stderr)
        return 2


def _run_reminder_worker(args: argparse.Namespace) -> int:
    from os import getpid
    from socket import gethostname
    from time import sleep
    from uuid import uuid4

    from app.feishu.reminder_sender import FeishuReminderSender
    from app.reminders.worker import (
        ReminderWorker,
        ReminderWorkerStatus,
        run_reminder_worker_loop,
    )

    if args.forever and args.reminder_id is not None:
        print(
            "Reminder Worker failed: --reminder-id can only be used with --once",
            file=sys.stderr,
        )
        return 2

    try:
        database_settings = load_database_settings()
        reminder_settings = load_reminder_settings()
        worker_settings = load_reminder_worker_settings()
        feishu_settings = load_settings()
        worker_id = f"{gethostname()[:40]}:{getpid()}:{uuid4().hex[:12]}"
        with open_database_runtime(
            database_settings,
            reminder_settings=reminder_settings,
        ) as runtime:
            worker = ReminderWorker(
                runtime.reminders,
                FeishuReminderSender(
                    feishu_settings,
                    reminder_settings=reminder_settings,
                ),
                lease_seconds=worker_settings.lease_seconds,
                retry_base_seconds=worker_settings.retry_base_seconds,
            )
            if args.once:
                outcome = worker.run_once(
                    worker_id,
                    reminder_id=args.reminder_id,
                )
            else:
                print(
                    "Reminder Worker polling started. Press Ctrl+C to stop.",
                    file=sys.stderr,
                )
                try:
                    run_reminder_worker_loop(
                        worker,
                        worker_id,
                        poll_seconds=worker_settings.poll_seconds,
                        sleeper=sleep,
                        on_outcome=_print_worker_outcome,
                    )
                except KeyboardInterrupt:
                    print(
                        "\nReminder Worker stopped cleanly.",
                        file=sys.stderr,
                    )
                return 0
    except (SettingsError, ValueError) as exc:
        print(f"Reminder Worker failed: {exc}", file=sys.stderr)
        return 2

    _print_worker_outcome(outcome)
    return (
        0
        if outcome.status
        in {ReminderWorkerStatus.IDLE, ReminderWorkerStatus.SENT}
        else 1
    )


def _run_task_notification_worker(args: argparse.Namespace) -> int:
    from os import getpid
    from socket import gethostname
    from time import sleep
    from uuid import uuid4

    from app.feishu.task_notification_sender import (
        FeishuTaskNotificationSender,
    )
    from app.notifications.worker import (
        TaskNotificationWorker,
        TaskNotificationWorkerStatus,
        run_task_notification_worker_loop,
    )

    if args.forever and args.notification_id is not None:
        print(
            "Task notification Worker failed: --notification-id can only "
            "be used with --once",
            file=sys.stderr,
        )
        return 2
    try:
        database_settings = load_database_settings()
        reminder_settings = load_reminder_settings()
        worker_settings = load_reminder_worker_settings()
        feishu_settings = load_settings()
        worker_id = f"{gethostname()[:40]}:{getpid()}:{uuid4().hex[:12]}"
        with open_database_runtime(
            database_settings,
            reminder_settings=reminder_settings,
            lifecycle_administrator_open_ids=(
                feishu_settings.task_admin_open_ids
            ),
            lifecycle_allowed_chat_ids=feishu_settings.allowed_chat_ids,
        ) as runtime:
            worker = TaskNotificationWorker(
                runtime.notifications,
                FeishuTaskNotificationSender(
                    feishu_settings,
                    reminder_settings=reminder_settings,
                ),
                lease_seconds=worker_settings.lease_seconds,
                retry_base_seconds=worker_settings.retry_base_seconds,
            )
            if args.once:
                outcome = worker.run_once(
                    worker_id,
                    notification_id=args.notification_id,
                )
            else:
                print(
                    "Task notification Worker polling started. "
                    "Press Ctrl+C to stop.",
                    file=sys.stderr,
                )
                try:
                    run_task_notification_worker_loop(
                        worker,
                        worker_id,
                        poll_seconds=worker_settings.poll_seconds,
                        sleeper=sleep,
                        on_outcome=_print_task_notification_worker_outcome,
                    )
                except KeyboardInterrupt:
                    print(
                        "\nTask notification Worker stopped cleanly.",
                        file=sys.stderr,
                    )
                return 0
    except (SettingsError, ValueError) as exc:
        print(
            f"Task notification Worker failed: {exc}", file=sys.stderr
        )
        return 2

    _print_task_notification_worker_outcome(outcome)
    return (
        0
        if outcome.status
        in {
            TaskNotificationWorkerStatus.IDLE,
            TaskNotificationWorkerStatus.SENT,
        }
        else 1
    )


def _print_task_notification_worker_outcome(outcome: object) -> None:
    print(
        json.dumps(
            {
                "status": outcome.status.value,
                "notification_id": outcome.notification_id,
                "task_id": outcome.task_id,
                "kind": outcome.kind,
                "attempt": outcome.attempt,
                "receive_id_type": outcome.receive_id_type,
                "receive_id": outcome.receive_id,
                "feishu_message_id": outcome.feishu_message_id,
                "error_code": outcome.error_code,
                "retry_at": (
                    None
                    if outcome.retry_at is None
                    else outcome.retry_at.astimezone(
                        timezone.utc
                    ).isoformat()
                ),
            },
            ensure_ascii=False,
        )
    )


def _run_task_context(args: argparse.Namespace, runtime: object) -> int:
    from app.agent.context import TaskDetectionContextBuilder
    from app.agent.prompt import build_task_detection_input
    from app.database.repository import MessageLookupError
    from app.identity.aliases import AliasError

    try:
        context = TaskDetectionContextBuilder(
            runtime.repository,
            runtime.aliases,
            task_scope_resolver=getattr(
                getattr(runtime, "chat_settings", None), "task_scope", None
            ),
        ).build(
            args.chat_id,
            args.message_id,
            limit=args.limit,
        )
    except (AliasError, MessageLookupError, ValueError) as exc:
        print(f"Task context error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            build_task_detection_input(context),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_llm_check(
    *,
    probe: bool = False,
    batch_probe: bool = False,
) -> int:
    from app.agent.provider import ModelProviderError, OpenAICompatibleTaskDetector

    try:
        settings = load_task_llm_settings()
        with OpenAICompatibleTaskDetector(settings) as detector:
            models = detector.list_models()
            if batch_probe:
                call = detector.detect_batch(_fictional_batch_probe_context())
            elif probe:
                call = detector.detect(_fictional_probe_context())
            else:
                call = None
    except (SettingsError, ModelProviderError) as exc:
        print(f"LLM check failed: {exc}", file=sys.stderr)
        return 2

    if settings.model not in models:
        print(
            f'LLM check failed: configured model "{settings.model}" '
            "is not listed by the service.",
            file=sys.stderr,
        )
        return 1
    print("Task-detection model service is reachable.")
    print(f"model: {settings.model}")
    print(f"available_models: {len(models)}")
    if call is not None:
        print(f"structured_output: {call.response_format}")
        print(
            "Fictional batch probe result:"
            if batch_probe
            else "Fictional probe result:"
        )
        print(json.dumps(call.result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _run_detection_worker(args: argparse.Namespace) -> int:
    from os import getpid
    from socket import gethostname
    from time import sleep
    from uuid import uuid4

    from app.agent.context import TaskDetectionContextBuilder
    from app.agent.provider import OpenAICompatibleTaskDetector
    from app.agent.queue import DetectionQueueError
    from app.agent.worker import (
        DetectionWorker,
        WorkerOutcomeStatus,
        run_worker_loop,
    )

    if args.forever and args.job_id is not None:
        print(
            "Detection Worker failed: --job-id can only be used with --once",
            file=sys.stderr,
        )
        return 2

    try:
        database_settings = load_database_settings()
        llm_settings = load_task_llm_settings()
        worker_settings = load_detection_worker_settings()
        task_settings = load_task_settings()
        reminder_settings = load_reminder_settings()
        lease_seconds = _effective_worker_lease_seconds(
            worker_settings.lease_seconds,
            timeout_seconds=llm_settings.timeout_seconds,
            max_retries=llm_settings.max_retries,
        )
        worker_id = (
            f"{gethostname()[:40]}:{getpid()}:{uuid4().hex[:12]}"
        )
        with open_database_runtime(
            database_settings,
            task_settings=task_settings,
            reminder_settings=reminder_settings,
        ) as runtime:
            context_builder = TaskDetectionContextBuilder(
                runtime.repository,
                runtime.aliases,
                task_scope_resolver=getattr(
                    getattr(runtime, "chat_settings", None),
                    "task_scope",
                    None,
                ),
            )
            with OpenAICompatibleTaskDetector(llm_settings) as detector:
                worker = DetectionWorker(
                    runtime.detection_queue,
                    context_builder,
                    detector,
                    runtime.tasks,
                    model=llm_settings.model,
                    context_limit=worker_settings.context_limit,
                    lease_seconds=lease_seconds,
                    retry_base_seconds=worker_settings.retry_base_seconds,
                )
                if args.once:
                    outcome = worker.run_once(worker_id, job_id=args.job_id)
                else:
                    print(
                        "Detection Worker polling started. Press Ctrl+C to stop.",
                        file=sys.stderr,
                    )
                    try:
                        run_worker_loop(
                            worker,
                            worker_id,
                            poll_seconds=worker_settings.poll_seconds,
                            sleeper=sleep,
                            on_outcome=_print_worker_outcome,
                        )
                    except KeyboardInterrupt:
                        print(
                            "\nDetection Worker stopped cleanly.",
                            file=sys.stderr,
                        )
                    return 0
    except (DetectionQueueError, SettingsError, ValueError) as exc:
        print(f"Detection Worker failed: {exc}", file=sys.stderr)
        return 2

    _print_worker_outcome(outcome)
    return (
        0
        if outcome.status
        in {WorkerOutcomeStatus.IDLE, WorkerOutcomeStatus.COMPLETED}
        else 1
    )


def _print_worker_outcome(outcome: object) -> None:
    print(
        json.dumps(outcome.to_dict(), ensure_ascii=False),
        flush=True,
    )


def _effective_worker_lease_seconds(
    configured_seconds: int,
    *,
    timeout_seconds: float,
    max_retries: int,
) -> int:
    """Keep the lease longer than the provider's worst-case retry budget."""

    retry_sleep_seconds = 0.5 * ((2**max_retries) - 1)
    request_budget = (
        timeout_seconds * (max_retries + 1)
        + retry_sleep_seconds
        + 30
    )
    return min(3_600, max(configured_seconds, math.ceil(request_budget)))


def _fictional_probe_context() -> TaskDetectionContext:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.agent.context import ContextMessage, ContextParticipant

    timestamp = datetime(
        2026, 8, 22, 18, 46, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    return TaskDetectionContext(
        chat_id="oc_fictional_probe",
        trigger_message_id="om_probe_3",
        timezone="Asia/Shanghai",
        reference_time=timestamp,
        participants=(
            ContextParticipant("ou_probe_teacher", "林老师"),
            ContextParticipant("ou_probe_member", "小周"),
        ),
        messages=(
            ContextMessage(
                "om_probe_1",
                "ou_probe_teacher",
                "林老师",
                "测试报告还缺结果分析。",
                timestamp,
            ),
            ContextMessage(
                "om_probe_2",
                "ou_probe_member",
                "小周",
                "我来整理。",
                timestamp,
            ),
            ContextMessage(
                "om_probe_3",
                "ou_probe_teacher",
                "林老师",
                "好，周四之前交给我。",
                timestamp,
            ),
        ),
        focus_message_ids=("om_probe_1", "om_probe_2", "om_probe_3"),
    )


def _fictional_batch_probe_context() -> TaskDetectionContext:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.agent.context import ContextMessage, ContextParticipant

    timestamp = datetime(
        2026, 8, 22, 19, 10, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    return TaskDetectionContext(
        chat_id="oc_fictional_batch_probe",
        trigger_message_id="om_batch_probe_4",
        timezone="Asia/Shanghai",
        reference_time=timestamp,
        participants=(
            ContextParticipant("ou_batch_teacher", "林老师"),
            ContextParticipant("ou_batch_zhou", "小周"),
            ContextParticipant("ou_batch_li", "小李"),
        ),
        messages=(
            ContextMessage(
                "om_batch_probe_1",
                "ou_batch_teacher",
                "林老师",
                "小周负责整理测试报告，8月27日下班前完成。",
                timestamp,
            ),
            ContextMessage(
                "om_batch_probe_2",
                "ou_batch_teacher",
                "林老师",
                "小李负责整理数据字典，8月28日下班前完成。",
                timestamp,
            ),
            ContextMessage(
                "om_batch_probe_3",
                "ou_batch_zhou",
                "小周",
                "收到。",
                timestamp,
            ),
            ContextMessage(
                "om_batch_probe_4",
                "ou_batch_li",
                "小李",
                "好的。",
                timestamp,
            ),
        ),
        focus_message_ids=(
            "om_batch_probe_1",
            "om_batch_probe_2",
            "om_batch_probe_3",
            "om_batch_probe_4",
        ),
    )


def _run_task_detect(args: argparse.Namespace, runtime: object) -> int:
    from app.agent.context import TaskDetectionContextBuilder
    from app.agent.provider import ModelProviderError, OpenAICompatibleTaskDetector
    from app.database.repository import MessageLookupError
    from app.identity.aliases import AliasError

    try:
        settings = load_task_llm_settings()
        context = TaskDetectionContextBuilder(
            runtime.repository,
            runtime.aliases,
            task_scope_resolver=getattr(
                getattr(runtime, "chat_settings", None), "task_scope", None
            ),
        ).build(
            args.chat_id,
            args.message_id,
            limit=args.limit,
        )
        with OpenAICompatibleTaskDetector(settings) as detector:
            call = detector.detect(context)
    except (
        AliasError,
        MessageLookupError,
        ModelProviderError,
        SettingsError,
        ValueError,
    ) as exc:
        print(f"Task detection error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(call.result.to_dict(), ensure_ascii=False, indent=2))
    diagnostics = [
        f"model={call.model}",
        f"response_format={call.response_format}",
    ]
    if call.request_id:
        diagnostics.append(f"request_id={call.request_id}")
    if call.usage:
        diagnostics.append(
            "tokens=" + str(call.usage.get("total_tokens", "unknown"))
        )
    print("Task detection: " + ", ".join(diagnostics), file=sys.stderr)
    return 0


def _run_lifecycle_detect(
    args: argparse.Namespace, runtime: object
) -> int:
    from app.agent.context import TaskDetectionContextBuilder
    from app.agent.provider import (
        ModelProviderError,
        OpenAICompatibleTaskDetector,
    )
    from app.database.repository import MessageLookupError
    from app.identity.aliases import AliasError
    from app.lifecycle.context import LifecycleDetectionContextBuilder

    try:
        settings = load_task_llm_settings()
        conversation_builder = TaskDetectionContextBuilder(
            runtime.repository,
            runtime.aliases,
        )
        context = LifecycleDetectionContextBuilder(
            conversation_builder,
            runtime.tasks,
        ).build(
            args.chat_id,
            args.message_id,
            message_limit=args.limit,
        )
        with OpenAICompatibleTaskDetector(settings) as detector:
            call = detector.detect_lifecycle(context)
    except (
        AliasError,
        MessageLookupError,
        ModelProviderError,
        SettingsError,
        ValueError,
    ) as exc:
        print(f"Lifecycle detection error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(call.result.to_dict(), ensure_ascii=False, indent=2))
    diagnostics = [
        f"model={call.model}",
        f"response_format={call.response_format}",
        "read_only=true",
    ]
    if call.request_id:
        diagnostics.append(f"request_id={call.request_id}")
    if call.usage:
        diagnostics.append(
            "tokens=" + str(call.usage.get("total_tokens", "unknown"))
        )
    print("Lifecycle detection: " + ", ".join(diagnostics), file=sys.stderr)
    return 0


def _run_private_lifecycle_detect(
    args: argparse.Namespace, runtime: object
) -> int:
    from app.agent.context import TaskDetectionContextBuilder
    from app.agent.provider import (
        ModelProviderError,
        OpenAICompatibleTaskDetector,
    )
    from app.database.repository import MessageLookupError
    from app.identity.aliases import AliasError
    from app.lifecycle.context import (
        PrivateLifecycleDetectionContextBuilder,
    )
    from app.tasks.codes import TaskCodeError, parse_task_code

    try:
        settings = load_task_llm_settings()
        task_id = parse_task_code(args.task_code)
        sender = runtime.aliases.sender_for_message(args.message_id)
        if sender.chat_type != "p2p":
            raise ValueError("trigger message must come from a P2P chat")
        task = runtime.tasks.find_lifecycle_target_across_chats(
            task_id,
            owner_open_id=sender.open_id,
        )
        if task is None:
            raise ValueError(
                "task is not actionable or is not owned by the message sender"
            )
        context = PrivateLifecycleDetectionContextBuilder(
            TaskDetectionContextBuilder(
                runtime.repository,
                runtime.aliases,
            ),
            runtime.aliases,
        ).build(
            sender.chat_id,
            sender.message_id,
            actor_open_id=sender.open_id,
            task=task,
            message_limit=args.limit,
        )
        with OpenAICompatibleTaskDetector(settings) as detector:
            call = detector.detect_lifecycle(context)
    except (
        AliasError,
        MessageLookupError,
        ModelProviderError,
        SettingsError,
        TaskCodeError,
        ValueError,
    ) as exc:
        print(f"Private lifecycle detection error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(call.result.to_dict(), ensure_ascii=False, indent=2))
    diagnostics = [
        f"model={call.model}",
        f"response_format={call.response_format}",
        "scope=private_owner_task",
        "read_only=true",
    ]
    if call.request_id:
        diagnostics.append(f"request_id={call.request_id}")
    if call.usage:
        diagnostics.append(
            "tokens=" + str(call.usage.get("total_tokens", "unknown"))
        )
    print(
        "Private lifecycle detection: " + ", ".join(diagnostics),
        file=sys.stderr,
    )
    return 0


def _print_binding(binding: AliasBinding) -> None:
    print(f"chat_id: {binding.chat_id}")
    print(f"open_id: {binding.open_id}")
    print(f"name: {binding.alias}")
    print(f"source: {binding.source}")


def _json_compatible(value: object) -> object:
    """Convert management dataclasses to deterministic JSON-safe values."""

    from dataclasses import fields, is_dataclass
    from datetime import datetime
    from enum import Enum

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _json_compatible(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {
            str(key): _json_compatible(item) for key, item in value.items()
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_compatible(item) for item in value]
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
