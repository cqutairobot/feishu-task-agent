"""Phase 7A local provisioning and read-model CLI tests."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.config import DatabaseSettings
from app.database.engine import create_session_factory, session_scope
from app.database.models import (
    Chat,
    ChatMemberAlias,
    ChatMembership,
    Task,
    TaskAssignee,
    User,
)
from app.database.runtime import open_database_runtime
from app.main import main


class ManagementCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "management-cli.db"
        self.settings = DatabaseSettings(
            url=f"sqlite:///{database_path}", echo=False
        )
        self.now = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)
        with open_database_runtime(self.settings) as runtime:
            session_factory = create_session_factory(runtime.engine)
            with session_scope(session_factory) as session:
                session.add(
                    Chat(
                        chat_id="oc_lab",
                        tenant_key="tenant",
                        name="实验群",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
                session.add(
                    User(
                        open_id="ou_admin",
                        union_id=None,
                        name="莉莉",
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
                session.flush()
                session.add_all(
                    (
                        ChatMemberAlias(
                            chat_id="oc_lab",
                            open_id="ou_admin",
                            alias="莉莉",
                            normalized_alias="莉莉",
                            source="self_command",
                            confidence=1.0,
                            verified_at=self.now,
                            created_at=self.now,
                            updated_at=self.now,
                        ),
                        ChatMembership(
                            chat_id="oc_lab",
                            open_id="ou_admin",
                            display_name_snapshot="莉莉",
                            active=True,
                            is_owner=True,
                            first_synced_at=self.now,
                            last_synced_at=self.now,
                            left_at=None,
                        ),
                    )
                )
                task = Task(
                    chat_id="oc_lab",
                    owner_open_id="ou_admin",
                    owner_name_snapshot="莉莉",
                    title="验收管理查询",
                    normalized_title="验收管理查询",
                    description="只读查询",
                    deadline=None,
                    status="todo",
                    confidence=0.99,
                    completed_at=None,
                    cancelled_at=None,
                    created_at=self.now,
                    updated_at=self.now,
                )
                session.add(task)
                session.flush()
                session.add(
                    TaskAssignee(
                        task_id=task.id,
                        open_id="ou_admin",
                        name_snapshot="莉莉",
                        position=0,
                        created_at=self.now,
                    )
                )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_grant_then_read_dashboard_as_json(self) -> None:
        grant_output = io.StringIO()
        dashboard_output = io.StringIO()
        with patch("app.main.load_database_settings", return_value=self.settings):
            with redirect_stdout(grant_output):
                grant_status = main(
                    [
                        "chat-admin",
                        "grant",
                        "--chat-id",
                        "oc_lab",
                        "--open-id",
                        "ou_admin",
                    ]
                )
            with redirect_stdout(dashboard_output):
                dashboard_status = main(
                    [
                        "management",
                        "dashboard",
                        "--actor-open-id",
                        "ou_admin",
                        "--chat-id",
                        "oc_lab",
                    ]
                )

        self.assertEqual(grant_status, 0)
        self.assertEqual(dashboard_status, 0)
        self.assertTrue(json.loads(grant_output.getvalue())["changed"])
        dashboard = json.loads(dashboard_output.getvalue())
        self.assertEqual(dashboard["chat_id"], "oc_lab")
        self.assertEqual(dashboard["todo_count"], 1)

    def test_unauthorized_cli_read_has_no_payload(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch("app.main.load_database_settings", return_value=self.settings),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = main(
                [
                    "management",
                    "task",
                    "--actor-open-id",
                    "ou_admin",
                    "--chat-id",
                    "oc_lab",
                    "--task-id",
                    "999",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("not authorized for this chat", error.getvalue())


if __name__ == "__main__":
    unittest.main()
