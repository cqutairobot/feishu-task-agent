"""Task-notification Worker delivery and retry tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import unittest

from app.feishu.task_notification_sender import (
    TaskNotificationDeliveryError,
    TaskNotificationDeliveryReceipt,
)
from app.notifications.repository import (
    TaskNotificationKind,
    TaskNotificationLease,
    TaskNotificationStatus,
)
from app.notifications.worker import (
    TaskNotificationWorker,
    TaskNotificationWorkerStatus,
)


class TaskNotificationWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
        self.repository = Mock()
        self.sender = Mock()
        self.lease = TaskNotificationLease(
            notification_id=7,
            task_id=1,
            kind=TaskNotificationKind.TASK_DONE_ADMIN,
            recipient_open_id="ou_admin",
            recipient_private_chat_id="oc_admin_dm",
            task_code="T-1A",
            owner_open_id="ou_owner",
            owner_name="王政",
            title="完成前端页面",
            status_snapshot="done",
            deadline=None,
            deadline_before=None,
            reason=None,
            task_created_at=self.now,
            scheduled_for=self.now,
            attempt=1,
            max_attempts=3,
            worker_id="worker",
            lease_expires_at=self.now + timedelta(minutes=2),
        )

    def test_success_marks_notification_sent(self) -> None:
        self.repository.claim_due.return_value = self.lease
        self.sender.deliver.return_value = TaskNotificationDeliveryReceipt(
            message_id="om_sent",
            receive_id_type="chat_id",
            receive_id="oc_admin_dm",
        )

        outcome = self._worker().run_once("worker")

        self.assertEqual(outcome.status, TaskNotificationWorkerStatus.SENT)
        self.repository.mark_sent.assert_called_once_with(
            self.lease,
            feishu_message_id="om_sent",
            receive_id_type="chat_id",
            receive_id="oc_admin_dm",
            sent_at=self.now,
        )

    def test_delivery_failure_is_retried_durably(self) -> None:
        self.repository.claim_due.return_value = self.lease
        self.sender.deliver.side_effect = TaskNotificationDeliveryError(
            "transport_error", "temporary"
        )
        self.repository.fail.return_value = Mock(
            status=TaskNotificationStatus.SCHEDULED,
            retry_at=self.now + timedelta(seconds=30),
        )

        outcome = self._worker().run_once("worker")

        self.assertEqual(
            outcome.status,
            TaskNotificationWorkerStatus.RETRY_SCHEDULED,
        )
        self.repository.fail.assert_called_once()
        self.repository.mark_sent.assert_not_called()

    def test_delivery_audit_failure_is_retried_without_crashing_worker(self) -> None:
        self.repository.claim_due.return_value = self.lease
        self.sender.deliver.return_value = TaskNotificationDeliveryReceipt(
            message_id="om_sent",
            receive_id_type="chat_id",
            receive_id="oc_admin_dm",
        )
        self.repository.mark_sent.side_effect = RuntimeError("database locked")
        self.repository.fail.return_value = Mock(
            status=TaskNotificationStatus.SCHEDULED,
            retry_at=self.now + timedelta(seconds=30),
        )

        outcome = self._worker().run_once("worker")

        self.assertEqual(
            outcome.status,
            TaskNotificationWorkerStatus.RETRY_SCHEDULED,
        )
        self.assertEqual(outcome.error_code, "delivery_audit_error")
        self.repository.fail.assert_called_once()

    def test_keyboard_interrupt_is_recorded_then_propagated(self) -> None:
        self.repository.claim_due.return_value = self.lease
        self.sender.deliver.side_effect = KeyboardInterrupt()
        self.repository.fail.return_value = Mock(
            status=TaskNotificationStatus.SCHEDULED,
            retry_at=self.now + timedelta(seconds=30),
        )

        with self.assertRaises(KeyboardInterrupt):
            self._worker().run_once("worker")

        self.repository.fail.assert_called_once()
        self.assertEqual(
            self.repository.fail.call_args.kwargs["error_code"],
            "worker_interrupted",
        )
        self.repository.mark_sent.assert_not_called()

    def test_idle_does_not_call_sender(self) -> None:
        self.repository.claim_due.return_value = None

        outcome = self._worker().run_once("worker")

        self.assertEqual(outcome.status, TaskNotificationWorkerStatus.IDLE)
        self.sender.deliver.assert_not_called()

    def _worker(self) -> TaskNotificationWorker:
        return TaskNotificationWorker(
            self.repository,
            self.sender,
            clock=lambda: self.now,
        )


if __name__ == "__main__":
    unittest.main()
