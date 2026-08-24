"""Phase 3C-2 atomic message ingestion and debounce tests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event

from app.database.repository import (
    DetectionEnqueueStatus,
    SaveStatus,
)
from app.config import DatabaseSettings
from app.database.runtime import open_database_runtime
from tests.test_messages import TEXT_EVENT


class AutomaticDetectionEnqueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "auto-queue.db"
        self.runtime_manager = open_database_runtime(
            DatabaseSettings(url=f"sqlite:///{database_path}", echo=False)
        )
        self.runtime = self.runtime_manager.__enter__()
        self.now = datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.runtime_manager.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_human_group_text_creates_detection_job_atomically(self) -> None:
        outcome = self._ingest("evt_1", "om_1", received_at=self.now)

        self.assertEqual(outcome.persistence.status, SaveStatus.INSERTED)
        self.assertEqual(
            outcome.persistence.detection.status,
            DetectionEnqueueStatus.CREATED,
        )
        job = self.runtime.detection_queue.get_job(
            outcome.persistence.detection.job_id
        )
        self.assertEqual(job.trigger_message_id, "om_1")
        self.assertEqual(job.available_at, self.now + timedelta(seconds=20))
        counts = self.runtime.repository.counts()
        self.assertEqual(counts.messages, 1)
        self.assertEqual(counts.detection_jobs, 1)

    def test_messages_within_sliding_window_coalesce_to_latest_trigger(self) -> None:
        first = self._ingest("evt_1", "om_1", received_at=self.now)
        second_time = self.now + timedelta(seconds=2)
        second = self._ingest(
            "evt_2", "om_2", received_at=second_time
        )

        self.assertEqual(
            second.persistence.detection.status,
            DetectionEnqueueStatus.COALESCED,
        )
        self.assertEqual(
            second.persistence.detection.job_id,
            first.persistence.detection.job_id,
        )
        job = self.runtime.detection_queue.get_job(
            first.persistence.detection.job_id
        )
        self.assertEqual(job.trigger_message_id, "om_2")
        self.assertEqual(job.available_at, second_time + timedelta(seconds=20))
        self.assertEqual(self.runtime.repository.counts().detection_jobs, 1)

    def test_message_after_window_creates_another_job(self) -> None:
        first = self._ingest("evt_1", "om_1", received_at=self.now)
        second = self._ingest(
            "evt_2",
            "om_2",
            received_at=self.now + timedelta(seconds=21),
        )

        self.assertEqual(
            first.persistence.detection.status,
            DetectionEnqueueStatus.CREATED,
        )
        self.assertEqual(
            second.persistence.detection.status,
            DetectionEnqueueStatus.CREATED,
        )
        self.assertNotEqual(
            first.persistence.detection.job_id,
            second.persistence.detection.job_id,
        )
        self.assertEqual(self.runtime.repository.counts().detection_jobs, 2)

    def test_out_of_order_arrival_does_not_move_trigger_backwards(self) -> None:
        newer_created_at = self.now + timedelta(seconds=10)
        first = self._ingest(
            "evt_1",
            "om_newer",
            received_at=self.now,
            created_at=newer_created_at,
        )
        second = self._ingest(
            "evt_2",
            "om_older",
            received_at=self.now + timedelta(seconds=2),
            created_at=self.now,
        )

        self.assertEqual(
            second.persistence.detection.status,
            DetectionEnqueueStatus.COALESCED,
        )
        job = self.runtime.detection_queue.get_job(
            first.persistence.detection.job_id
        )
        self.assertEqual(job.trigger_message_id, "om_newer")
        self.assertEqual(
            job.available_at, self.now + timedelta(seconds=22)
        )

    def test_duplicate_delivery_does_not_extend_debounce_window(self) -> None:
        payload = self._payload(
            "evt_1", "om_1", created_at=self.now, chat_id="oc_test"
        )
        first = self.runtime.ingestion.process_payload(
            payload, received_at=self.now
        )
        duplicate = self.runtime.ingestion.process_payload(
            payload, received_at=self.now + timedelta(seconds=4)
        )

        self.assertEqual(duplicate.persistence.status, SaveStatus.DUPLICATE)
        self.assertEqual(
            duplicate.persistence.detection.status,
            DetectionEnqueueStatus.SKIPPED,
        )
        job = self.runtime.detection_queue.get_job(
            first.persistence.detection.job_id
        )
        self.assertEqual(job.available_at, self.now + timedelta(seconds=20))

    def test_non_eligible_messages_are_stored_without_queue_job(self) -> None:
        private = self._payload(
            "evt_private",
            "om_private",
            created_at=self.now,
            chat_id="oc_private",
        )
        private["event"]["message"]["chat_type"] = "p2p"

        image = self._payload(
            "evt_image",
            "om_image",
            created_at=self.now,
            chat_id="oc_image",
        )
        image["event"]["message"]["message_type"] = "image"
        image["event"]["message"]["content"] = '{"image_key":"img_1"}'

        bot = self._payload(
            "evt_bot",
            "om_bot",
            created_at=self.now,
            chat_id="oc_bot",
        )
        bot["event"]["sender"]["sender_type"] = "bot"

        suppressed = self._payload(
            "evt_command",
            "om_command",
            created_at=self.now,
            chat_id="oc_command",
        )

        outcomes = [
            self.runtime.ingestion.process_payload(private, received_at=self.now),
            self.runtime.ingestion.process_payload(image, received_at=self.now),
            self.runtime.ingestion.process_payload(bot, received_at=self.now),
            self.runtime.ingestion.process_payload(
                suppressed,
                received_at=self.now,
                enqueue_detection=False,
            ),
        ]

        self.assertTrue(
            all(
                outcome.persistence.detection.status
                is DetectionEnqueueStatus.SKIPPED
                for outcome in outcomes
            )
        )
        counts = self.runtime.repository.counts()
        self.assertEqual(counts.messages, 4)
        self.assertEqual(counts.detection_jobs, 0)

    def test_queue_insert_failure_rolls_back_message_chat_and_user(self) -> None:
        def fail_queue_insert(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith(
                "INSERT INTO DETECTION_JOBS"
            ):
                raise RuntimeError("simulated queue insert failure")

        event.listen(
            self.runtime.engine, "before_cursor_execute", fail_queue_insert
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "queue insert failure"):
                self._ingest("evt_1", "om_1", received_at=self.now)
        finally:
            event.remove(
                self.runtime.engine,
                "before_cursor_execute",
                fail_queue_insert,
            )

        counts = self.runtime.repository.counts()
        self.assertEqual(counts.chats, 0)
        self.assertEqual(counts.users, 0)
        self.assertEqual(counts.messages, 0)
        self.assertEqual(counts.detection_jobs, 0)

    def _ingest(
        self,
        event_id: str,
        message_id: str,
        *,
        received_at: datetime,
        created_at: datetime | None = None,
        chat_id: str = "oc_test",
    ):
        payload = self._payload(
            event_id,
            message_id,
            created_at=created_at or received_at,
            chat_id=chat_id,
        )
        return self.runtime.ingestion.process_payload(
            payload, received_at=received_at
        )

    @staticmethod
    def _payload(
        event_id: str,
        message_id: str,
        *,
        created_at: datetime,
        chat_id: str,
    ) -> dict:
        payload = deepcopy(TEXT_EVENT)
        timestamp = str(round(created_at.timestamp() * 1_000))
        payload["header"]["event_id"] = event_id
        payload["header"]["create_time"] = timestamp
        payload["event"]["message"]["message_id"] = message_id
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["message"]["create_time"] = timestamp
        payload["event"]["message"]["content"] = json.dumps(
            {"text": message_id}, ensure_ascii=False
        )
        return payload


if __name__ == "__main__":
    unittest.main()
