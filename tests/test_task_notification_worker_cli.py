"""Task-notification Worker CLI wiring tests."""

from contextlib import redirect_stdout
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
from app.notifications.worker import (
    TaskNotificationWorkerOutcome,
    TaskNotificationWorkerStatus,
)


class TaskNotificationWorkerCliTest(unittest.TestCase):
    def test_runs_one_notification_attempt(self) -> None:
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            notifications=MagicMock()
        )
        worker = MagicMock()
        worker.run_once.return_value = TaskNotificationWorkerOutcome(
            status=TaskNotificationWorkerStatus.SENT,
            notification_id=7,
            task_id=3,
            kind="task_done_admin",
            attempt=1,
            receive_id_type="open_id",
            receive_id="ou_admin",
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
                        "FEISHU_TASK_ADMIN_OPEN_IDS": "ou_admin",
                    }
                ),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            patch(
                "app.feishu.task_notification_sender."
                "FeishuTaskNotificationSender"
            ),
            patch(
                "app.notifications.worker.TaskNotificationWorker",
                return_value=worker,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "task-notification-worker",
                    "--once",
                    "--notification-id",
                    "7",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            worker.run_once.call_args.kwargs["notification_id"], 7
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["notification_id"], 7)


if __name__ == "__main__":
    unittest.main()
