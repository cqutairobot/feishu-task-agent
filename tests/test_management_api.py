"""Phase 7A chat-scoped administration and read-only query tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
    ChatAdministratorEvent,
    ChatMemberAlias,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskEvidence,
    User,
)
from app.management.access import (
    AdministratorSource,
    ChatAdministratorError,
    ChatAdministratorRepository,
)
from app.management.queries import (
    ManagementAccessDenied,
    ManagementQueryError,
    ManagementReadApi,
)
from app.tasks.codes import format_task_code


class ManagementReadApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "management.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.administrators = ChatAdministratorRepository(self.session_factory)
        self.api = ManagementReadApi(self.session_factory)
        self.now = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)
        self._seed()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_grant_is_chat_scoped_idempotent_and_audited(self) -> None:
        first = self.administrators.grant(
            "oc_a",
            "ou_admin",
            source=AdministratorSource.BOOTSTRAP,
            granted_at=self.now,
        )
        repeated = self.administrators.grant(
            "oc_a", "ou_admin", granted_at=self.now + timedelta(minutes=1)
        )

        self.assertTrue(first.changed)
        self.assertFalse(repeated.changed)
        self.assertTrue(self.administrators.is_administrator("oc_a", "ou_admin"))
        self.assertFalse(self.administrators.is_administrator("oc_b", "ou_admin"))
        self.assertEqual(
            self.administrators.chat_ids_for_administrator("ou_admin"),
            {"oc_a"},
        )
        self.assertEqual(self.administrators.managed_chat_ids(), {"oc_a"})
        self.assertEqual(
            self.administrators.admitted_chat_ids(frozenset({"oc_static"})),
            {"oc_static", "oc_a"},
        )
        self.assertIsNone(
            self.administrators.admitted_chat_ids(frozenset())
        )
        with session_scope(self.session_factory) as session:
            self.assertEqual(
                session.scalar(select(func.count(ChatAdministrator.id))), 1
            )
            self.assertEqual(
                session.scalar(select(func.count(ChatAdministratorEvent.id))), 1
            )

    def test_grant_requires_current_membership_in_exact_group(self) -> None:
        with self.assertRaisesRegex(ChatAdministratorError, "current verified"):
            self.administrators.grant("oc_a", "ou_other", granted_at=self.now)
        with self.assertRaisesRegex(ChatAdministratorError, "enabled group"):
            self.administrators.grant("oc_direct", "ou_admin", granted_at=self.now)

    def test_grant_does_not_require_task_alias(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add(
                User(
                    open_id="ou_aliasless",
                    union_id=None,
                    name="未绑定成员",
                    tenant_key="tenant",
                    last_seen_at=self.now,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.flush()
            session.add(
                self._membership(
                    "oc_a", "ou_aliasless", "未绑定成员"
                )
            )

        result = self.administrators.grant(
            "oc_a", "ou_aliasless", granted_at=self.now
        )

        self.assertTrue(result.changed)
        assert result.administrator is not None
        self.assertEqual(result.administrator.name, "未绑定成员")

    def test_revoke_removes_access_and_preserves_audit(self) -> None:
        self._grant_admin()
        with session_scope(self.session_factory) as session:
            session.add(
                ChatAdministrator(
                    chat_id="oc_a",
                    open_id="ou_owner",
                    granted_by_open_id=None,
                    source="bootstrap",
                    created_at=self.now,
                )
            )

        result = self.administrators.revoke(
            "oc_a", "ou_admin", revoked_at=self.now + timedelta(minutes=1)
        )
        repeated = self.administrators.revoke(
            "oc_a", "ou_admin", revoked_at=self.now + timedelta(minutes=2)
        )

        self.assertTrue(result.changed)
        self.assertFalse(repeated.changed)
        self.assertFalse(self.administrators.is_administrator("oc_a", "ou_admin"))
        with session_scope(self.session_factory) as session:
            actions = tuple(
                session.scalars(
                    select(ChatAdministratorEvent.action).order_by(
                        ChatAdministratorEvent.id
                    )
                )
            )
        self.assertEqual(actions, ("grant", "revoke"))

    def test_last_administrator_cannot_be_removed_manually(self) -> None:
        self._grant_admin()

        with self.assertRaisesRegex(ChatAdministratorError, "last administrator"):
            self.administrators.revoke(
                "oc_a", "ou_admin", revoked_at=self.now
            )

        self.assertTrue(
            self.administrators.is_administrator("oc_a", "ou_admin")
        )

    def test_list_chats_returns_only_actor_administered_groups(self) -> None:
        self._grant_admin()
        self.administrators.grant("oc_b", "ou_other", granted_at=self.now)

        chats = self.api.list_chats("ou_admin")

        self.assertEqual([item.chat_id for item in chats], ["oc_a"])
        self.assertEqual(chats[0].chat_name, "实验 A")
        self.assertEqual(chats[0].open_task_count, 3)

    def test_dashboard_counts_are_isolated_to_requested_chat(self) -> None:
        self._grant_admin()

        dashboard = self.api.dashboard("ou_admin", "oc_a", now=self.now)

        self.assertEqual(dashboard.total_task_count, 4)
        self.assertEqual(dashboard.todo_count, 2)
        self.assertEqual(dashboard.overdue_count, 1)
        self.assertEqual(dashboard.done_count, 1)
        self.assertEqual(dashboard.open_without_deadline_count, 1)
        self.assertEqual(dashboard.due_next_7_days_count, 1)
        self.assertEqual(dashboard.member_count, 2)
        self.assertEqual(dashboard.administrator_count, 1)

    def test_member_and_administrator_audit_views_are_chat_scoped(self) -> None:
        self._grant_admin()

        members = self.api.list_members("ou_admin", "oc_a")
        events = self.api.list_administrator_events("ou_admin", "oc_a")

        self.assertEqual([item.name for item in members], ["莉莉", "王政"])
        self.assertEqual(
            [item.feishu_name for item in members], ["莉莉", "王政"]
        )
        self.assertTrue(members[0].is_owner)
        self.assertTrue(members[0].is_administrator)
        self.assertEqual(members[0].task_alias, "莉莉")
        self.assertFalse(members[1].is_administrator)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].target_name, "莉莉")
        self.assertEqual(events[0].source, "bootstrap")

        with self.assertRaises(ManagementAccessDenied):
            self.api.list_members("ou_other", "oc_a")

    def test_task_list_supports_status_owner_search_and_deadline_filters(self) -> None:
        self._grant_admin()

        todo = self.api.list_tasks(
            "ou_admin", "oc_a", statuses=("todo",), limit=10
        )
        owned = self.api.list_tasks(
            "ou_admin", "oc_a", owner_open_id="ou_owner", limit=10
        )
        searched = self.api.list_tasks(
            "ou_admin", "oc_a", query="前端", limit=10
        )
        searched_by_code = self.api.list_tasks(
            "ou_admin", "oc_a", query=format_task_code(self.task_a_id), limit=10
        )
        searched_by_short_code = self.api.list_tasks(
            "ou_admin", "oc_a", query="1A", limit=10
        )
        cross_chat_code = self.api.list_tasks(
            "ou_admin", "oc_a", query=format_task_code(self.task_b_id), limit=10
        )
        missing = self.api.list_tasks(
            "ou_admin", "oc_a", missing_deadline=True, limit=10
        )
        due = self.api.list_tasks(
            "ou_admin",
            "oc_a",
            deadline_before=self.now + timedelta(days=3),
            limit=10,
        )
        first_page = self.api.list_tasks(
            "ou_admin", "oc_a", limit=2, offset=0
        )
        second_page = self.api.list_tasks(
            "ou_admin", "oc_a", limit=2, offset=2
        )

        self.assertEqual(todo.total_count, 2)
        self.assertEqual(todo.total_pages, 1)
        self.assertEqual(owned.total_count, 4)
        self.assertEqual([item.title for item in searched.tasks], ["完成前端页面"])
        self.assertEqual([item.task_id for item in searched_by_code.tasks], [self.task_a_id])
        self.assertEqual([item.task_id for item in searched_by_short_code.tasks], [self.task_a_id])
        self.assertEqual(cross_chat_code.total_count, 0)
        self.assertEqual(first_page.total_count, 4)
        self.assertEqual(first_page.total_pages, 2)
        self.assertEqual(first_page.page, 1)
        self.assertEqual(first_page.offset, 0)
        self.assertEqual(len(first_page.tasks), 2)
        self.assertEqual(second_page.offset, 2)
        self.assertEqual(second_page.page, 2)
        self.assertEqual(len(second_page.tasks), 2)
        self.assertEqual([item.title for item in missing.tasks], ["整理无期限资料"])
        self.assertEqual(
            {item.title for item in due.tasks},
            {"已逾期实验", "完成前端页面", "已完成事项"},
        )

    def test_task_detail_contains_evidence_but_not_another_chat(self) -> None:
        self._grant_admin()

        detail = self.api.task_detail("ou_admin", "oc_a", self.task_a_id)

        self.assertEqual(detail.task.title, "完成前端页面")
        self.assertEqual(detail.evidence[0].message_id, "om_evidence_a")
        self.assertEqual(detail.evidence[0].content, "王政完成前端页面")
        self.assertEqual(detail.lifecycle, ())
        self.assertEqual(detail.deliveries, ())
        with self.assertRaisesRegex(ManagementQueryError, "does not exist"):
            self.api.task_detail("ou_admin", "oc_a", self.task_b_id)

    def test_unauthorized_reads_fail_before_resource_lookup(self) -> None:
        self._grant_admin()

        with self.assertRaisesRegex(
            ManagementAccessDenied, "not authorized for this chat"
        ):
            self.api.dashboard("ou_admin", "oc_b", now=self.now)
        with self.assertRaisesRegex(
            ManagementAccessDenied, "not authorized for this chat"
        ):
            self.api.task_detail("ou_other", "oc_a", 999999)

    def test_invalid_filters_are_rejected(self) -> None:
        self._grant_admin()

        with self.assertRaisesRegex(ManagementQueryError, "status"):
            self.api.list_tasks(
                "ou_admin", "oc_a", statuses=("secret",), limit=10
            )
        with self.assertRaisesRegex(ManagementQueryError, "timezone-aware"):
            self.api.list_tasks(
                "ou_admin",
                "oc_a",
                deadline_before=datetime(2026, 8, 30),
                limit=10,
            )

    def _grant_admin(self) -> None:
        self.administrators.grant(
            "oc_a",
            "ou_admin",
            source=AdministratorSource.BOOTSTRAP,
            granted_at=self.now,
        )

    def _seed(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Chat(
                        chat_id="oc_a",
                        tenant_key="tenant",
                        name="实验 A",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Chat(
                        chat_id="oc_b",
                        tenant_key="tenant",
                        name="实验 B",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Chat(
                        chat_id="oc_direct",
                        tenant_key="tenant",
                        name=None,
                        chat_type="p2p",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
            for open_id, name in (
                ("ou_admin", "莉莉"),
                ("ou_owner", "王政"),
                ("ou_other", "田野"),
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
            session.add_all(
                (
                    self._alias("oc_a", "ou_admin", "莉莉"),
                    self._alias("oc_a", "ou_owner", "王政"),
                    self._alias("oc_b", "ou_other", "田野"),
                    self._alias("oc_b", "ou_owner", "王政"),
                )
            )
            session.add_all(
                (
                    self._membership("oc_a", "ou_admin", "莉莉", owner=True),
                    self._membership("oc_a", "ou_owner", "王政"),
                    self._membership("oc_b", "ou_other", "田野", owner=True),
                    self._membership("oc_b", "ou_owner", "王政"),
                )
            )
            task_a = self._task(
                "oc_a",
                "完成前端页面",
                "todo",
                self.now + timedelta(days=2),
            )
            task_overdue = self._task(
                "oc_a",
                "已逾期实验",
                "overdue",
                self.now - timedelta(days=1),
            )
            task_open = self._task(
                "oc_a", "整理无期限资料", "todo", None
            )
            task_done = self._task(
                "oc_a",
                "已完成事项",
                "done",
                self.now + timedelta(days=1),
            )
            task_done.completed_at = self.now
            task_b = self._task(
                "oc_b",
                "另一个群的秘密任务",
                "todo",
                self.now + timedelta(days=2),
            )
            session.add_all((task_a, task_overdue, task_open, task_done, task_b))
            session.flush()
            for task in (task_a, task_overdue, task_open, task_done, task_b):
                session.add(
                    TaskAssignee(
                        task_id=task.id,
                        open_id="ou_owner",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    )
                )
            evidence = Message(
                tenant_key="tenant",
                event_id="ev_evidence_a",
                message_id="om_evidence_a",
                chat_id="oc_a",
                sender_open_id="ou_admin",
                sender_name_snapshot="莉莉",
                message_type="text",
                text_content="王政完成前端页面",
                raw_content='{"text":"王政完成前端页面"}',
                raw_event_json="{}",
                root_id=None,
                parent_id=None,
                message_created_at=self.now,
                received_at=self.now,
                is_from_bot=False,
            )
            session.add(evidence)
            session.flush()
            session.add(
                TaskEvidence(
                    task_id=task_a.id,
                    message_db_id=evidence.id,
                    created_at=self.now,
                )
            )
            self.task_a_id = task_a.id
            self.task_b_id = task_b.id

    def _alias(
        self, chat_id: str, open_id: str, alias: str
    ) -> ChatMemberAlias:
        return ChatMemberAlias(
            chat_id=chat_id,
            open_id=open_id,
            alias=alias,
            normalized_alias=alias,
            source="self_command",
            confidence=1.0,
            verified_at=self.now,
            created_at=self.now,
            updated_at=self.now,
        )

    def _membership(
        self,
        chat_id: str,
        open_id: str,
        name: str,
        *,
        owner: bool = False,
    ) -> ChatMembership:
        return ChatMembership(
            chat_id=chat_id,
            open_id=open_id,
            display_name_snapshot=name,
            active=True,
            is_owner=owner,
            first_synced_at=self.now,
            last_synced_at=self.now,
            left_at=None,
        )

    def _task(
        self,
        chat_id: str,
        title: str,
        status: str,
        deadline: datetime | None,
    ) -> Task:
        return Task(
            chat_id=chat_id,
            owner_open_id="ou_owner",
            owner_name_snapshot="王政",
            title=title,
            normalized_title=title,
            description=f"{title}的详细说明",
            deadline=deadline,
            status=status,
            confidence=0.96,
            completed_at=None,
            cancelled_at=None,
            created_at=self.now - timedelta(days=1),
            updated_at=self.now,
        )
