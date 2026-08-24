"""Phase 3C-3B Worker orchestration tests without network access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from sqlalchemy import text

from app.agent.context import TaskDetectionContextBuilder
from app.agent.contracts import (
    TaskCandidate,
    TaskDetectionBatchResult,
    TaskOwner,
)
from app.agent.provider import (
    ModelProviderError,
    TaskBatchDetectionCall,
)
from app.agent.queue import DetectionJobStatus, DetectionRunStatus
from app.agent.worker import (
    DetectionWorker,
    WorkerOutcome,
    WorkerOutcomeStatus,
    run_worker_loop,
)
from app.config import DatabaseSettings
from app.database.models import DetectionMaterialization
from app.database.repository import MessageLookupError
from app.database.runtime import open_database_runtime
from app.tasks.repository import TaskMaterializationError
from tests.test_messages import TEXT_EVENT


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeBatchDetector:
    def __init__(
        self,
        result: TaskDetectionBatchResult | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or TaskDetectionBatchResult(candidates=())
        self.error = error
        self.contexts = []

    def detect_batch(self, context):
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return TaskBatchDetectionCall(
            result=self.result,
            model="qwen-test",
            response_format="json_schema",
            request_id="req_worker_test",
            usage={"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
        )


class FailingContextBuilder:
    def build(self, *_args, **_kwargs):
        raise MessageLookupError("trigger disappeared")


class FailingTaskMaterializer:
    def materialize_run_in_session(
        self, session, detection_run_id, *, materialized_at
    ):
        session.add(
            DetectionMaterialization(
                detection_run_id=detection_run_id,
                candidate_count=0,
                created_task_count=0,
                reused_task_count=0,
                materialized_at=materialized_at,
            )
        )
        session.flush()
        raise TaskMaterializationError("simulated materialization failure")


class DetectionWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "worker.db"
        settings = DatabaseSettings(
            url=f"sqlite:///{database_path}", echo=False
        )
        self.runtime_manager = open_database_runtime(settings)
        self.runtime = self.runtime_manager.__enter__()
        self.clock = MutableClock(
            datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)
        )
        self._ingest(
            "evt_a1", "om_a1", "oc_a", "ou_teacher", "王政补 baseline。"
        )
        self._ingest("evt_a2", "om_a2", "oc_a", "ou_wang", "收到。")
        self._ingest("evt_b1", "om_b1", "oc_b", "ou_other", "另一个群。")
        self.runtime.aliases.bind("oc_a", "ou_teacher", "老师")
        self.runtime.aliases.bind("oc_a", "ou_wang", "王政")
        self.builder = TaskDetectionContextBuilder(
            self.runtime.repository,
            self.runtime.aliases,
        )

    def tearDown(self) -> None:
        self.runtime_manager.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_success_completes_job_and_audits_multiple_candidates(self) -> None:
        job = self._enqueue("om_a2")
        detector = FakeBatchDetector(
            TaskDetectionBatchResult(
                candidates=(
                    self._candidate("补充 baseline", ("om_a1", "om_a2")),
                    self._candidate("整理实验记录", ("om_a2",)),
                )
            )
        )
        worker = self._worker(detector)

        outcome = worker.run_once("worker-test")

        self.assertEqual(outcome.status, WorkerOutcomeStatus.COMPLETED)
        self.assertEqual(outcome.job_id, job.job_id)
        self.assertEqual(outcome.candidate_count, 2)
        self.assertEqual(outcome.created_task_count, 2)
        self.assertEqual(outcome.reused_task_count, 0)
        self.assertEqual(len(outcome.task_ids), 2)
        self.assertEqual(
            self.runtime.detection_queue.get_job(job.job_id).status,
            DetectionJobStatus.COMPLETED,
        )
        runs = self.runtime.detection_queue.list_runs(job.job_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, DetectionRunStatus.SUCCEEDED)
        self.assertEqual(len(runs[0].result["candidates"]), 2)
        self.assertEqual(runs[0].request_id, "req_worker_test")
        self.assertEqual(runs[0].total_tokens, 120)
        self.assertEqual(runs[0].context_message_ids, ("om_a1", "om_a2"))
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 2)
        self.assertEqual(
            self.runtime.repository.counts().task_materializations, 1
        )

    def test_empty_candidate_result_is_a_success(self) -> None:
        job = self._enqueue("om_a2")
        worker = self._worker(FakeBatchDetector())

        outcome = worker.run_once("worker-empty")

        self.assertEqual(outcome.status, WorkerOutcomeStatus.COMPLETED)
        self.assertEqual(outcome.candidate_count, 0)
        self.assertEqual(outcome.created_task_count, 0)
        self.assertEqual(outcome.task_ids, ())
        run = self.runtime.detection_queue.list_runs(job.job_id)[0]
        self.assertEqual(run.result, {"candidates": []})
        self.assertEqual(
            self.runtime.repository.counts().task_materializations, 1
        )

    def test_single_explicit_assignment_materializes_without_owner_reply(self) -> None:
        job = self._enqueue("om_a1")
        detector = FakeBatchDetector(
            TaskDetectionBatchResult(
                candidates=(
                    self._candidate("补充 baseline", ("om_a1",)),
                )
            )
        )

        outcome = self._worker(detector).run_once(
            "worker-single-assignment", job_id=job.job_id
        )

        self.assertEqual(outcome.status, WorkerOutcomeStatus.COMPLETED)
        self.assertEqual(outcome.created_task_count, 1)
        self.assertEqual(
            [message.message_id for message in detector.contexts[0].messages],
            ["om_a1"],
        )
        self.assertEqual(
            detector.contexts[0].focus_message_ids, ("om_a1",)
        )
        task = self.runtime.tasks.get_task(outcome.task_ids[0])
        self.assertEqual(task.owner_open_id, "ou_wang")
        self.assertEqual(
            self.runtime.tasks.evidence_message_ids(task.task_id),
            ("om_a1",),
        )

    def test_materialization_failure_rolls_back_completion_then_retries(self) -> None:
        job = self._enqueue("om_a2", max_attempts=2)
        detector = FakeBatchDetector(
            TaskDetectionBatchResult(
                candidates=(
                    self._candidate("补充 baseline", ("om_a1", "om_a2")),
                )
            )
        )
        failing_worker = self._worker(
            detector, tasks=FailingTaskMaterializer()
        )

        first = failing_worker.run_once("worker-materialization-failure")

        self.assertEqual(first.status, WorkerOutcomeStatus.RETRY_SCHEDULED)
        self.assertEqual(first.error_code, "task_materialization_error")
        self.assertEqual(self.runtime.tasks.list_tasks("oc_a"), ())
        self.assertEqual(
            self.runtime.repository.counts().task_materializations, 0
        )
        failed_run = self.runtime.detection_queue.list_runs(job.job_id)[0]
        self.assertEqual(failed_run.status, DetectionRunStatus.FAILED)
        self.assertEqual(
            failed_run.error_code, "task_materialization_error"
        )

        self.clock.value = first.retry_at
        second = self._worker(detector).run_once(
            "worker-materialization-retry"
        )

        self.assertEqual(second.status, WorkerOutcomeStatus.COMPLETED)
        self.assertEqual(second.created_task_count, 1)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 1)
        self.assertEqual(
            [run.status for run in self.runtime.detection_queue.list_runs(job.job_id)],
            [DetectionRunStatus.FAILED, DetectionRunStatus.SUCCEEDED],
        )

    def test_worker_does_not_backfill_preexisting_successful_runs(self) -> None:
        old_job = self._enqueue("om_a1")
        old_lease = self.runtime.detection_queue.claim_job(
            old_job.job_id,
            "legacy-worker",
            now=self.clock.value,
        )
        old_run_id = self.runtime.detection_queue.start_run(
            old_lease,
            provider="openai_compatible",
            model="qwen-test",
            response_format="json_schema",
            context_version="1.0",
            context_message_ids=("om_a1",),
            started_at=self.clock.value,
        )
        self.runtime.detection_queue.complete(
            old_lease,
            old_run_id,
            result={"candidates": []},
            response_format="json_schema",
            request_id="req_legacy",
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
            finished_at=self.clock.value,
        )
        new_job = self._enqueue("om_a2")

        outcome = self._worker(FakeBatchDetector()).run_once(
            "worker-new-only", job_id=new_job.job_id
        )

        self.assertEqual(outcome.status, WorkerOutcomeStatus.COMPLETED)
        with self.runtime.engine.connect() as connection:
            materialized_run_ids = connection.execute(
                text(
                    "SELECT detection_run_id FROM detection_materializations "
                    "ORDER BY detection_run_id"
                )
            ).scalars().all()
        self.assertEqual(materialized_run_ids, [outcome.run_id])
        self.assertNotIn(old_run_id, materialized_run_ids)

    def test_model_failure_retries_then_becomes_dead(self) -> None:
        job = self._enqueue("om_a2", max_attempts=2)
        detector = FakeBatchDetector(
            error=ModelProviderError("temporary model failure")
        )
        worker = self._worker(detector, retry_base_seconds=5)

        first = worker.run_once("worker-retry")

        self.assertEqual(first.status, WorkerOutcomeStatus.RETRY_SCHEDULED)
        self.assertEqual(first.error_code, "model_provider_error")
        self.assertEqual(
            self.runtime.detection_queue.get_job(job.job_id).status,
            DetectionJobStatus.QUEUED,
        )
        self.clock.value = first.retry_at
        second = worker.run_once("worker-retry")

        self.assertEqual(second.status, WorkerOutcomeStatus.DEAD)
        self.assertIsNone(second.retry_at)
        self.assertEqual(
            self.runtime.detection_queue.get_job(job.job_id).status,
            DetectionJobStatus.DEAD,
        )
        runs = self.runtime.detection_queue.list_runs(job.job_id)
        self.assertEqual(
            [run.status for run in runs],
            [DetectionRunStatus.FAILED, DetectionRunStatus.FAILED],
        )

    def test_interrupt_during_model_call_is_audited_before_stopping(self) -> None:
        job = self._enqueue("om_a2", max_attempts=2)
        detector = FakeBatchDetector(error=KeyboardInterrupt())
        worker = self._worker(detector, retry_base_seconds=5)

        with self.assertRaises(KeyboardInterrupt):
            worker.run_once("worker-interrupted")

        snapshot = self.runtime.detection_queue.get_job(job.job_id)
        self.assertEqual(snapshot.status, DetectionJobStatus.QUEUED)
        self.assertEqual(snapshot.last_error_code, "worker_interrupted")
        run = self.runtime.detection_queue.list_runs(job.job_id)[0]
        self.assertEqual(run.status, DetectionRunStatus.FAILED)
        self.assertEqual(run.error_code, "worker_interrupted")

    def test_targeted_job_does_not_consume_older_job(self) -> None:
        older = self._enqueue("om_a1", priority=10)
        target = self._enqueue("om_a2", priority=0)
        worker = self._worker(FakeBatchDetector())

        outcome = worker.run_once("worker-target", job_id=target.job_id)

        self.assertEqual(outcome.job_id, target.job_id)
        self.assertEqual(
            self.runtime.detection_queue.get_job(older.job_id).status,
            DetectionJobStatus.QUEUED,
        )

    def test_context_is_strictly_isolated_to_the_job_chat(self) -> None:
        self._enqueue("om_a2")
        detector = FakeBatchDetector()
        worker = self._worker(detector)

        worker.run_once("worker-isolation")

        context = detector.contexts[0]
        self.assertEqual(context.chat_id, "oc_a")
        self.assertNotIn(
            "om_b1", {message.message_id for message in context.messages}
        )

    def test_context_failure_is_audited_without_calling_model(self) -> None:
        job = self._enqueue("om_a2", max_attempts=1)
        detector = FakeBatchDetector()
        worker = DetectionWorker(
            self.runtime.detection_queue,
            FailingContextBuilder(),
            detector,
            self.runtime.tasks,
            model="qwen-test",
            clock=self.clock,
        )

        outcome = worker.run_once("worker-context-error")

        self.assertEqual(outcome.status, WorkerOutcomeStatus.DEAD)
        self.assertEqual(outcome.error_code, "context_error")
        self.assertEqual(detector.contexts, [])
        run = self.runtime.detection_queue.list_runs(job.job_id)[0]
        self.assertEqual(run.status, DetectionRunStatus.FAILED)
        self.assertEqual(run.response_format, "not_requested")
        self.assertEqual(run.context_message_ids, ("om_a2",))

    def test_no_ready_job_returns_idle_without_model_call(self) -> None:
        detector = FakeBatchDetector()
        worker = self._worker(detector)

        outcome = worker.run_once("worker-idle")

        self.assertEqual(outcome.status, WorkerOutcomeStatus.IDLE)
        self.assertIsNone(outcome.job_id)
        self.assertEqual(detector.contexts, [])

    def test_continuous_loop_sleeps_only_when_idle_and_stops_cleanly(self) -> None:
        completed = WorkerOutcome(
            status=WorkerOutcomeStatus.COMPLETED,
            job_id=1,
            run_id=1,
            attempt=1,
            candidate_count=0,
            created_task_count=0,
            reused_task_count=0,
            task_ids=(),
            error_code=None,
            retry_at=None,
        )
        idle = WorkerOutcome(
            status=WorkerOutcomeStatus.IDLE,
            job_id=None,
            run_id=None,
            attempt=None,
            candidate_count=None,
            created_task_count=None,
            reused_task_count=None,
            task_ids=(),
            error_code=None,
            retry_at=None,
        )
        fake_worker = MagicMock()
        fake_worker.run_once.side_effect = [completed, idle]
        emitted = []
        stopped = False

        def sleeper(seconds: float) -> None:
            nonlocal stopped
            self.assertEqual(seconds, 0.5)
            stopped = True

        summary = run_worker_loop(
            fake_worker,
            "worker-loop",
            poll_seconds=0.5,
            sleeper=sleeper,
            stop_requested=lambda: stopped,
            on_outcome=emitted.append,
        )

        self.assertEqual(summary.iterations, 2)
        self.assertEqual(summary.processed, 1)
        self.assertEqual(summary.idle_polls, 1)
        self.assertEqual(emitted, [completed])

    def _worker(
        self,
        detector: FakeBatchDetector,
        *,
        retry_base_seconds: int = 30,
        tasks=None,
    ) -> DetectionWorker:
        return DetectionWorker(
            self.runtime.detection_queue,
            self.builder,
            detector,
            self.runtime.tasks if tasks is None else tasks,
            model="qwen-test",
            lease_seconds=300,
            retry_base_seconds=retry_base_seconds,
            clock=self.clock,
        )

    def _enqueue(
        self,
        message_id: str,
        *,
        priority: int = 0,
        max_attempts: int = 3,
    ):
        return self.runtime.detection_queue.enqueue(
            "oc_a",
            message_id,
            available_at=self.clock.value,
            priority=priority,
            max_attempts=max_attempts,
        )

    @staticmethod
    def _candidate(
        title: str,
        evidence: tuple[str, ...],
    ) -> TaskCandidate:
        return TaskCandidate(
            confidence=0.96,
            owner=TaskOwner(name="王政", open_id="ou_wang"),
            title=title,
            description=title,
            deadline=None,
            evidence_message_ids=evidence,
        )

    def _ingest(
        self,
        event_id: str,
        message_id: str,
        chat_id: str,
        open_id: str,
        text: str,
    ) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = event_id
        payload["event"]["message"]["message_id"] = message_id
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["sender"]["sender_id"]["open_id"] = open_id
        payload["event"]["sender"]["sender_id"]["union_id"] = f"on_{open_id}"
        self.runtime.ingestion.process_payload(
            payload,
            received_at=self.clock.value,
            enqueue_detection=False,
        )


if __name__ == "__main__":
    unittest.main()
