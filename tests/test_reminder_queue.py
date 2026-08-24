"""Phase 5B durable reminder claim, retry, and audit tests."""

from datetime import datetime, timedelta, timezone
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
    ChatSettings,
    Message,
    Task,
    TaskReminder,
    User,
)
from app.reminders.repository import ReminderRepository, ReminderStatus
from app.reminders.schedule import ReminderKind


class ReminderQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "queue.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.repository = ReminderRepository(
            self.session_factory,
            settings=ReminderSettings(max_attempts=3),
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
                    name="王政",
                    tenant_key="tenant_test",
                    last_seen_at=self.now,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_claim_is_exclusive_and_sent_target_is_audited(self) -> None:
        task_id = self._add_task(deadline=self.now + timedelta(days=3))

        lease = self.repository.claim_due("worker-a", claimed_at=self.now)
        duplicate = self.repository.claim_due(
            "worker-b", claimed_at=self.now
        )

        self.assertIsNotNone(lease)
        self.assertEqual(lease.task_id, task_id)
        self.assertEqual(lease.kind, ReminderKind.DUE_72H)
        self.assertEqual(lease.attempt, 1)
        self.assertIsNone(duplicate)

        sent = self.repository.mark_sent(
            lease,
            feishu_message_id="om_sent",
            receive_id_type="open_id",
            receive_id="ou_wang",
            sent_at=self.now + timedelta(seconds=1),
        )

        self.assertEqual(sent.status, ReminderStatus.SENT)
        self.assertEqual(sent.feishu_message_id, "om_sent")
        self.assertEqual(sent.delivery_receive_id_type, "open_id")
        self.assertEqual(sent.delivery_receive_id, "ou_wang")

    def test_private_failure_and_group_fallback_are_preserved(self) -> None:
        self._add_task(deadline=self.now + timedelta(days=3))
        lease = self.repository.claim_due("worker-a", claimed_at=self.now)

        sent = self.repository.mark_sent(
            lease,
            feishu_message_id="om_group",
            receive_id_type="chat_id",
            receive_id="oc_test",
            sent_at=self.now,
            private_error_code="230002",
            private_error_message="bot cannot initiate private chat",
        )

        self.assertEqual(sent.delivery_receive_id_type, "chat_id")
        self.assertEqual(sent.last_error_code, "230002")
        self.assertIn("private", sent.last_error_message)

    def test_failure_retries_with_backoff_and_dies_at_max_attempts(self) -> None:
        self._add_task(deadline=self.now + timedelta(days=3))
        current = self.now

        for expected_attempt in (1, 2, 3):
            lease = self.repository.claim_due(
                f"worker-{expected_attempt}", claimed_at=current
            )
            self.assertEqual(lease.attempt, expected_attempt)
            failure = self.repository.fail(
                lease,
                error_code="all_delivery_failed",
                error_message="Feishu rejected both targets",
                failed_at=current,
                retry_delay=timedelta(seconds=30),
            )
            if expected_attempt < 3:
                self.assertEqual(failure.status, ReminderStatus.SCHEDULED)
                self.assertEqual(
                    failure.retry_at, current + timedelta(seconds=30)
                )
                self.assertIsNone(
                    self.repository.claim_due(
                        "too-early",
                        claimed_at=current + timedelta(seconds=29),
                    )
                )
                current += timedelta(seconds=30)
            else:
                self.assertEqual(failure.status, ReminderStatus.DEAD)
                self.assertIsNone(failure.retry_at)

    def test_expired_lease_is_recovered_and_old_worker_is_rejected(self) -> None:
        self._add_task(deadline=self.now + timedelta(days=3))
        old_lease = self.repository.claim_due(
            "worker-old",
            claimed_at=self.now,
            lease_duration=timedelta(seconds=10),
        )
        new_lease = self.repository.claim_due(
            "worker-new",
            claimed_at=self.now + timedelta(seconds=11),
            lease_duration=timedelta(seconds=10),
        )

        self.assertEqual(new_lease.attempt, 2)
        with self.assertRaisesRegex(ValueError, "no longer active"):
            self.repository.mark_sent(
                old_lease,
                feishu_message_id="om_stale",
                receive_id_type="open_id",
                receive_id="ou_wang",
                sent_at=self.now + timedelta(seconds=12),
            )

    def test_worker_can_finish_after_expiry_until_another_claims(self) -> None:
        self._add_task(deadline=self.now + timedelta(days=3))
        lease = self.repository.claim_due(
            "slow-worker",
            claimed_at=self.now,
            lease_duration=timedelta(seconds=10),
        )

        sent = self.repository.mark_sent(
            lease,
            feishu_message_id="om_slow",
            receive_id_type="open_id",
            receive_id="ou_wang",
            sent_at=self.now + timedelta(seconds=11),
        )

        self.assertEqual(sent.status, ReminderStatus.SENT)

    def test_missed_stages_collapse_to_most_urgent_due_stage(self) -> None:
        deadline = self.now + timedelta(days=5)
        task_id = self._add_task(deadline=deadline)
        self.repository.sync_task(task_id, synced_at=self.now)
        resumed_at = deadline - timedelta(hours=23)

        lease = self.repository.claim_due(
            "resumed-worker", claimed_at=resumed_at
        )

        self.assertEqual(lease.kind, ReminderKind.DUE_24H)
        reminders = self.repository.list_for_task(task_id)
        due_72 = next(
            item for item in reminders
            if item.kind is ReminderKind.DUE_72H
        )
        self.assertEqual(due_72.status, ReminderStatus.CANCELLED)
        self.assertEqual(due_72.cancel_reason, "superseded_by_due_24h")

        self.repository.mark_sent(
            lease,
            feishu_message_id="om_24h",
            receive_id_type="open_id",
            receive_id="ou_wang",
            sent_at=resumed_at + timedelta(seconds=1),
        )
        duplicate = self.repository.claim_due(
            "next-worker",
            claimed_at=resumed_at + timedelta(seconds=2),
        )

        self.assertIsNone(duplicate)
        due_72_after_resync = next(
            item
            for item in self.repository.list_for_task(task_id)
            if item.kind is ReminderKind.DUE_72H
        )
        self.assertEqual(
            due_72_after_resync.status,
            ReminderStatus.CANCELLED,
        )
        self.assertEqual(
            due_72_after_resync.cancel_reason,
            "superseded_by_due_24h",
        )

    def test_completed_task_is_cancelled_before_claim(self) -> None:
        task_id = self._add_task(deadline=self.now + timedelta(days=3))
        self.repository.sync_task(task_id, synced_at=self.now)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, task_id)
            task.status = "done"
            task.completed_at = self.now

        lease = self.repository.claim_due("worker", claimed_at=self.now)

        self.assertIsNone(lease)
        self.assertTrue(
            all(
                item.status is ReminderStatus.CANCELLED
                for item in self.repository.list_for_task(task_id)
            )
        )

    def test_chat_stage_switches_cancel_and_reactivate_unsent_plan(self) -> None:
        task_id = self._add_task(deadline=self.now + timedelta(days=5))
        initial = self.repository.sync_task(task_id, synced_at=self.now)
        self.assertEqual(initial.reminders_created, 4)

        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_test",
                    reminder_due_72h_enabled=False,
                    reminder_due_24h_enabled=True,
                    reminder_due_today_enabled=True,
                    reminder_overdue_enabled=False,
                )
            )
        disabled = self.repository.sync_task(
            task_id, synced_at=self.now + timedelta(seconds=1)
        )
        reminders = {
            item.kind: item for item in self.repository.list_for_task(task_id)
        }

        self.assertEqual(disabled.reminders_cancelled, 2)
        self.assertEqual(len(reminders), 4)
        self.assertEqual(
            reminders[ReminderKind.DUE_72H].status,
            ReminderStatus.CANCELLED,
        )
        self.assertEqual(
            reminders[ReminderKind.DUE_72H].cancel_reason,
            "reminder_stage_disabled",
        )
        self.assertEqual(
            reminders[ReminderKind.OVERDUE].status,
            ReminderStatus.CANCELLED,
        )
        self.assertEqual(
            reminders[ReminderKind.OVERDUE].cancel_reason,
            "reminder_stage_disabled",
        )
        self.assertEqual(
            reminders[ReminderKind.DUE_24H].status,
            ReminderStatus.SCHEDULED,
        )
        self.assertEqual(
            reminders[ReminderKind.DUE_TODAY].status,
            ReminderStatus.SCHEDULED,
        )

        with session_scope(self.session_factory) as session:
            settings = session.get(ChatSettings, "oc_test")
            settings.reminder_due_72h_enabled = True
            settings.reminder_overdue_enabled = True
        reenabled = self.repository.sync_task(
            task_id, synced_at=self.now + timedelta(seconds=2)
        )
        reminders = self.repository.list_for_task(task_id)

        self.assertEqual(reenabled.reminders_created, 2)
        self.assertEqual(len(reminders), 4)
        self.assertTrue(
            all(item.status is ReminderStatus.SCHEDULED for item in reminders)
        )

    def test_stage_switch_never_rewrites_sent_history(self) -> None:
        task_id = self._add_task(deadline=self.now + timedelta(days=3))
        lease = self.repository.claim_due("worker", claimed_at=self.now)
        self.assertEqual(lease.kind, ReminderKind.DUE_72H)
        sent = self.repository.mark_sent(
            lease,
            feishu_message_id="om_stage_sent",
            receive_id_type="open_id",
            receive_id="ou_wang",
            sent_at=self.now + timedelta(seconds=1),
        )

        with session_scope(self.session_factory) as session:
            session.add(
                ChatSettings(
                    chat_id="oc_test",
                    reminder_due_72h_enabled=False,
                )
            )
        self.repository.sync_task(
            task_id, synced_at=self.now + timedelta(seconds=2)
        )
        with session_scope(self.session_factory) as session:
            settings = session.get(ChatSettings, "oc_test")
            settings.reminder_due_72h_enabled = True
        self.repository.sync_task(
            task_id, synced_at=self.now + timedelta(seconds=3)
        )
        due_72 = [
            item
            for item in self.repository.list_for_task(task_id)
            if item.kind is ReminderKind.DUE_72H
        ]

        self.assertEqual(len(due_72), 1)
        self.assertEqual(due_72[0].reminder_id, sent.reminder_id)
        self.assertEqual(due_72[0].status, ReminderStatus.SENT)
        self.assertEqual(due_72[0].feishu_message_id, "om_stage_sent")

    def test_targeted_claim_does_not_take_another_due_reminder(self) -> None:
        first_task = self._add_task(deadline=self.now + timedelta(days=3))
        second_task = self._add_task(deadline=self.now + timedelta(days=3))
        self.repository.sync_all(synced_at=self.now)
        with session_scope(self.session_factory) as session:
            future_id = session.scalar(
                select(TaskReminder.id).where(
                    TaskReminder.task_id == first_task,
                    TaskReminder.kind == ReminderKind.DUE_24H.value,
                )
            )

        lease = self.repository.claim_due(
            "worker", claimed_at=self.now, reminder_id=future_id
        )

        self.assertIsNone(lease)
        other_due = self.repository.claim_due(
            "worker", claimed_at=self.now
        )
        self.assertIn(other_due.task_id, {first_task, second_task})

    def test_claim_uses_newest_known_private_chat_for_owner(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_private_wang",
                    tenant_key="tenant_test",
                    name=None,
                    chat_type="p2p",
                )
            )
            session.flush()
            session.add(
                Message(
                    tenant_key="tenant_test",
                    event_id="evt_private",
                    message_id="om_private",
                    chat_id="oc_private_wang",
                    sender_open_id="ou_wang",
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
        self._add_task(deadline=self.now + timedelta(days=3))

        lease = self.repository.claim_due("worker", claimed_at=self.now)

        self.assertEqual(lease.owner_private_chat_id, "oc_private_wang")
        self.assertEqual(
            self.repository.find_private_chat_id("ou_wang"),
            "oc_private_wang",
        )

    def _add_task(self, *, deadline: datetime) -> int:
        with session_scope(self.session_factory) as session:
            task = Task(
                chat_id="oc_test",
                owner_open_id="ou_wang",
                owner_name_snapshot="王政",
                title=f"测试任务 {deadline.isoformat()} {id(deadline)}",
                normalized_title=f"测试任务 {deadline.isoformat()} {id(deadline)}",
                description="测试提醒发送",
                deadline=deadline,
                status="todo",
                confidence=0.95,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(task)
            session.flush()
            return task.id


if __name__ == "__main__":
    unittest.main()
