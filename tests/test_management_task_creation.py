"""Phase 7D-5A authorized management-page task creation tests."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import func, select

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
    ChatMemberAlias,
    ChatMembership,
    Task,
    TaskAssignee,
    TaskCreationEvent,
    TaskNotification,
    TaskReminder,
    User,
)
from app.tasks.manual_creation import (
    ManagementTaskCreationError,
    ManagementTaskCreationService,
)


class ManagementTaskCreationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "manual.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.service = ManagementTaskCreationService(
            self.session_factory,
            reminder_settings=ReminderSettings(),
        )
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Chat(
                        chat_id="oc_lab",
                        tenant_key="tenant",
                        name="实验群",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Chat(
                        chat_id="oc_other",
                        tenant_key="tenant",
                        name="其他群",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
            for open_id, name in (
                ("ou_admin", "导师"),
                ("ou_one", "王哈"),
                ("ou_two", "李明"),
                ("ou_unbound", "未绑定成员"),
                ("ou_departed", "已离群成员"),
                ("ou_other", "其他群成员"),
                ("ou_outsider", "普通成员"),
            ):
                session.add(
                    User(
                        open_id=open_id,
                        union_id=None,
                        name=name,
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
            session.flush()
            session.add(
                ChatAdministrator(
                    chat_id="oc_lab",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="bootstrap",
                    created_at=self.now,
                )
            )
            for open_id, name, active in (
                ("ou_admin", "导师", True),
                ("ou_one", "王哈", True),
                ("ou_two", "李明", True),
                ("ou_unbound", "未绑定成员", True),
                ("ou_departed", "已离群成员", False),
                ("ou_outsider", "普通成员", True),
            ):
                session.add(
                    ChatMembership(
                        chat_id="oc_lab",
                        open_id=open_id,
                        display_name_snapshot=name,
                        active=active,
                        is_owner=open_id == "ou_admin" and active,
                        first_synced_at=self.now,
                        last_synced_at=self.now,
                        left_at=None if active else self.now,
                    )
                )
            session.add(
                ChatMembership(
                    chat_id="oc_other",
                    open_id="ou_other",
                    display_name_snapshot="其他群成员",
                    active=True,
                    is_owner=False,
                    first_synced_at=self.now,
                    last_synced_at=self.now,
                    left_at=None,
                )
            )
            for chat_id, open_id, alias in (
                ("oc_lab", "ou_admin", "导师"),
                ("oc_lab", "ou_one", "王哈"),
                ("oc_lab", "ou_two", "李明"),
                ("oc_lab", "ou_departed", "已离群成员"),
                ("oc_lab", "ou_outsider", "普通成员"),
                ("oc_other", "ou_other", "其他群成员"),
            ):
                session.add(
                    ChatMemberAlias(
                        chat_id=chat_id,
                        open_id=open_id,
                        alias=alias,
                        normalized_alias=alias.casefold(),
                        source="self_command",
                        confidence=1.0,
                        verified_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_single_owner_creation_is_atomic_audited_and_idempotent(self) -> None:
        deadline = self.now + timedelta(days=10)
        result = self._create(
            request_id="manual-single",
            title="  完成   单人补建验收  ",
            description="验证后台手动补建",
            deadline=deadline,
            owner_open_ids=("ou_one",),
        )

        self.assertFalse(result.already_created)
        self.assertEqual(result.reminder_count, 4)
        self.assertEqual(result.notification_count, 1)
        with session_scope(self.session_factory) as session:
            task = session.get(Task, result.task_id)
            assert task is not None
            self.assertEqual(task.title, "完成 单人补建验收")
            self.assertEqual(task.status, "todo")
            self.assertEqual(task.confidence, 1.0)
            self.assertEqual(task.owner_open_id, "ou_one")
            self.assertEqual(task.owner_name_snapshot, "王哈")
            self.assertEqual(task.deadline, deadline)
            event = session.scalar(
                select(TaskCreationEvent).where(
                    TaskCreationEvent.task_id == task.id
                )
            )
            assert event is not None
            self.assertEqual(event.actor_open_id, "ou_admin")
            self.assertEqual(event.source, "management_page")
            self.assertEqual(
                json.loads(event.assignees_json),
                [{"name": "王哈", "open_id": "ou_one"}],
            )
            notification = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == task.id
                )
            )
            assert notification is not None
            self.assertEqual(notification.kind, "task_created_assignee")
            self.assertEqual(notification.recipient_open_id, "ou_one")
            self.assertEqual(notification.dedupe_key, "assignment:created")

        replay = self._create(
            request_id="manual-single",
            title="完成 单人补建验收",
            description="验证后台手动补建",
            deadline=deadline,
            owner_open_ids=("ou_one",),
        )
        self.assertTrue(replay.already_created)
        self.assertEqual(replay.task_id, result.task_id)
        self.assertEqual(replay.reminder_count, 0)
        self.assertEqual(replay.notification_count, 0)
        self.assertEqual(self._count(Task), 1)
        self.assertEqual(self._count(TaskCreationEvent), 1)
        self.assertEqual(self._count(TaskNotification), 1)
        self.assertEqual(self._count(TaskReminder), 4)

        with self.assertRaisesRegex(
            ManagementTaskCreationError, "different values"
        ):
            self._create(
                request_id="manual-single",
                title="被改动的标题",
                description="验证后台手动补建",
                deadline=deadline,
                owner_open_ids=("ou_one",),
            )

    def test_shared_task_creates_one_task_and_per_owner_delivery_plan(self) -> None:
        result = self._create(
            request_id="manual-shared",
            title="完成共享补建验收",
            description="",
            deadline=self.now + timedelta(days=10),
            owner_open_ids=("ou_two", "ou_one"),
        )

        self.assertEqual(result.notification_count, 2)
        self.assertEqual(result.reminder_count, 8)
        self.assertEqual(self._count(Task), 1)
        with session_scope(self.session_factory) as session:
            assignees = session.scalars(
                select(TaskAssignee).order_by(TaskAssignee.position)
            ).all()
            notifications = session.scalars(
                select(TaskNotification).order_by(
                    TaskNotification.recipient_open_id
                )
            ).all()
            reminders = session.scalars(select(TaskReminder)).all()
        self.assertEqual(
            [(item.open_id, item.name_snapshot) for item in assignees],
            [("ou_two", "李明"), ("ou_one", "王哈")],
        )
        self.assertEqual(
            {item.recipient_open_id for item in notifications},
            {"ou_one", "ou_two"},
        )
        self.assertEqual(len(reminders), 8)
        self.assertEqual(
            {item.recipient_open_id for item in reminders},
            {"ou_one", "ou_two"},
        )

    def test_no_deadline_has_assignment_notice_but_no_stage_reminders(self) -> None:
        result = self._create(
            request_id="manual-no-deadline",
            title="无截止时间任务",
            description="",
            deadline=None,
            owner_open_ids=("ou_one",),
        )

        self.assertEqual(result.notification_count, 1)
        self.assertEqual(result.reminder_count, 0)
        self.assertEqual(self._count(TaskNotification), 1)
        self.assertEqual(self._count(TaskReminder), 0)

    def test_creation_rejects_unauthorized_or_invalid_group_member(self) -> None:
        cases = (
            ("ou_unbound", "active member"),
            ("ou_departed", "active member"),
            ("ou_other", "active member"),
        )
        for index, (owner_open_id, message) in enumerate(cases):
            with self.subTest(owner_open_id=owner_open_id):
                with self.assertRaisesRegex(
                    ManagementTaskCreationError, message
                ):
                    self._create(
                        request_id=f"manual-invalid-{index}",
                        title="不会创建",
                        description="",
                        deadline=None,
                        owner_open_ids=(owner_open_id,),
                    )
        with self.assertRaisesRegex(
            ManagementTaskCreationError, "authorized administrator"
        ):
            self._create(
                actor_open_id="ou_outsider",
                request_id="manual-outsider",
                title="不会创建",
                description="",
                deadline=None,
                owner_open_ids=("ou_one",),
            )
        with self.assertRaisesRegex(
            ManagementTaskCreationError, "must be unique"
        ):
            self._create(
                request_id="manual-duplicate-owner",
                title="不会创建",
                description="",
                deadline=None,
                owner_open_ids=("ou_one", "ou_one"),
            )
        with self.assertRaisesRegex(
            ManagementTaskCreationError, "future"
        ):
            self._create(
                request_id="manual-past-deadline",
                title="不会创建",
                description="",
                deadline=self.now,
                owner_open_ids=("ou_one",),
            )
        self.assertEqual(self._count(Task), 0)
        self.assertEqual(self._count(TaskCreationEvent), 0)

    def _create(
        self,
        *,
        request_id: str,
        title: str,
        description: str,
        deadline: datetime | None,
        owner_open_ids: tuple[str, ...],
        actor_open_id: str = "ou_admin",
    ):
        return self.service.create(
            actor_open_id=actor_open_id,
            request_id=request_id,
            chat_id="oc_lab",
            title=title,
            description=description,
            deadline=deadline,
            owner_open_ids=owner_open_ids,
            created_at=self.now,
        )

    def _count(self, model: type[object]) -> int:
        with session_scope(self.session_factory) as session:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)


if __name__ == "__main__":
    unittest.main()
