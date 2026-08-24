"""Crash-recoverable one-reminder delivery Worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol

from app.feishu.reminder_sender import (
    ReminderDeliveryError,
    ReminderDeliveryReceipt,
)
from app.reminders.repository import (
    ReminderLease,
    ReminderRepository,
    ReminderStatus,
)


class ReminderSender(Protocol):
    def deliver(self, lease: ReminderLease) -> ReminderDeliveryReceipt: ...


class ReminderWorkerStatus(StrEnum):
    IDLE = "idle"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class ReminderWorkerOutcome:
    status: ReminderWorkerStatus
    reminder_id: int | None
    task_id: int | None
    kind: str | None
    attempt: int | None
    receive_id_type: str | None
    receive_id: str | None
    feishu_message_id: str | None
    error_code: str | None
    retry_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reminder_id": self.reminder_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "attempt": self.attempt,
            "receive_id_type": self.receive_id_type,
            "receive_id": self.receive_id,
            "feishu_message_id": self.feishu_message_id,
            "error_code": self.error_code,
            "retry_at": (
                None
                if self.retry_at is None
                else self.retry_at.astimezone(timezone.utc).isoformat()
            ),
        }


@dataclass(frozen=True, slots=True)
class ReminderWorkerLoopSummary:
    iterations: int
    processed: int
    idle_polls: int


class ReminderWorker:
    def __init__(
        self,
        repository: ReminderRepository,
        sender: ReminderSender,
        *,
        lease_seconds: int = 120,
        retry_base_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        if not 1 <= retry_base_seconds <= 3_600:
            raise ValueError(
                "retry_base_seconds must be between 1 and 3600"
            )
        self._repository = repository
        self._sender = sender
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(
        self,
        worker_id: str,
        *,
        reminder_id: int | None = None,
    ) -> ReminderWorkerOutcome:
        lease = self._repository.claim_due(
            worker_id,
            claimed_at=self._clock(),
            lease_duration=timedelta(seconds=self._lease_seconds),
            reminder_id=reminder_id,
        )
        if lease is None:
            return ReminderWorkerOutcome(
                status=ReminderWorkerStatus.IDLE,
                reminder_id=reminder_id,
                task_id=None,
                kind=None,
                attempt=None,
                receive_id_type=None,
                receive_id=None,
                feishu_message_id=None,
                error_code=None,
                retry_at=None,
            )

        try:
            receipt = self._sender.deliver(lease)
        except KeyboardInterrupt as exc:
            self._record_failure(
                lease,
                error_code="worker_interrupted",
                error=exc,
            )
            raise
        except ReminderDeliveryError as exc:
            return self._record_failure(
                lease,
                error_code=exc.code,
                error=exc,
            )
        except Exception as exc:
            return self._record_failure(
                lease,
                error_code="delivery_error",
                error=exc,
            )

        try:
            sent = self._repository.mark_sent(
                lease,
                feishu_message_id=receipt.message_id,
                receive_id_type=receipt.receive_id_type,
                receive_id=receipt.receive_id,
                sent_at=self._clock(),
                private_error_code=receipt.private_error_code,
                private_error_message=receipt.private_error_message,
            )
        except Exception as exc:
            return self._record_failure(
                lease,
                error_code="delivery_audit_error",
                error=exc,
            )
        return ReminderWorkerOutcome(
            status=ReminderWorkerStatus.SENT,
            reminder_id=lease.reminder_id,
            task_id=lease.task_id,
            kind=lease.kind.value,
            attempt=lease.attempt,
            receive_id_type=sent.delivery_receive_id_type,
            receive_id=sent.delivery_receive_id,
            feishu_message_id=sent.feishu_message_id,
            error_code=sent.last_error_code,
            retry_at=None,
        )

    def _record_failure(
        self,
        lease: ReminderLease,
        *,
        error_code: str,
        error: BaseException,
    ) -> ReminderWorkerOutcome:
        delay_seconds = min(
            3_600,
            self._retry_base_seconds * (2 ** (lease.attempt - 1)),
        )
        failure = self._repository.fail(
            lease,
            error_code=error_code,
            error_message=_bounded_error_message(error),
            failed_at=self._clock(),
            retry_delay=timedelta(seconds=delay_seconds),
        )
        dead = failure.status is ReminderStatus.DEAD
        return ReminderWorkerOutcome(
            status=(
                ReminderWorkerStatus.DEAD
                if dead
                else ReminderWorkerStatus.RETRY_SCHEDULED
            ),
            reminder_id=lease.reminder_id,
            task_id=lease.task_id,
            kind=lease.kind.value,
            attempt=lease.attempt,
            receive_id_type=None,
            receive_id=None,
            feishu_message_id=None,
            error_code=error_code,
            retry_at=failure.retry_at,
        )


def run_reminder_worker_loop(
    worker: ReminderWorker,
    worker_id: str,
    *,
    poll_seconds: float,
    sleeper: Callable[[float], None],
    on_outcome: Callable[[ReminderWorkerOutcome], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> ReminderWorkerLoopSummary:
    if not 0.1 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be between 0.1 and 60")
    stop_requested = stop_requested or (lambda: False)
    iterations = 0
    processed = 0
    idle_polls = 0
    while not stop_requested():
        outcome = worker.run_once(worker_id)
        iterations += 1
        if outcome.status is ReminderWorkerStatus.IDLE:
            idle_polls += 1
            sleeper(poll_seconds)
            continue
        processed += 1
        if on_outcome is not None:
            on_outcome(outcome)
    return ReminderWorkerLoopSummary(
        iterations=iterations,
        processed=processed,
        idle_polls=idle_polls,
    )


def _bounded_error_message(error: BaseException) -> str:
    message = " ".join(str(error).split())
    if not message:
        message = type(error).__name__
    return f"{type(error).__name__}: {message}"[:2_000]
