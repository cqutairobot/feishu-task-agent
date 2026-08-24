"""Phase 5B reminder Worker orchestration tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.feishu.reminder_sender import (
    ReminderDeliveryError,
    ReminderDeliveryReceipt,
)
from app.reminders.repository import (
    ReminderFailureResult,
    ReminderLease,
    ReminderStatus,
)
from app.reminders.schedule import ReminderKind
from app.reminders.worker import (
    ReminderWorker,
    ReminderWorkerOutcome,
    ReminderWorkerStatus,
    run_reminder_worker_loop,
)


class ReminderWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
        self.lease = ReminderLease(
            reminder_id=7,
            task_id=3,
            kind=ReminderKind.DUE_24H,
            deadline=self.now + timedelta(days=1),
            scheduled_for=self.now,
            attempt=1,
            max_attempts=3,
            worker_id="worker",
            lease_expires_at=self.now + timedelta(minutes=2),
            chat_id="oc_test",
            owner_open_id="ou_wang",
            owner_name="王政",
            title="完成前端页面",
            task_status="todo",
        )

    def test_successful_delivery_is_marked_sent(self) -> None:
        repository = MagicMock()
        repository.claim_due.return_value = self.lease
        repository.mark_sent.return_value = SimpleNamespace(
            delivery_receive_id_type="open_id",
            delivery_receive_id="ou_wang",
            feishu_message_id="om_sent",
            last_error_code=None,
        )
        sender = MagicMock()
        sender.deliver.return_value = ReminderDeliveryReceipt(
            message_id="om_sent",
            receive_id_type="open_id",
            receive_id="ou_wang",
        )
        worker = ReminderWorker(
            repository, sender, clock=lambda: self.now
        )

        outcome = worker.run_once("worker")

        self.assertEqual(outcome.status, ReminderWorkerStatus.SENT)
        repository.mark_sent.assert_called_once()
        kwargs = repository.mark_sent.call_args.kwargs
        self.assertEqual(kwargs["receive_id_type"], "open_id")
        self.assertEqual(kwargs["feishu_message_id"], "om_sent")

    def test_delivery_failure_is_retried_with_exponential_delay(self) -> None:
        lease = _with_attempt(self.lease, 2)
        repository = MagicMock()
        repository.claim_due.return_value = lease
        repository.fail.return_value = ReminderFailureResult(
            reminder_id=7,
            status=ReminderStatus.SCHEDULED,
            retry_at=self.now + timedelta(seconds=60),
        )
        sender = MagicMock()
        sender.deliver.side_effect = ReminderDeliveryError(
            "all_delivery_failed", "both rejected"
        )
        worker = ReminderWorker(
            repository,
            sender,
            retry_base_seconds=30,
            clock=lambda: self.now,
        )

        outcome = worker.run_once("worker")

        self.assertEqual(
            outcome.status, ReminderWorkerStatus.RETRY_SCHEDULED
        )
        self.assertEqual(
            repository.fail.call_args.kwargs["retry_delay"],
            timedelta(seconds=60),
        )
        self.assertEqual(outcome.error_code, "all_delivery_failed")

    def test_idle_does_not_call_sender(self) -> None:
        repository = MagicMock()
        repository.claim_due.return_value = None
        sender = MagicMock()
        worker = ReminderWorker(
            repository, sender, clock=lambda: self.now
        )

        outcome = worker.run_once("worker", reminder_id=9)

        self.assertEqual(outcome.status, ReminderWorkerStatus.IDLE)
        sender.deliver.assert_not_called()

    def test_loop_sleeps_only_when_idle(self) -> None:
        outcomes = iter(
            (
                _outcome(ReminderWorkerStatus.IDLE),
                _outcome(ReminderWorkerStatus.SENT),
            )
        )
        calls = 0

        class FakeWorker:
            def run_once(self, worker_id: str) -> ReminderWorkerOutcome:
                nonlocal calls
                calls += 1
                return next(outcomes)

        sleeps: list[float] = []
        emitted: list[ReminderWorkerOutcome] = []
        summary = run_reminder_worker_loop(
            FakeWorker(),
            "worker",
            poll_seconds=2,
            sleeper=sleeps.append,
            on_outcome=emitted.append,
            stop_requested=lambda: calls >= 2,
        )

        self.assertEqual(sleeps, [2])
        self.assertEqual(len(emitted), 1)
        self.assertEqual(summary.processed, 1)
        self.assertEqual(summary.idle_polls, 1)


def _with_attempt(lease: ReminderLease, attempt: int) -> ReminderLease:
    return ReminderLease(
        reminder_id=lease.reminder_id,
        task_id=lease.task_id,
        kind=lease.kind,
        deadline=lease.deadline,
        scheduled_for=lease.scheduled_for,
        attempt=attempt,
        max_attempts=lease.max_attempts,
        worker_id=lease.worker_id,
        lease_expires_at=lease.lease_expires_at,
        chat_id=lease.chat_id,
        owner_open_id=lease.owner_open_id,
        owner_name=lease.owner_name,
        title=lease.title,
        task_status=lease.task_status,
    )


def _outcome(status: ReminderWorkerStatus) -> ReminderWorkerOutcome:
    return ReminderWorkerOutcome(
        status=status,
        reminder_id=None,
        task_id=None,
        kind=None,
        attempt=None,
        receive_id_type=None,
        receive_id=None,
        feishu_message_id=None,
        error_code=None,
        retry_at=None,
    )


if __name__ == "__main__":
    unittest.main()
