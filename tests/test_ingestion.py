"""Phase 2B simulated-event ingestion tests."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from sqlalchemy import event, func, select

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
    ManagementSession,
    Message,
    User,
)
from app.database.repository import MessageRepository, SaveStatus
from app.ingestion.service import MessageIngestionService
from tests.test_messages import TEXT_EVENT


class MessageIngestionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "ingestion.db"
        self.database_url = f"sqlite:///{database_path}"
        upgrade_database(self.database_url)
        self._open_runtime()
        self.received_at = datetime(
            2026, 8, 22, 18, 32, tzinfo=ZoneInfo("Asia/Shanghai")
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_simulated_event_is_persisted(self) -> None:
        outcome = self.service.process_payload(
            TEXT_EVENT, received_at=self.received_at
        )

        self.assertEqual(outcome.persistence.status, SaveStatus.INSERTED)
        self.assertEqual(self.repository.count(), 1)
        stored = self.repository.list_recent("oc_test")[0]
        self.assertEqual(stored.message_id, "om_test")
        self.assertEqual(stored.text_content, "今天实验结果出来了吗？")
        self.assertEqual(stored.sender_open_id, "ou_test")
        self.assertNotIn("event-token-must-not-be-persisted", stored.raw_event_json)

    def test_replayed_event_is_reported_as_duplicate(self) -> None:
        first = self.service.process_payload(TEXT_EVENT, received_at=self.received_at)
        second = self.service.process_payload(TEXT_EVENT, received_at=self.received_at)

        self.assertEqual(first.persistence.status, SaveStatus.INSERTED)
        self.assertEqual(second.persistence.status, SaveStatus.DUPLICATE)
        self.assertEqual(self.repository.count(), 1)

    def test_duplicate_message_id_with_new_event_id_is_ignored(self) -> None:
        repeated_message = deepcopy(TEXT_EVENT)
        repeated_message["header"]["event_id"] = "evt_second_delivery"

        self.service.process_payload(TEXT_EVENT, received_at=self.received_at)
        duplicate = self.service.process_payload(
            repeated_message, received_at=self.received_at
        )

        self.assertEqual(duplicate.persistence.status, SaveStatus.DUPLICATE)
        self.assertEqual(self.repository.count(), 1)

    def test_duplicate_event_id_with_new_message_id_is_ignored(self) -> None:
        repeated_event = deepcopy(TEXT_EVENT)
        repeated_event["event"]["message"]["message_id"] = "om_second"

        self.service.process_payload(TEXT_EVENT, received_at=self.received_at)
        duplicate = self.service.process_payload(
            repeated_event, received_at=self.received_at
        )

        self.assertEqual(duplicate.persistence.status, SaveStatus.DUPLICATE)
        self.assertEqual(self.repository.count(), 1)

    def test_data_survives_engine_restart(self) -> None:
        self.service.process_payload(TEXT_EVENT, received_at=self.received_at)
        self.engine.dispose()

        self._open_runtime()

        self.assertEqual(self.repository.count(), 1)
        self.assertEqual(
            self.repository.list_recent("oc_test")[0].message_id, "om_test"
        )

    def test_failure_during_message_insert_rolls_back_chat_and_user(self) -> None:
        def fail_message_insert(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith("INSERT INTO MESSAGES"):
                raise RuntimeError("simulated insert failure")

        event.listen(self.engine, "before_cursor_execute", fail_message_insert)
        try:
            with self.assertRaisesRegex(RuntimeError, "simulated insert failure"):
                self.service.process_payload(
                    TEXT_EVENT, received_at=self.received_at
                )
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_message_insert)

        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count(Chat.chat_id))), 0)
            self.assertEqual(session.scalar(select(func.count(User.open_id))), 0)
            self.assertEqual(session.scalar(select(func.count(Message.id))), 0)

    def test_recent_messages_are_returned_in_conversation_order(self) -> None:
        later = deepcopy(TEXT_EVENT)
        later["header"]["event_id"] = "evt_later"
        later["event"]["message"]["message_id"] = "om_later"
        later["event"]["message"]["create_time"] = "1787383990000"
        later["event"]["message"]["content"] = '{"text":"第二条"}'

        self.service.process_payload(later, received_at=self.received_at)
        self.service.process_payload(TEXT_EVENT, received_at=self.received_at)

        messages = self.repository.list_recent("oc_test")
        self.assertEqual(
            [message.message_id for message in messages],
            ["om_test", "om_later"],
        )

    def test_directory_snapshot_backfills_names_and_conversation(self) -> None:
        self.service.process_payload(TEXT_EVENT, received_at=self.received_at)

        result = self.repository.apply_directory_snapshot(
            "oc_test",
            "实验群",
            {"ou_test": "张三"},
            updated_at=self.received_at,
        )
        conversation = self.repository.conversation("oc_test")

        self.assertEqual(result.chats_updated, 1)
        self.assertEqual(result.users_updated, 1)
        self.assertEqual(result.message_snapshots_updated, 1)
        self.assertEqual(conversation[0].sender_name, "张三")
        self.assertEqual(conversation[0].content, "今天实验结果出来了吗？")

    def test_authoritative_directory_snapshot_tracks_owner_and_departures(self) -> None:
        first = self.repository.apply_directory_snapshot(
            "oc_new",
            "新实验群",
            {"ou_owner": "导师", "ou_member": "王政"},
            owner_open_id="ou_owner",
            member_tenant_keys={
                "ou_owner": "tenant_test",
                "ou_member": "tenant_test",
            },
            authoritative_members=True,
            chat_type="group",
            tenant_key="tenant_test",
            updated_at=self.received_at,
        )
        with session_scope(self.session_factory) as session:
            session.add(
                ChatMemberAlias(
                    chat_id="oc_new",
                    open_id="ou_owner",
                    alias="李明",
                    normalized_alias="李明",
                    source="self_command",
                    confidence=1.0,
                    verified_at=self.received_at,
                    created_at=self.received_at,
                    updated_at=self.received_at,
                )
            )
        later = self.received_at.replace(minute=33)
        second = self.repository.apply_directory_snapshot(
            "oc_new",
            "新实验群",
            {"ou_member": "王政", "ou_new_owner": "新导师"},
            owner_open_id="ou_new_owner",
            member_tenant_keys={"ou_new_owner": "tenant_test"},
            authoritative_members=True,
            updated_at=later,
        )

        with self.session_factory() as session:
            memberships = {
                item.open_id: item
                for item in session.scalars(
                    select(ChatMembership).where(
                        ChatMembership.chat_id == "oc_new"
                    )
                )
            }

        self.assertEqual(first.memberships_created, 2)
        self.assertEqual(second.memberships_created, 1)
        self.assertEqual(second.memberships_updated, 1)
        self.assertEqual(second.memberships_deactivated, 1)
        self.assertEqual(second.aliases_released, 1)
        self.assertFalse(memberships["ou_owner"].active)
        self.assertEqual(memberships["ou_owner"].left_at, later)
        self.assertTrue(memberships["ou_member"].active)
        self.assertFalse(memberships["ou_member"].is_owner)
        self.assertTrue(memberships["ou_new_owner"].is_owner)
        with session_scope(self.session_factory) as session:
            self.assertIsNone(
                session.scalar(
                    select(ChatMemberAlias).where(
                        ChatMemberAlias.chat_id == "oc_new",
                        ChatMemberAlias.open_id == "ou_owner",
                    )
                )
            )
            session.add(
                ChatMemberAlias(
                    chat_id="oc_new",
                    open_id="ou_new_owner",
                    alias="李明",
                    normalized_alias="李明",
                    source="self_command",
                    confidence=1.0,
                    verified_at=later,
                    created_at=later,
                    updated_at=later,
                )
            )

    def test_group_snapshot_without_owner_rolls_back(self) -> None:
        with self.assertRaisesRegex(ValueError, "has no owner"):
            self.repository.apply_directory_snapshot(
                "oc_ownerless",
                "无群主群",
                {"ou_member": "王政"},
                authoritative_members=True,
                chat_type="group",
                tenant_key="tenant_test",
                updated_at=self.received_at,
            )
        with self.session_factory() as session:
            self.assertIsNone(session.get(Chat, "oc_ownerless"))

    def test_departed_administrator_is_revoked_and_session_invalidated(self) -> None:
        self.repository.apply_directory_snapshot(
            "oc_departure",
            "离群测试",
            {"ou_owner": "导师", "ou_admin": "管理员"},
            owner_open_id="ou_owner",
            authoritative_members=True,
            chat_type="group",
            tenant_key="tenant_test",
            updated_at=self.received_at,
        )
        with session_scope(self.session_factory) as session:
            session.add(
                ChatAdministrator(
                    chat_id="oc_departure",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="bootstrap",
                    created_at=self.received_at,
                )
            )
            session.add(
                ManagementSession(
                    session_hash="a" * 64,
                    actor_open_id="ou_admin",
                    expires_at=self.received_at + timedelta(hours=1),
                    revoked_at=None,
                    last_seen_at=self.received_at,
                    created_at=self.received_at,
                )
            )
        later = self.received_at + timedelta(minutes=1)

        result = self.repository.apply_directory_snapshot(
            "oc_departure",
            "离群测试",
            {"ou_owner": "导师"},
            owner_open_id="ou_owner",
            authoritative_members=True,
            updated_at=later,
        )

        with self.session_factory() as session:
            administrator = session.scalar(select(ChatAdministrator))
            event_row = session.scalar(
                select(ChatAdministratorEvent).where(
                    ChatAdministratorEvent.chat_id == "oc_departure"
                )
            )
            browser_session = session.scalar(select(ManagementSession))
        self.assertIsNone(administrator)
        assert event_row is not None and browser_session is not None
        self.assertEqual(event_row.action, "revoke")
        self.assertEqual(event_row.source, "membership_sync")
        self.assertEqual(browser_session.revoked_at, later)
        self.assertEqual(result.administrators_revoked, ("ou_admin",))
        self.assertEqual(result.management_sessions_revoked, 1)

    def _open_runtime(self) -> None:
        self.engine = create_database_engine(self.database_url)
        self.session_factory = create_session_factory(self.engine)
        self.repository = MessageRepository(self.session_factory)
        self.service = MessageIngestionService(self.repository)


if __name__ == "__main__":
    unittest.main()
