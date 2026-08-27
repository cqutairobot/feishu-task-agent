"""Crash-recoverable private task-notification Worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol

from app.feishu.task_notification_sender import (
    TaskNotificationDeliveryError,
    TaskNotificationDeliveryReceipt,
)
from app.notifications.repository import (
    TaskNotificationLease,
    TaskNotificationRepository,
    TaskNotificationStatus,
)


class TaskNotificationSender(Protocol):
    def deliver(
        self, lease: TaskNotificationLease
    ) -> TaskNotificationDeliveryReceipt: ...


class TaskNotificationWorkerStatus(StrEnum):
    IDLE = "idle"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class TaskNotificationWorkerOutcome:
    status: TaskNotificationWorkerStatus
    notification_id: int | None
    task_id: int | None
    kind: str | None
    attempt: int | None
    receive_id_type: str | None
    receive_id: str | None
    feishu_message_id: str | None
    error_code: str | None
    retry_at: datetime | None


class TaskNotificationWorker:
    def __init__(
        self,
        repository: TaskNotificationRepository,
        sender: TaskNotificationSender,
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
        notification_id: int | None = None,
    ) -> TaskNotificationWorkerOutcome:
        lease = self._repository.claim_due(
            worker_id,
            claimed_at=self._clock(),
            lease_duration=timedelta(seconds=self._lease_seconds),
            notification_id=notification_id,
        )
        if lease is None:
            return TaskNotificationWorkerOutcome(
                status=TaskNotificationWorkerStatus.IDLE,
                notification_id=notification_id,
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
        except TaskNotificationDeliveryError as exc:
            return self._record_failure(
                lease, error_code=exc.code, error=exc
            )
        except Exception as exc:
            return self._record_failure(
                lease, error_code="delivery_error", error=exc
            )
        try:
            self._repository.mark_sent(
                lease,
                feishu_message_id=receipt.message_id,
                receive_id_type=receipt.receive_id_type,
                receive_id=receipt.receive_id,
                sent_at=self._clock(),
            )
        except Exception as exc:
            return self._record_failure(
                lease,
                error_code="delivery_audit_error",
                error=exc,
            )
        return TaskNotificationWorkerOutcome(
            status=TaskNotificationWorkerStatus.SENT,
            notification_id=lease.notification_id,
            task_id=lease.task_id,
            kind=lease.kind.value,
            attempt=lease.attempt,
            receive_id_type=receipt.receive_id_type,
            receive_id=receipt.receive_id,
            feishu_message_id=receipt.message_id,
            error_code=None,
            retry_at=None,
        )

    def _record_failure(
        self,
        lease: TaskNotificationLease,
        *,
        error_code: str,
        error: BaseException,
    ) -> TaskNotificationWorkerOutcome:
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
        dead = failure.status is TaskNotificationStatus.DEAD
        return TaskNotificationWorkerOutcome(
            status=(
                TaskNotificationWorkerStatus.DEAD
                if dead
                else TaskNotificationWorkerStatus.RETRY_SCHEDULED
            ),
            notification_id=lease.notification_id,
            task_id=lease.task_id,
            kind=lease.kind.value,
            attempt=lease.attempt,
            receive_id_type=None,
            receive_id=None,
            feishu_message_id=None,
            error_code=error_code,
            retry_at=failure.retry_at,
        )


def run_task_notification_worker_loop(
    worker: TaskNotificationWorker,
    worker_id: str,
    *,
    poll_seconds: float,
    sleeper: Callable[[float], None],
    on_outcome: Callable[[TaskNotificationWorkerOutcome], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    if not 0.1 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be between 0.1 and 60")
    stop_requested = stop_requested or (lambda: False)
    while not stop_requested():
        outcome = worker.run_once(worker_id)
        if outcome.status is TaskNotificationWorkerStatus.IDLE:
            sleeper(poll_seconds)
            continue
        if on_outcome is not None:
            on_outcome(outcome)


def _bounded_error_message(error: BaseException) -> str:
    message = " ".join(str(error).split()) or type(error).__name__
    return f"{type(error).__name__}: {message}"[:2_000]
