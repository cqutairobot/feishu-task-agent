"""Phase 5A reminder planning CLI tests."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.config import DatabaseSettings, FeishuSettings, ReminderSettings
from app.feishu.reminder_sender import ReminderDeliveryReceipt
from app.main import main
from app.reminders.repository import (
    ReminderSnapshot,
    ReminderStatus,
    ReminderSyncResult,
)
from app.reminders.schedule import ReminderKind


class ReminderCliTest(unittest.TestCase):
    def test_syncs_one_exact_task(self) -> None:
        repository = MagicMock()
        synced_at = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)
        repository.sync_task.return_value = ReminderSyncResult(
            tasks_scanned=1,
            task_statuses_changed=0,
            reminders_created=4,
            reminders_cancelled=0,
            active_reminders=4,
            synced_at=synced_at,
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            reminders=repository
        )
        output = io.StringIO()
        settings = ReminderSettings()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.load_reminder_settings", return_value=settings
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ) as open_runtime,
            redirect_stdout(output),
        ):
            exit_code = main(["reminder", "sync", "--task-id", "1"])

        self.assertEqual(exit_code, 0)
        repository.sync_task.assert_called_once_with(1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["reminders_created"], 4)
        self.assertEqual(payload["active_reminders"], 4)
        self.assertEqual(
            open_runtime.call_args.kwargs["reminder_settings"], settings
        )

    def test_lists_schedule_in_shanghai_time(self) -> None:
        repository = MagicMock()
        deadline = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
        repository.list_for_task.return_value = (
            ReminderSnapshot(
                reminder_id=1,
                task_id=1,
                kind=ReminderKind.DUE_72H,
                deadline_snapshot=deadline,
                scheduled_for=datetime(
                    2026, 8, 27, 10, 0, tzinfo=timezone.utc
                ),
                available_at=datetime(
                    2026, 8, 27, 10, 0, tzinfo=timezone.utc
                ),
                status=ReminderStatus.SCHEDULED,
                attempt_count=0,
                max_attempts=3,
                sent_at=None,
                feishu_message_id=None,
                delivery_receive_id_type=None,
                delivery_receive_id=None,
                last_error_code=None,
                last_error_message=None,
                cancelled_at=None,
                cancel_reason=None,
                recipient_open_id="ou_wang",
                recipient_name="王政",
            ),
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            reminders=repository
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
                "app.main.load_reminder_settings",
                return_value=ReminderSettings(),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(["reminder", "list", "--task-id", "1"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload["reminders"][0]["scheduled_for"],
            "2026-08-27T18:00:00+08:00",
        )
        self.assertEqual(
            payload["reminders"][0]["deadline_snapshot"],
            "2026-08-30T18:00:00+08:00",
        )

    def test_reports_unknown_task(self) -> None:
        repository = MagicMock()
        repository.sync_task.side_effect = ValueError("task 99 does not exist")
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            reminders=repository
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
                "app.main.load_reminder_settings",
                return_value=ReminderSettings(),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            redirect_stderr(error),
        ):
            exit_code = main(["reminder", "sync", "--task-id", "99"])

        self.assertEqual(exit_code, 2)
        self.assertIn("does not exist", error.getvalue())

    def test_probe_sends_without_mutating_formal_plan(self) -> None:
        reminder_repository = MagicMock()
        task = SimpleNamespace(task_id=1, owner_open_id="ou_wang")
        task_repository = MagicMock()
        task_repository.get_task.return_value = task
        reminder_repository.find_private_chat_id.return_value = (
            "oc_private_wang"
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            reminders=reminder_repository,
            tasks=task_repository,
        )
        sender = MagicMock()
        sender.probe.return_value = ReminderDeliveryReceipt(
            message_id="om_probe",
            receive_id_type="open_id",
            receive_id="ou_wang",
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
                "app.main.load_reminder_settings",
                return_value=ReminderSettings(),
            ),
            patch(
                "app.main.load_settings",
                return_value=FeishuSettings.from_mapping(
                    {
                        "FEISHU_APP_ID": "cli_test",
                        "FEISHU_APP_SECRET": "secret",
                    }
                ),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            patch(
                "app.feishu.reminder_sender.FeishuReminderSender",
                return_value=sender,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(["reminder", "probe", "--task-id", "1"])

        self.assertEqual(exit_code, 0)
        task_repository.get_task.assert_called_once_with(1)
        sender.probe.assert_called_once()
        self.assertEqual(
            sender.probe.call_args.kwargs["private_chat_id"],
            "oc_private_wang",
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["receive_id_type"], "open_id")
        self.assertFalse(payload["formal_reminder_plan_changed"])


if __name__ == "__main__":
    unittest.main()
