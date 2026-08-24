"""Phase 5B reminder Worker CLI tests."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.config import (
    DatabaseSettings,
    FeishuSettings,
    ReminderSettings,
    ReminderWorkerSettings,
)
from app.main import main
from app.reminders.worker import ReminderWorkerOutcome, ReminderWorkerStatus


class ReminderWorkerCliTest(unittest.TestCase):
    def test_runs_one_delivery_attempt(self) -> None:
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            reminders=MagicMock()
        )
        worker = MagicMock()
        worker.run_once.return_value = ReminderWorkerOutcome(
            status=ReminderWorkerStatus.SENT,
            reminder_id=7,
            task_id=3,
            kind="due_24h",
            attempt=1,
            receive_id_type="open_id",
            receive_id="ou_wang",
            feishu_message_id="om_sent",
            error_code=None,
            retry_at=None,
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
                "app.main.load_reminder_worker_settings",
                return_value=ReminderWorkerSettings(),
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
                "app.feishu.reminder_sender.FeishuReminderSender"
            ),
            patch(
                "app.reminders.worker.ReminderWorker",
                return_value=worker,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(
                ["reminder-worker", "--once", "--reminder-id", "7"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(worker.run_once.call_args.kwargs["reminder_id"], 7)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["feishu_message_id"], "om_sent")

    def test_rejects_targeted_forever_mode(self) -> None:
        error = io.StringIO()

        with redirect_stderr(error):
            exit_code = main(
                [
                    "reminder-worker",
                    "--forever",
                    "--reminder-id",
                    "7",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("only be used with --once", error.getvalue())


if __name__ == "__main__":
    unittest.main()
