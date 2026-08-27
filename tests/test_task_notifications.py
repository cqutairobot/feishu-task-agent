"""Durable missing-deadline and administrator notification tests."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
    ChatAdministrator,
    ChatSettings,
    Message,
    Task,
    TaskAssignee,
    TaskLifecycleEvent,
    TaskNotification,
    TaskNotificationDeferredLifecycleEvent,
    TaskNotificationState,
    User,
)
from app.notifications.repository import (
    TaskNotificationKind,
    TaskNotificationRepository,
    TaskNotificationStatus,
)
from app.tasks.codes import format_task_code


class TaskNotificationRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "notify.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
        self._seed_base()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_missing_deadline_prompts_owner_then_admin_once(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        repository = self._repository(test_mode=True)

        first = repository.sync_all(synced_at=self.now)
        second = repository.sync_all(synced_at=self.now)

        self.assertEqual(first.created, 2)
        self.assertEqual(second.created, 0)
        with session_scope(self.session_factory) as session:
            notifications = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task_id)
                .order_by(TaskNotification.scheduled_for)
            ).all()
            self.assertEqual(
                [item.kind for item in notifications],
                [
                    TaskNotificationKind.MISSING_DEADLINE_OWNER.value,
                    TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
                ],
            )
            self.assertEqual(
                [item.scheduled_for for item in notifications],
                [self.now + timedelta(minutes=2), self.now + timedelta(minutes=4)],
            )

        owner_lease = repository.claim_due(
            "worker",
            claimed_at=self.now + timedelta(minutes=2),
        )
        self.assertIsNotNone(owner_lease)
        assert owner_lease is not None
        self.assertEqual(
            owner_lease.kind,
            TaskNotificationKind.MISSING_DEADLINE_OWNER,
        )
        self.assertEqual(owner_lease.recipient_open_id, "ou_owner")
        self.assertEqual(owner_lease.recipient_private_chat_id, "oc_owner_dm")
        repository.mark_sent(
            owner_lease,
            feishu_message_id="om_owner_prompt",
            receive_id_type="chat_id",
            receive_id="oc_owner_dm",
            sent_at=self.now + timedelta(minutes=2, seconds=1),
        )

        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            assert task is not None
            task.deadline = self.now + timedelta(days=2)
            task.updated_at = self.now + timedelta(minutes=3)
        result = repository.sync_all(
            synced_at=self.now + timedelta(minutes=3)
        )

        self.assertEqual(result.cancelled, 1)
        with session_scope(self.session_factory) as session:
            notifications = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task_id)
                .order_by(TaskNotification.id)
            ).all()
            self.assertEqual(notifications[0].status, "sent")
            self.assertEqual(notifications[1].status, "cancelled")
            self.assertEqual(
                notifications[1].cancel_reason, "task_deadline_set"
            )

    def test_self_service_group_is_not_excluded_by_static_allowlist(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        with session_scope(self.session_factory) as session:
            session.add(
                ChatAdministrator(
                    chat_id="oc_group",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="group_owner_init",
                    created_at=self.now,
                )
            )
        repository = TaskNotificationRepository(
            self.session_factory,
            administrator_open_ids=frozenset(),
            allowed_chat_ids=frozenset({"oc_static"}),
            settings=ReminderSettings(test_mode=True),
        )

        result = repository.sync_all(synced_at=self.now)

        self.assertEqual(result.created, 2)
        with session_scope(self.session_factory) as session:
            recipients = set(
                session.scalars(
                    select(TaskNotification.recipient_open_id).where(
                        TaskNotification.task_id == task_id
                    )
                )
            )
        self.assertEqual(recipients, {"ou_owner", "ou_admin"})

    def test_production_missing_deadline_delays_are_one_and_three_days(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        self._repository(test_mode=False).sync_all(synced_at=self.now)

        with session_scope(self.session_factory) as session:
            notifications = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task_id)
                .order_by(TaskNotification.scheduled_for)
            ).all()
        self.assertEqual(
            [item.scheduled_for for item in notifications],
            [self.now + timedelta(days=1), self.now + timedelta(days=3)],
        )

    def test_selected_administrator_policy_replans_missing_deadline_recipient(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_group",
                    administrator_notification_mode="selected",
                    administrator_notification_open_ids_json='["ou_admin"]',
                )
            )
        repository = self._repository(
            test_mode=True,
            admins=frozenset({"ou_admin", "ou_other_admin"}),
        )

        first = repository.sync_all(synced_at=self.now)
        with session_scope(self.session_factory) as session:
            settings = session.get(ChatSettings, "oc_group")
            assert settings is not None
            settings.administrator_notification_open_ids_json = (
                '["ou_other_admin"]'
            )
        second = repository.sync_all(
            synced_at=self.now + timedelta(seconds=1)
        )

        self.assertEqual(first.created, 2)
        self.assertEqual(second.created, 1)
        self.assertEqual(second.cancelled, 1)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task_id)
                .order_by(TaskNotification.id)
            ).all()
        admin_rows = [
            row
            for row in rows
            if row.kind == TaskNotificationKind.MISSING_DEADLINE_ADMIN.value
        ]
        self.assertEqual(len(admin_rows), 2)
        self.assertEqual(
            {
                (row.recipient_open_id, row.status, row.cancel_reason)
                for row in admin_rows
            },
            {
                (
                    "ou_admin",
                    TaskNotificationStatus.CANCELLED.value,
                    "administrator_notification_policy_changed",
                ),
                (
                    "ou_other_admin",
                    TaskNotificationStatus.SCHEDULED.value,
                    None,
                ),
            },
        )

        with session_scope(self.session_factory) as session:
            settings = session.get(ChatSettings, "oc_group")
            assert settings is not None
            settings.administrator_notification_open_ids_json = '["ou_admin"]'
        third = repository.sync_all(
            synced_at=self.now + timedelta(seconds=2)
        )
        self.assertEqual(third.created, 1)
        self.assertEqual(third.cancelled, 1)
        with session_scope(self.session_factory) as session:
            admin_rows = session.scalars(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id,
                    TaskNotification.kind
                    == TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
                )
            ).all()
        self.assertEqual(len(admin_rows), 2)
        self.assertEqual(
            {
                row.recipient_open_id
                for row in admin_rows
                if row.status == TaskNotificationStatus.SCHEDULED.value
            },
            {"ou_admin"},
        )

    def test_selected_administrator_policy_applies_to_lifecycle_notifications(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_group",
                    administrator_notification_mode="selected",
                    administrator_notification_open_ids_json=(
                        '["ou_other_admin"]'
                    ),
                )
            )
        self._lifecycle_event(task_id, action="complete", actor="ou_owner")

        result = self._repository(
            admins=frozenset({"ou_admin", "ou_other_admin"})
        ).sync_all(synced_at=self.now)

        self.assertEqual(result.created, 1)
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(TaskNotification))
        assert row is not None
        self.assertEqual(row.kind, TaskNotificationKind.TASK_DONE_ADMIN.value)
        self.assertEqual(row.recipient_open_id, "ou_other_admin")

    def test_chat_policy_replans_switches_and_reactivates_without_duplicates(
        self,
    ) -> None:
        task_id = self._task(deadline=None, status="todo")
        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_group",
                    missing_deadline_owner_enabled=True,
                    missing_deadline_admin_enabled=True,
                    missing_deadline_owner_delay_hours=12,
                    missing_deadline_admin_delay_hours=48,
                )
            )
        repository = self._repository(test_mode=False)

        first = repository.sync_all(synced_at=self.now)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task_id)
                .order_by(TaskNotification.id)
            ).all()
            original_ids = {row.id for row in rows}
        self.assertEqual(first.created, 2)
        self.assertEqual(
            [row.scheduled_for for row in rows],
            [self.now + timedelta(hours=12), self.now + timedelta(hours=48)],
        )

        with session_scope(self.session_factory) as session:
            settings = session.get(ChatSettings, "oc_group")
            assert settings is not None
            settings.missing_deadline_owner_enabled = False
            settings.missing_deadline_admin_delay_hours = 60
        disabled = repository.sync_all(synced_at=self.now + timedelta(minutes=1))
        with session_scope(self.session_factory) as session:
            owner = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id,
                    TaskNotification.kind
                    == TaskNotificationKind.MISSING_DEADLINE_OWNER.value,
                )
            )
            admin = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id,
                    TaskNotification.kind
                    == TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
                )
            )
        assert owner is not None and admin is not None
        self.assertEqual(disabled.cancelled, 1)
        self.assertEqual(owner.status, TaskNotificationStatus.CANCELLED.value)
        self.assertEqual(owner.cancel_reason, "notification_stage_disabled")
        self.assertEqual(admin.scheduled_for, self.now + timedelta(hours=60))

        with session_scope(self.session_factory) as session:
            settings = session.get(ChatSettings, "oc_group")
            assert settings is not None
            settings.missing_deadline_owner_enabled = True
            settings.missing_deadline_owner_delay_hours = 18
        reenabled = repository.sync_all(
            synced_at=self.now + timedelta(minutes=2)
        )
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id
                )
            ).all()

        self.assertEqual(reenabled.created, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.id for row in rows}, original_ids)
        owner = next(
            row
            for row in rows
            if row.kind == TaskNotificationKind.MISSING_DEADLINE_OWNER.value
        )
        self.assertEqual(owner.status, TaskNotificationStatus.SCHEDULED.value)
        self.assertEqual(owner.scheduled_for, self.now + timedelta(hours=18))

    def test_disabling_missing_deadline_stage_preserves_sent_history(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        repository = self._repository(test_mode=True)
        repository.sync_all(synced_at=self.now)
        lease = repository.claim_due(
            "worker", claimed_at=self.now + timedelta(minutes=2)
        )
        assert lease is not None
        repository.mark_sent(
            lease,
            feishu_message_id="om_missing_sent",
            receive_id_type="open_id",
            receive_id="ou_owner",
            sent_at=self.now + timedelta(minutes=2, seconds=1),
        )
        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_group",
                    missing_deadline_owner_enabled=False,
                )
            )

        repository.sync_all(synced_at=self.now + timedelta(minutes=3))
        with session_scope(self.session_factory) as session:
            owner = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id,
                    TaskNotification.kind
                    == TaskNotificationKind.MISSING_DEADLINE_OWNER.value,
                )
            )

        assert owner is not None
        self.assertEqual(owner.status, TaskNotificationStatus.SENT.value)
        self.assertEqual(owner.feishu_message_id, "om_missing_sent")

    def test_persisted_group_administrator_receives_and_revocation_cancels_prompt(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        with session_scope(self.session_factory) as session:
            session.add(
                ChatAdministrator(
                    chat_id="oc_group",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="bootstrap",
                    created_at=self.now,
                )
            )
        repository = self._repository(test_mode=True, admins=frozenset())

        first = repository.sync_all(synced_at=self.now)
        with session_scope(self.session_factory) as session:
            membership = session.scalar(select(ChatAdministrator))
            assert membership is not None
            session.delete(membership)
        second = repository.sync_all(synced_at=self.now + timedelta(minutes=1))

        self.assertEqual(first.created, 2)
        self.assertEqual(second.cancelled, 1)
        with session_scope(self.session_factory) as session:
            admin_prompt = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id,
                    TaskNotification.kind
                    == TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
                )
            )
        assert admin_prompt is not None
        self.assertEqual(admin_prompt.status, "cancelled")
        self.assertEqual(
            admin_prompt.cancel_reason, "recipient_no_longer_authorized"
        )

    def test_shared_task_prompts_each_assignee_but_admin_only_once(self) -> None:
        task_id = self._task(deadline=None, status="todo")
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_owner",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    ),
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_coowner",
                        name_snapshot="李四",
                        position=1,
                        created_at=self.now,
                    ),
                )
            )

        result = self._repository(test_mode=True).sync_all(
            synced_at=self.now
        )

        self.assertEqual(result.created, 3)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification)
                .where(TaskNotification.task_id == task_id)
                .order_by(TaskNotification.id)
            ).all()
        owner_rows = [
            row
            for row in rows
            if row.kind
            == TaskNotificationKind.MISSING_DEADLINE_OWNER.value
        ]
        admin_rows = [
            row
            for row in rows
            if row.kind
            == TaskNotificationKind.MISSING_DEADLINE_ADMIN.value
        ]
        self.assertEqual(
            {row.recipient_open_id for row in owner_rows},
            {"ou_owner", "ou_coowner"},
        )
        self.assertEqual(len(admin_rows), 1)
        self.assertEqual(admin_rows[0].owner_name_snapshot, "王政、李四")

    def test_owner_complete_and_cancel_events_notify_admin(self) -> None:
        done_task = self._task(deadline=self.now + timedelta(days=1))
        cancelled_task = self._task(deadline=self.now + timedelta(days=2))
        self._lifecycle_event(done_task, action="complete", actor="ou_owner")
        self._lifecycle_event(
            cancelled_task, action="cancel", actor="ou_owner"
        )

        result = self._repository().sync_all(synced_at=self.now)

        self.assertEqual(result.created, 2)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification).order_by(TaskNotification.id)
            ).all()
            self.assertEqual(
                [row.kind for row in rows],
                [
                    TaskNotificationKind.TASK_DONE_ADMIN.value,
                    TaskNotificationKind.TASK_CANCELLED_ADMIN.value,
                ],
            )
            self.assertTrue(
                all(row.recipient_open_id == "ou_admin" for row in rows)
            )
            self.assertTrue(
                all(row.source_lifecycle_event_id is not None for row in rows)
            )
        replay = self._repository().sync_all(
            synced_at=self.now + timedelta(minutes=1)
        )
        self.assertEqual(replay.created, 0)

    def test_administrator_action_notifies_owner_but_not_same_administrator(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        self._lifecycle_event(task_id, action="complete", actor="ou_admin")

        result = self._repository().sync_all(synced_at=self.now)

        self.assertEqual(result.created, 1)
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(TaskNotification))
        self.assertEqual(
            row.kind,
            TaskNotificationKind.TASK_DONE_COASSIGNEE.value,
        )
        self.assertEqual(row.recipient_open_id, "ou_owner")

    def test_restore_event_notifies_owner_and_other_administrator(self) -> None:
        task_id = self._task(
            deadline=self.now + timedelta(days=1), status="done"
        )
        self._lifecycle_event(task_id, action="restore", actor="ou_admin")

        result = self._repository(
            admins=frozenset({"ou_admin", "ou_other_admin"})
        ).sync_all(synced_at=self.now)

        self.assertEqual(result.created, 2)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification).order_by(TaskNotification.id)
            ).all()
        self.assertEqual(
            {(row.kind, row.recipient_open_id) for row in rows},
            {
                (
                    TaskNotificationKind.TASK_RESTORED_COASSIGNEE.value,
                    "ou_owner",
                ),
                (
                    TaskNotificationKind.TASK_RESTORED_ADMIN.value,
                    "ou_other_admin",
                ),
            },
        )
        replay = self._repository(
            admins=frozenset({"ou_admin", "ou_other_admin"})
        ).sync_all(synced_at=self.now + timedelta(minutes=1))
        self.assertEqual(replay.created, 0)

    def test_merge_event_skips_notice_but_advances_lifecycle_cursor(self) -> None:
        source_id = self._task(deadline=self.now, status="cancelled")
        target_id = self._task(
            deadline=self.now + timedelta(days=2), status="todo"
        )
        repository = self._repository(test_mode=True)

        first = repository.sync_all(synced_at=self.now)
        self.assertEqual(first.created, 0)

        with session_scope(self.session_factory) as session:
            session.add(
                TaskLifecycleEvent(
                    task_id=source_id,
                    actor_open_id="ou_admin",
                    trigger_source="management_page",
                    trigger_management_request_id="merge-notification-test",
                    action="merge",
                    authorization_role="administrator",
                    task_code_snapshot=format_task_code(source_id),
                    previous_status="todo",
                    new_status="cancelled",
                    deadline_before=self.now,
                    deadline_after=self.now,
                    merge_target_task_id=target_id,
                    confidence=1.0,
                    applied_at=self.now + timedelta(minutes=1),
                    created_at=self.now + timedelta(minutes=1),
                )
            )

        second = repository.sync_all(synced_at=self.now + timedelta(minutes=2))

        self.assertEqual(second.created, 0)
        with session_scope(self.session_factory) as session:
            event_id = session.scalar(
                select(TaskLifecycleEvent.id).where(
                    TaskLifecycleEvent.trigger_management_request_id
                    == "merge-notification-test"
                )
            )
            cursor = session.scalar(
                select(TaskNotificationState.last_lifecycle_event_id)
            )
            merge_notifications = session.scalars(
                select(TaskNotification).where(
                    TaskNotification.source_lifecycle_event_id == event_id
                )
            ).all()
        self.assertIsNotNone(event_id)
        self.assertEqual(cursor, event_id)
        self.assertEqual(merge_notifications, [])

    def test_responsible_administrator_receives_only_one_role_notification(
        self,
    ) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        self._lifecycle_event(
            task_id,
            action="reschedule",
            actor="ou_admin",
            new_deadline=self.now + timedelta(days=3),
        )

        result = self._repository(
            admins=frozenset({"ou_admin", "ou_owner"})
        ).sync_all(synced_at=self.now)

        self.assertEqual(result.created, 1)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(TaskNotification)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].recipient_open_id, "ou_owner")
        self.assertEqual(
            rows[0].kind,
            TaskNotificationKind.TASK_RESCHEDULED_COASSIGNEE.value,
        )

    def test_shared_lifecycle_event_notifies_other_owner_and_admin(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_owner",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    ),
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_coowner",
                        name_snapshot="李四",
                        position=1,
                        created_at=self.now,
                    ),
                )
            )
        self._lifecycle_event(
            task_id, action="complete", actor="ou_owner"
        )

        result = self._repository().sync_all(synced_at=self.now)

        self.assertEqual(result.created, 2)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification).order_by(TaskNotification.id)
            ).all()
        self.assertEqual(
            {(row.kind, row.recipient_open_id) for row in rows},
            {
                (
                    TaskNotificationKind.TASK_DONE_COASSIGNEE.value,
                    "ou_coowner",
                ),
                (
                    TaskNotificationKind.TASK_DONE_ADMIN.value,
                    "ou_admin",
                ),
            },
        )

    def test_shared_reschedule_notifies_other_owner_and_admin_with_dates(self) -> None:
        original = self.now + timedelta(days=1)
        changed = self.now + timedelta(days=3)
        task_id = self._task(deadline=original)
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_owner",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    ),
                    TaskAssignee(
                        task_id=task_id,
                        open_id="ou_coowner",
                        name_snapshot="李四",
                        position=1,
                        created_at=self.now,
                    ),
                )
            )
        self._lifecycle_event(
            task_id,
            action="reschedule",
            actor="ou_owner",
            new_deadline=changed,
        )

        result = self._repository().sync_all(synced_at=self.now)

        self.assertEqual(result.created, 2)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification).order_by(TaskNotification.id)
            ).all()
        self.assertEqual(
            {row.kind for row in rows},
            {
                TaskNotificationKind.TASK_RESCHEDULED_COASSIGNEE.value,
                TaskNotificationKind.TASK_RESCHEDULED_ADMIN.value,
            },
        )
        self.assertTrue(
            all(row.deadline_before_snapshot == original for row in rows)
        )
        self.assertTrue(
            all(row.deadline_snapshot == changed for row in rows)
        )

    def test_administrator_title_correction_notifies_responsible_member(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            assert task is not None
            before = task.title
            task.title = "纠正后的标题"
            task.normalized_title = "纠正后的标题"
            session.add(
                TaskLifecycleEvent(
                    task_id=task_id,
                    actor_open_id="ou_admin",
                    trigger_source="card_action",
                    trigger_card_action_id=f"evt_rename_{task_id}",
                    trigger_card_message_id=f"om_rename_{task_id}",
                    trigger_card_chat_id="oc_admin_dm",
                    action="rename",
                    authorization_role="administrator",
                    task_code_snapshot=format_task_code(task_id),
                    previous_status="todo",
                    new_status="todo",
                    deadline_before=task.deadline,
                    deadline_after=task.deadline,
                    title_before=before,
                    title_after=task.title,
                    confidence=1.0,
                    applied_at=self.now,
                    created_at=self.now,
                )
            )

        result = self._repository().sync_all(synced_at=self.now)
        self.assertEqual(result.created, 1)
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(TaskNotification))
            assert row is not None
            self.assertEqual(row.kind, TaskNotificationKind.TASK_RENAMED_ASSIGNEE)
            self.assertEqual(row.recipient_open_id, "ou_owner")
            self.assertEqual(row.title_snapshot, "纠正后的标题")

    def test_administrator_reassignment_notifies_added_and_removed_members(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        before = [{"name": "王政", "open_id": "ou_owner"}]
        after = [{"name": "李四", "open_id": "ou_coowner"}]
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            assert task is not None
            task.owner_open_id = "ou_coowner"
            task.owner_name_snapshot = "李四"
            session.add(
                TaskAssignee(
                    task_id=task_id,
                    open_id="ou_coowner",
                    name_snapshot="李四",
                    position=0,
                    created_at=self.now,
                )
            )
            session.add(
                TaskLifecycleEvent(
                    task_id=task_id,
                    actor_open_id="ou_admin",
                    trigger_source="card_action",
                    trigger_card_action_id=f"evt_reassign_{task_id}",
                    trigger_card_message_id=f"om_reassign_{task_id}",
                    trigger_card_chat_id="oc_admin_dm",
                    action="reassign",
                    authorization_role="administrator",
                    task_code_snapshot=format_task_code(task_id),
                    previous_status="todo",
                    new_status="todo",
                    deadline_before=task.deadline,
                    deadline_after=task.deadline,
                    assignees_before_json=json.dumps(before, ensure_ascii=False),
                    assignees_after_json=json.dumps(after, ensure_ascii=False),
                    confidence=1.0,
                    applied_at=self.now,
                    created_at=self.now,
                )
            )

        result = self._repository().sync_all(synced_at=self.now)
        self.assertEqual(result.created, 2)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(TaskNotification).order_by(TaskNotification.id)
            ).all()
        self.assertEqual(
            {(row.kind, row.recipient_open_id) for row in rows},
            {
                (TaskNotificationKind.TASK_ASSIGNEE_REMOVED.value, "ou_owner"),
                (TaskNotificationKind.TASK_ASSIGNEE_ADDED.value, "ou_coowner"),
            },
        )

    def test_administrator_invalidation_notifies_owner(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        self._lifecycle_event(task_id, action="invalidate", actor="ou_admin")

        result = self._repository().sync_all(synced_at=self.now)

        self.assertEqual(result.created, 1)
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(TaskNotification))
        self.assertEqual(
            row.kind,
            TaskNotificationKind.TASK_INVALIDATED_ASSIGNEE.value,
        )
        self.assertEqual(row.recipient_open_id, "ou_owner")

    def test_excluded_lifecycle_event_is_delivered_after_chat_is_admitted(self) -> None:
        task_id = self._task(deadline=self.now + timedelta(days=1))
        admitted = self._repository()
        admitted.sync_all(synced_at=self.now)
        self._lifecycle_event(task_id, action="complete", actor="ou_owner")
        excluded = TaskNotificationRepository(
            self.session_factory,
            administrator_open_ids=frozenset({"ou_admin"}),
            allowed_chat_ids=frozenset({"oc_static"}),
            settings=ReminderSettings(),
        )

        skipped = excluded.sync_all(synced_at=self.now + timedelta(seconds=1))

        self.assertEqual(skipped.created, 0)
        with session_scope(self.session_factory) as session:
            event = session.scalar(
                select(TaskLifecycleEvent).where(
                    TaskLifecycleEvent.task_id == task_id
                )
            )
            assert event is not None
            self.assertIsNotNone(
                session.get(TaskNotificationDeferredLifecycleEvent, event.id)
            )
            state = session.get(TaskNotificationState, 1)
            assert state is not None
            self.assertEqual(state.last_lifecycle_event_id, event.id)

        delivered = admitted.sync_all(
            synced_at=self.now + timedelta(seconds=2)
        )

        self.assertEqual(delivered.created, 1)
        with session_scope(self.session_factory) as session:
            self.assertEqual(
                session.scalar(
                    select(TaskNotification.recipient_open_id).where(
                        TaskNotification.task_id == task_id,
                        TaskNotification.kind
                        == TaskNotificationKind.TASK_DONE_ADMIN.value,
                    )
                ),
                "ou_admin",
            )
            self.assertIsNone(
                session.scalar(
                    select(TaskNotificationDeferredLifecycleEvent)
                )
            )

    def test_overdue_notification_is_deadline_versioned_and_cancellable(self) -> None:
        task_id = self._task(
            deadline=self.now - timedelta(minutes=1), status="overdue"
        )
        repository = self._repository()

        first = repository.sync_all(synced_at=self.now)
        second = repository.sync_all(synced_at=self.now)

        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 0)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            assert task is not None
            task.status = "todo"
            task.deadline = self.now + timedelta(days=3)
            task.updated_at = self.now + timedelta(minutes=1)
        result = repository.sync_all(
            synced_at=self.now + timedelta(minutes=1)
        )
        self.assertEqual(result.cancelled, 1)
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == task_id
                )
            )
            assert row is not None
            self.assertEqual(row.status, "cancelled")
            self.assertEqual(row.cancel_reason, "task_no_longer_overdue")

    def _repository(
        self,
        *,
        test_mode: bool = False,
        admins: frozenset[str] = frozenset({"ou_admin"}),
    ) -> TaskNotificationRepository:
        return TaskNotificationRepository(
            self.session_factory,
            administrator_open_ids=admins,
            allowed_chat_ids=frozenset({"oc_group"}),
            settings=ReminderSettings(test_mode=test_mode),
        )

    def _task(
        self,
        *,
        deadline: datetime | None,
        status: str = "todo",
    ) -> int:
        with session_scope(self.session_factory) as session:
            task = Task(
                chat_id="oc_group",
                owner_open_id="ou_owner",
                owner_name_snapshot="王政",
                title=f"测试任务 {deadline} {status}",
                normalized_title=f"测试任务 {deadline} {status}",
                description="验证通知",
                deadline=deadline,
                status=status,
                confidence=0.95,
                completed_at=self.now if status == "done" else None,
                cancelled_at=self.now if status == "cancelled" else None,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(task)
            session.flush()
            return task.id

    def _lifecycle_event(
        self,
        task_id: int,
        *,
        action: str,
        actor: str,
        new_deadline: datetime | None = None,
    ) -> None:
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            assert task is not None
            previous = task.status
            deadline_before = task.deadline
            new_status = {
                "complete": "done",
                "cancel": "cancelled",
                "reschedule": "todo",
                "invalidate": "cancelled",
                "restore": (
                    "overdue"
                    if task.deadline is not None and task.deadline <= self.now
                    else "todo"
                ),
            }[action]
            task.status = new_status
            task.completed_at = self.now if new_status == "done" else None
            task.cancelled_at = (
                self.now if new_status == "cancelled" else None
            )
            if action == "reschedule":
                assert new_deadline is not None
                task.deadline = new_deadline
            event = TaskLifecycleEvent(
                task_id=task.id,
                actor_open_id=actor,
                trigger_source="card_action",
                trigger_message_db_id=None,
                trigger_card_action_id=f"evt_{action}_{task.id}",
                trigger_card_message_id=f"om_{action}_{task.id}",
                trigger_card_chat_id="oc_actor_dm",
                action=action,
                authorization_role=(
                    "administrator" if actor == "ou_admin" else "owner"
                ),
                task_code_snapshot=format_task_code(task.id),
                previous_status=previous,
                new_status=new_status,
                deadline_before=deadline_before,
                deadline_after=task.deadline,
                confidence=1.0,
                applied_at=self.now,
                created_at=self.now,
            )
            session.add(event)

    def _seed_base(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Chat(
                        chat_id="oc_group",
                        tenant_key="tenant",
                        name="测试群",
                        chat_type="group",
                    ),
                    Chat(
                        chat_id="oc_owner_dm",
                        tenant_key="tenant",
                        name=None,
                        chat_type="p2p",
                    ),
                )
            )
            session.add_all(
                User(
                    open_id=open_id,
                    name=name,
                    tenant_key="tenant",
                    last_seen_at=self.now,
                )
                for open_id, name in (
                    ("ou_owner", "王政"),
                    ("ou_coowner", "李四"),
                    ("ou_admin", "导师"),
                    ("ou_other_admin", "另一位导师"),
                )
            )
            session.add(
                Message(
                    tenant_key="tenant",
                    event_id="evt_owner_dm",
                    message_id="om_owner_dm",
                    chat_id="oc_owner_dm",
                    sender_open_id="ou_owner",
                    sender_name_snapshot="王政",
                    message_type="text",
                    text_content="任务列表",
                    raw_content='{"text":"任务列表"}',
                    raw_event_json="{}",
                    message_created_at=self.now,
                    received_at=self.now,
                    is_from_bot=False,
                )
            )


if __name__ == "__main__":
    unittest.main()
