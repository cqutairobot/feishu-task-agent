"""Durable SQLite-backed queue for asynchronous task detection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Protocol

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    DetectionJob,
    DetectionRun,
    DetectionRunFocusMessage,
    Message,
)


class DetectionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class DetectionRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DetectionQueueError(RuntimeError):
    """Raised when a queue transition or input violates the contract."""


class DetectionLeaseError(DetectionQueueError):
    """Raised when a worker no longer owns the requested job lease."""


class DetectionCompletionHook(Protocol):
    def __call__(
        self,
        session: Session,
        detection_run_id: int,
        *,
        materialized_at: datetime,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: int
    inserted: bool
    status: DetectionJobStatus


@dataclass(frozen=True, slots=True)
class DetectionJobLease:
    job_id: int
    chat_id: str
    trigger_message_id: str
    worker_id: str
    attempt: int
    max_attempts: int
    batch_started_at: datetime
    leased_at: datetime
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class DetectionJobSnapshot:
    job_id: int
    chat_id: str
    trigger_message_id: str
    status: DetectionJobStatus
    priority: int
    attempt_count: int
    max_attempts: int
    available_at: datetime
    worker_id: str | None
    lease_expires_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class DetectionRunSnapshot:
    run_id: int
    job_id: int
    attempt: int
    status: DetectionRunStatus
    provider: str
    model: str
    response_format: str
    context_fingerprint: str
    context_message_ids: tuple[str, ...]
    focus_message_ids: tuple[str, ...]
    request_id: str | None
    total_tokens: int | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    latency_ms: int | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class FailureResult:
    job_status: DetectionJobStatus
    available_at: datetime


@dataclass(frozen=True, slots=True)
class CancellationResult:
    job_id: int
    changed: bool
    status: DetectionJobStatus
    cancelled_at: datetime
    reason: str


class DetectionQueueRepository:
    """Apply idempotent queue transitions in short database transactions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def enqueue(
        self,
        chat_id: str,
        trigger_message_id: str,
        *,
        available_at: datetime,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> EnqueueResult:
        chat_id = _required_text(chat_id, "chat_id", maximum=128)
        trigger_message_id = _required_text(
            trigger_message_id, "trigger_message_id", maximum=128
        )
        available_at = _aware_utc(available_at, "available_at")
        if not -100 <= priority <= 100:
            raise DetectionQueueError("priority must be between -100 and 100")
        if not 1 <= max_attempts <= 10:
            raise DetectionQueueError("max_attempts must be between 1 and 10")

        with session_scope(self._session_factory) as session:
            message = session.execute(
                select(
                    Message.message_type,
                    Message.is_from_bot,
                    Message.received_at,
                ).where(
                    Message.chat_id == chat_id,
                    Message.message_id == trigger_message_id,
                )
            ).one_or_none()
            if message is None:
                raise DetectionQueueError(
                    "trigger message does not exist in the requested chat"
                )
            if message.message_type != "text" or message.is_from_bot:
                raise DetectionQueueError(
                    "only human text messages can trigger task detection"
                )

            now = datetime.now(timezone.utc)
            insert_result = session.execute(
                sqlite_insert(DetectionJob)
                .values(
                    chat_id=chat_id,
                    trigger_message_id=trigger_message_id,
                    status=DetectionJobStatus.QUEUED.value,
                    priority=priority,
                    attempt_count=0,
                    max_attempts=max_attempts,
                    available_at=available_at,
                    created_at=message.received_at,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["chat_id", "trigger_message_id"]
                )
            )
            row = session.execute(
                select(DetectionJob.id, DetectionJob.status).where(
                    DetectionJob.chat_id == chat_id,
                    DetectionJob.trigger_message_id == trigger_message_id,
                )
            ).one()

        return EnqueueResult(
            job_id=row.id,
            inserted=insert_result.rowcount == 1,
            status=DetectionJobStatus(row.status),
        )

    def cancel_jobs(
        self,
        job_ids: Sequence[int],
        *,
        reason: str,
        cancelled_at: datetime,
    ) -> tuple[CancellationResult, ...]:
        """Atomically cancel exact queued jobs; repeated cancellation is safe."""

        ids = _job_ids(job_ids)
        reason = _required_text(reason, "reason", maximum=500)
        cancelled_at = _aware_utc(cancelled_at, "cancelled_at")
        with session_scope(self._session_factory) as session:
            jobs = list(
                session.scalars(
                    select(DetectionJob)
                    .where(DetectionJob.id.in_(ids))
                    .order_by(DetectionJob.id)
                )
            )
            found_ids = {job.id for job in jobs}
            missing = sorted(set(ids) - found_ids)
            if missing:
                raise DetectionQueueError(
                    f"detection jobs do not exist: {missing}"
                )
            invalid = [
                job.id
                for job in jobs
                if job.status
                not in {
                    DetectionJobStatus.QUEUED.value,
                    DetectionJobStatus.CANCELLED.value,
                }
            ]
            if invalid:
                raise DetectionQueueError(
                    "only queued jobs can be cancelled; "
                    f"invalid job IDs: {invalid}"
                )

            changed_ids = {
                job.id
                for job in jobs
                if job.status == DetectionJobStatus.QUEUED.value
            }
            if changed_ids:
                session.execute(
                    update(DetectionJob)
                    .where(
                        DetectionJob.id.in_(changed_ids),
                        DetectionJob.status
                        == DetectionJobStatus.QUEUED.value,
                    )
                    .values(
                        status=DetectionJobStatus.CANCELLED.value,
                        cancelled_at=cancelled_at,
                        cancel_reason=reason,
                        worker_id=None,
                        leased_at=None,
                        lease_expires_at=None,
                        updated_at=cancelled_at,
                    )
                )
                session.flush()

            return tuple(
                CancellationResult(
                    job_id=job.id,
                    changed=job.id in changed_ids,
                    status=DetectionJobStatus(job.status),
                    cancelled_at=job.cancelled_at,
                    reason=job.cancel_reason,
                )
                for job in jobs
            )

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int = 300,
    ) -> DetectionJobLease | None:
        return self._claim(
            worker_id,
            now=now,
            lease_seconds=lease_seconds,
            job_id=None,
        )

    def claim_job(
        self,
        job_id: int,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int = 300,
    ) -> DetectionJobLease | None:
        """Claim one exact ready job without consuming older queue entries."""

        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
            raise DetectionQueueError("job_id must be a positive integer")
        return self._claim(
            worker_id,
            now=now,
            lease_seconds=lease_seconds,
            job_id=job_id,
        )

    def _claim(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
        job_id: int | None,
    ) -> DetectionJobLease | None:
        worker_id = _required_text(worker_id, "worker_id", maximum=128)
        now = _aware_utc(now, "now")
        if not 10 <= lease_seconds <= 3_600:
            raise DetectionQueueError(
                "lease_seconds must be between 10 and 3600"
            )
        lease_expires_at = now + timedelta(seconds=lease_seconds)

        with session_scope(self._session_factory) as session:
            self._recover_expired_leases(session, now)
            candidate_id: object = job_id
            if candidate_id is None:
                candidate_id = (
                    select(DetectionJob.id)
                    .where(
                        DetectionJob.status == DetectionJobStatus.QUEUED.value,
                        DetectionJob.available_at <= now,
                        DetectionJob.attempt_count < DetectionJob.max_attempts,
                    )
                    .order_by(
                        DetectionJob.priority.desc(),
                        DetectionJob.available_at,
                        DetectionJob.id,
                    )
                    .limit(1)
                    .scalar_subquery()
                )
            row = session.execute(
                update(DetectionJob)
                .where(
                    DetectionJob.id == candidate_id,
                    DetectionJob.status == DetectionJobStatus.QUEUED.value,
                    DetectionJob.available_at <= now,
                    DetectionJob.attempt_count < DetectionJob.max_attempts,
                )
                .values(
                    status=DetectionJobStatus.RUNNING.value,
                    worker_id=worker_id,
                    leased_at=now,
                    lease_expires_at=lease_expires_at,
                    attempt_count=DetectionJob.attempt_count + 1,
                    updated_at=now,
                )
                .returning(
                    DetectionJob.id,
                    DetectionJob.chat_id,
                    DetectionJob.trigger_message_id,
                    DetectionJob.attempt_count,
                    DetectionJob.max_attempts,
                    DetectionJob.created_at,
                )
            ).one_or_none()
            if row is None:
                return None

        return DetectionJobLease(
            job_id=row.id,
            chat_id=row.chat_id,
            trigger_message_id=row.trigger_message_id,
            worker_id=worker_id,
            attempt=row.attempt_count,
            max_attempts=row.max_attempts,
            batch_started_at=row.created_at,
            leased_at=now,
            lease_expires_at=lease_expires_at,
        )

    def heartbeat(
        self,
        lease: DetectionJobLease,
        *,
        now: datetime,
        lease_seconds: int = 300,
    ) -> datetime:
        now = _aware_utc(now, "now")
        if not 10 <= lease_seconds <= 3_600:
            raise DetectionQueueError(
                "lease_seconds must be between 10 and 3600"
            )
        expires_at = now + timedelta(seconds=lease_seconds)
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(DetectionJob)
                .where(
                    *self._lease_conditions(lease),
                    DetectionJob.lease_expires_at > now,
                )
                .values(lease_expires_at=expires_at, updated_at=now)
            )
            if result.rowcount != 1:
                raise DetectionLeaseError("worker no longer owns this job lease")
        return expires_at

    def start_run(
        self,
        lease: DetectionJobLease,
        *,
        provider: str,
        model: str,
        response_format: str,
        context_version: str,
        context_message_ids: Sequence[str],
        focus_message_ids: Sequence[str] | None = None,
        started_at: datetime,
    ) -> int:
        provider = _required_text(provider, "provider", maximum=64)
        model = _required_text(model, "model", maximum=128)
        response_format = _required_text(
            response_format, "response_format", maximum=32
        )
        context_version = _required_text(
            context_version, "context_version", maximum=16
        )
        started_at = _aware_utc(started_at, "started_at")
        message_ids = tuple(
            _required_text(item, "context message ID", maximum=128)
            for item in context_message_ids
        )
        if not message_ids or len(message_ids) != len(set(message_ids)):
            raise DetectionQueueError(
                "context_message_ids must be non-empty and unique"
            )
        if message_ids[-1] != lease.trigger_message_id:
            raise DetectionQueueError(
                "context must end at the job trigger message"
            )
        focus_ids = (
            message_ids
            if focus_message_ids is None
            else tuple(
                _required_text(item, "focus message ID", maximum=128)
                for item in focus_message_ids
            )
        )
        if not focus_ids or len(focus_ids) != len(set(focus_ids)):
            raise DetectionQueueError(
                "focus_message_ids must be non-empty and unique"
            )
        if not set(focus_ids).issubset(message_ids):
            raise DetectionQueueError(
                "focus_message_ids must be contained in context_message_ids"
            )

        with session_scope(self._session_factory) as session:
            self._require_owned_job(session, lease, active_at=started_at)
            known_messages = list(
                session.execute(
                    select(Message.id, Message.message_id).where(
                        Message.chat_id == lease.chat_id,
                        Message.message_id.in_(message_ids),
                    )
                )
            )
            message_db_ids = {
                row.message_id: row.id for row in known_messages
            }
            if len(message_db_ids) != len(message_ids):
                raise DetectionQueueError(
                    "context contains messages outside the job chat"
                )
            fingerprint = _context_fingerprint(
                lease.chat_id,
                lease.trigger_message_id,
                context_version,
                message_ids,
                focus_ids,
            )
            run = DetectionRun(
                job_id=lease.job_id,
                attempt=lease.attempt,
                status=DetectionRunStatus.RUNNING.value,
                provider=provider,
                model=model,
                response_format=response_format,
                context_version=context_version,
                context_fingerprint=fingerprint,
                context_message_ids_json=json.dumps(
                    message_ids, ensure_ascii=False
                ),
                started_at=started_at,
            )
            session.add(run)
            session.flush()
            for position, message_id in enumerate(focus_ids):
                session.add(
                    DetectionRunFocusMessage(
                        detection_run_id=run.id,
                        message_db_id=message_db_ids[message_id],
                        position=position,
                    )
                )
            session.flush()
            run_id = run.id
        return run_id

    def complete(
        self,
        lease: DetectionJobLease,
        run_id: int,
        *,
        result: Mapping[str, object],
        response_format: str,
        request_id: str | None,
        usage: Mapping[str, int],
        finished_at: datetime,
        completion_hook: DetectionCompletionHook | None = None,
    ) -> object | None:
        finished_at = _aware_utc(finished_at, "finished_at")
        response_format = _required_text(
            response_format, "response_format", maximum=32
        )
        request_id = _optional_text(request_id, "request_id", maximum=128)
        result_json = _json_object(result, "result")
        prompt_tokens = _token_count(usage, "prompt_tokens")
        completion_tokens = _token_count(usage, "completion_tokens")
        total_tokens = _token_count(usage, "total_tokens")

        hook_result: object | None = None
        with session_scope(self._session_factory) as session:
            job = self._require_owned_job(
                session, lease, active_at=finished_at
            )
            run = self._require_running_run(session, lease, run_id)
            latency_ms = _latency_ms(run.started_at, finished_at)
            run.status = DetectionRunStatus.SUCCEEDED.value
            run.response_format = response_format
            run.request_id = request_id
            run.prompt_tokens = prompt_tokens
            run.completion_tokens = completion_tokens
            run.total_tokens = total_tokens
            run.result_json = result_json
            run.error_code = None
            run.error_message = None
            run.latency_ms = latency_ms
            run.finished_at = finished_at

            job.status = DetectionJobStatus.COMPLETED.value
            job.worker_id = None
            job.leased_at = None
            job.lease_expires_at = None
            job.completed_at = finished_at
            job.last_error_code = None
            job.updated_at = finished_at
            session.flush()

            if completion_hook is not None:
                hook_result = completion_hook(
                    session,
                    run.id,
                    materialized_at=finished_at,
                )

        return hook_result

    def fail(
        self,
        lease: DetectionJobLease,
        run_id: int,
        *,
        error_code: str,
        error_message: str,
        failed_at: datetime,
        retry_delay: timedelta,
    ) -> FailureResult:
        failed_at = _aware_utc(failed_at, "failed_at")
        error_code = _required_text(error_code, "error_code", maximum=64)
        error_message = _required_text(
            error_message, "error_message", maximum=2_000
        )
        if retry_delay < timedelta(0) or retry_delay > timedelta(days=1):
            raise DetectionQueueError(
                "retry_delay must be between zero and one day"
            )

        with session_scope(self._session_factory) as session:
            job = self._require_owned_job(session, lease, active_at=failed_at)
            run = self._require_running_run(session, lease, run_id)
            run.status = DetectionRunStatus.FAILED.value
            run.result_json = None
            run.error_code = error_code
            run.error_message = error_message
            run.latency_ms = _latency_ms(run.started_at, failed_at)
            run.finished_at = failed_at

            exhausted = job.attempt_count >= job.max_attempts
            job.status = (
                DetectionJobStatus.DEAD.value
                if exhausted
                else DetectionJobStatus.QUEUED.value
            )
            job.available_at = failed_at + retry_delay
            job.worker_id = None
            job.leased_at = None
            job.lease_expires_at = None
            job.last_error_code = error_code
            job.updated_at = failed_at
            status = DetectionJobStatus(job.status)
            available_at = job.available_at

        return FailureResult(job_status=status, available_at=available_at)

    def get_job(self, job_id: int) -> DetectionJobSnapshot | None:
        with session_scope(self._session_factory) as session:
            job = session.get(DetectionJob, job_id)
            return None if job is None else _job_snapshot(job)

    def list_runs(self, job_id: int) -> list[DetectionRunSnapshot]:
        with session_scope(self._session_factory) as session:
            runs = list(
                session.scalars(
                    select(DetectionRun)
                    .where(DetectionRun.job_id == job_id)
                    .order_by(DetectionRun.attempt)
                )
            )
            return [_run_snapshot(run) for run in runs]

    @staticmethod
    def _lease_conditions(lease: DetectionJobLease) -> tuple[object, ...]:
        return (
            DetectionJob.id == lease.job_id,
            DetectionJob.chat_id == lease.chat_id,
            DetectionJob.status == DetectionJobStatus.RUNNING.value,
            DetectionJob.worker_id == lease.worker_id,
            DetectionJob.attempt_count == lease.attempt,
        )

    def _require_owned_job(
        self,
        session: Session,
        lease: DetectionJobLease,
        *,
        active_at: datetime,
    ) -> DetectionJob:
        job = session.scalar(
            select(DetectionJob).where(
                *self._lease_conditions(lease),
                DetectionJob.lease_expires_at > active_at,
            )
        )
        if job is None:
            raise DetectionLeaseError("worker no longer owns this job lease")
        return job

    @staticmethod
    def _require_running_run(
        session: Session,
        lease: DetectionJobLease,
        run_id: int,
    ) -> DetectionRun:
        run = session.scalar(
            select(DetectionRun).where(
                DetectionRun.id == run_id,
                DetectionRun.job_id == lease.job_id,
                DetectionRun.attempt == lease.attempt,
                DetectionRun.status == DetectionRunStatus.RUNNING.value,
            )
        )
        if run is None:
            raise DetectionQueueError("detection run is not active for this lease")
        return run

    @staticmethod
    def _recover_expired_leases(session: Session, now: datetime) -> None:
        expired_job_ids = select(DetectionJob.id).where(
            DetectionJob.status == DetectionJobStatus.RUNNING.value,
            DetectionJob.lease_expires_at <= now,
        )
        session.execute(
            update(DetectionRun)
            .where(
                DetectionRun.job_id.in_(expired_job_ids),
                DetectionRun.status == DetectionRunStatus.RUNNING.value,
            )
            .values(
                status=DetectionRunStatus.FAILED.value,
                result_json=None,
                error_code="worker_lease_expired",
                error_message="worker lease expired before completion",
                latency_ms=None,
                finished_at=now,
            )
        )
        session.execute(
            update(DetectionJob)
            .where(
                DetectionJob.status == DetectionJobStatus.RUNNING.value,
                DetectionJob.lease_expires_at <= now,
            )
            .values(
                status=case(
                    (
                        DetectionJob.attempt_count >= DetectionJob.max_attempts,
                        DetectionJobStatus.DEAD.value,
                    ),
                    else_=DetectionJobStatus.QUEUED.value,
                ),
                available_at=now,
                worker_id=None,
                leased_at=None,
                lease_expires_at=None,
                last_error_code="worker_lease_expired",
                updated_at=now,
            )
        )


