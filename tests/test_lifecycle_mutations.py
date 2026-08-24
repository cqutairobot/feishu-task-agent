"""Authorized and atomic Phase 6B task lifecycle mutation tests."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import upgrade_database
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatMemberAlias,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskEvidence,
    TaskLifecycleEvent,
    TaskLifecycleEvidence,
    TaskNotification,
    TaskReminder,
    User,
)
from app.agent.contracts import TaskOwner
from app.lifecycle.contracts import LifecycleAction, LifecycleCandidate
from app.lifecycle.mutations import (
    LifecycleAuthorizationRole,
    LifecycleModelAudit,
    LifecycleMutationError,
    LifecycleMutationService,
)
from app.notifications.repository import (
    create_task_assignment_notifications_in_session,
)
from app.reminders.repository import ReminderRepository, ReminderStatus
from app.tasks.codes import format_task_code
from app.tasks.repository import TaskStatus


SHANGHAI = ZoneInfo("Asia/Shanghai")


class LifecycleMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "lifecycle.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.reference_time = datetime(
            2026, 8, 23, 10, 0, tzinfo=SHANGHAI
        )
        self.applied_at = self.reference_time + timedelta(hours=1)
        self._seed()
        reminders = ReminderRepository(self.session_factory)
        for task_id in (1, 2, 3, 5):
            reminders.sync_task(task_id, synced_at=self.reference_time)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_owner_completion_is_audited_and_cancels_reminders(self) -> None:
        result = self._service().apply_candidate(
            self._candidate(1, LifecycleAction.COMPLETE, "om_owner_done"),
            actor_open_id="ou_owner",
            trigger_message_id="om_owner_done",
            task_code="1A",
            applied_at=self.applied_at,
            model_audit=LifecycleModelAudit(
                provider="openai_compatible",
                model="qwen-test",
                response_format="json_schema",
                request_id="req_done",
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
        )

        self.assertEqual(result.task_code, "T-1A")
        self.assertEqual(
            result.authorization_role, LifecycleAuthorizationRole.OWNER
        )
        self.assertEqual(result.new_status.value, "done")
        self.assertEqual(result.reminders_cancelled, 4)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            event = session.get(TaskLifecycleEvent, result.event_id)
            self.assertEqual(task.status, "done")
            self.assertEqual(task.completed_at, self.applied_at)
            self.assertEqual(event.task_code_snapshot, "T-1A")
            self.assertEqual(event.model, "qwen-test")
            self.assertEqual(event.model_request_id, "req_done")
            self.assertEqual(event.total_tokens, 120)
            self.assertEqual(
                [link.message.message_id for link in event.evidence_links],
                ["om_context", "om_owner_done"],
            )
            statuses = set(
                session.scalars(
                    select(TaskReminder.status).where(
                        TaskReminder.task_id == 1
                    )
                )
            )
            self.assertEqual(statuses, {ReminderStatus.CANCELLED.value})

    def test_overdue_task_can_be_rescheduled_and_replanned(self) -> None:
        new_deadline = datetime(2026, 9, 2, 18, 0, tzinfo=SHANGHAI)
        result = self._service().apply_candidate(
            self._candidate(
                2,
                LifecycleAction.RESCHEDULE,
                "om_owner_reschedule",
                deadline=new_deadline,
            ),
            actor_open_id="ou_owner",
            trigger_message_id="om_owner_reschedule",
            task_code=format_task_code(2),
            applied_at=self.applied_at,
        )

        self.assertEqual(result.previous_status.value, "overdue")
        self.assertEqual(result.new_status.value, "todo")
        self.assertEqual(result.reminders_created, 4)
        self.assertEqual(result.reminders_cancelled, 1)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 2)
            self.assertEqual(task.deadline, new_deadline)
            self.assertEqual(task.status, "todo")
            active = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id == 2,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            )
            self.assertEqual(active, 4)

    def test_management_reschedule_is_admin_scoped_audited_and_idempotent(
        self,
    ) -> None:
        new_deadline = datetime(2026, 9, 3, 18, 0, tzinfo=SHANGHAI)
        service = self._service(admins={"ou_admin"})

        result = service.apply_management_action(
            LifecycleAction.RESCHEDULE,
            actor_open_id="ou_admin",
            request_id="management-request-1",
            chat_id="oc_lab",
            task_id=1,
            new_deadline=new_deadline,
            applied_at=self.applied_at,
        )
        replay = service.apply_management_action(
            LifecycleAction.RESCHEDULE,
            actor_open_id="ou_admin",
            request_id="management-request-1",
            chat_id="oc_lab",
            task_id=1,
            new_deadline=new_deadline,
            applied_at=self.applied_at + timedelta(seconds=1),
        )

        self.assertEqual(
            result.authorization_role,
            LifecycleAuthorizationRole.ADMINISTRATOR,
        )
        self.assertFalse(result.already_applied)
        self.assertTrue(replay.already_applied)
        self.assertEqual(replay.event_id, result.event_id)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            event = session.get(TaskLifecycleEvent, result.event_id)
            self.assertEqual(task.deadline, new_deadline)
            self.assertEqual(event.trigger_source, "management_page")
            self.assertEqual(
                event.trigger_management_request_id,
                "management-request-1",
            )
            self.assertIsNone(event.trigger_message_db_id)
            self.assertIsNone(event.trigger_card_action_id)

        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            service.apply_management_action(
                LifecycleAction.RESCHEDULE,
                actor_open_id="ou_admin",
                request_id="management-request-1",
                chat_id="oc_lab",
                task_id=1,
                new_deadline=new_deadline + timedelta(days=1),
                applied_at=self.applied_at + timedelta(seconds=2),
            )
        with self.assertRaisesRegex(LifecycleMutationError, "administrator"):
            self._service().apply_management_action(
                LifecycleAction.RESCHEDULE,
                actor_open_id="ou_owner",
                request_id="management-request-owner",
                chat_id="oc_lab",
                task_id=1,
                new_deadline=new_deadline + timedelta(days=2),
                applied_at=self.applied_at + timedelta(seconds=3),
            )
        with self.assertRaisesRegex(LifecycleMutationError, "requested chat"):
            service.apply_management_action(
                LifecycleAction.RESCHEDULE,
                actor_open_id="ou_admin",
                request_id="management-request-cross-chat",
                chat_id="oc_other",
                task_id=1,
                new_deadline=new_deadline + timedelta(days=3),
                applied_at=self.applied_at + timedelta(seconds=4),
            )

    def test_management_restore_done_task_replans_and_is_idempotent(self) -> None:
        service = self._service(admins={"ou_admin"})
        service.apply_management_action(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_admin",
            request_id="management-complete-before-restore",
            chat_id="oc_lab",
            task_id=1,
            applied_at=self.applied_at,
        )
        restored = service.apply_management_action(
            LifecycleAction.RESTORE,
            actor_open_id="ou_admin",
            request_id="management-restore-done",
            chat_id="oc_lab",
            task_id=1,
            applied_at=self.applied_at + timedelta(minutes=1),
        )
        replay = service.apply_management_action(
            LifecycleAction.RESTORE,
            actor_open_id="ou_admin",
            request_id="management-restore-done",
            chat_id="oc_lab",
            task_id=1,
            applied_at=self.applied_at + timedelta(minutes=2),
        )

        self.assertEqual(restored.previous_status, TaskStatus.DONE)
        self.assertEqual(restored.new_status, TaskStatus.TODO)
        self.assertEqual(restored.reminders_created, 4)
        self.assertTrue(replay.already_applied)
        self.assertEqual(replay.event_id, restored.event_id)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            assert task is not None
            self.assertEqual(task.status, "todo")
            self.assertIsNone(task.completed_at)
            self.assertIsNone(task.cancelled_at)
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id == 1)
                .order_by(TaskLifecycleEvent.id)
            ).all()
            active_reminders = session.scalars(
                select(TaskReminder).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            ).all()
        self.assertEqual([event.action for event in events], ["complete", "restore"])
        self.assertEqual(len(active_reminders), 4)

        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            service.apply_management_action(
                LifecycleAction.COMPLETE,
                actor_open_id="ou_admin",
                request_id="management-restore-done",
                chat_id="oc_lab",
                task_id=1,
                applied_at=self.applied_at + timedelta(minutes=3),
            )

        with session_scope(self.session_factory) as session:
            cancelled_target = session.get(Task, 2)
            assert cancelled_target is not None
            cancelled_target.status = "cancelled"
            cancelled_target.cancelled_at = self.applied_at
        with self.assertRaisesRegex(LifecycleMutationError, "merge targets"):
            service.apply_management_action(
                LifecycleAction.MERGE,
                actor_open_id="ou_admin",
                request_id="management-merge-cancelled-target",
                chat_id="oc_lab",
                task_id=3,
                merge_target_task_id=2,
                applied_at=self.applied_at + timedelta(minutes=4),
            )

    def test_management_restore_cancelled_past_deadline_becomes_overdue(self) -> None:
        service = self._service(admins={"ou_admin"})
        service.apply_management_action(
            LifecycleAction.CANCEL,
            actor_open_id="ou_admin",
            request_id="management-cancel-before-restore",
            chat_id="oc_lab",
            task_id=2,
            applied_at=self.applied_at,
        )
        restored = service.apply_management_action(
            LifecycleAction.RESTORE,
            actor_open_id="ou_admin",
            request_id="management-restore-overdue",
            chat_id="oc_lab",
            task_id=2,
            applied_at=self.applied_at + timedelta(hours=1),
        )

        self.assertEqual(restored.previous_status, TaskStatus.CANCELLED)
        self.assertEqual(restored.new_status, TaskStatus.OVERDUE)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 2)
            assert task is not None
            self.assertEqual(task.status, "overdue")
            active = session.scalars(
                select(TaskReminder).where(
                    TaskReminder.task_id == 2,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            ).all()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].kind, "overdue")

    def test_management_restore_requires_terminal_task_and_admin(self) -> None:
        service = self._service(admins={"ou_admin"})
        with self.assertRaisesRegex(
            LifecycleMutationError, "only completed or cancelled"
        ):
            service.apply_management_action(
                LifecycleAction.RESTORE,
                actor_open_id="ou_admin",
                request_id="management-restore-open",
                chat_id="oc_lab",
                task_id=1,
                applied_at=self.applied_at,
            )
        with self.assertRaisesRegex(
            LifecycleMutationError, "authorized administrator"
        ):
            service.apply_management_action(
                LifecycleAction.RESTORE,
                actor_open_id="ou_owner",
                request_id="management-restore-owner",
                chat_id="oc_lab",
                task_id=1,
                applied_at=self.applied_at,
            )
        self._assert_event_count(0)

    def test_management_merge_preserves_target_and_moves_duplicate_evidence(self) -> None:
        with session_scope(self.session_factory) as session:
            message = session.scalar(
                select(Message).where(Message.message_id == "om_owner_done")
            )
            assert message is not None
            session.add(
                TaskEvidence(task_id=1, message_db_id=message.id)
            )
            source_task = session.get(Task, 1)
            assert source_task is not None
            create_task_assignment_notifications_in_session(
                session,
                source_task,
                scheduled_for=self.reference_time,
                max_attempts=3,
                reason="created",
            )
            other = Task(
                chat_id="oc_other",
                owner_open_id="ou_owner",
                owner_name_snapshot="王政",
                title="其他群重复任务",
                normalized_title="其他群重复任务",
                description="不应成为跨群合并目标",
                deadline=self.reference_time + timedelta(days=3),
                status="todo",
                confidence=0.95,
                created_at=self.reference_time,
                updated_at=self.reference_time,
            )
            session.add(other)
            session.flush()
            other_task_id = other.id

        service = self._service(admins={"ou_admin"})
        merged = service.apply_management_action(
            LifecycleAction.MERGE,
            actor_open_id="ou_admin",
            request_id="management-merge-task-1",
            chat_id="oc_lab",
            task_id=1,
            merge_target_task_id=3,
            applied_at=self.applied_at,
        )
        replay = service.apply_management_action(
            LifecycleAction.MERGE,
            actor_open_id="ou_admin",
            request_id="management-merge-task-1",
            chat_id="oc_lab",
            task_id=1,
            merge_target_task_id=3,
            applied_at=self.applied_at + timedelta(minutes=1),
        )

        self.assertEqual(merged.action, LifecycleAction.MERGE)
        self.assertEqual(merged.previous_status, TaskStatus.TODO)
        self.assertEqual(merged.new_status, TaskStatus.CANCELLED)
        self.assertEqual(merged.merge_target_task_id, 3)
        self.assertEqual(merged.reminders_cancelled, 4)
        self.assertTrue(replay.already_applied)
        with session_scope(self.session_factory) as session:
            source = session.get(Task, 1)
            target = session.get(Task, 3)
            event = session.scalar(
                select(TaskLifecycleEvent).where(
                    TaskLifecycleEvent.trigger_management_request_id
                    == "management-merge-task-1"
                )
            )
            evidence = session.scalars(
                select(TaskEvidence).where(TaskEvidence.task_id == 3)
            ).all()
            active_reminders = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            )
            assignment_notice = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == 1,
                    TaskNotification.kind == "task_created_assignee",
                )
            )
        assert source is not None
        assert target is not None
        assert event is not None
        self.assertEqual(source.status, "cancelled")
        self.assertEqual(source.merged_into_task_id, 3)
        self.assertEqual(source.merged_at, self.applied_at)
        self.assertEqual(target.status, "todo")
        self.assertEqual(event.action, "merge")
        self.assertEqual(event.merge_target_task_id, 3)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(active_reminders, 0)
        assert assignment_notice is not None
        self.assertEqual(assignment_notice.status, "cancelled")
        self.assertEqual(assignment_notice.cancel_reason, "task_merged")

        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            service.apply_management_action(
                LifecycleAction.MERGE,
                actor_open_id="ou_admin",
                request_id="management-merge-task-1",
                chat_id="oc_lab",
                task_id=1,
                merge_target_task_id=2,
                applied_at=self.applied_at + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(LifecycleMutationError, "requested chat"):
            service.apply_management_action(
                LifecycleAction.MERGE,
                actor_open_id="ou_admin",
                request_id="management-merge-cross-chat",
                chat_id="oc_lab",
                task_id=3,
                merge_target_task_id=other_task_id,
                applied_at=self.applied_at + timedelta(minutes=3),
            )

    def test_management_title_and_assignees_are_grounded_and_audited(
        self,
    ) -> None:
        service = self._service(admins={"ou_admin"})
        renamed = service.apply_management_action(
            LifecycleAction.RENAME,
            actor_open_id="ou_admin",
            request_id="management-title-request",
            chat_id="oc_lab",
            task_id=1,
            new_title="  后台纠错后的任务标题  ",
            applied_at=self.applied_at,
        )
        reassigned = service.apply_management_action(
            LifecycleAction.REASSIGN,
            actor_open_id="ou_admin",
            request_id="management-assignees-request",
            chat_id="oc_lab",
            task_id=1,
            new_owner_open_ids=("ou_owner", "ou_coowner"),
            applied_at=self.applied_at + timedelta(seconds=1),
        )
        replay = service.apply_management_action(
            LifecycleAction.REASSIGN,
            actor_open_id="ou_admin",
            request_id="management-assignees-request",
            chat_id="oc_lab",
            task_id=1,
            new_owner_open_ids=("ou_owner", "ou_coowner"),
            applied_at=self.applied_at + timedelta(seconds=2),
        )

        self.assertEqual(renamed.title_before, "任务一")
        self.assertEqual(renamed.title_after, "后台纠错后的任务标题")
        self.assertEqual(
            tuple(owner.open_id for owner in reassigned.assignees_before),
            ("ou_owner",),
        )
        self.assertEqual(
            tuple(owner.open_id for owner in reassigned.assignees_after),
            ("ou_owner", "ou_coowner"),
        )
        self.assertTrue(replay.already_applied)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(
                    TaskLifecycleEvent.trigger_source == "management_page"
                )
                .order_by(TaskLifecycleEvent.id)
            ).all()
            self.assertEqual(task.title, "后台纠错后的任务标题")
            self.assertEqual(
                [item.open_id for item in task.assignees],
                ["ou_owner", "ou_coowner"],
            )
            self.assertEqual([event.action for event in events], ["rename", "reassign"])

        with self.assertRaisesRegex(LifecycleMutationError, "active member"):
            service.apply_management_action(
                LifecycleAction.REASSIGN,
                actor_open_id="ou_admin",
                request_id="management-inactive-assignee",
                chat_id="oc_lab",
                task_id=1,
                new_owner_open_ids=("ou_intruder",),
                applied_at=self.applied_at + timedelta(seconds=3),
            )

    def test_management_terminal_actions_are_idempotent_and_cancel_reminders(
        self,
    ) -> None:
        service = self._service(admins={"ou_admin"})
        completed = service.apply_management_action(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_admin",
            request_id="management-complete-request",
            chat_id="oc_lab",
            task_id=1,
            applied_at=self.applied_at,
        )
        replay = service.apply_management_action(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_admin",
            request_id="management-complete-request",
            chat_id="oc_lab",
            task_id=1,
            applied_at=self.applied_at + timedelta(seconds=1),
        )
        cancelled = service.apply_management_action(
            LifecycleAction.CANCEL,
            actor_open_id="ou_admin",
            request_id="management-cancel-request",
            chat_id="oc_lab",
            task_id=2,
            applied_at=self.applied_at + timedelta(seconds=2),
        )
        invalidated = service.apply_management_action(
            LifecycleAction.INVALIDATE,
            actor_open_id="ou_admin",
            request_id="management-invalidate-request",
            chat_id="oc_lab",
            task_id=3,
            applied_at=self.applied_at + timedelta(seconds=3),
        )

        self.assertEqual(completed.new_status, TaskStatus.DONE)
        self.assertEqual(completed.reminders_cancelled, 4)
        self.assertTrue(replay.already_applied)
        self.assertEqual(replay.event_id, completed.event_id)
        self.assertEqual(cancelled.new_status, TaskStatus.CANCELLED)
        self.assertEqual(invalidated.new_status, TaskStatus.CANCELLED)
        with session_scope(self.session_factory) as session:
            tasks = [session.get(Task, task_id) for task_id in (1, 2, 3)]
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.trigger_source == "management_page")
                .order_by(TaskLifecycleEvent.id)
            ).all()
            active_reminders = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id.in_((1, 2, 3)),
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            )
        self.assertEqual([task.status for task in tasks], ["done", "cancelled", "cancelled"])
        self.assertEqual([event.action for event in events], ["complete", "cancel", "invalidate"])
        self.assertEqual(active_reminders, 0)

        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            service.apply_management_action(
                LifecycleAction.CANCEL,
                actor_open_id="ou_admin",
                request_id="management-complete-request",
                chat_id="oc_lab",
                task_id=1,
                applied_at=self.applied_at + timedelta(seconds=4),
            )
        with self.assertRaisesRegex(LifecycleMutationError, "accepts no"):
            service.apply_management_action(
                LifecycleAction.COMPLETE,
                actor_open_id="ou_admin",
                request_id="management-complete-extra-value",
                chat_id="oc_lab",
                task_id=5,
                new_title="不应接受",
                applied_at=self.applied_at + timedelta(seconds=5),
            )

    def test_management_pending_review_confirms_or_invalidates_atomically(
        self,
    ) -> None:
        service = self._service(admins={"ou_admin"})
        confirmed = service.apply_management_action(
            LifecycleAction.CONFIRM,
            actor_open_id="ou_admin",
            request_id="management-confirm-pending",
            chat_id="oc_lab",
            task_id=4,
            applied_at=self.applied_at,
        )
        replay = service.apply_management_action(
            LifecycleAction.CONFIRM,
            actor_open_id="ou_admin",
            request_id="management-confirm-pending",
            chat_id="oc_lab",
            task_id=4,
            applied_at=self.applied_at + timedelta(seconds=1),
        )
        invalidated = service.apply_management_action(
            LifecycleAction.INVALIDATE,
            actor_open_id="ou_admin",
            request_id="management-invalidate-pending",
            chat_id="oc_lab",
            task_id=6,
            applied_at=self.applied_at + timedelta(seconds=2),
        )

        self.assertEqual(confirmed.previous_status, TaskStatus.PENDING)
        self.assertEqual(confirmed.new_status, TaskStatus.TODO)
        self.assertTrue(replay.already_applied)
        self.assertEqual(invalidated.previous_status, TaskStatus.PENDING)
        self.assertEqual(invalidated.new_status, TaskStatus.CANCELLED)
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id.in_((4, 6)))
                .order_by(TaskLifecycleEvent.id)
            ).all()
            assignment_notices = session.scalars(
                select(TaskNotification).where(
                    TaskNotification.task_id == 4,
                    TaskNotification.kind == "task_created_assignee",
                )
            ).all()
            invalidated_notices = session.scalars(
                select(TaskNotification).where(TaskNotification.task_id == 6)
            ).all()
        self.assertEqual([event.action for event in events], ["confirm", "invalidate"])
        self.assertEqual(len(assignment_notices), 1)
        self.assertEqual(assignment_notices[0].dedupe_key, "assignment:activated")
        self.assertEqual(invalidated_notices, [])

        with self.assertRaisesRegex(LifecycleMutationError, "only pending"):
            service.apply_management_action(
                LifecycleAction.CONFIRM,
                actor_open_id="ou_admin",
                request_id="management-confirm-todo",
                chat_id="oc_lab",
                task_id=1,
                applied_at=self.applied_at + timedelta(seconds=3),
            )
        with self.assertRaisesRegex(LifecycleMutationError, "administrators"):
            service.apply_candidate(
                self._candidate(1, LifecycleAction.CONFIRM, "om_owner_done"),
                actor_open_id="ou_owner",
                trigger_message_id="om_owner_done",
                applied_at=self.applied_at + timedelta(seconds=4),
            )

    def test_administrator_can_cancel_another_members_group_task(self) -> None:
        result = self._service(admins={"ou_admin"}).apply_candidate(
            self._candidate(
                5,
                LifecycleAction.CANCEL,
                "om_admin_group",
                evidence=("om_admin_group",),
            ),
            actor_open_id="ou_admin",
            trigger_message_id="om_admin_group",
            applied_at=self.applied_at,
        )

        self.assertEqual(
            result.authorization_role,
            LifecycleAuthorizationRole.ADMINISTRATOR,
        )
        self.assertEqual(result.new_status.value, "cancelled")

    def test_persisted_group_administrator_can_cancel_without_global_allowlist(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add(
                ChatAdministrator(
                    chat_id="oc_lab",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="bootstrap",
                    created_at=self.reference_time,
                )
            )

        result = self._service().apply_candidate(
            self._candidate(
                5,
                LifecycleAction.CANCEL,
                "om_admin_group",
                evidence=("om_admin_group",),
            ),
            actor_open_id="ou_admin",
            trigger_message_id="om_admin_group",
            applied_at=self.applied_at,
        )

        self.assertEqual(
            result.authorization_role,
            LifecycleAuthorizationRole.ADMINISTRATOR,
        )

    def test_only_administrator_can_rename_and_audit_old_and_new_titles(self) -> None:
        candidate = self._candidate(
            1,
            LifecycleAction.RENAME,
            "om_admin_rename",
            evidence=("om_admin_rename",),
            new_title="纠正后的任务标题",
        )
        with self.assertRaisesRegex(LifecycleMutationError, "administrator"):
            self._service().apply_candidate(
                candidate,
                actor_open_id="ou_admin",
                trigger_message_id="om_admin_rename",
                applied_at=self.applied_at,
            )

        result = self._service(admins={"ou_admin"}).apply_candidate(
            candidate,
            actor_open_id="ou_admin",
            trigger_message_id="om_admin_rename",
            applied_at=self.applied_at,
        )
        self.assertEqual(result.title_before, "任务一")
        self.assertEqual(result.title_after, "纠正后的任务标题")
        self.assertEqual(result.new_status, TaskStatus.TODO)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            event = session.get(TaskLifecycleEvent, result.event_id)
            self.assertEqual(task.title, "纠正后的任务标题")
            self.assertEqual(task.normalized_title, "纠正后的任务标题")
            self.assertEqual(event.title_before, "任务一")
            self.assertEqual(event.title_after, "纠正后的任务标题")

    def test_administrator_reassignment_updates_visibility_and_reminders(self) -> None:
        result = self._service(admins={"ou_admin"}).apply_candidate(
            self._candidate(
                1,
                LifecycleAction.REASSIGN,
                "om_admin_reassign",
                evidence=("om_admin_reassign",),
                new_owners=(TaskOwner("李四", "ou_coowner"),),
            ),
            actor_open_id="ou_admin",
            trigger_message_id="om_admin_reassign",
            applied_at=self.applied_at,
        )

        self.assertEqual(
            tuple(owner.open_id for owner in result.assignees_before),
            ("ou_owner",),
        )
        self.assertEqual(
            tuple(owner.open_id for owner in result.assignees_after),
            ("ou_coowner",),
        )
        self.assertEqual(result.reminders_cancelled, 4)
        self.assertEqual(result.reminders_created, 4)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            self.assertEqual(task.owner_open_id, "ou_coowner")
            self.assertEqual(
                [(item.open_id, item.position) for item in task.assignees],
                [("ou_coowner", 0)],
            )
            active_recipients = set(
                session.scalars(
                    select(TaskReminder.recipient_open_id).where(
                        TaskReminder.task_id == 1,
                        TaskReminder.status == ReminderStatus.SCHEDULED.value,
                    )
                )
            )
            self.assertEqual(active_recipients, {"ou_coowner"})

        restored = self._service(admins={"ou_admin"}).apply_candidate(
            self._candidate(
                1,
                LifecycleAction.REASSIGN,
                "om_admin_reassign_back",
                evidence=("om_admin_reassign_back",),
                new_owners=(
                    TaskOwner("王政", "ou_owner"),
                    TaskOwner("李四", "ou_coowner"),
                ),
            ),
            actor_open_id="ou_admin",
            trigger_message_id="om_admin_reassign_back",
            applied_at=self.applied_at + timedelta(minutes=1),
        )
        self.assertEqual(restored.reminders_created, 4)
        self.assertEqual(restored.reminders_cancelled, 0)
        with session_scope(self.session_factory) as session:
            active = session.scalars(
                select(TaskReminder).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            ).all()
            self.assertEqual(len(active), 8)
            self.assertEqual(
                {item.recipient_open_id for item in active},
                {"ou_owner", "ou_coowner"},
            )

    def test_administrator_can_invalidate_false_positive_without_deleting_audit(self) -> None:
        result = self._service(admins={"ou_admin"}).apply_candidate(
            self._candidate(
                1,
                LifecycleAction.INVALIDATE,
                "om_admin_invalidate",
                evidence=("om_admin_invalidate",),
            ),
            actor_open_id="ou_admin",
            trigger_message_id="om_admin_invalidate",
            applied_at=self.applied_at,
        )

        self.assertEqual(result.new_status, TaskStatus.CANCELLED)
        self.assertEqual(result.reminders_cancelled, 4)
        with session_scope(self.session_factory) as session:
            self.assertIsNotNone(session.get(Task, 1))
            event = session.get(TaskLifecycleEvent, result.event_id)
            self.assertEqual(event.action, "invalidate")

    def test_private_owner_update_may_target_an_allowed_group(self) -> None:
        result = self._service(allowed_chats={"oc_lab"}).apply_candidate(
            self._candidate(3, LifecycleAction.CANCEL, "om_owner_cancel"),
            actor_open_id="ou_owner",
            trigger_message_id="om_owner_cancel",
            applied_at=self.applied_at,
        )

        self.assertEqual(result.task_id, 3)
        self.assertEqual(result.new_status.value, "cancelled")

    def test_private_owner_update_may_target_a_self_service_group(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add(
                ChatAdministrator(
                    chat_id="oc_lab",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="group_owner_init",
                    created_at=self.reference_time,
                )
            )

        result = self._service(allowed_chats={"oc_other"}).apply_candidate(
            self._candidate(3, LifecycleAction.CANCEL, "om_owner_cancel"),
            actor_open_id="ou_owner",
            trigger_message_id="om_owner_cancel",
            applied_at=self.applied_at,
        )

        self.assertEqual(result.task_id, 3)
        self.assertEqual(result.new_status.value, "cancelled")

    def test_unauthorized_or_cross_group_actor_cannot_mutate(self) -> None:
        cases = (
            (
                self._service(),
                "ou_intruder",
                "om_intruder_dm",
                ("om_context", "om_intruder_dm"),
                "neither the task owner",
            ),
            (
                self._service(admins={"ou_admin"}),
                "ou_admin",
                "om_admin_other_group",
                ("om_admin_other_group",),
                "cannot cross chats",
            ),
        )
        for service, actor, message_id, evidence, error in cases:
            with self.subTest(message_id=message_id):
                with self.assertRaisesRegex(LifecycleMutationError, error):
                    service.apply_candidate(
                        self._candidate(
                            1,
                            LifecycleAction.COMPLETE,
                            message_id,
                            evidence=evidence,
                        ),
                        actor_open_id=actor,
                        trigger_message_id=message_id,
                        applied_at=self.applied_at,
                    )
        self._assert_task_status(1, "todo")
        self._assert_event_count(0)

    def test_allowlist_code_confidence_and_state_are_hard_validated(self) -> None:
        cases = (
            (
                self._service(allowed_chats={"oc_other"}),
                self._candidate(1, LifecycleAction.COMPLETE, "om_owner_done"),
                "T-1A",
                "outside the configured allowlist",
            ),
            (
                self._service(),
                self._candidate(1, LifecycleAction.COMPLETE, "om_owner_done"),
                format_task_code(2),
                "does not match",
            ),
            (
                self._service(),
                self._candidate(
                    1,
                    LifecycleAction.COMPLETE,
                    "om_owner_done",
                    confidence=0.89,
                ),
                "T-1A",
                "below the mutation threshold",
            ),
            (
                self._service(),
                self._candidate(4, LifecycleAction.COMPLETE, "om_owner_done"),
                format_task_code(4),
                "not actionable",
            ),
        )
        for service, candidate, task_code, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(LifecycleMutationError, error):
                    service.apply_candidate(
                        candidate,
                        actor_open_id="ou_owner",
                        trigger_message_id="om_owner_done",
                        task_code=task_code,
                        applied_at=self.applied_at,
                    )
        self._assert_event_count(0)

    def test_evidence_must_exist_include_trigger_and_stay_in_chat(self) -> None:
        cases = (
            (("om_context",), "include the trigger"),
            (("om_owner_done", "om_missing"), "does not exist"),
            (
                ("om_owner_done", "om_other_context"),
                "crosses chat boundaries",
            ),
        )
        for evidence, error in cases:
            with self.subTest(evidence=evidence):
                candidate = LifecycleCandidate(
                    action=LifecycleAction.COMPLETE,
                    confidence=0.98,
                    task_id=1,
                    new_deadline=None,
                    evidence_message_ids=evidence,
                )
                with self.assertRaisesRegex(LifecycleMutationError, error):
                    self._service().apply_candidate(
                        candidate,
                        actor_open_id="ou_owner",
                        trigger_message_id="om_owner_done",
                        applied_at=self.applied_at,
                    )

    def test_exact_replay_is_idempotent_and_conflict_is_rejected(self) -> None:
        candidate = self._candidate(
            1, LifecycleAction.COMPLETE, "om_owner_done"
        )
        first = self._service().apply_candidate(
            candidate,
            actor_open_id="ou_owner",
            trigger_message_id="om_owner_done",
            applied_at=self.applied_at,
        )
        replay = self._service().apply_candidate(
            candidate,
            actor_open_id="ou_owner",
            trigger_message_id="om_owner_done",
            applied_at=self.applied_at + timedelta(minutes=1),
        )

        self.assertEqual(first.event_id, replay.event_id)
        self.assertTrue(replay.already_applied)
        self._assert_event_count(1)
        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            self._service().apply_candidate(
                self._candidate(1, LifecycleAction.CANCEL, "om_owner_done"),
                actor_open_id="ou_owner",
                trigger_message_id="om_owner_done",
                applied_at=self.applied_at + timedelta(minutes=2),
            )
        self._assert_task_status(1, "done")

    def test_reminder_failure_rolls_back_task_audit_and_evidence(self) -> None:
        with (
            patch(
                "app.lifecycle.mutations.sync_task_reminders_in_session",
                side_effect=RuntimeError("simulated reminder failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "simulated reminder failure"),
        ):
            self._service().apply_candidate(
                self._candidate(
                    1, LifecycleAction.COMPLETE, "om_owner_done"
                ),
                actor_open_id="ou_owner",
                trigger_message_id="om_owner_done",
                applied_at=self.applied_at,
            )

        self._assert_task_status(1, "todo")
        self._assert_event_count(0)
        with session_scope(self.session_factory) as session:
            evidence_count = session.scalar(
                select(func.count(TaskLifecycleEvidence.event_id))
            )
            active = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                )
            )
        self.assertEqual(evidence_count, 0)
        self.assertEqual(active, 4)

    def test_reschedule_must_still_be_future_at_commit_time(self) -> None:
        candidate = self._candidate(
            1,
            LifecycleAction.RESCHEDULE,
            "om_owner_reschedule",
            deadline=self.applied_at,
        )
        with self.assertRaisesRegex(LifecycleMutationError, "in the future"):
            self._service().apply_candidate(
                candidate,
                actor_open_id="ou_owner",
                trigger_message_id="om_owner_reschedule",
                applied_at=self.applied_at,
            )

    def test_owner_card_completion_is_audited_without_fake_evidence(self) -> None:
        result = self._service().apply_card_action(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_owner",
            callback_id="evt_card_done",
            card_message_id="om_card_list",
            card_chat_id="oc_dm",
            task_code="1A",
            applied_at=self.applied_at,
        )

        self.assertEqual(result.new_status, TaskStatus.DONE)
        self.assertEqual(result.reminders_cancelled, 4)
        with session_scope(self.session_factory) as session:
            event = session.get(TaskLifecycleEvent, result.event_id)
            self.assertEqual(event.trigger_source, "card_action")
            self.assertIsNone(event.trigger_message_db_id)
            self.assertEqual(event.trigger_card_action_id, "evt_card_done")
            self.assertEqual(event.trigger_card_message_id, "om_card_list")
            self.assertEqual(event.trigger_card_chat_id, "oc_dm")
            self.assertEqual(event.confidence, 1.0)
            self.assertIsNone(event.model)
            self.assertEqual(event.evidence_links, [])

    def test_shared_co_owner_can_complete_and_cancels_everyones_reminders(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    TaskAssignee(
                        task_id=3,
                        open_id="ou_owner",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.reference_time,
                    ),
                    TaskAssignee(
                        task_id=3,
                        open_id="ou_coowner",
                        name_snapshot="李四",
                        position=1,
                        created_at=self.reference_time,
                    ),
                )
            )
        ReminderRepository(self.session_factory).sync_task(
            3, synced_at=self.reference_time
        )

        result = self._service().apply_card_action(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_coowner",
            callback_id="evt_shared_done",
            card_message_id="om_shared_card",
            card_chat_id="oc_dm",
            task_code=format_task_code(3),
            applied_at=self.applied_at,
        )

        self.assertEqual(
            result.authorization_role, LifecycleAuthorizationRole.OWNER
        )
        self.assertEqual(result.reminders_cancelled, 8)
        self._assert_task_status(3, "done")

    def test_administrator_can_cancel_from_card(self) -> None:
        result = self._service(admins={"ou_admin"}).apply_card_action(
            LifecycleAction.CANCEL,
            actor_open_id="ou_admin",
            callback_id="evt_card_cancel",
            card_message_id="om_admin_card",
            card_chat_id="oc_admin_dm",
            task_code=format_task_code(5),
            applied_at=self.applied_at,
        )

        self.assertEqual(
            result.authorization_role,
            LifecycleAuthorizationRole.ADMINISTRATOR,
        )
        self.assertEqual(result.new_status, TaskStatus.CANCELLED)

    def test_card_action_rechecks_authorization_allowlist_and_code(self) -> None:
        cases = (
            (
                self._service(),
                "ou_intruder",
                "T-1A",
                "neither the task owner",
            ),
            (
                self._service(allowed_chats={"oc_other"}),
                "ou_owner",
                "T-1A",
                "outside the configured allowlist",
            ),
            (
                self._service(),
                "ou_owner",
                "T-1B",
                "checksum",
            ),
        )
        for index, (service, actor, code, error) in enumerate(cases):
            with self.subTest(error=error), self.assertRaisesRegex(
                LifecycleMutationError, error
            ):
                service.apply_card_action(
                    LifecycleAction.COMPLETE,
                    actor_open_id=actor,
                    callback_id=f"evt_rejected_{index}",
                    card_message_id="om_card",
                    card_chat_id="oc_dm",
                    task_code=code,
                    applied_at=self.applied_at,
                )
        self._assert_task_status(1, "todo")
        self._assert_event_count(0)

    def test_card_callback_replay_is_idempotent_and_globally_bound(self) -> None:
        kwargs = {
            "actor_open_id": "ou_owner",
            "callback_id": "evt_same_card_action",
            "card_message_id": "om_card",
            "card_chat_id": "oc_dm",
            "task_code": "1A",
        }
        first = self._service().apply_card_action(
            LifecycleAction.COMPLETE,
            applied_at=self.applied_at,
            **kwargs,
        )
        replay = self._service().apply_card_action(
            LifecycleAction.COMPLETE,
            applied_at=self.applied_at + timedelta(minutes=1),
            **kwargs,
        )

        self.assertEqual(first.event_id, replay.event_id)
        self.assertTrue(replay.already_applied)
        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            self._service().apply_card_action(
                LifecycleAction.COMPLETE,
                actor_open_id="ou_owner",
                callback_id="evt_same_card_action",
                card_message_id="om_card",
                card_chat_id="oc_dm",
                task_code=format_task_code(3),
                applied_at=self.applied_at + timedelta(minutes=2),
            )
        self._assert_task_status(3, "todo")
        self._assert_event_count(1)

    def test_card_reschedule_replay_is_bound_to_the_same_deadline(self) -> None:
        deadline = datetime(2026, 9, 5, 18, 30, tzinfo=SHANGHAI)
        kwargs = {
            "actor_open_id": "ou_owner",
            "callback_id": "evt_same_card_reschedule",
            "card_message_id": "om_card",
            "card_chat_id": "oc_dm",
            "task_code": "1A",
        }
        first = self._service().apply_card_action(
            LifecycleAction.RESCHEDULE,
            applied_at=self.applied_at,
            new_deadline=deadline,
            **kwargs,
        )
        replay = self._service().apply_card_action(
            LifecycleAction.RESCHEDULE,
            applied_at=self.applied_at + timedelta(minutes=1),
            new_deadline=deadline,
            **kwargs,
        )

        self.assertEqual(first.event_id, replay.event_id)
        self.assertTrue(replay.already_applied)
        with self.assertRaisesRegex(LifecycleMutationError, "different"):
            self._service().apply_card_action(
                LifecycleAction.RESCHEDULE,
                applied_at=self.applied_at + timedelta(minutes=2),
                new_deadline=deadline + timedelta(days=1),
                **kwargs,
            )
        self._assert_event_count(1)

    def test_owner_card_reschedule_is_audited_and_replans_reminders(self) -> None:
        new_deadline = datetime(2026, 9, 5, 18, 30, tzinfo=SHANGHAI)
        result = self._service().apply_card_action(
            LifecycleAction.RESCHEDULE,
            actor_open_id="ou_owner",
            callback_id="evt_card_reschedule",
            card_message_id="om_card",
            card_chat_id="oc_dm",
            task_code="1A",
            applied_at=self.applied_at,
            new_deadline=new_deadline,
        )

        self.assertEqual(result.new_status, TaskStatus.TODO)
        self.assertEqual(result.deadline_after, new_deadline)
        self.assertEqual(result.reminders_cancelled, 4)
        self.assertEqual(result.reminders_created, 4)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, 1)
            event = session.get(TaskLifecycleEvent, result.event_id)
            self.assertEqual(task.deadline, new_deadline)
            self.assertEqual(event.action, "reschedule")
            self.assertEqual(event.deadline_after, new_deadline)
            self.assertEqual(event.trigger_source, "card_action")
            self.assertIsNone(event.model)
            self.assertEqual(event.evidence_links, [])
            active = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.status == ReminderStatus.SCHEDULED.value,
                    TaskReminder.deadline_snapshot == new_deadline,
                )
            )
            self.assertEqual(active, 4)
            obsolete = session.scalars(
                select(TaskReminder).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.deadline_snapshot != new_deadline,
                )
            ).all()
            self.assertEqual(len(obsolete), 4)
            self.assertTrue(
                all(
                    reminder.status == ReminderStatus.CANCELLED.value
                    and reminder.cancel_reason == "task_deadline_changed"
                    for reminder in obsolete
                )
            )

    def test_card_reschedule_requires_a_changed_future_deadline(self) -> None:
        for index, deadline in enumerate(
            (
                None,
                self.applied_at,
                self.reference_time + timedelta(days=7),
            )
        ):
            with self.subTest(deadline=deadline), self.assertRaises(
                LifecycleMutationError
            ):
                self._service().apply_card_action(
                    LifecycleAction.RESCHEDULE,
                    actor_open_id="ou_owner",
                    callback_id=f"evt_bad_reschedule_{index}",
                    card_message_id="om_card",
                    card_chat_id="oc_dm",
                    task_code="1A",
                    applied_at=self.applied_at,
                    new_deadline=deadline,
                )

        self._assert_task_status(1, "todo")
        self._assert_event_count(0)

    def test_card_reminder_failure_rolls_back_task_and_audit(self) -> None:
        with (
            patch(
                "app.lifecycle.mutations.sync_task_reminders_in_session",
                side_effect=RuntimeError("simulated card reminder failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "card reminder failure"),
        ):
            self._service().apply_card_action(
                LifecycleAction.COMPLETE,
                actor_open_id="ou_owner",
                callback_id="evt_card_rollback",
                card_message_id="om_card",
                card_chat_id="oc_dm",
                task_code="1A",
                applied_at=self.applied_at,
            )

        self._assert_task_status(1, "todo")
        self._assert_event_count(0)

    def _service(
        self,
        *,
        admins: set[str] | frozenset[str] = frozenset(),
        allowed_chats: set[str] | frozenset[str] = frozenset(),
    ) -> LifecycleMutationService:
        return LifecycleMutationService(
            self.session_factory,
            administrator_open_ids=frozenset(admins),
            allowed_chat_ids=frozenset(allowed_chats),
        )

    def _candidate(
        self,
        task_id: int,
        action: LifecycleAction,
        trigger_message_id: str,
        *,
        deadline: datetime | None = None,
        confidence: float = 0.98,
        evidence: tuple[str, ...] | None = None,
        new_title: str | None = None,
        new_owners: tuple[TaskOwner, ...] = (),
    ) -> LifecycleCandidate:
        return LifecycleCandidate(
            action=action,
            confidence=confidence,
            task_id=task_id,
            new_deadline=deadline,
            evidence_message_ids=(
                evidence
                if evidence is not None
                else ("om_context", trigger_message_id)
            ),
            new_title=new_title,
            new_owners=new_owners,
        )

    def _assert_task_status(self, task_id: int, expected: str) -> None:
        with session_scope(self.session_factory) as session:
            self.assertEqual(session.get(Task, task_id).status, expected)

    def _assert_event_count(self, expected: int) -> None:
        with session_scope(self.session_factory) as session:
            count = session.scalar(select(func.count(TaskLifecycleEvent.id)))
        self.assertEqual(count, expected)

    def _seed(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                [
                    Chat(
                        chat_id="oc_lab",
                        tenant_key="tenant_test",
                        name="实验群",
                        chat_type="group",
                    ),
                    Chat(
                        chat_id="oc_other",
                        tenant_key="tenant_test",
                        name="其他群",
                        chat_type="group",
                    ),
                    Chat(
                        chat_id="oc_dm",
                        tenant_key="tenant_test",
                        name=None,
                        chat_type="p2p",
                    ),
                ]
            )
            session.add_all(
                User(
                    open_id=open_id,
                    name=name,
                    tenant_key="tenant_test",
                    last_seen_at=self.reference_time,
                )
                for open_id, name in (
                    ("ou_owner", "王政"),
                    ("ou_admin", "导师"),
                    ("ou_intruder", "其他成员"),
                    ("ou_coowner", "李四"),
                )
            )
            session.flush()
            messages = (
                ("om_context", "oc_dm", "ou_owner", 0),
                ("om_owner_done", "oc_dm", "ou_owner", 10),
                ("om_owner_reschedule", "oc_dm", "ou_owner", 20),
                ("om_owner_cancel", "oc_dm", "ou_owner", 30),
                ("om_intruder_dm", "oc_dm", "ou_intruder", 40),
                ("om_admin_group", "oc_lab", "ou_admin", 50),
                ("om_admin_other_group", "oc_other", "ou_admin", 60),
                ("om_other_context", "oc_other", "ou_owner", 5),
                ("om_admin_rename", "oc_dm", "ou_admin", 70),
                ("om_admin_reassign", "oc_dm", "ou_admin", 80),
                ("om_admin_invalidate", "oc_dm", "ou_admin", 90),
                ("om_admin_reassign_back", "oc_dm", "ou_admin", 100),
            )
            for index, (message_id, chat_id, sender, seconds) in enumerate(
                messages, start=1
            ):
                created_at = self.reference_time + timedelta(seconds=seconds)
                session.add(
                    Message(
                        tenant_key="tenant_test",
                        event_id=f"evt_{index}",
                        message_id=message_id,
                        chat_id=chat_id,
                        sender_open_id=sender,
                        sender_name_snapshot=None,
                        message_type="text",
                        text_content=message_id,
                        raw_content='{"text":"test"}',
                        raw_event_json="{}",
                        message_created_at=created_at,
                        received_at=created_at,
                        is_from_bot=False,
                    )
                )
            session.add_all(
                [
                    self._task(
                        "任务一",
                        "todo",
                        self.reference_time + timedelta(days=7),
                    ),
                    self._task(
                        "逾期任务",
                        "overdue",
                        self.reference_time - timedelta(days=1),
                    ),
                    self._task(
                        "任务三",
                        "todo",
                        self.reference_time + timedelta(days=8),
                    ),
                    self._task("待确认任务", "pending", None),
                    self._task(
                        "其他成员任务",
                        "todo",
                        self.reference_time + timedelta(days=9),
                        owner_open_id="ou_intruder",
                        owner_name="其他成员",
                    ),
                    self._task("第二个待确认任务", "pending", None),
                ]
            )
            session.add_all(
                ChatMemberAlias(
                    chat_id="oc_lab",
                    open_id=open_id,
                    alias=name,
                    normalized_alias=name.casefold(),
                    source="self_command",
                    confidence=1.0,
                    verified_at=self.reference_time,
                    created_at=self.reference_time,
                    updated_at=self.reference_time,
                )
                for open_id, name in (
                    ("ou_owner", "王政"),
                    ("ou_coowner", "李四"),
                    ("ou_admin", "导师"),
                )
            )
            session.add_all(
                ChatMembership(
                    chat_id="oc_lab",
                    open_id=open_id,
                    display_name_snapshot=name,
                    active=True,
                    is_owner=open_id == "ou_admin",
                    first_synced_at=self.reference_time,
                    last_synced_at=self.reference_time,
                )
                for open_id, name in (
                    ("ou_owner", "王政"),
                    ("ou_coowner", "李四"),
                    ("ou_admin", "导师"),
                )
            )

    def _task(
        self,
        title: str,
        status: str,
        deadline: datetime | None,
        *,
        owner_open_id: str = "ou_owner",
        owner_name: str = "王政",
    ) -> Task:
        return Task(
            chat_id="oc_lab",
            owner_open_id=owner_open_id,
            owner_name_snapshot=owner_name,
            title=title,
            normalized_title=title,
            description=f"{title}说明",
            deadline=deadline,
            status=status,
            confidence=0.95,
            created_at=self.reference_time,
            updated_at=self.reference_time,
        )


if __name__ == "__main__":
    unittest.main()
