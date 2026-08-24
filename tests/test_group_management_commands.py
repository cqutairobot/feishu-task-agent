"""Phase 7C-2 group-owner administration command tests."""

from dataclasses import replace
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
from app.database.models import Chat, ChatMembership, User
from app.feishu.directory import DirectoryMember, DirectorySnapshot
from app.feishu.messages import IncomingMessage, MessageMention
from app.management.access import (
    AdministratorSource,
    ChatAdministratorRepository,
)
from app.management.group_commands import (
    GroupManagementCommandKind,
    GroupManagementCommandProcessor,
    is_group_management_command_message,
)


class _Directory:
    def __init__(self, snapshot: DirectorySnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.failure: Exception | None = None

    def refresh_strict(self, chat_id: str, **_kwargs) -> DirectorySnapshot:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        if chat_id != self.snapshot.chat_id:
            raise AssertionError("unexpected chat")
        return self.snapshot


class GroupManagementCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "group-admin.db"
        url = f"sqlite:///{path}"
        upgrade_database(url)
        self.engine = create_database_engine(url)
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
        self.administrators = ChatAdministratorRepository(self.session_factory)
        self.snapshot = DirectorySnapshot(
            chat_id="oc_lab",
            chat_name="实验群",
            chat_tenant_key="tenant",
            owner_open_id="ou_owner",
            members={
                "ou_owner": DirectoryMember("ou_owner", "导师", "tenant"),
                "ou_member": DirectoryMember("ou_member", "王政", "tenant"),
            },
        )
        self.directory = _Directory(self.snapshot)
        self.processor = GroupManagementCommandProcessor(
            self.administrators,
            self.directory,
            bot_open_id="ou_bot",
        )
        self._seed()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_owner_initializes_once_after_live_verification(self) -> None:
        message = self._message("ou_owner", "初始化本群")

        first = self.processor.handle(message)
        repeated = self.processor.handle(message)

        assert first is not None and repeated is not None
        self.assertTrue(first.succeeded)
        self.assertIn("初始化成功", first.reply_text)
        self.assertTrue(repeated.succeeded)
        self.assertIn("已经是", repeated.reply_text)
        self.assertEqual(self.directory.calls, 2)
        listed = self.administrators.list_chat("oc_lab")
        self.assertEqual([item.open_id for item in listed], ["ou_owner"])
        self.assertEqual(listed[0].source, AdministratorSource.GROUP_OWNER_INIT)

    def test_non_owner_is_rejected_without_database_change(self) -> None:
        result = self.processor.handle(self._message("ou_member", "初始化本群"))

        assert result is not None
        self.assertFalse(result.succeeded)
        self.assertIn("只有当前飞书群主", result.reply_text)
        self.assertEqual(self.administrators.list_chat("oc_lab"), ())

    def test_owner_can_take_over_when_another_administrator_exists(self) -> None:
        self.administrators.grant("oc_lab", "ou_member", granted_at=self.now)

        result = self.processor.handle(self._message("ou_owner", "接管本群"))

        assert result is not None
        self.assertTrue(result.succeeded)
        self.assertEqual(
            {item.open_id for item in self.administrators.list_chat("oc_lab")},
            {"ou_owner", "ou_member"},
        )

    def test_initialize_does_not_override_existing_administrator(self) -> None:
        self.administrators.grant("oc_lab", "ou_member", granted_at=self.now)

        result = self.processor.handle(self._message("ou_owner", "初始化本群"))

        assert result is not None
        self.assertFalse(result.succeeded)
        self.assertIn("接管本群", result.reply_text)
        self.assertFalse(
            self.administrators.is_administrator("oc_lab", "ou_owner")
        )

    def test_verification_failure_and_non_commands_fail_closed(self) -> None:
        self.directory.failure = RuntimeError("Feishu unavailable")
        result = self.processor.handle(self._message("ou_owner", "接管本群"))

        assert result is not None
        self.assertFalse(result.succeeded)
        self.assertIn("未修改管理员", result.reply_text)
        plain = self._message("ou_owner", "接管本群")
        plain = replace(plain, mentions=(), text="接管本群")
        self.assertIsNone(self.processor.handle(plain))

    def test_generic_classifier_recognizes_any_bot_mention(self) -> None:
        message = self._message("ou_owner", "初始化本群")

        self.assertTrue(is_group_management_command_message(message))
        result = self.processor.handle(message)
        assert result is not None
        self.assertEqual(result.kind, GroupManagementCommandKind.INITIALIZE)

    def test_dynamic_chat_admission_requires_setup_or_onboarding_command(self) -> None:
        plain = replace(
            self._message("ou_owner", "初始化本群"),
            mentions=(),
            text="大家下午好",
        )
        self.assertFalse(self.processor.allows_chat(plain))
        self.assertTrue(
            self.processor.allows_chat(
                self._message("ou_owner", "初始化本群")
            )
        )

        result = self.processor.handle(
            self._message("ou_owner", "初始化本群")
        )

        assert result is not None and result.succeeded
        self.assertTrue(self.processor.allows_chat(plain))

    def _seed(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_lab",
                    tenant_key="tenant",
                    name="实验群",
                    chat_type="group",
                    enabled=True,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.add_all(
                User(
                    open_id=open_id,
                    union_id=None,
                    name=name,
                    tenant_key="tenant",
                    last_seen_at=self.now,
                    created_at=self.now,
                    updated_at=self.now,
                )
                for open_id, name in (("ou_owner", "导师"), ("ou_member", "王政"))
            )
            session.flush()
            session.add_all(
                ChatMembership(
                    chat_id="oc_lab",
                    open_id=open_id,
                    display_name_snapshot=name,
                    active=True,
                    is_owner=open_id == "ou_owner",
                    first_synced_at=self.now,
                    last_synced_at=self.now,
                    left_at=None,
                )
                for open_id, name in (("ou_owner", "导师"), ("ou_member", "王政"))
            )

    def _message(self, sender_open_id: str, command: str) -> IncomingMessage:
        mention = MessageMention(
            key="@_user_1",
            open_id="ou_bot",
            name="Lab Task agent",
            mentioned_type="bot",
            tenant_key="tenant",
        )
        return IncomingMessage(
            event_id=f"ev_{sender_open_id}_{command}",
            tenant_key="tenant",
            message_id=f"om_{sender_open_id}_{command}",
            chat_id="oc_lab",
            chat_type="group",
            sender_open_id=sender_open_id,
            sender_union_id=None,
            sender_name=None,
            sender_type="user",
            message_type="text",
            text=f"{mention.key} {command}",
            mentions=(mention,),
            raw_content="{}",
            raw_event_json="{}",
            root_id=None,
            parent_id=None,
            created_at=self.now,
            received_at=self.now,
        )