def _job_snapshot(job: DetectionJob) -> DetectionJobSnapshot:
    return DetectionJobSnapshot(
        job_id=job.id,
        chat_id=job.chat_id,
        trigger_message_id=job.trigger_message_id,
        status=DetectionJobStatus(job.status),
        priority=job.priority,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        available_at=job.available_at,
        worker_id=job.worker_id,
        lease_expires_at=job.lease_expires_at,
        completed_at=job.completed_at,
        cancelled_at=job.cancelled_at,
        cancel_reason=job.cancel_reason,
        last_error_code=job.last_error_code,
    )


def _run_snapshot(run: DetectionRun) -> DetectionRunSnapshot:
    context_ids = json.loads(run.context_message_ids_json)
    result = None if run.result_json is None else json.loads(run.result_json)
    return DetectionRunSnapshot(
        run_id=run.id,
        job_id=run.job_id,
        attempt=run.attempt,
        status=DetectionRunStatus(run.status),
        provider=run.provider,
        model=run.model,
        response_format=run.response_format,
        context_fingerprint=run.context_fingerprint,
        context_message_ids=tuple(context_ids),
        focus_message_ids=tuple(
            item.message.message_id for item in run.focus_messages
        ),
        request_id=run.request_id,
        total_tokens=run.total_tokens,
        result=result,
        error_code=run.error_code,
        error_message=run.error_message,
        latency_ms=run.latency_ms,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _required_text(value: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DetectionQueueError(f"{field} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise DetectionQueueError(f"{field} must be at most {maximum} characters")
    return cleaned


def _job_ids(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise DetectionQueueError("job_ids must be a sequence of integers")
    ids = tuple(values)
    if not ids:
        raise DetectionQueueError("at least one job_id is required")
    if len(ids) > 1_000:
        raise DetectionQueueError("at most 1000 jobs can be cancelled at once")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1
        for item in ids
    ):
        raise DetectionQueueError("job IDs must be positive integers")
    if len(ids) != len(set(ids)):
        raise DetectionQueueError("job IDs must be unique")
    return ids


def _optional_text(
    value: str | None, field: str, *, maximum: int
) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum=maximum)


def _aware_utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DetectionQueueError(f"{field} must include timezone information")
    return value.astimezone(timezone.utc)


def _json_object(value: Mapping[str, object], field: str) -> str:
    if not isinstance(value, Mapping):
        raise DetectionQueueError(f"{field} must be a JSON object")
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DetectionQueueError(f"{field} must be JSON serializable") from exc


def _token_count(usage: Mapping[str, int], field: str) -> int | None:
    value = usage.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DetectionQueueError(f"{field} must be a non-negative integer")
    return value


def _latency_ms(started_at: datetime, finished_at: datetime) -> int:
    if finished_at < started_at:
        raise DetectionQueueError("finish time cannot be before start time")
    return round((finished_at - started_at).total_seconds() * 1_000)


def _context_fingerprint(
    chat_id: str,
    trigger_message_id: str,
    context_version: str,
    message_ids: Sequence[str],
    focus_message_ids: Sequence[str],
) -> str:
    canonical = json.dumps(
        {
            "chat_id": chat_id,
            "trigger_message_id": trigger_message_id,
            "context_version": context_version,
            "message_ids": list(message_ids),
            "focus_message_ids": list(focus_message_ids),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
