"""Phase 2A migration and persistence constraint tests."""

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import downgrade_database, upgrade_database
from app.database.models import (
    Chat,
    Message,
    Task,
    TaskLifecycleEvent,
    TaskReminder,
    User,
)


class DatabaseSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "messages.db"
        self.database_url = f"sqlite:///{database_path}"
        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        self.session_factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_migration_creates_expected_tables_and_constraints(self) -> None:
        inspector = inspect(self.engine)

        self.assertEqual(
            set(inspector.get_table_names()),
            {
                "alembic_version",
                "chat_administrator_events",
                "chat_administrators",
                "chat_setting_events",
                "chat_settings",
                "chat_member_aliases",
                "chat_memberships",
                "chats",
                "detection_jobs",
                "detection_materializations",
                "detection_run_focus_messages",
                "detection_runs",
                "messages",
                "task_evidence",
                "task_lifecycle_events",
                "task_lifecycle_evidence",
                "task_assignees",
                "task_creation_events",
                "task_notification_state",
                "task_notification_deferred_lifecycle_events",
                "task_notifications",
                "management_login_tokens",
                "management_sessions",
                "task_reminders",
                "task_sources",
                "tasks",
                "users",
            },
        )
        unique_constraints = {
            item["name"] for item in inspector.get_unique_constraints("messages")
        }
        self.assertIn("uq_messages_tenant_event", unique_constraints)
        self.assertIn("uq_messages_tenant_message", unique_constraints)
        indexes = {item["name"] for item in inspector.get_indexes("messages")}
        self.assertEqual(
            indexes,
            {"ix_messages_chat_created", "ix_messages_sender_created"},
        )
        alias_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("chat_member_aliases")
        }
        self.assertIn(
            "uq_chat_member_aliases_chat_normalized", alias_constraints
        )
        self.assertIn("uq_chat_member_aliases_chat_user", alias_constraints)
        alias_indexes = {
            item["name"]: item
            for item in inspector.get_indexes("chat_member_aliases")
        }
        self.assertEqual(
            set(alias_indexes),
            {"ix_chat_member_aliases_open_id"},
        )
        alias_columns = {
            item["name"] for item in inspector.get_columns("chat_member_aliases")
        }
        self.assertNotIn("is_primary", alias_columns)
        chat_setting_columns = {
            item["name"] for item in inspector.get_columns("chat_settings")
        }
        self.assertTrue(
            {
                "reminder_due_72h_enabled",
                "reminder_due_24h_enabled",
                "reminder_due_today_enabled",
                "reminder_overdue_enabled",
                "reminder_due_72h_offset_hours",
                "reminder_due_24h_offset_hours",
                "reminder_due_today_hour",
                "reminder_overdue_grace_minutes",
                "missing_deadline_owner_enabled",
                "missing_deadline_admin_enabled",
                "missing_deadline_owner_delay_hours",
                "missing_deadline_admin_delay_hours",
                "administrator_notification_mode",
                "administrator_notification_open_ids_json",
                "task_scope",
            }.issubset(chat_setting_columns)
        )
        self.assertIn(
            "ck_chat_settings_task_scope",
            {
                item["name"]
                for item in inspector.get_check_constraints("chat_settings")
            },
        )

        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "chat_memberships"
                )
            },
            {"uq_chat_memberships_chat_user"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "chat_memberships"
                )
            },
            {
                "ck_chat_memberships_active_state",
                "ck_chat_memberships_owner_active",
            },
        )
        self.assertEqual(
            set(
                inspector.get_pk_constraint(
                    "task_notification_deferred_lifecycle_events"
                )["constrained_columns"]
            ),
            {"event_id"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes("chat_memberships")
            },
            {
                "ix_chat_memberships_chat_active",
                "ix_chat_memberships_user_active",
            },
        )

        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "chat_administrators"
                )
            },
            {"uq_chat_administrators_chat_user"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "chat_administrators"
                )
            },
            {"ck_chat_administrators_source"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes("chat_administrators")
            },
            {"ix_chat_administrators_user_chat"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "chat_administrator_events"
                )
            },
            {
                "ck_chat_administrator_events_action",
                "ck_chat_administrator_events_source",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes(
                    "chat_administrator_events"
                )
            },
            {
                "ix_chat_administrator_events_chat_created",
                "ix_chat_administrator_events_target_created",
            },
        )

        job_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("detection_jobs")
        }
        self.assertEqual(
            job_constraints, {"uq_detection_jobs_chat_trigger"}
        )
        job_checks = {
            item["name"]
            for item in inspector.get_check_constraints("detection_jobs")
        }
        self.assertEqual(
            job_checks,
            {
                "ck_detection_jobs_attempt_count",
                "ck_detection_jobs_cancelled_state",
                "ck_detection_jobs_completed_state",
                "ck_detection_jobs_lease_state",
                "ck_detection_jobs_max_attempts",
                "ck_detection_jobs_status",
            },
        )
        job_indexes = {
            item["name"] for item in inspector.get_indexes("detection_jobs")
        }
        self.assertEqual(
            job_indexes,
            {"ix_detection_jobs_chat_status", "ix_detection_jobs_ready"},
        )

        run_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("detection_runs")
        }
        self.assertEqual(
            run_constraints, {"uq_detection_runs_job_attempt"}
        )
        run_checks = {
            item["name"]
            for item in inspector.get_check_constraints("detection_runs")
        }
        self.assertEqual(
            run_checks,
            {
                "ck_detection_runs_attempt",
                "ck_detection_runs_completion_tokens",
                "ck_detection_runs_finished_state",
                "ck_detection_runs_latency",
                "ck_detection_runs_prompt_tokens",
                "ck_detection_runs_result_state",
                "ck_detection_runs_status",
                "ck_detection_runs_total_tokens",
            },
        )
        run_indexes = {
            item["name"] for item in inspector.get_indexes("detection_runs")
        }
        self.assertEqual(run_indexes, {"ix_detection_runs_status_started"})

        focus_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints(
                "detection_run_focus_messages"
            )
        }
        self.assertEqual(
            focus_constraints, {"uq_detection_run_focus_position"}
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "detection_run_focus_messages"
                )
            },
            {"ck_detection_run_focus_position"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes(
                    "detection_run_focus_messages"
                )
            },
            {"ix_detection_run_focus_message"},
        )

        task_checks = {
            item["name"]
            for item in inspector.get_check_constraints("tasks")
        }
        self.assertEqual(
            task_checks,
            {
                "ck_tasks_cancelled_state",
                "ck_tasks_completed_state",
                "ck_tasks_confidence",
                "ck_tasks_status",
            },
        )
        task_indexes = {
            item["name"] for item in inspector.get_indexes("tasks")
        }
        self.assertEqual(
            task_indexes,
            {
                "ix_tasks_chat_status",
                "ix_tasks_deadline_status",
                "ix_tasks_dedupe_lookup",
                "ix_tasks_merged_into_task",
                "ix_tasks_owner_status",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "task_assignees"
                )
            },
            {
                "uq_task_assignees_task_position",
                "uq_task_assignees_task_user",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "task_assignees"
                )
            },
            {"ck_task_assignees_position"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes("task_assignees")
            },
            {"ix_task_assignees_user_task"},
        )
        evidence_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("task_evidence")
        }
        self.assertEqual(
            evidence_constraints, {"uq_task_evidence_task_message"}
        )
        self.assertEqual(
            {item["name"] for item in inspector.get_indexes("task_evidence")},
            {"ix_task_evidence_message"},
        )
        source_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("task_sources")
        }
        self.assertEqual(
            source_constraints, {"uq_task_sources_run_candidate"}
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints("task_sources")
            },
            {
                "ck_task_sources_candidate_index",
                "ck_task_sources_confidence",
            },
        )
        self.assertEqual(
            {item["name"] for item in inspector.get_indexes("task_sources")},
            {"ix_task_sources_task"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "task_creation_events"
                )
            },
            {
                "uq_task_creation_events_request",
                "uq_task_creation_events_task",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "task_creation_events"
                )
            },
            {"ck_task_creation_events_source"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes(
                    "task_creation_events"
                )
            },
            {"ix_task_creation_events_actor_created"},
        )
        materialization_checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "detection_materializations"
            )
        }
        self.assertEqual(
            materialization_checks,
            {
                "ck_detection_materializations_candidate_count",
                "ck_detection_materializations_counts",
            },
        )
        reminder_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("task_reminders")
        }
        self.assertEqual(
            reminder_constraints,
            {"uq_task_reminders_task_recipient_kind_deadline"},
        )
        reminder_checks = {
            item["name"]
            for item in inspector.get_check_constraints("task_reminders")
        }
        self.assertEqual(
            reminder_checks,
            {
                "ck_task_reminders_attempt_count",
                "ck_task_reminders_cancelled_state",
                "ck_task_reminders_delivery_receive_type",
                "ck_task_reminders_delivery_state",
                "ck_task_reminders_kind",
                "ck_task_reminders_lease_state",
                "ck_task_reminders_max_attempts",
                "ck_task_reminders_sent_state",
                "ck_task_reminders_status",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes("task_reminders")
            },
            {
                "ix_task_reminders_recipient_status",
                "ix_task_reminders_ready",
                "ix_task_reminders_task_status",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "task_notifications"
                )
            },
            {"uq_task_notifications_dedupe"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "task_notifications"
                )
            },
            {
                "ck_task_notifications_attempt_count",
                "ck_task_notifications_cancelled_state",
                "ck_task_notifications_delivery_receive_type",
                "ck_task_notifications_kind",
                "ck_task_notifications_lease_state",
                "ck_task_notifications_max_attempts",
                "ck_task_notifications_sent_state",
                "ck_task_notifications_status",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes("task_notifications")
            },
            {
                "ix_task_notifications_ready",
                "ix_task_notifications_recipient_status",
                "ix_task_notifications_task_status",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "task_notification_state"
                )
            },
            {
                "ck_task_notification_state_event_id",
                "ck_task_notification_state_singleton",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "task_lifecycle_events"
                )
            },
            {
                "uq_task_lifecycle_events_card_action",
                "uq_task_lifecycle_events_management_request",
                "uq_task_lifecycle_events_task_trigger",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "task_lifecycle_events"
                )
            },
            {
                "ck_task_lifecycle_events_action",
                "ck_task_lifecycle_events_authorization",
                "ck_task_lifecycle_events_correction_payload",
                "ck_task_lifecycle_events_confidence",
                "ck_task_lifecycle_events_new_status",
                "ck_task_lifecycle_events_outcome",
                "ck_task_lifecycle_events_previous_status",
                "ck_task_lifecycle_events_prompt_tokens",
                "ck_task_lifecycle_events_completion_tokens",
                "ck_task_lifecycle_events_total_tokens",
                "ck_task_lifecycle_events_trigger_source",
                "ck_task_lifecycle_events_merge_target",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes("task_lifecycle_events")
            },
            {
                "ix_task_lifecycle_events_actor_applied",
                "ix_task_lifecycle_events_task_applied",
                "ix_task_lifecycle_events_trigger",
            },
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "task_lifecycle_evidence"
                )
            },
            {"uq_task_lifecycle_evidence_position"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "task_lifecycle_evidence"
                )
            },
            {"ck_task_lifecycle_evidence_position"},
        )
        self.assertEqual(
            {
                item["name"]
                for item in inspector.get_indexes(
                    "task_lifecycle_evidence"
                )
            },
            {"ix_task_lifecycle_evidence_message"},
        )

    def test_lifecycle_migration_downgrade_preserves_tasks(self) -> None:
        self.engine.dispose()
        downgrade_database(self.database_url, "20260823_0009")
        self.engine = create_database_engine(self.database_url)
        tables = set(inspect(self.engine).get_table_names())

        self.assertNotIn("task_lifecycle_events", tables)
        self.assertNotIn("task_lifecycle_evidence", tables)
        self.assertIn("task_reminders", tables)
        self.assertIn("tasks", tables)

    def test_notification_migration_cursor_skips_historical_events(self) -> None:
        timestamp = datetime(
            2026, 8, 23, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        self.engine.dispose()
        downgrade_database(self.database_url, "20260823_0012")
        self.engine = create_database_engine(self.database_url)
        self.session_factory = create_session_factory(self.engine)
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_cursor",
                    tenant_key="tenant_test",
                    name="测试群",
                    chat_type="group",
                )
            )
            session.add(
                User(
                    open_id="ou_cursor",
                    name="王政",
                    tenant_key="tenant_test",
                    last_seen_at=timestamp,
                )
            )
            session.flush()
            task_id = session.execute(
                text(
                    "INSERT INTO tasks ("
                    "chat_id, owner_open_id, owner_name_snapshot, title, "
                    "normalized_title, description, deadline, status, "
                    "confidence, completed_at, created_at, updated_at) "
                    "VALUES ("
                    "'oc_cursor', 'ou_cursor', '王政', '历史任务', "
                    "'历史任务', '迁移前已完成', :timestamp, 'done', 0.95, "
                    ":timestamp, :timestamp, :timestamp) RETURNING id"
                ),
                {"timestamp": timestamp.isoformat()},
            ).scalar_one()
            event_id = session.execute(
                text(
                    "INSERT INTO task_lifecycle_events ("
                    "task_id, actor_open_id, trigger_source, "
                    "trigger_card_action_id, trigger_card_message_id, "
                    "trigger_card_chat_id, action, authorization_role, "
                    "task_code_snapshot, previous_status, new_status, "
                    "deadline_before, deadline_after, confidence, applied_at, "
                    "created_at) VALUES ("
                    ":task_id, 'ou_cursor', 'card_action', 'evt_historical', "
                    "'om_historical', 'oc_private', 'complete', 'owner', "
                    "'T-1A', 'todo', 'done', :timestamp, :timestamp, 1.0, "
                    ":timestamp, :timestamp) RETURNING id"
                ),
                {"task_id": task_id, "timestamp": timestamp.isoformat()},
            ).scalar_one()

        self.engine.dispose()
        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        with self.engine.connect() as connection:
            cursor = connection.execute(
                text(
                    "SELECT last_lifecycle_event_id "
                    "FROM task_notification_state WHERE id = 1"
                )
            ).scalar_one()
            notification_count = connection.execute(
                text("SELECT COUNT(*) FROM task_notifications")
            ).scalar_one()

        self.assertEqual(cursor, event_id)
        self.assertEqual(notification_count, 0)

    def test_shared_assignee_migration_backfills_tasks_and_reminders(self) -> None:
        self.engine.dispose()
        downgrade_database(self.database_url, "20260823_0013")
        engine = create_database_engine(self.database_url)
        timestamp = "2026-08-23 08:00:00"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats "
                    "(chat_id, tenant_key, name, chat_type, enabled, "
                    "created_at, updated_at) VALUES "
                    "('oc_shared_migration', 'tenant', '迁移群', "
                    "'group', 1, :timestamp, :timestamp)"
                ),
                {"timestamp": timestamp},
            )
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(open_id, name, tenant_key, last_seen_at, created_at, "
                    "updated_at) VALUES ('ou_shared_migration', '王政', "
                    "'tenant', :timestamp, :timestamp, :timestamp)"
                ),
                {"timestamp": timestamp},
            )
            connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(chat_id, owner_open_id, owner_name_snapshot, title, "
                    "normalized_title, description, deadline, status, "
                    "confidence, created_at, updated_at) VALUES "
                    "('oc_shared_migration', 'ou_shared_migration', '王政', "
                    "'历史任务', '历史任务', '迁移验证', :timestamp, "
                    "'todo', 0.95, :timestamp, :timestamp)"
                ),
                {"timestamp": timestamp},
            )
            task_id = connection.execute(
                text("SELECT id FROM tasks")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO task_reminders "
                    "(task_id, kind, deadline_snapshot, scheduled_for, "
                    "available_at, status, attempt_count, max_attempts, "
                    "created_at, updated_at) VALUES "
                    "(:task_id, 'due_24h', :timestamp, :timestamp, "
                    ":timestamp, 'scheduled', 0, 3, :timestamp, :timestamp)"
                ),
                {"task_id": task_id, "timestamp": timestamp},
            )
        engine.dispose()

        upgrade_database(self.database_url)
        engine = create_database_engine(self.database_url)
        with engine.connect() as connection:
            assignee = connection.execute(
                text(
                    "SELECT task_id, open_id, name_snapshot, position "
                    "FROM task_assignees"
                )
            ).one()
            reminder = connection.execute(
                text(
                    "SELECT recipient_open_id, recipient_name_snapshot "
                    "FROM task_reminders"
                )
            ).one()
        engine.dispose()
        self.engine = create_database_engine(self.database_url)
        self.session_factory = create_session_factory(self.engine)

        self.assertEqual(
            tuple(assignee),
            (task_id, "ou_shared_migration", "王政", 0),
        )
        self.assertEqual(
            tuple(reminder), ("ou_shared_migration", "王政")
        )

    def test_reminder_migration_downgrade_preserves_tasks(self) -> None:
        self.engine.dispose()
        downgrade_database(self.database_url, "20260822_0007")
        self.engine = create_database_engine(self.database_url)
        tables = set(inspect(self.engine).get_table_names())

        self.assertNotIn("task_reminders", tables)
        self.assertIn("tasks", tables)
        self.assertIn("detection_run_focus_messages", tables)

    def test_delivery_target_migration_backfills_legacy_sent_row(self) -> None:
        timestamp = datetime(
            2026, 8, 23, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_delivery_migration",
                    tenant_key="tenant_test",
                    name="迁移群",
                    chat_type="group",
                )
            )
            session.add(
                User(
                    open_id="ou_delivery_migration",
                    name="王政",
                    tenant_key="tenant_test",
                    last_seen_at=timestamp,
                )
            )
            task = Task(
                chat_id="oc_delivery_migration",
                owner_open_id="ou_delivery_migration",
                owner_name_snapshot="王政",
                title="迁移任务",
                normalized_title="迁移任务",
                description="验证旧发送记录",
                deadline=timestamp,
                status="overdue",
                confidence=0.95,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(task)
            session.flush()
            session.add(
                TaskReminder(
                    task_id=task.id,
                    recipient_open_id="ou_delivery_migration",
                    recipient_name_snapshot="王政",
                    kind="overdue",
                    deadline_snapshot=timestamp,
                    scheduled_for=timestamp,
                    available_at=timestamp,
                    status="sent",
                    attempt_count=1,
                    max_attempts=3,
                    sent_at=timestamp,
                    feishu_message_id="om_legacy_sent",
                    delivery_receive_id_type="open_id",
                    delivery_receive_id="ou_delivery_migration",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        self.engine.dispose()
        downgrade_database(self.database_url, "20260823_0008")
        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT delivery_receive_id_type, delivery_receive_id "
                    "FROM task_reminders "
                    "WHERE feishu_message_id = 'om_legacy_sent'"
                )
            ).one()

        self.assertEqual(row.delivery_receive_id_type, "chat_id")
        self.assertEqual(row.delivery_receive_id, "oc_delivery_migration")

    def test_one_name_migration_keeps_current_name_and_releases_old_one(self) -> None:
        self.engine.dispose()
        downgrade_database(self.database_url, "20260822_0002")
        self.engine = create_database_engine(self.database_url)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO chats (
                        chat_id, tenant_key, name, chat_type, enabled,
                        created_at, updated_at
                    ) VALUES (
                        'oc_legacy', 'tenant_test', '测试群', 'group', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO users (
                        open_id, union_id, name, tenant_key, last_seen_at,
                        created_at, updated_at
                    ) VALUES (
                        'ou_legacy', NULL, '飞书用户', 'tenant_test',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO chat_member_aliases (
                        chat_id, open_id, alias, normalized_alias, source,
                        confidence, is_primary, verified_at, created_at, updated_at
                    ) VALUES
                        ('oc_legacy', 'ou_legacy', '王政', '王政', 'manual',
                         1.0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                         CURRENT_TIMESTAMP),
                        ('oc_legacy', 'ou_legacy', '王哈', '王哈', 'self_command',
                         1.0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                         CURRENT_TIMESTAMP)
                    """
                )
            )
        self.engine.dispose()

        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT alias FROM chat_member_aliases "
                    "WHERE chat_id = 'oc_legacy' AND open_id = 'ou_legacy'"
                )
            ).scalars().all()

        self.assertEqual(rows, ["王哈"])

    def test_cancelled_job_downgrades_to_auditable_dead_job(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO chats (
                        chat_id, tenant_key, name, chat_type, enabled,
                        created_at, updated_at
                    ) VALUES (
                        'oc_cancel', 'tenant_test', '测试群', 'group', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO detection_jobs (
                        chat_id, trigger_message_id, status, priority,
                        attempt_count, max_attempts, available_at,
                        cancelled_at, cancel_reason, created_at, updated_at
                    ) VALUES (
                        'oc_cancel', 'om_cancel', 'cancelled', 0,
                        0, 3, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, 'test cleanup',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        self.engine.dispose()

        downgrade_database(self.database_url, "20260822_0004")
        self.engine = create_database_engine(self.database_url)
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status, last_error_code FROM detection_jobs "
                    "WHERE trigger_message_id = 'om_cancel'"
                )
            ).one()
            columns = {
                item["name"]
                for item in inspect(self.engine).get_columns("detection_jobs")
            }

        self.assertEqual(row.status, "dead")
        self.assertEqual(row.last_error_code, "cancelled_before_downgrade")
        self.assertNotIn("cancelled_at", columns)
        self.assertNotIn("cancel_reason", columns)

    def test_cancellation_migration_preserves_detection_runs_both_ways(self) -> None:
        self.engine.dispose()
        downgrade_database(self.database_url, "20260822_0004")
        self.engine = create_database_engine(self.database_url)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO chats (
                        chat_id, tenant_key, name, chat_type, enabled,
                        created_at, updated_at
                    ) VALUES (
                        'oc_audit', 'tenant_test', '审计群', 'group', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO detection_jobs (
                        id, chat_id, trigger_message_id, status, priority,
                        attempt_count, max_attempts, available_at, completed_at,
                        created_at, updated_at
                    ) VALUES (
                        81, 'oc_audit', 'om_audit', 'completed', 0,
                        1, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO detection_runs (
                        id, job_id, attempt, status, provider, model,
                        response_format, context_version, context_fingerprint,
                        context_message_ids_json, total_tokens, result_json,
                        latency_ms, started_at, finished_at
                    ) VALUES (
                        91, 81, 1, 'succeeded', 'openai_compatible',
                        'qwen-test', 'json_schema', '1.0',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        '["om_audit"]', 123, '{"candidates": []}', 500,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        self.engine.dispose()

        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        self._assert_audit_run_preserved()
        self.engine.dispose()

        downgrade_database(self.database_url, "20260822_0004")
        self.engine = create_database_engine(self.database_url)
        self._assert_audit_run_preserved()

    def test_task_migration_downgrade_preserves_messages_and_detection_runs(self) -> None:
        sent_at = datetime(2026, 8, 22, 10, 30, tzinfo=ZoneInfo("UTC"))
        self._insert_dependencies(sent_at)
        with session_scope(self.session_factory) as session:
            session.add(self._message("evt_task", "om_task", sent_at))
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO detection_jobs (
                        id, chat_id, trigger_message_id, status, priority,
                        attempt_count, max_attempts, available_at, completed_at,
                        created_at, updated_at
                    ) VALUES (
                        82, 'oc_test', 'om_task', 'completed', 0,
                        1, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO detection_runs (
                        id, job_id, attempt, status, provider, model,
                        response_format, context_version, context_fingerprint,
                        context_message_ids_json, total_tokens, result_json,
                        latency_ms, started_at, finished_at
                    ) VALUES (
                        92, 82, 1, 'succeeded', 'openai_compatible',
                        'qwen-test', 'json_schema', '1.0',
                        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                        '["om_task"]', 321, '{"candidates": []}', 400,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        self.engine.dispose()

        downgrade_database(self.database_url, "20260822_0005")
        self.engine = create_database_engine(self.database_url)
        inspector = inspect(self.engine)
        with self.engine.connect() as connection:
            message_count = connection.execute(
                text("SELECT COUNT(*) FROM messages WHERE message_id = 'om_task'")
            ).scalar_one()
            run = connection.execute(
                text(
                    "SELECT status, total_tokens, result_json "
                    "FROM detection_runs WHERE id = 92"
                )
            ).one()

        self.assertNotIn("tasks", inspector.get_table_names())
        self.assertNotIn("task_sources", inspector.get_table_names())
        self.assertEqual(message_count, 1)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.total_tokens, 321)
        self.assertEqual(json.loads(run.result_json), {"candidates": []})

    def test_sqlite_foreign_keys_are_enabled(self) -> None:
        with self.engine.connect() as connection:
            enabled = connection.execute(text("PRAGMA foreign_keys")).scalar_one()

        self.assertEqual(enabled, 1)

    def test_sqlite_uses_wal_and_busy_timeout(self) -> None:
        with self.engine.connect() as connection:
            journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
            busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

        self.assertEqual(journal_mode, "wal")
        self.assertEqual(busy_timeout, 5000)

    def test_persists_message_and_restores_aware_utc_time(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        sent_at = datetime(2026, 8, 22, 18, 30, tzinfo=shanghai)
        self._insert_dependencies(sent_at)

        with session_scope(self.session_factory) as session:
            session.add(self._message("evt_one", "om_one", sent_at))

        with session_scope(self.session_factory) as session:
            stored = session.query(Message).one()
            self.assertEqual(stored.text_content, "测试消息")
            self.assertIsNotNone(stored.message_created_at.tzinfo)
            self.assertEqual(stored.message_created_at.utcoffset().total_seconds(), 0)
            self.assertEqual(stored.message_created_at.hour, 10)

    def test_duplicate_message_id_in_same_tenant_is_rejected(self) -> None:
        sent_at = datetime(2026, 8, 22, 10, 30, tzinfo=ZoneInfo("UTC"))
        self._insert_dependencies(sent_at)
        with session_scope(self.session_factory) as session:
            session.add(self._message("evt_one", "om_same", sent_at))

        with self.assertRaises(IntegrityError):
            with session_scope(self.session_factory) as session:
                session.add(self._message("evt_two", "om_same", sent_at))

    def test_foreign_key_rejects_unknown_chat_and_sender(self) -> None:
        sent_at = datetime(2026, 8, 22, 10, 30, tzinfo=ZoneInfo("UTC"))

        with self.assertRaises(IntegrityError):
            with session_scope(self.session_factory) as session:
                session.add(self._message("evt_one", "om_one", sent_at))

    def test_naive_datetime_is_rejected(self) -> None:
        aware_time = datetime(2026, 8, 22, 10, 30, tzinfo=ZoneInfo("UTC"))
        self._insert_dependencies(aware_time)
        naive_time = datetime(2026, 8, 22, 10, 30)

        with self.assertRaises(StatementError):
            with session_scope(self.session_factory) as session:
                session.add(self._message("evt_one", "om_one", naive_time))

    def test_migration_can_downgrade_to_empty_schema(self) -> None:
        self.engine.dispose()
        downgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)

        self.assertEqual(inspect(self.engine).get_table_names(), ["alembic_version"])

    def _insert_dependencies(self, seen_at: datetime) -> None:
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_test",
                    tenant_key="tenant_test",
                    name="测试群",
                    chat_type="group",
                )
            )
            session.add(
                User(
                    open_id="ou_test",
                    union_id="on_test",
                    name="测试成员",
                    tenant_key="tenant_test",
                    last_seen_at=seen_at,
                )
            )

    def _assert_audit_run_preserved(self) -> None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, job_id, status, total_tokens, result_json "
                    "FROM detection_runs WHERE id = 91"
                )
            ).one()

        self.assertEqual(row.id, 91)
        self.assertEqual(row.job_id, 81)
        self.assertEqual(row.status, "succeeded")
        self.assertEqual(row.total_tokens, 123)
        self.assertEqual(json.loads(row.result_json), {"candidates": []})

    @staticmethod
    def _message(event_id: str, message_id: str, sent_at: datetime) -> Message:
        return Message(
            tenant_key="tenant_test",
            event_id=event_id,
            message_id=message_id,
            chat_id="oc_test",
            sender_open_id="ou_test",
            sender_name_snapshot="测试成员",
            message_type="text",
            text_content="测试消息",
            raw_content=json.dumps({"text": "测试消息"}, ensure_ascii=False),
            raw_event_json=json.dumps({"header": {"event_id": event_id}}),
            root_id=None,
            parent_id=None,
            message_created_at=sent_at,
            is_from_bot=False,
        )


if __name__ == "__main__":
    unittest.main()
