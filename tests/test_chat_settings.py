"""Phase 7E-1 chat-scoped settings and audit tests."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import upgrade_database
from app.database.models import Chat, ChatAdministrator, User
from app.management.settings import (
    ChatSettingsError,
    ChatSettingsRepository,
    DEFAULT_AUTO_TODO_CONFIDENCE,
    DEFAULT_TIMEZONE,
)


class ChatSettingsRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "settings.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Chat(
                        chat_id="oc_a",
                        tenant_key="tenant",
                        name="群 A",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Chat(
                        chat_id="oc_b",
                        tenant_key="tenant",
                        name="群 B",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    User(
                        open_id="ou_admin_a",
                        union_id=None,
                        name="管理员 A",
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    User(
                        open_id="ou_admin_b",
                        union_id=None,
                        name="管理员 B",
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    User(
                        open_id="ou_member",
                        union_id=None,
                        name="普通成员",
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
            session.flush()
            session.add_all(
                (
                    ChatAdministrator(
                        chat_id="oc_a",
                        open_id="ou_admin_a",
                        granted_by_open_id=None,
                        source="bootstrap",
                        created_at=self.now,
                    ),
                    ChatAdministrator(
                        chat_id="oc_b",
                        open_id="ou_admin_b",
                        granted_by_open_id=None,
                        source="bootstrap",
                        created_at=self.now,
                    ),
                    ChatAdministrator(
                        chat_id="oc_a",
                        open_id="ou_admin_b",
                        granted_by_open_id="ou_admin_a",
                        source="bootstrap",
                        created_at=self.now,
                    ),
                )
            )
        self.repository = ChatSettingsRepository(self.session_factory)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_defaults_are_materialized_lazily_per_chat(self) -> None:
        settings = self.repository.get_for_administrator("ou_admin_a", "oc_a")

        self.assertTrue(settings.detection_enabled)
        self.assertEqual(settings.auto_todo_confidence, DEFAULT_AUTO_TODO_CONFIDENCE)
        self.assertEqual(settings.task_scope, "broad")
        self.assertEqual(settings.timezone, DEFAULT_TIMEZONE)
        self.assertTrue(settings.reminder_due_72h_enabled)
        self.assertTrue(settings.reminder_due_24h_enabled)
        self.assertTrue(settings.reminder_due_today_enabled)
        self.assertTrue(settings.reminder_overdue_enabled)
        self.assertEqual(settings.reminder_due_72h_offset_hours, 72)
        self.assertEqual(settings.reminder_due_24h_offset_hours, 24)
        self.assertEqual(settings.reminder_due_today_hour, 9)
        self.assertEqual(settings.reminder_overdue_grace_minutes, 1)
        self.assertTrue(settings.missing_deadline_owner_enabled)
        self.assertTrue(settings.missing_deadline_admin_enabled)
        self.assertEqual(settings.missing_deadline_owner_delay_hours, 24)
        self.assertEqual(settings.missing_deadline_admin_delay_hours, 72)
        self.assertEqual(settings.administrator_notification_mode, "all")
        self.assertEqual(settings.administrator_notification_open_ids, ())
        self.assertIsNone(settings.updated_at)
        self.assertTrue(self.repository.detection_enabled("oc_a"))
        self.assertEqual(self.repository.task_scope("oc_a"), "broad")

    def test_update_requires_group_admin_and_records_audit(self) -> None:
        updated = self.repository.update_for_administrator(
            "ou_admin_a",
            "oc_a",
            detection_enabled=False,
            auto_todo_confidence=0.95,
            task_scope="work_only",
            reminder_due_72h_enabled=False,
            reminder_overdue_enabled=False,
            reminder_due_72h_offset_hours=96,
            reminder_due_24h_offset_hours=36,
            reminder_due_today_hour=8,
            reminder_overdue_grace_minutes=15,
            missing_deadline_owner_enabled=False,
            missing_deadline_admin_enabled=True,
            missing_deadline_owner_delay_hours=12,
            missing_deadline_admin_delay_hours=48,
            updated_at=self.now,
        )

        self.assertFalse(updated.detection_enabled)
        self.assertEqual(updated.auto_todo_confidence, 0.95)
        self.assertEqual(updated.task_scope, "work_only")
        self.assertFalse(updated.reminder_due_72h_enabled)
        self.assertTrue(updated.reminder_due_24h_enabled)
        self.assertTrue(updated.reminder_due_today_enabled)
        self.assertFalse(updated.reminder_overdue_enabled)
        self.assertEqual(updated.reminder_due_72h_offset_hours, 96)
        self.assertEqual(updated.reminder_due_24h_offset_hours, 36)
        self.assertEqual(updated.reminder_due_today_hour, 8)
        self.assertEqual(updated.reminder_overdue_grace_minutes, 15)
        self.assertFalse(updated.missing_deadline_owner_enabled)
        self.assertTrue(updated.missing_deadline_admin_enabled)
        self.assertEqual(updated.missing_deadline_owner_delay_hours, 12)
        self.assertEqual(updated.missing_deadline_admin_delay_hours, 48)
        self.assertEqual(updated.updated_by_open_id, "ou_admin_a")
        events = self.repository.list_events_for_administrator("ou_admin_a", "oc_a")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor_open_id, "ou_admin_a")
        self.assertEqual(events[0].changed_fields["before"]["auto_todo_confidence"], 0.85)
        self.assertEqual(events[0].changed_fields["after"]["auto_todo_confidence"], 0.95)
        self.assertEqual(events[0].changed_fields["before"]["task_scope"], "broad")
        self.assertEqual(events[0].changed_fields["after"]["task_scope"], "work_only")
        self.assertTrue(
            events[0].changed_fields["before"]["reminder_due_72h_enabled"]
        )
        self.assertFalse(
            events[0].changed_fields["after"]["reminder_due_72h_enabled"]
        )
        self.assertEqual(
            events[0].changed_fields["after"][
                "reminder_due_72h_offset_hours"
            ],
            96,
        )
        self.assertEqual(
            events[0].changed_fields["after"][
                "missing_deadline_admin_delay_hours"
            ],
            48,
        )

        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_member", "oc_a", detection_enabled=True
            )

    def test_settings_are_isolated_between_groups_and_validate_input(self) -> None:
        self.repository.update_for_administrator(
            "ou_admin_a",
            "oc_a",
            auto_todo_confidence=0.9,
            task_scope="work_only",
            reminder_due_24h_enabled=False,
            reminder_due_today_hour=7,
            missing_deadline_owner_delay_hours=18,
            missing_deadline_admin_delay_hours=60,
        )

        group_a = self.repository.get("oc_a")
        group_b = self.repository.get_for_administrator("ou_admin_b", "oc_b")
        self.assertEqual(group_a.auto_todo_confidence, 0.9)
        self.assertEqual(group_a.task_scope, "work_only")
        self.assertEqual(group_b.task_scope, "broad")
        self.assertEqual(group_b.auto_todo_confidence, DEFAULT_AUTO_TODO_CONFIDENCE)
        self.assertTrue(group_b.detection_enabled)
        self.assertFalse(group_a.reminder_due_24h_enabled)
        self.assertTrue(group_b.reminder_due_24h_enabled)
        self.assertTrue(group_b.reminder_due_72h_enabled)
        self.assertTrue(group_b.reminder_due_today_enabled)
        self.assertTrue(group_b.reminder_overdue_enabled)
        self.assertEqual(group_a.reminder_due_today_hour, 7)
        self.assertEqual(group_b.reminder_due_72h_offset_hours, 72)
        self.assertEqual(group_b.reminder_due_24h_offset_hours, 24)
        self.assertEqual(group_b.reminder_due_today_hour, 9)
        self.assertEqual(group_b.reminder_overdue_grace_minutes, 1)
        self.assertEqual(group_a.missing_deadline_owner_delay_hours, 18)
        self.assertEqual(group_a.missing_deadline_admin_delay_hours, 60)
        self.assertTrue(group_b.missing_deadline_owner_enabled)
        self.assertTrue(group_b.missing_deadline_admin_enabled)
        self.assertEqual(group_b.missing_deadline_owner_delay_hours, 24)
        self.assertEqual(group_b.missing_deadline_admin_delay_hours, 72)
        self.assertEqual(self.repository.auto_todo_confidence("oc_b"), 0.85)

        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", auto_todo_confidence=1.01
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", auto_todo_confidence=True
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", task_scope="personal_only"
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", reminder_due_today_enabled="yes"
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", reminder_due_today_hour=True
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", reminder_overdue_grace_minutes=1_441
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a",
                "oc_a",
                reminder_due_72h_offset_hours=24,
                reminder_due_24h_offset_hours=48,
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", missing_deadline_owner_enabled="yes"
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a", "oc_a", missing_deadline_owner_delay_hours=0
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator(
                "ou_admin_a",
                "oc_a",
                missing_deadline_owner_delay_hours=72,
                missing_deadline_admin_delay_hours=48,
            )
        with self.assertRaises(ChatSettingsError):
            self.repository.update_for_administrator("ou_admin_a", "oc_a")
        with self.assertRaises(ChatSettingsError):
            self.repository.get_for_administrator("ou_admin_a", "oc_b")

    def test_selected_administrator_notification_recipients_are_audited(self) -> None:
        updated = self.repository.update_for_administrator(
            "ou_admin_a",
            "oc_a",
            administrator_notification_mode="selected",
            administrator_notification_open_ids=(
                "ou_admin_a",
                "ou_admin_b",
            ),
            updated_at=self.now,
        )

        self.assertEqual(updated.administrator_notification_mode, "selected")
        self.assertEqual(
            updated.administrator_notification_open_ids,
            ("ou_admin_a", "ou_admin_b"),
        )
        group_b = self.repository.get_for_administrator("ou_admin_b", "oc_b")
        self.assertEqual(group_b.administrator_notification_mode, "all")
        self.assertEqual(group_b.administrator_notification_open_ids, ())
        event = self.repository.list_events_for_administrator(
            "ou_admin_a", "oc_a"
        )[0]
        self.assertEqual(
            event.changed_fields["after"][
                "administrator_notification_open_ids"
            ],
            ["ou_admin_a", "ou_admin_b"],
        )

        restored = self.repository.update_for_administrator(
            "ou_admin_a",
            "oc_a",
            administrator_notification_mode="all",
            administrator_notification_open_ids=(),
            updated_at=self.now,
        )
        self.assertEqual(restored.administrator_notification_mode, "all")
        self.assertEqual(restored.administrator_notification_open_ids, ())

    def test_selected_notification_recipients_must_be_current_administrators(self) -> None:
        invalid_updates = (
            {
                "administrator_notification_mode": "selected",
                "administrator_notification_open_ids": (),
            },
            {
                "administrator_notification_mode": "selected",
                "administrator_notification_open_ids": ("ou_member",),
            },
            {
                "administrator_notification_mode": "all",
                "administrator_notification_open_ids": ("ou_admin_a",),
            },
            {
                "administrator_notification_mode": "invalid",
                "administrator_notification_open_ids": (),
            },
        )
        for update in invalid_updates:
            with self.subTest(update=update), self.assertRaises(
                ChatSettingsError
            ):
                self.repository.update_for_administrator(
                    "ou_admin_a", "oc_a", **update
                )


if __name__ == "__main__":
    unittest.main()
