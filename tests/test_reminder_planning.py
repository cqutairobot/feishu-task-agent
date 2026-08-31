"""Phase 5A durable reminder schedule and lifecycle tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import ReminderSettings
from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import upgrade_database
from app.database.models import (
    Chat,
    ChatSettings,
    Task,
    TaskAssignee,
    TaskLifecycleEvent,
    TaskReminder,
    User,
)
from app.reminders.repository import ReminderRepository, ReminderStatus
from app.reminders.schedule import ReminderKind, reminder_moments
from app.system_lifecycle import (
    SYSTEM_REMINDER_ACTOR_NAME,
    SYSTEM_REMINDER_ACTOR_OPEN_ID,
    overdue_transition_key,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class ReminderPlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "reminders.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.settings = ReminderSettings(
            due_day_hour=9,
            overdue_grace_minutes=1,
            max_attempts=3,
        )
        self.repository = ReminderRepository(
            self.session_factory,
            settings=self.settings,
        )
        self.now = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_test",
                    tenant_key="tenant_test",
                    name="实验群",
                    chat_type="group",
                )
            )
            session.add(
                User(
                    open_id="ou_wang",
                    union_id="on_wang",
                    name="王政",
                    tenant_key="tenant_test",
                    last_seen_at=self.now,
                )
            )
            session.add(
                User(
                    open_id="ou_li",
                    union_id="on_li",
                    name="李四",
                    tenant_key="tenant_test",
                    last_seen_at=self.now,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_four_schedule_moments_use_shanghai_policy(self) -> None:
        deadline = datetime(
            2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ
        )

        moments = reminder_moments(deadline, self.settings)

        self.assertEqual(
            [moment.kind for moment in moments],
            [
                ReminderKind.DUE_72H,
                ReminderKind.DUE_24H,
                ReminderKind.DUE_TODAY,
                ReminderKind.OVERDUE,
            ],
        )
        self.assertEqual(
            [
                moment.scheduled_for.astimezone(SHANGHAI_TZ).isoformat()
                for moment in moments
            ],
            [
                "2026-08-27T18:00:00+08:00",
                "2026-08-29T18:00:00+08:00",
                "2026-08-30T09:00:00+08:00",
                "2026-08-30T18:01:00+08:00",
            ],
        )

    def test_due_today_never_falls_after_an_early_deadline(self) -> None:
        deadline = datetime(
            2026, 8, 30, 8, 0, tzinfo=SHANGHAI_TZ
        )

        moments = reminder_moments(deadline, self.settings)

        due_today = next(
            moment for moment in moments
            if moment.kind is ReminderKind.DUE_TODAY
        )
        self.assertEqual(due_today.scheduled_for, deadline)

    def test_custom_policy_calculates_all_four_moments(self) -> None:
        deadline = datetime(2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ)

        moments = reminder_moments(
            deadline,
            self.settings,
            due_72h_offset_hours=96,
            due_24h_offset_hours=36,
            due_day_hour=7,
            overdue_grace_minutes=15,
        )

        self.assertEqual(
            [
                moment.scheduled_for.astimezone(SHANGHAI_TZ).isoformat()
                for moment in moments
            ],
            [
                "2026-08-26T18:00:00+08:00",
                "2026-08-29T06:00:00+08:00",
                "2026-08-30T07:00:00+08:00",
                "2026-08-30T18:15:00+08:00",
            ],
        )

        with self.assertRaisesRegex(ValueError, "first reminder"):
            reminder_moments(
                deadline,
                self.settings,
                due_72h_offset_hours=24,
                due_24h_offset_hours=48,
            )

    def test_test_mode_uses_four_short_deadline_relative_moments(self) -> None:
        deadline = datetime(
            2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ
        )

        moments = reminder_moments(
            deadline,
            ReminderSettings(test_mode=True),
        )

        self.assertEqual(
            [
                moment.scheduled_for.astimezone(SHANGHAI_TZ).isoformat()
                for moment in moments
            ],
            [
                "2026-08-30T17:54:00+08:00",
                "2026-08-30T17:56:00+08:00",
                "2026-08-30T17:58:00+08:00",
                "2026-08-30T18:01:00+08:00",
            ],
        )

    def test_todo_task_creates_four_idempotent_rows(self) -> None:
        task_id = self._add_task(
            deadline=datetime(
                2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ
            )
        )

        first = self.repository.sync_task(task_id, synced_at=self.now)
        second = self.repository.sync_task(task_id, synced_at=self.now)

        self.assertEqual(first.reminders_created, 4)
        self.assertEqual(first.active_reminders, 4)
        self.assertEqual(second.reminders_created, 0)
        reminders = self.repository.list_for_task(task_id)
        self.assertEqual(len(reminders), 4)
        self.assertTrue(
            all(
                reminder.status is ReminderStatus.SCHEDULED
                for reminder in reminders
            )
        )

    def test_shared_task_creates_one_plan_for_each_assignee(self) -> None:
        task_id = self._add_task(
            deadline=datetime(
                2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ
            )
        )
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_wang",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    ),
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_li",
                        name_snapshot="李四",
                        position=1,
                        created_at=self.now,
                    ),
                )
            )

        result = self.repository.sync_task(task_id, synced_at=self.now)
        reminders = self.repository.list_for_task(task_id)

        self.assertEqual(result.reminders_created, 8)
        self.assertEqual(result.active_reminders, 8)
        self.assertEqual(
            {reminder.recipient_open_id for reminder in reminders},
            {"ou_wang", "ou_li"},
        )

    def test_pending_task_does_not_schedule_automatic_reminders(self) -> None:
        task_id = self._add_task(
            status="pending",
            deadline=self.now + timedelta(days=7),
        )

        result = self.repository.sync_task(task_id, synced_at=self.now)

        self.assertEqual(result.reminders_created, 0)
        self.assertEqual(self.repository.list_for_task(task_id), ())

    def test_done_task_cancels_only_unsent_reminders(self) -> None:
        task_id = self._add_task(deadline=self.now + timedelta(days=7))
        self.repository.sync_task(task_id, synced_at=self.now)
        with session_scope(self.session_factory) as session:
            first = session.scalar(
                select(TaskReminder)
                .where(TaskReminder.task_id == task_id)
                .order_by(TaskReminder.id)
                .limit(1)
            )
            first.status = "sent"
            first.sent_at = self.now
            first.feishu_message_id = "om_reminder"
            first.delivery_receive_id_type = "open_id"
            first.delivery_receive_id = "ou_wang"
            task = session.get(Task, task_id)
            task.status = "done"
            task.completed_at = self.now

        result = self.repository.sync_task(task_id, synced_at=self.now)

        self.assertEqual(result.reminders_cancelled, 3)
        reminders = self.repository.list_for_task(task_id)
        self.assertEqual(
            [reminder.status for reminder in reminders].count(
                ReminderStatus.SENT
            ),
            1,
        )
        self.assertEqual(
            [reminder.status for reminder in reminders].count(
                ReminderStatus.CANCELLED
            ),
            3,
        )
        self.assertTrue(
            all(
                reminder.cancel_reason == "task_done"
                for reminder in reminders
                if reminder.status is ReminderStatus.CANCELLED
            )
        )

    def test_deadline_change_cancels_old_plan_and_creates_new_plan(self) -> None:
        first_deadline = self.now + timedelta(days=7)
        task_id = self._add_task(deadline=first_deadline)
        self.repository.sync_task(task_id, synced_at=self.now)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            task.deadline = first_deadline + timedelta(days=2)

        result = self.repository.sync_task(task_id, synced_at=self.now)

        self.assertEqual(result.reminders_created, 4)
        self.assertEqual(result.reminders_cancelled, 4)
        reminders = self.repository.list_for_task(task_id)
        self.assertEqual(len(reminders), 8)
        self.assertEqual(
            [reminder.status for reminder in reminders].count(
                ReminderStatus.SCHEDULED
            ),
            4,
        )
        self.assertTrue(
            all(
                reminder.cancel_reason == "task_deadline_changed"
                for reminder in reminders
                if reminder.status is ReminderStatus.CANCELLED
            )
        )

    def test_policy_change_reschedules_same_audit_rows(self) -> None:
        deadline = datetime(
            2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ
        )
        task_id = self._add_task(deadline=deadline)
        self.repository.sync_task(task_id, synced_at=self.now)
        original_ids = {
            reminder.reminder_id
            for reminder in self.repository.list_for_task(task_id)
        }
        changed_repository = ReminderRepository(
            self.session_factory,
            settings=ReminderSettings(
                due_day_hour=8,
                overdue_grace_minutes=5,
                max_attempts=3,
            ),
        )

        result = changed_repository.sync_task(
            task_id, synced_at=self.now
        )

        reminders = changed_repository.list_for_task(task_id)
        self.assertEqual(result.reminders_created, 0)
        self.assertEqual(result.reminders_cancelled, 0)
        self.assertEqual(
            {reminder.reminder_id for reminder in reminders},
            original_ids,
        )
        due_today = next(
            reminder for reminder in reminders
            if reminder.kind is ReminderKind.DUE_TODAY
        )
        overdue = next(
            reminder for reminder in reminders
            if reminder.kind is ReminderKind.OVERDUE
        )
        self.assertEqual(
            due_today.scheduled_for.astimezone(SHANGHAI_TZ).hour, 8
        )
        self.assertEqual(
            overdue.scheduled_for,
            deadline.astimezone(timezone.utc) + timedelta(minutes=5),
        )

    def test_chat_policy_reschedules_same_audit_rows(self) -> None:
        deadline = datetime(2026, 8, 30, 18, 0, tzinfo=SHANGHAI_TZ)
        task_id = self._add_task(deadline=deadline)
        self.repository.sync_task(task_id, synced_at=self.now)
        original_ids = {
            reminder.reminder_id
            for reminder in self.repository.list_for_task(task_id)
        }
        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_test",
                    reminder_due_72h_offset_hours=96,
                    reminder_due_24h_offset_hours=36,
                    reminder_due_today_hour=7,
                    reminder_overdue_grace_minutes=15,
                )
            )

        result = self.repository.sync_task(task_id, synced_at=self.now)
        reminders = {
            reminder.kind: reminder
            for reminder in self.repository.list_for_task(task_id)
        }

        self.assertEqual(result.reminders_created, 0)
        self.assertEqual(result.reminders_cancelled, 0)
        self.assertEqual(
            {reminder.reminder_id for reminder in reminders.values()},
            original_ids,
        )
        self.assertEqual(
            reminders[ReminderKind.DUE_72H].scheduled_for,
            deadline.astimezone(timezone.utc) - timedelta(hours=96),
        )
        self.assertEqual(
            reminders[ReminderKind.DUE_24H].scheduled_for,
            deadline.astimezone(timezone.utc) - timedelta(hours=36),
        )
        self.assertEqual(
            reminders[ReminderKind.DUE_TODAY]
            .scheduled_for.astimezone(SHANGHAI_TZ)
            .hour,
            7,
        )
        self.assertEqual(
            reminders[ReminderKind.OVERDUE].scheduled_for,
            deadline.astimezone(timezone.utc) + timedelta(minutes=15),
        )

    def test_past_todo_becomes_overdue_and_keeps_only_overdue_stage(self) -> None:
        task_id = self._add_task(deadline=self.now - timedelta(minutes=2))

        result = self.repository.sync_task(task_id, synced_at=self.now)

        self.assertEqual(result.task_statuses_changed, 1)
        self.assertEqual(result.reminders_created, 1)
        reminder = self.repository.list_for_task(task_id)[0]
        self.assertEqual(reminder.kind, ReminderKind.OVERDUE)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "overdue")
            events = tuple(
                session.scalars(
                    select(TaskLifecycleEvent).where(
                        TaskLifecycleEvent.task_id == task_id
                    )
                )
            )
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event.actor_open_id, SYSTEM_REMINDER_ACTOR_OPEN_ID)
            self.assertEqual(event.actor_name_snapshot, SYSTEM_REMINDER_ACTOR_NAME)
            self.assertEqual(event.trigger_source, "system")
            self.assertEqual(event.action, "overdue")
            self.assertEqual(event.authorization_role, "system")
            self.assertEqual(event.previous_status, "todo")
            self.assertEqual(event.new_status, "overdue")
            self.assertEqual(event.deadline_before, task.deadline)
            self.assertEqual(event.deadline_after, task.deadline)
            self.assertEqual(event.source_message_id, None)
            self.assertEqual(event.completion_cycle, task.completion_cycle)
            self.assertEqual(event.from_review_status, task.review_status)
            self.assertEqual(event.to_review_status, task.review_status)
            self.assertEqual(event.confidence, 1.0)
            self.assertEqual(
                event.idempotency_key,
                overdue_transition_key(task_id, task.deadline),
            )

        second = self.repository.sync_task(task_id, synced_at=self.now)
        self.assertEqual(second.task_statuses_changed, 0)
        with session_scope(self.session_factory) as session:
            self.assertEqual(
                session.scalar(
                    select(TaskLifecycleEvent.id).where(
                        TaskLifecycleEvent.task_id == task_id
                    )
                ),
                events[0].id,
            )

    def test_sync_all_reports_active_rows_across_tasks(self) -> None:
        self._add_task(deadline=self.now + timedelta(days=7))
        self._add_task(status="pending", deadline=self.now + timedelta(days=3))
        self._add_task(status="todo", deadline=None)

        result = self.repository.sync_all(synced_at=self.now)

        self.assertEqual(result.tasks_scanned, 3)
        self.assertEqual(result.reminders_created, 4)
        self.assertEqual(result.active_reminders, 4)

    def _add_task(
        self,
        *,
        status: str = "todo",
        deadline: datetime | None,
    ) -> int:
        with session_scope(self.session_factory) as session:
            task = Task(
                chat_id="oc_test",
                owner_open_id="ou_wang",
                owner_name_snapshot="王政",
                title=f"测试任务 {self.now.timestamp()}",
                normalized_title=f"测试任务 {self.now.timestamp()}",
                description="测试提醒计划",
                deadline=deadline,
                status=status,
                confidence=0.95 if status != "pending" else 0.60,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(task)
            session.flush()
            return task.id


if __name__ == "__main__":
    unittest.main()
