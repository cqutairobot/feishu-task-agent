"""One-job execution core for durable automatic task detection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol

from app.agent.context import TaskDetectionContext, TaskDetectionContextBuilder
from app.agent.provider import ModelProviderError, TaskBatchDetectionCall
from app.agent.queue import (
    DetectionJobLease,
    DetectionJobStatus,
    DetectionQueueRepository,
)
from app.database.repository import MessageLookupError
from app.identity.aliases import AliasError
from app.tasks.repository import (
    MaterializationResult,
    TaskMaterializationError,
    TaskRepository,
)


class BatchTaskDetector(Protocol):
    def detect_batch(
        self, context: TaskDetectionContext
    ) -> TaskBatchDetectionCall: ...


class WorkerOutcomeStatus(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    status: WorkerOutcomeStatus
    job_id: int | None
    run_id: int | None
    attempt: int | None
    candidate_count: int | None
    created_task_count: int | None
    reused_task_count: int | None
    task_ids: tuple[int, ...]
    error_code: str | None
    retry_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "candidate_count": self.candidate_count,
            "created_task_count": self.created_task_count,
            "reused_task_count": self.reused_task_count,
            "task_ids": list(self.task_ids),
            "error_code": self.error_code,
            "retry_at": (
                None
                if self.retry_at is None
                else self.retry_at.astimezone(timezone.utc).isoformat()
            ),
        }


@dataclass(frozen=True, slots=True)
class WorkerLoopSummary:
    iterations: int
    processed: int
    idle_polls: int


class DetectionWorker:
    """Claim and finish at most one detection job per invocation."""

    def __init__(
        self,
        queue: DetectionQueueRepository,
        context_builder: TaskDetectionContextBuilder,
        detector: BatchTaskDetector,
        tasks: TaskRepository,
        *,
        model: str,
        context_limit: int = 30,
        lease_seconds: int = 300,
        retry_base_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not 1 <= context_limit <= 100:
            raise ValueError("context_limit must be between 1 and 100")
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        if not 1 <= retry_base_seconds <= 3_600:
            raise ValueError("retry_base_seconds must be between 1 and 3600")
        self._queue = queue
        self._context_builder = context_builder
        self._detector = detector
        self._tasks = tasks
        self._model = model.strip()
        self._context_limit = context_limit
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(
        self,
        worker_id: str,
        *,
        job_id: int | None = None,
    ) -> WorkerOutcome:
        """Process one ready job, optionally targeting an exact job ID."""

        claimed_at = self._clock()
        if job_id is None:
            lease = self._queue.claim_next(
                worker_id,
                now=claimed_at,
                lease_seconds=self._lease_seconds,
            )
        else:
            lease = self._queue.claim_job(
                job_id,
                worker_id,
                now=claimed_at,
                lease_seconds=self._lease_seconds,
            )
        if lease is None:
            return WorkerOutcome(
                status=WorkerOutcomeStatus.IDLE,
                job_id=job_id,
                run_id=None,
                attempt=None,
                candidate_count=None,
                created_task_count=None,
                reused_task_count=None,
                task_ids=(),
                error_code=None,
                retry_at=None,
            )

        try:
            context = self._context_builder.build(
                lease.chat_id,
                lease.trigger_message_id,
                limit=self._context_limit,
                focus_since=lease.batch_started_at,
            )
        except (AliasError, MessageLookupError, ValueError) as exc:
            run_id = self._queue.start_run(
                lease,
                provider="openai_compatible",
                model=self._model,
                response_format="not_requested",
                context_version="1.2",
                context_message_ids=(lease.trigger_message_id,),
                focus_message_ids=(lease.trigger_message_id,),
                started_at=self._clock(),
            )
            return self._record_failure(
                lease,
                run_id,
                error_code="context_error",
                error=exc,
            )

        run_id = self._queue.start_run(
            lease,
            provider="openai_compatible",
            model=self._model,
            response_format="json_schema",
            context_version="1.2",
            context_message_ids=tuple(
                message.message_id for message in context.messages
            ),
            focus_message_ids=context.focus_message_ids,
            started_at=self._clock(),
        )
        try:
            call = self._detector.detect_batch(context)
        except ModelProviderError as exc:
            return self._record_failure(
                lease,
                run_id,
                error_code="model_provider_error",
                error=exc,
            )
        except KeyboardInterrupt as exc:
            self._record_failure(
                lease,
                run_id,
                error_code="worker_interrupted",
                error=exc,
            )
            raise

        try:
            completion = self._queue.complete(
                lease,
                run_id,
                result=call.result.to_dict(),
                response_format=call.response_format,
                request_id=call.request_id,
                usage=call.usage,
                finished_at=self._clock(),
                completion_hook=self._tasks.materialize_run_in_session,
            )
            if not isinstance(completion, MaterializationResult):
                raise TaskMaterializationError(
                    "task materializer returned an invalid result"
                )
        except TaskMaterializationError as exc:
            return self._record_failure(
                lease,
                run_id,
                error_code="task_materialization_error",
                error=exc,
            )
        return WorkerOutcome(
            status=WorkerOutcomeStatus.COMPLETED,
            job_id=lease.job_id,
            run_id=run_id,
            attempt=lease.attempt,
            candidate_count=len(call.result.candidates),
            created_task_count=completion.created_task_count,
            reused_task_count=completion.reused_task_count,
            task_ids=completion.task_ids,
            error_code=None,
            retry_at=None,
        )

    def _record_failure(
        self,
        lease: DetectionJobLease,
        run_id: int,
        *,
        error_code: str,
        error: BaseException,
    ) -> WorkerOutcome:
        delay_seconds = min(
            3_600,
            self._retry_base_seconds * (2 ** (lease.attempt - 1)),
        )
        failure = self._queue.fail(
            lease,
            run_id,
            error_code=error_code,
            error_message=_bounded_error_message(error),
            failed_at=self._clock(),
            retry_delay=timedelta(seconds=delay_seconds),
        )
        dead = failure.job_status is DetectionJobStatus.DEAD
        return WorkerOutcome(
            status=(
                WorkerOutcomeStatus.DEAD
                if dead
                else WorkerOutcomeStatus.RETRY_SCHEDULED
            ),
            job_id=lease.job_id,
            run_id=run_id,
            attempt=lease.attempt,
            candidate_count=None,
            created_task_count=None,
            reused_task_count=None,
            task_ids=(),
            error_code=error_code,
            retry_at=None if dead else failure.available_at,
        )


def run_worker_loop(
    worker: DetectionWorker,
    worker_id: str,
    *,
    poll_seconds: float,
    on_outcome: Callable[[WorkerOutcome], None] | None = None,
    sleeper: Callable[[float], None],
    stop_requested: Callable[[], bool] | None = None,
) -> WorkerLoopSummary:
    """Poll until asked to stop, sleeping only while no job is ready."""

    if not 0.1 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be between 0.1 and 60")
    stop_requested = stop_requested or (lambda: False)
    iterations = 0
    processed = 0
    idle_polls = 0
    while not stop_requested():
        outcome = worker.run_once(worker_id)
        iterations += 1
        if outcome.status is WorkerOutcomeStatus.IDLE:
            idle_polls += 1
            sleeper(poll_seconds)
            continue
        processed += 1
        if on_outcome is not None:
            on_outcome(outcome)
    return WorkerLoopSummary(
        iterations=iterations,
        processed=processed,
        idle_polls=idle_polls,
    )


def _bounded_error_message(error: BaseException) -> str:
    message = " ".join(str(error).split())
    if not message:
        message = type(error).__name__
    return f"{type(error).__name__}: {message}"[:2_000]
