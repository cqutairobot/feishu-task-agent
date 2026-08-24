"""Phase 3C-1 durable detection queue tests."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from app.agent.queue import (
    DetectionJobStatus,
    DetectionLeaseError,
    DetectionQueueError,
    DetectionRunStatus,
)
from app.config import DatabaseSettings
from app.database.runtime import open_database_runtime
from tests.test_messages import TEXT_EVENT


class DetectionQueueRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "queue.db"
        self.settings = DatabaseSettings(
            url=f"sqlite:///{database_path}", echo=False
        )
        self.runtime_manager = open_database_runtime(self.settings)
        self.runtime = self.runtime_manager.__enter__()
        self.queue = self.runtime.detection_queue
        self.now = datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)
        self._ingest("evt_a1", "om_a1", "oc_a", "ou_a", "第一条")
        self._ingest("evt_a2", "om_a2", "oc_a", "ou_b", "第二条")
        self._ingest("evt_b1", "om_b1", "oc_b", "ou_c", "另一个群")

    def tearDown(self) -> None:
        self.runtime_manager.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_enqueue_is_idempotent_per_chat_and_trigger(self) -> None:
        first = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now, max_attempts=3
        )
        duplicate = self.queue.enqueue(
            "oc_a",
            "om_a1",
            available_at=self.now,
            priority=50,
            max_attempts=9,
        )

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.job_id, duplicate.job_id)
        snapshot = self.queue.get_job(first.job_id)
        self.assertEqual(snapshot.priority, 0)
        self.assertEqual(snapshot.max_attempts, 3)

    def test_enqueue_rejects_cross_chat_trigger(self) -> None:
        with self.assertRaisesRegex(DetectionQueueError, "does not exist"):
            self.queue.enqueue("oc_a", "om_b1", available_at=self.now)

    def test_claim_respects_priority_and_available_time(self) -> None:
        first = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now, priority=0
        )
        second = self.queue.enqueue(
            "oc_a", "om_a2", available_at=self.now, priority=10
        )

        lease = self.queue.claim_next("worker-1", now=self.now)

        self.assertEqual(lease.job_id, second.job_id)
        self.assertEqual(lease.attempt, 1)
        self.assertEqual(
            self.queue.get_job(second.job_id).status,
            DetectionJobStatus.RUNNING,
        )
        self.assertEqual(
            self.queue.get_job(first.job_id).status,
            DetectionJobStatus.QUEUED,
        )

    def test_claim_job_targets_exact_ready_job(self) -> None:
        first = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now, priority=10
        )
        second = self.queue.enqueue(
            "oc_a", "om_a2", available_at=self.now, priority=0
        )

        lease = self.queue.claim_job(
            second.job_id,
            "worker-targeted",
            now=self.now,
        )

        self.assertEqual(lease.job_id, second.job_id)
        self.assertEqual(
            self.queue.get_job(first.job_id).status,
            DetectionJobStatus.QUEUED,
        )

    def test_claim_job_does_not_bypass_availability_or_status(self) -> None:
        future = self.queue.enqueue(
            "oc_a",
            "om_a1",
            available_at=self.now + timedelta(seconds=5),
        )

        self.assertIsNone(
            self.queue.claim_job(future.job_id, "worker-1", now=self.now)
        )
        lease = self.queue.claim_job(
            future.job_id,
            "worker-1",
            now=self.now + timedelta(seconds=5),
        )
        self.assertIsNotNone(lease)
        self.assertIsNone(
            self.queue.claim_job(
                future.job_id,
                "worker-2",
                now=self.now + timedelta(seconds=5),
            )
        )

    def test_cancels_exact_queued_jobs_atomically(self) -> None:
        first = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now
        )
        second = self.queue.enqueue(
            "oc_a", "om_a2", available_at=self.now
        )

        results = self.queue.cancel_jobs(
            (first.job_id, second.job_id),
            reason="obsolete acceptance jobs",
            cancelled_at=self.now,
        )
        self._restart_runtime()

        self.assertEqual([item.changed for item in results], [True, True])
        self.assertEqual(
            [item.status for item in results],
            [DetectionJobStatus.CANCELLED, DetectionJobStatus.CANCELLED],
        )
        for job_id in (first.job_id, second.job_id):
            snapshot = self.queue.get_job(job_id)
            self.assertEqual(snapshot.status, DetectionJobStatus.CANCELLED)
            self.assertEqual(snapshot.cancel_reason, "obsolete acceptance jobs")
            self.assertEqual(snapshot.cancelled_at, self.now)
        self.assertIsNone(self.queue.claim_next("worker-1", now=self.now))

    def test_repeated_cancellation_preserves_original_audit(self) -> None:
        job = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now
        )
        first = self.queue.cancel_jobs(
            (job.job_id,),
            reason="first reason",
            cancelled_at=self.now,
        )[0]
        later = self.now + timedelta(minutes=1)

        repeated = self.queue.cancel_jobs(
            (job.job_id,),
            reason="replacement reason",
            cancelled_at=later,
        )[0]

        self.assertTrue(first.changed)
        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.reason, "first reason")
        self.assertEqual(repeated.cancelled_at, self.now)

    def test_cancellation_rejects_invalid_set_without_partial_change(self) -> None:
        queued = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now
        )
        completed = self.queue.enqueue(
            "oc_a", "om_a2", available_at=self.now
        )
        lease = self.queue.claim_job(
            completed.job_id, "worker-1", now=self.now
        )
        run_id = self._start_run(lease, self.now)
        self.queue.complete(
            lease,
            run_id,
            result={"candidates": []},
            response_format="json_schema",
            request_id=None,
            usage={},
            finished_at=self.now,
        )

        with self.assertRaisesRegex(DetectionQueueError, "only queued"):
            self.queue.cancel_jobs(
                (queued.job_id, completed.job_id),
                reason="must be atomic",
                cancelled_at=self.now,
            )

        self.assertEqual(
            self.queue.get_job(queued.job_id).status,
            DetectionJobStatus.QUEUED,
        )

    def test_cancellation_rejects_unknown_job(self) -> None:
        with self.assertRaisesRegex(DetectionQueueError, "do not exist"):
            self.queue.cancel_jobs(
                (999,),
                reason="unknown",
                cancelled_at=self.now,
            )

    def test_future_job_is_not_claimed_early(self) -> None:
        self.queue.enqueue(
            "oc_a",
            "om_a1",
            available_at=self.now + timedelta(seconds=5),
        )

        self.assertIsNone(self.queue.claim_next("worker-1", now=self.now))
        self.assertIsNotNone(
            self.queue.claim_next(
                "worker-1", now=self.now + timedelta(seconds=5)
            )
        )

    def test_success_completes_job_and_records_auditable_run(self) -> None:
        job = self.queue.enqueue(
            "oc_a", "om_a2", available_at=self.now
        )
        lease = self.queue.claim_next("worker-1", now=self.now)
        run_id = self.queue.start_run(
            lease,
            provider="openai_compatible",
            model="qwen-test",
            response_format="json_schema",
            context_version="1.0",
            context_message_ids=("om_a1", "om_a2"),
            started_at=self.now + timedelta(seconds=1),
        )
        result = {
            "is_task": False,
            "confidence": 0.9,
            "owner": None,
            "title": None,
            "description": None,
            "deadline": None,
            "evidence_message_ids": [],
        }

        self.queue.complete(
            lease,
            run_id,
            result=result,
            response_format="json_schema",
            request_id="req_test",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
            finished_at=self.now + timedelta(seconds=3),
        )
        self._restart_runtime()

        snapshot = self.queue.get_job(job.job_id)
        self.assertEqual(snapshot.status, DetectionJobStatus.COMPLETED)
        self.assertIsNotNone(snapshot.completed_at)
        self.assertIsNone(snapshot.worker_id)
        self.assertIsNone(self.queue.claim_next("worker-2", now=self.now))
        runs = self.queue.list_runs(job.job_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, DetectionRunStatus.SUCCEEDED)
        self.assertEqual(runs[0].result, result)
        self.assertEqual(runs[0].total_tokens, 120)
        self.assertEqual(runs[0].latency_ms, 2_000)
        self.assertEqual(len(runs[0].context_fingerprint), 64)
        self.assertEqual(
            runs[0].context_message_ids, ("om_a1", "om_a2")
        )
        self.assertEqual(
            runs[0].focus_message_ids, ("om_a1", "om_a2")
        )

    def test_run_records_an_explicit_focus_subset(self) -> None:
        job = self.queue.enqueue("oc_a", "om_a2", available_at=self.now)
        lease = self.queue.claim_next("worker-focus", now=self.now)

        run_id = self.queue.start_run(
            lease,
            provider="openai_compatible",
            model="qwen-test",
            response_format="json_schema",
            context_version="1.1",
            context_message_ids=("om_a1", "om_a2"),
            focus_message_ids=("om_a2",),
            started_at=self.now + timedelta(seconds=1),
        )

        run = self.queue.list_runs(job.job_id)[0]
        self.assertEqual(run.run_id, run_id)
        self.assertEqual(run.focus_message_ids, ("om_a2",))

    def test_run_rejects_focus_outside_context(self) -> None:
        self.queue.enqueue("oc_a", "om_a2", available_at=self.now)
        lease = self.queue.claim_next("worker-focus", now=self.now)

        with self.assertRaisesRegex(DetectionQueueError, "contained"):
            self.queue.start_run(
                lease,
                provider="openai_compatible",
                model="qwen-test",
                response_format="json_schema",
                context_version="1.1",
                context_message_ids=("om_a1", "om_a2"),
                focus_message_ids=("om_b1",),
                started_at=self.now + timedelta(seconds=1),
            )

    def test_failure_retries_with_delay_then_becomes_dead(self) -> None:
        job = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now, max_attempts=2
        )
        first_lease = self.queue.claim_next("worker-1", now=self.now)
        first_run = self._start_run(first_lease, self.now)
        failure = self.queue.fail(
            first_lease,
            first_run,
            error_code="provider_timeout",
            error_message="model request timed out",
            failed_at=self.now + timedelta(seconds=1),
            retry_delay=timedelta(seconds=10),
        )

        self.assertEqual(failure.job_status, DetectionJobStatus.QUEUED)
        self.assertIsNone(
            self.queue.claim_next(
                "worker-2", now=self.now + timedelta(seconds=10)
            )
        )
        second_lease = self.queue.claim_next(
            "worker-2", now=self.now + timedelta(seconds=11)
        )
        self.assertEqual(second_lease.attempt, 2)
        second_run = self._start_run(
            second_lease, self.now + timedelta(seconds=11)
        )
        exhausted = self.queue.fail(
            second_lease,
            second_run,
            error_code="invalid_output",
            error_message="contract validation failed",
            failed_at=self.now + timedelta(seconds=12),
            retry_delay=timedelta(seconds=10),
        )
        self._restart_runtime()

        self.assertEqual(exhausted.job_status, DetectionJobStatus.DEAD)
        self.assertEqual(
            self.queue.get_job(job.job_id).last_error_code,
            "invalid_output",
        )
        self.assertIsNone(
            self.queue.claim_next(
                "worker-3", now=self.now + timedelta(minutes=1)
            )
        )
        self.assertEqual(
            [run.status for run in self.queue.list_runs(job.job_id)],
            [DetectionRunStatus.FAILED, DetectionRunStatus.FAILED],
        )

    def test_expired_lease_is_recovered_for_another_worker(self) -> None:
        job = self.queue.enqueue(
            "oc_a", "om_a1", available_at=self.now, max_attempts=3
        )
        first_lease = self.queue.claim_next(
            "worker-1", now=self.now, lease_seconds=10
        )
        self._start_run(first_lease, self.now)

        second_lease = self.queue.claim_next(
            "worker-2",
            now=self.now + timedelta(seconds=11),
            lease_seconds=10,
        )

        self.assertEqual(second_lease.job_id, job.job_id)
        self.assertEqual(second_lease.attempt, 2)
        old_run = self.queue.list_runs(job.job_id)[0]
        self.assertEqual(old_run.status, DetectionRunStatus.FAILED)
        self.assertEqual(old_run.error_code, "worker_lease_expired")
        with self.assertRaises(DetectionLeaseError):
            self.queue.heartbeat(
                first_lease,
                now=self.now + timedelta(seconds=11),
                lease_seconds=10,
            )

    def test_context_messages_must_all_belong_to_job_chat(self) -> None:
        self.queue.enqueue("oc_a", "om_a2", available_at=self.now)
        lease = self.queue.claim_next("worker-1", now=self.now)

        with self.assertRaisesRegex(DetectionQueueError, "outside"):
            self.queue.start_run(
                lease,
                provider="openai_compatible",
                model="qwen-test",
                response_format="json_schema",
                context_version="1.0",
                context_message_ids=("om_b1", "om_a2"),
                started_at=self.now,
            )

    def test_only_one_concurrent_worker_claims_single_job(self) -> None:
        self.queue.enqueue("oc_a", "om_a1", available_at=self.now)
        barrier = threading.Barrier(2)

        def claim(worker_id: str):
            barrier.wait()
            return self.queue.claim_next(worker_id, now=self.now)

        with ThreadPoolExecutor(max_workers=2) as executor:
            leases = list(
                executor.map(claim, ("worker-1", "worker-2"))
            )

        claimed = [lease for lease in leases if lease is not None]
        self.assertEqual(len(claimed), 1)

    def test_naive_schedule_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(DetectionQueueError, "timezone"):
            self.queue.enqueue(
                "oc_a",
                "om_a1",
                available_at=datetime(2026, 8, 22, 11, 0),
            )

    def _start_run(self, lease, started_at: datetime) -> int:
        return self.queue.start_run(
            lease,
            provider="openai_compatible",
            model="qwen-test",
            response_format="json_schema",
            context_version="1.0",
            context_message_ids=(lease.trigger_message_id,),
            started_at=started_at,
        )

    def _restart_runtime(self) -> None:
        self.runtime_manager.__exit__(None, None, None)
        self.runtime_manager = open_database_runtime(self.settings)
        self.runtime = self.runtime_manager.__enter__()
        self.queue = self.runtime.detection_queue

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
            received_at=self.now,
            enqueue_detection=False,
        )


if __name__ == "__main__":
    unittest.main()
