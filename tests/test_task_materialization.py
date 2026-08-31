"""Phase 4A transactional task materialization tests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DatabaseSettings, TaskSettings
from app.database.models import (
    ChatMembership,
    Task,
    TaskCompletionSubmission,
    TaskLifecycleEvent,
    TaskNote,
    TaskNotification,
    TaskReminder,
)
from app.database.runtime import open_database_runtime
from app.lifecycle.contracts import LifecycleAction, LifecycleCandidate
from app.notifications.repository import TaskNotificationKind
from app.tasks.codes import format_task_code
from app.tasks.notes import TaskNoteType, build_task_note_idempotency_key
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
            lifecycle_administrator_open_ids=frozenset({"ou_teacher"}),
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
            self.runtime.tasks.source_candidates(tasks[1].task_id),
            ((run_id, 1),),
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

    def test_materializes_detected_task_publisher_provenance(self) -> None:
        run_id = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [
                self._candidate(
                    evidence=["om_a1"],
                    publisher_name="老师",
                    publisher_open_id="ou_teacher",
                    publisher_attribution_basis="message_sender",
                    publisher_attribution_confidence=0.97,
                )
            ],
        )

        result = self.runtime.tasks.materialize_run(
            run_id, materialized_at=self.now + timedelta(minutes=1)
        )

        task_id = result.task_ids[0]
        with Session(self.runtime.engine) as session:
            task = session.get(Task, task_id)
            assert task is not None
            self.assertEqual(task.created_by_open_id, "ou_teacher")
            self.assertEqual(task.created_by_name, "老师")
            self.assertEqual(task.created_via, "detected")
            self.assertEqual(
                task.creator_attribution_basis,
                "message_sender",
            )
            self.assertEqual(task.creator_attribution_confidence, 0.97)

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

    def test_later_deadline_refines_same_open_task_instead_of_duplicating(
        self,
    ) -> None:
        first_title = "补充ResNet50 baseline不同随机种子实验结果"
        refined_title = "补充ResNet50不同随机种子实验结果并上传至实验服务器"
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [
                self._candidate(
                    confidence=0.90,
                    title=first_title,
                    deadline=None,
                    evidence=["om_a2"],
                )
            ],
        )
        first = self.runtime.tasks.materialize_run(
            first_run,
            materialized_at=self.now,
        )
        self._ingest(
            "oc_a",
            "om_a4",
            "ou_teacher",
            "好，那明天下午6点前跑完，把均值、方差和日志路径上传。",
        )
        second_run = self._completed_run(
            "oc_a",
            "om_a4",
            ("om_a1", "om_a2", "om_a4"),
            [
                self._candidate(
                    confidence=0.95,
                    title=refined_title,
                    deadline="2026-08-23T18:00:00+08:00",
                    evidence=["om_a2", "om_a4"],
                    publisher_name="老师",
                    publisher_open_id="ou_teacher",
                    publisher_attribution_basis="message_sender",
                    publisher_attribution_confidence=0.94,
                )
            ],
        )

        second = self.runtime.tasks.materialize_run(
            second_run,
            materialized_at=self.now + timedelta(minutes=2),
        )

        self.assertEqual(second.created_task_count, 0)
        self.assertEqual(second.reused_task_count, 1)
        self.assertEqual(second.task_ids, first.task_ids)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 1)
        task_id = first.task_ids[0]
        task = self.runtime.tasks.get_task(task_id)
        self.assertEqual(task.title, refined_title)
        self.assertEqual(
            task.deadline,
            datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(task.confidence, 0.95)
        with Session(self.runtime.engine) as session:
            stored_task = session.get(Task, task_id)
            assert stored_task is not None
            self.assertEqual(stored_task.created_by_open_id, "ou_teacher")
            self.assertEqual(stored_task.created_by_name, "老师")
            self.assertEqual(
                stored_task.creator_attribution_basis,
                "message_sender",
            )
            self.assertEqual(
                stored_task.creator_attribution_confidence,
                0.94,
            )
        self.assertEqual(
            self.runtime.tasks.evidence_message_ids(task_id),
            ("om_a2", "om_a4"),
        )
        self.assertEqual(
            self.runtime.tasks.source_candidates(task_id),
            ((first_run, 0), (second_run, 0)),
        )
        self.assertEqual(
            len(self.runtime.reminders.list_for_task(task_id)),
            4,
        )
        with Session(self.runtime.engine) as session:
            notices = session.scalars(select(TaskNotification)).all()
        self.assertEqual(len(notices), 1)

    def test_later_deadline_does_not_merge_a_different_task(self) -> None:
        first_run = self._completed_run(
            "oc_a",
            "om_a2",
            ("om_a1", "om_a2"),
            [
                self._candidate(
                    title="完成登录页适配",
                    deadline=None,
                    evidence=["om_a2"],
                )
            ],
        )
        self.runtime.tasks.materialize_run(
            first_run,
            materialized_at=self.now,
        )
        self._ingest(
            "oc_a",
            "om_a4",
            "ou_teacher",
            "另外把登录接口适配也在明天下午6点前完成。",
        )
        second_run = self._completed_run(
            "oc_a",
            "om_a4",
            ("om_a1", "om_a2", "om_a4"),
            [
                self._candidate(
                    title="完成登录接口适配",
                    deadline="2026-08-23T18:00:00+08:00",
                    evidence=["om_a2", "om_a4"],
                )
            ],
        )

        second = self.runtime.tasks.materialize_run(
            second_run,
            materialized_at=self.now + timedelta(minutes=2),
        )

        self.assertEqual(second.created_task_count, 1)
        self.assertEqual(second.reused_task_count, 0)
        self.assertEqual(len(self.runtime.tasks.list_tasks("oc_a")), 2)

    def test_phase_9g_combines_meeting_batch_notes_review_and_card_actions(
        self,
    ) -> None:
        """Keep one realistic multi-task workflow coherent across subsystems."""

        self._ingest(
            "oc_a",
            "om_a4",
            "ou_teacher",
            (
                "会议纪要：王政和李四共同补齐答辩演示方案，要求包含失败场景、"
                "回滚步骤和现场分工表，9月3日18:00前提交。"
            ),
        )
        self._ingest(
            "oc_a",
            "om_a5",
            "ou_teacher",
            "李四另外整理实验服务器账号清单，截止时间稍后再定。",
        )
        self._ingest(
            "oc_a",
            "om_a6",
            "ou_teacher",
            "王政在9月4日12:00前完成答辩录屏并上传共享目录。",
        )
        run_id = self._completed_run(
            "oc_a",
            "om_a6",
            ("om_a4", "om_a5", "om_a6"),
            [
                self._candidate(
                    title="补齐答辩演示方案并提交现场分工表",
                    deadline="2026-09-03T18:00:00+08:00",
                    evidence=["om_a4"],
                    assignment_mode="shared",
                    co_owners=[{"name": "李四", "open_id": "ou_li"}],
                    publisher_name="老师",
                    publisher_open_id="ou_teacher",
                    publisher_attribution_basis="message_sender",
                    publisher_attribution_confidence=0.98,
                ),
                self._candidate(
                    owner_name="李四",
                    owner_open_id="ou_li",
                    title="整理实验服务器账号清单",
                    deadline=None,
                    evidence=["om_a5"],
                    publisher_name="老师",
                    publisher_open_id="ou_teacher",
                ),
                self._candidate(
                    title="完成答辩录屏并上传共享目录",
                    deadline="2026-09-04T12:00:00+08:00",
                    evidence=["om_a6"],
                    publisher_name="老师",
                    publisher_open_id="ou_teacher",
                ),
            ],
        )
        materialized_at = self.now + timedelta(minutes=2)
        first = self.runtime.tasks.materialize_run(
            run_id,
            materialized_at=materialized_at,
        )
        replay = self.runtime.tasks.materialize_run(
            run_id,
            materialized_at=materialized_at + timedelta(minutes=1),
        )

        self.assertEqual(first.created_task_count, 3)
        self.assertTrue(replay.already_materialized)
        self.assertEqual(replay.task_ids, first.task_ids)
        shared_task_id, no_deadline_task_id, recording_task_id = first.task_ids
        shared = self.runtime.tasks.get_task(shared_task_id)
        no_deadline = self.runtime.tasks.get_task(no_deadline_task_id)
        self.assertEqual(
            [member.open_id for member in shared.responsible_members],
            ["ou_wang", "ou_li"],
        )
        self.assertIsNone(no_deadline.deadline)
        self.assertEqual(
            self.runtime.reminders.list_for_task(no_deadline_task_id),
            (),
        )
        self.assertEqual(
            len(self.runtime.reminders.list_for_task(recording_task_id)),
            4,
        )

        # Notes require current group membership even when their evidence is a
        # private-chat message. This mirrors the production authorization gate.
        with Session(self.runtime.engine) as session:
            for open_id, name in (
                ("ou_teacher", "老师"),
                ("ou_wang", "王政"),
                ("ou_li", "李四"),
            ):
                session.add(
                    ChatMembership(
                        chat_id="oc_a",
                        open_id=open_id,
                        display_name_snapshot=name,
                        active=True,
                        is_owner=open_id == "ou_teacher",
                        first_synced_at=materialized_at,
                        last_synced_at=materialized_at,
                    )
                )
            session.commit()

        self._ingest(
            "oc_li_dm",
            "om_progress",
            "ou_li",
            (
                f"{format_task_code(shared_task_id)} 进度："
                "失败场景已补齐，回滚步骤正在复核。"
            ),
            chat_type="p2p",
        )
        progress = self.runtime.task_notes.append(
            actor_open_id="ou_li",
            chat_id="oc_a",
            task_id=shared_task_id,
            note_type=TaskNoteType.PROGRESS,
            content="失败场景已补齐，回滚步骤正在复核。",
            source_message_id="om_progress",
            source_chat_id="oc_li_dm",
            idempotency_key=build_task_note_idempotency_key(
                "phase-9g", "progress"
            ),
            created_at=materialized_at + timedelta(minutes=2),
        )
        self.assertFalse(progress.already_created)
        self.assertEqual(progress.completion_cycle, 0)

        self._ingest(
            "oc_wang_dm",
            "om_complete_cycle_1",
            "ou_wang",
            (
                f"{format_task_code(shared_task_id)} 已完成，失败场景、"
                "回滚步骤和现场分工表已上传共享目录。"
            ),
            chat_type="p2p",
        )
        completed_once = self.runtime.lifecycle_mutations.apply_candidate(
            LifecycleCandidate(
                action=LifecycleAction.COMPLETE,
                confidence=0.99,
                task_id=shared_task_id,
                new_deadline=None,
                evidence_message_ids=("om_complete_cycle_1",),
            ),
            actor_open_id="ou_wang",
            trigger_message_id="om_complete_cycle_1",
            task_code=format_task_code(shared_task_id),
            applied_at=materialized_at + timedelta(minutes=4),
        )
        self.assertEqual(completed_once.new_status, TaskStatus.DONE)

        reopened = self.runtime.lifecycle_mutations.apply_management_action(
            LifecycleAction.REOPEN,
            actor_open_id="ou_teacher",
            request_id="phase-9g-reopen-cycle-1",
            chat_id="oc_a",
            task_id=shared_task_id,
            reason="现场分工表缺少故障切换负责人，请补齐后重新提交。",
            applied_at=materialized_at + timedelta(minutes=5),
        )
        self.assertEqual(reopened.new_status, TaskStatus.TODO)

        # The second completion deliberately comes from a signed task-card
        # callback and from the other co-assignee.
        completed_twice = self.runtime.lifecycle_mutations.apply_card_action(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_li",
            callback_id="phase-9g-card-complete-cycle-2",
            card_message_id="om_task_card_cycle_2",
            card_chat_id="oc_li_dm",
            task_code=format_task_code(shared_task_id),
            applied_at=materialized_at + timedelta(minutes=6),
        )
        self.assertEqual(completed_twice.new_status, TaskStatus.DONE)

        accepted = self.runtime.lifecycle_mutations.apply_management_action(
            LifecycleAction.ACCEPT,
            actor_open_id="ou_teacher",
            request_id="phase-9g-accept-cycle-2",
            chat_id="oc_a",
            task_id=shared_task_id,
            applied_at=materialized_at + timedelta(minutes=7),
        )
        self.assertEqual(accepted.new_status, TaskStatus.DONE)

        # A no-deadline task can coexist in the same batch and later gain a
        # deadline through a card action without affecting the reviewed task.
        self.runtime.notifications.sync_all(synced_at=materialized_at)
        rescheduled = self.runtime.lifecycle_mutations.apply_card_action(
            LifecycleAction.RESCHEDULE,
            actor_open_id="ou_li",
            callback_id="phase-9g-card-reschedule-no-deadline",
            card_message_id="om_no_deadline_card",
            card_chat_id="oc_li_dm",
            task_code=format_task_code(no_deadline_task_id),
            applied_at=materialized_at + timedelta(minutes=8),
            new_deadline=self.now + timedelta(days=14),
        )
        self.assertEqual(rescheduled.new_status, TaskStatus.TODO)
        self.runtime.notifications.sync_all(
            synced_at=materialized_at + timedelta(minutes=9)
        )

        with Session(self.runtime.engine) as session:
            stored_shared = session.get(Task, shared_task_id)
            assert stored_shared is not None
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id == shared_task_id)
                .order_by(TaskLifecycleEvent.id)
            ).all()
            submissions = session.scalars(
                select(TaskCompletionSubmission)
                .where(TaskCompletionSubmission.task_id == shared_task_id)
                .order_by(TaskCompletionSubmission.cycle)
            ).all()
            notes = session.scalars(
                select(TaskNote)
                .where(TaskNote.task_id == shared_task_id)
                .order_by(TaskNote.id)
            ).all()
            notification_rows = session.scalars(
                select(TaskNotification).where(
                    TaskNotification.task_id.in_(
                        (shared_task_id, no_deadline_task_id)
                    )
                )
            ).all()
            no_deadline_reminders = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id == no_deadline_task_id,
                    TaskReminder.status == "scheduled",
                )
            )

        self.assertEqual(stored_shared.status, "done")
        self.assertEqual(stored_shared.review_status, "accepted")
        self.assertEqual(stored_shared.completion_cycle, 2)
        self.assertEqual(
            [event.action for event in events],
            ["complete", "reopen", "complete", "accept"],
        )
        self.assertEqual(
            [event.trigger_source for event in events],
            ["message", "management_page", "card_action", "management_page"],
        )
        self.assertEqual(
            [(item.cycle, item.review_status) for item in submissions],
            [(1, "rework_required"), (2, "accepted")],
        )
        self.assertEqual(
            [note.note_type for note in notes],
            ["progress", "completion", "reopen"],
        )
        self.assertEqual(no_deadline_reminders, 4)
        kinds = {row.kind for row in notification_rows}
        self.assertIn(
            TaskNotificationKind.TASK_REOPENED_COASSIGNEE.value,
            kinds,
        )
        self.assertIn(
            TaskNotificationKind.TASK_ACCEPTED_COASSIGNEE.value,
            kinds,
        )
        self.assertEqual(
            len(notification_rows),
            len(
                {
                    (
                        row.task_id,
                        row.kind,
                        row.recipient_open_id,
                        row.dedupe_key,
                    )
                    for row in notification_rows
                }
            ),
        )

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
        publisher_name: str | None = None,
        publisher_open_id: str | None = None,
        publisher_attribution_basis: str | None = None,
        publisher_attribution_confidence: float | None = None,
    ) -> dict:
        candidate = {
            "assignment_mode": assignment_mode,
            "confidence": confidence,
            "co_owners": [] if co_owners is None else co_owners,
            "owner": {"name": owner_name, "open_id": owner_open_id},
            "title": title,
            "description": f"完成{title.strip()}",
            "deadline": deadline,
            "evidence_message_ids": evidence,
        }
        if publisher_name is not None or publisher_open_id is not None:
            if publisher_name is None or publisher_open_id is None:
                raise ValueError("publisher_name and publisher_open_id are paired")
            candidate.update(
                {
                    "publisher": {
                        "name": publisher_name,
                        "open_id": publisher_open_id,
                    },
                    "publisher_attribution_basis": (
                        "message_sender"
                        if publisher_attribution_basis is None
                        else publisher_attribution_basis
                    ),
                    "publisher_attribution_confidence": (
                        0.9
                        if publisher_attribution_confidence is None
                        else publisher_attribution_confidence
                    ),
                }
            )
        return candidate

    def _ingest(
        self,
        chat_id: str,
        message_id: str,
        open_id: str,
        text: str,
        *,
        chat_type: str = "group",
    ) -> None:
        self.message_counter += 1
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = f"evt_{message_id}"
        payload["event"]["message"]["message_id"] = message_id
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["message"]["chat_type"] = chat_type
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
