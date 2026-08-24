"""CLI tests for chat-isolated Phase 4C task listing."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.config import DatabaseSettings
from app.main import main
from app.tasks.repository import TaskListPage, TaskSnapshot, TaskStatus


class TaskListCliTest(unittest.TestCase):
    def test_lists_only_the_requested_chat(self) -> None:
        repository = MagicMock()
        now = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
        repository.list_open_tasks.return_value = TaskListPage(
            chat_id="oc_a",
            total_count=1,
            tasks=(
                TaskSnapshot(
                    task_id=17,
                    chat_id="oc_a",
                    owner_open_id="ou_wang",
                    owner_name="王政",
                    title="完成验收记录",
                    description="完成验收记录",
                    deadline=datetime(
                        2026, 8, 30, 10, 0, tzinfo=timezone.utc
                    ),
                    status=TaskStatus.TODO,
                    confidence=0.95,
                    created_at=now,
                    updated_at=now,
                ),
            ),
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            tasks=repository
        )
        output = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(
                ["task-list", "--chat-id", "oc_a", "--limit", "7"]
            )

        self.assertEqual(exit_code, 0)
        repository.list_open_tasks.assert_called_once_with("oc_a", limit=7)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["chat_id"], "oc_a")
        self.assertEqual(payload["tasks"][0]["task_code"], "T-HS")
        self.assertEqual(payload["tasks"][0]["owner_open_id"], "ou_wang")
        self.assertEqual(
            payload["tasks"][0]["deadline"],
            "2026-08-30T18:00:00+08:00",
        )

    def test_reports_repository_validation_failure(self) -> None:
        repository = MagicMock()
        repository.list_open_tasks.side_effect = ValueError(
            "limit must be between 1 and 100"
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            tasks=repository
        )
        error = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            redirect_stderr(error),
        ):
            exit_code = main(
                ["task-list", "--chat-id", "oc_a", "--limit", "101"]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("between 1 and 100", error.getvalue())


if __name__ == "__main__":
    unittest.main()
