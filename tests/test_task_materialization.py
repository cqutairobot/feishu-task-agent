"""Phase 4A transactional task materialization tests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DatabaseSettings, TaskSettings
from app.database.models import TaskNotification
from app.database.runtime import open_database_runtime
from app.notifications.repository import TaskNotificationKind
from app.tasks.repository import TaskMaterializationError, TaskStatus
from tests.test_messages import TEXT_EVENT


class TaskMaterializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name) / "materialization.db"
        )
        self.runtime_manager = open_database_runtime(
            DatabaseSettings(
                url=f"sqlite:///{database_path}", echo=False
            ),
            task_settings=TaskSettings(auto_todo_confidence=0.85),
        )
        self.runtime = self.runtime_manager.__enter__()
        self.now = datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)
        self.message_counter = 0
        self.run_counter = 0

        self._ingest("oc_a", "om_a1", "ou_teacher", "王政补 baseline。")
        self._ingest("oc_a", "om_a2", "ou_wang", "收到。")
        self._ingest("oc_a", "om_a3", "ou_li", "我整理实验记录。")
        self.runtime.aliases.bind("oc_a", "ou_teacher", "老师")
        self.runtime.aliases.bind("oc_a", "ou_wang", "王政")
        self.runtime.aliases.bind("oc_a", "ou_li", "李四")

    def tearDown(self) -> None:
        self.runtime_manager.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_materializes_multiple_candidates_with_thresholded_statuses(self) -> None:
        run_id = self._completed_run(
            "oc_a",
            "om_a3",
            ("om_a1", "om_a2", "om_a3"),
            [
                self._candidate(
                    confidence=0.96,
                    evidence=["om_a1", "om_a2"],
                ),
                self._candidate(
                    confidence=0.60,
                    owner_name="李四",
                    owner_open_id="ou_li",
                    title="整理实验记录",
                    evidence=["om_a1", "om_a3"],
                ),
            ],
        )

        result = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now + timedelta(minutes=1)
        )

        self.assertFalse(result.already_materialized)
        self.assertEqual(result.candidate_count, 2)
        self.assertEqual(result.created_task_count, 2)
        self.assertEqual(result.reused_task_count, 0)
        tasks = self.runtime.tasks.list_tasks("oc_a")
        self.assertEqual(
            [task.status for task in tasks],
            [TaskStatus.TODO, TaskStatus.PENDING],
        )
        self.assertEqual(
            self.runtime.tasks.evidence_message_ids(tasks[0].task_id),
            ("om_a1", "om_a2"),
        )
        self.assertEqual(
            self.runtime.tasks.source_candidates(tasks[0].task_id),
            ((run_id, 0),),
        )
        self.assertEqual(
            len(self.runtime.reminders.list_for_task(tasks[0].task_id)),
            4,
        )
        self.assertEqual(
            self.runtime.reminders.list_for_task(tasks[1].task_id),
            (),
        )
        with Session(self.runtime.engine) as session:
            notices = session.scalars(select(TaskNotification)).all()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].task_id, tasks[0].task_id)
        self.assertEqual(notices[0].recipient_open_id, "ou_wang")

    def test_materializes_one_shared_task_with_two_assignees(self) -> None:
        run_id = self._completed_run(
            "oc_a",
            "om_a3",
            ("om_a1", "om_a2", "om_a3"),
            [
                self._candidate(
                    title="共同完成前端页面",
                    evidence=["om_a1", "om_a3"],
                    assignment_mode="shared",
                    co_owners=[
                        {"name": "李四", "open_id": "ou_li"}
                    ],
                )
            ],
        )

        result = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now + timedelta(minutes=1)
        )

        self.assertEqual(result.created_task_count, 1)
        self.assertEqual(len(result.task_ids), 1)
        task = self.runtime.tasks.get_task(result.task_ids[0])
        self.assertEqual(
            [member.open_id for member in task.responsible_members],
            ["ou_wang", "ou_li"],
        )
        self.assertEqual(
            len(self.runtime.reminders.list_for_task(task.task_id)), 8
        )
        self.assertEqual(
            {
                reminder.recipient_open_id
                for reminder in self.runtime.reminders.list_for_task(
                    task.task_id
                )
            },
            {"ou_wang", "ou_li"},
        )
        with Session(self.runtime.engine) as session:
            notices = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task.task_id)
                .order_by(TaskNotification.recipient_open_id)
            ).all()
        self.assertEqual(
            [notice.kind for notice in notices],
            [
                TaskNotificationKind.TASK_CREATED_ASSIGNEE.value,
                TaskNotificationKind.TASK_CREATED_ASSIGNEE.value,
            ],
        )
        self.assertEqual(
            {notice.recipient_open_id for notice in notices},
            {"ou_wang", "ou_li"},
        )
        self.assertTrue(all(notice.scheduled_for == self.now + timedelta(minutes=1) for notice in notices))

    def test_replaying_same_run_is_idempotent(self) -> None:
        run_id = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(evidence=["om_a1", "om_a2"])],
        )
        first = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now + timedelta(minutes=1)
        )

        replay = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now + timedelta(days=1)
        )

        self.assertTrue(replay.already_materialized)
        self.assertEqual(replay.task_ids, first.task_ids)
        self.assertEqual(replay.materialized_at, first.materialized_at)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 1)
        self.assertEqual(
            self.runtime.tasks.source_candidates(first.task_ids[0]),
            ((run_id, 0),),
        )
        with Session(self.runtime.engine) as session:
            notices = session.scalars(select(TaskNotification)).all()
        self.assertEqual(len(notices), 1)

    def test_zero_candidate_run_is_audited_and_replay_safe(self) -> None:
        run_id = self._completed_run(
            "oc_a", "om_a2", ("om_a1", "om_a2"), []
        )

        first = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now
        )
        replay = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now + timedelta(hours=1)
        )

        self.assertEqual(first.candidate_count, 0)
        self.assertEqual(first.task_ids, ())
        self.assertTrue(replay.already_materialized)
        self.assertEqual(self.runtime.tasks.list_tasks("oc_a"), ())

    def test_cross_run_candidate_reuses_only_with_shared_evidence(self) -> None:
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(evidence=["om_a1"])],
        )
        first = self.runtime.tasks.materialize_run(
            first_run, materialized_at=self.now
        )
        self._ingest("oc_a", "om_a4", "ou_teacher", "补充一下截图。")
        second_run = self._completed_run(
            "oc_a",
            "om_a4",
            ("om_a1", "om_a2", "om_a4"),
            [
                self._candidate(
                    confidence=0.99,
                    title="  补充   BASELINE 实验 ",
                    evidence=["om_a1", "om_a4"],
                )
            ],
        )

        second = self.runtime.tasks.materialize_run(
            second_run, materialized_at=self.now + timedelta(minutes=2)
        )

        self.assertEqual(second.created_task_count, 0)
        self.assertEqual(second.reused_task_count, 1)
        self.assertEqual(second.task_ids, first.task_ids)
        task_id = first.task_ids[0]
        self.assertEqual(
            self.runtime.tasks.evidence_message_ids(task_id),
            ("om_a1", "om_a4"),
        )
        self.assertEqual(
            self.runtime.tasks.source_candidates(task_id),
            ((first_run, 0), (second_run, 0)),
        )
        self.assertEqual(
            self.runtime.tasks.get_task(task_id).confidence, 0.99
        )

    def test_same_recurring_title_without_shared_evidence_stays_separate(self) -> None:
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(deadline=None, evidence=["om_a1"])],
        )
        self.runtime.tasks.materialize_run(first_run, materialized_at=self.now)
        self._ingest("oc_a", "om_a4", "ou_teacher", "再跑一次 baseline。")
        second_run = self._completed_run(
            "oc_a",
            "om_a4",
            ("om_a4",),
            [self._candidate(deadline=None, evidence=["om_a4"])],
        )

        result = self.runtime.tasks.materialize_run(
            second_run, materialized_at=self.now + timedelta(minutes=2)
        )

        self.assertEqual(result.created_task_count, 1)
        self.assertEqual(result.reused_task_count, 0)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 2)

    def test_shared_evidence_with_a_different_deadline_stays_separate(self) -> None:
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(evidence=["om_a1"])],
        )
        self.runtime.tasks.materialize_run(first_run, materialized_at=self.now)
        self._ingest("oc_a", "om_a4", "ou_teacher", "截止时间改成另一天。")
        second_run = self._completed_run(
            "oc_a",
            "om_a4",
            ("om_a1", "om_a4"),
            [
                self._candidate(
                    deadline="2026-08-28T18:00:00+08:00",
                    evidence=["om_a1", "om_a4"],
                )
            ],
        )

        result = self.runtime.tasks.materialize_run(
            second_run, materialized_at=self.now
        )

        self.assertEqual(result.created_task_count, 1)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 2)

    def test_stronger_repeated_detection_promotes_pending_to_todo(self) -> None:
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(confidence=0.60, evidence=["om_a1"])],
        )
        first = self.runtime.tasks.materialize_run(
            first_run, materialized_at=self.now
        )
        self.assertEqual(
            self.runtime.tasks.get_task(first.task_ids[0]).status,
            TaskStatus.PENDING,
        )
        self._ingest("oc_a", "om_a4", "ou_wang", "确认收到。")
        second_run = self._completed_run(
            "oc_a",
            "om_a4",
            ("om_a1", "om_a4"),
            [self._candidate(confidence=0.95, evidence=["om_a1", "om_a4"])],
        )

        self.runtime.tasks.materialize_run(
            second_run, materialized_at=self.now + timedelta(minutes=1)
        )

        promoted = self.runtime.tasks.get_task(first.task_ids[0])
        self.assertEqual(promoted.status, TaskStatus.TODO)
        self.assertEqual(promoted.confidence, 0.95)
        with Session(self.runtime.engine) as session:
            notices = session.scalars(select(TaskNotification)).all()
        self.assertEqual(len(notices), 1)
        self.assertEqual(
            notices[0].kind,
            TaskNotificationKind.TASK_CREATED_ASSIGNEE.value,
        )
        self.assertEqual(notices[0].dedupe_key, "assignment:activated")

    def test_identical_semantics_never_merge_across_chats(self) -> None:
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(evidence=["om_a1"])],
        )
        self.runtime.tasks.materialize_run(first_run, materialized_at=self.now)
        self._ingest("oc_b", "om_b1", "ou_wang", "收到任务。")
        self.runtime.aliases.bind("oc_b", "ou_wang", "王政")
        second_run = self._completed_run(
            "oc_b",
            "om_b1",
            ("om_b1",),
            [self._candidate(evidence=["om_b1"])],
        )

        result = self.runtime.tasks.materialize_run(
            second_run, materialized_at=self.now
        )

        self.assertEqual(result.created_task_count, 1)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 1)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_b")), 1)

    def test_historical_owner_name_survives_later_alias_rename(self) -> None:
        run_id = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [self._candidate(evidence=["om_a1"])],
        )
        self.runtime.aliases.bind("oc_a", "ou_wang", "王哈")

        result = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now
        )

        task = self.runtime.tasks.get_task(result.task_ids[0])
        self.assertEqual(task.owner_name, "王政")
        self.assertEqual(task.owner_open_id, "ou_wang")

    def test_invalid_evidence_rolls_back_every_candidate(self) -> None:
        run_id = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [
                self._candidate(evidence=["om_a1"]),
                self._candidate(
                    owner_name="李四",
                    owner_open_id="ou_li",
                    title="整理实验记录",
                    evidence=["om_a3"],
                ),
            ],
        )

        with self.assertRaisesRegex(
            TaskMaterializationError, "outside the current context"
        ):
            self.runtime.tasks.materialize_run(
                run_id, materialized_at=self.now
            )

        self.assertEqual(self.runtime.tasks.list_tasks("oc_a"), ())
        counts = self.runtime.repository.counts()
        self.assertEqual(counts.task_materializations, 0)
        self.assertEqual(counts.task_reminders, 0)

    def test_rejects_candidate_owner_never_observed_in_job_chat(self) -> None:
        self._ingest("oc_b", "om_b1", "ou_other", "另一个群。")
        run_id = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [
                self._candidate(
                    owner_name="其他人",
                    owner_open_id="ou_other",
                    evidence=["om_a1"],
                )
            ],
        )

        with self.assertRaisesRegex(
            TaskMaterializationError, "never observed in job chat"
        ):
            self.runtime.tasks.materialize_run(
                run_id, materialized_at=self.now
            )

        self.assertEqual(self.runtime.tasks.list_tasks("oc_a"), ())

    def test_rejects_failed_run(self) -> None:
        run_id, lease = self._started_run(
            "oc_a", "om_a2", ("om_a1", "om_a2")
        )
        self.runtime.detection_queue.fail(
            lease,
            run_id,
            error_code="model_provider_error",
            error_message="test failure",
            failed_at=self.now + timedelta(seconds=1),
            retry_delay=timedelta(seconds=5),
        )

        with self.assertRaisesRegex(
            TaskMaterializationError, "only succeeded"
        ):
            self.runtime.tasks.materialize_run(
                run_id, materialized_at=self.now
            )

    def _completed_run(
        self,
        chat_id: str,
        trigger_message_id: str,
        context_message_ids: tuple[str, ...],
        candidates: list[dict],
    ) -> int:
        run_id, lease = self._started_run(
            chat_id, trigger_message_id, context_message_ids
        )
        self.runtime.detection_queue.complete(
            lease,
            run_id,
            result={"candidates": candidates},
            response_format="json_schema",
            request_id=f"req_{run_id}",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
            },
            finished_at=self.now + timedelta(seconds=1),
        )
        return run_id

    def _started_run(
        self,
        chat_id: str,
        trigger_message_id: str,
        context_message_ids: tuple[str, ...],
    ):
        self.run_counter += 1
        job = self.runtime.detection_queue.enqueue(
            chat_id,
            trigger_message_id,
            available_at=self.now,
        )
        lease = self.runtime.detection_queue.claim_job(
            job.job_id,
            f"worker-{self.run_counter}",
            now=self.now,
        )
        self.assertIsNotNone(lease)
        run_id = self.runtime.detection_queue.start_run(
            lease,
            provider="test",
            model="test-model",
            response_format="json_schema",
            context_version="1.0",
            context_message_ids=context_message_ids,
            started_at=self.now,
        )
        return run_id, lease

    @staticmethod
    def _candidate(
        *,
        confidence: float = 0.96,
        owner_name: str = "王政",
        owner_open_id: str = "ou_wang",
        title: str = "补充 baseline 实验",
        deadline: str | None = "2026-08-27T18:00:00+08:00",
        evidence: list[str],
        assignment_mode: str = "single",
        co_owners: list[dict[str, str]] | None = None,
    ) -> dict:
        return {
            "assignment_mode": assignment_mode,
            "confidence": confidence,
            "co_owners": [] if co_owners is None else co_owners,
            "owner": {"name": owner_name, "open_id": owner_open_id},
            "title": title,
            "description": f"完成{title.strip()}",
            "deadline": deadline,
            "evidence_message_ids": evidence,
        }

    def _ingest(
        self,
        chat_id: str,
        message_id: str,
        open_id: str,
        text: str,
    ) -> None:
        self.message_counter += 1
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = f"evt_{message_id}"
        payload["event"]["message"]["message_id"] = message_id
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["message"]["create_time"] = str(
            int(
                (self.now + timedelta(seconds=self.message_counter))
                .timestamp()
                * 1000
            )
        )
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["sender"]["sender_id"]["open_id"] = open_id
        payload["event"]["sender"]["sender_id"]["union_id"] = (
            f"on_{open_id}"
        )
        self.runtime.ingestion.process_payload(
            payload,
            received_at=self.now,
            enqueue_detection=False,
        )


if __name__ == "__main__":
    unittest.main()
