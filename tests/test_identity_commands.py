"""Phase 2E-B deterministic group identity command tests."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from app.database.engine import create_database_engine, create_session_factory
from app.database.migrate import upgrade_database
from app.database.repository import MessageRepository
from app.identity.aliases import AliasRepository
from app.identity.commands import (
    IdentityCommandKind,
    IdentityCommandProcessor,
    is_identity_command_message,
)
from app.ingestion.service import MessageIngestionService
from tests.test_messages import TEXT_EVENT


BOT_OPEN_ID = "ou_bot"
BOT_MENTION = {
    "key": "@_user_1",
    "id": {"open_id": BOT_OPEN_ID},
    "mentioned_type": "bot",
    "name": "任务机器人",
    "tenant_key": "tenant_test",
}


class IdentityCommandProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "commands.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        session_factory = create_session_factory(self.engine)
        self.message_repository = MessageRepository(session_factory)
        self.aliases = AliasRepository(session_factory)
        self.ingestion = MessageIngestionService(self.message_repository)
        self.received_at = datetime(
            2026, 8, 22, 18, 32, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_self_binding_uses_sender_open_id(self) -> None:
        message = self._ingest_command("@_user_1 绑定姓名：王哈")
        processor = self._processor()

        result = processor.handle(message)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.kind, IdentityCommandKind.SELF_BIND)
        self.assertIn("王哈", result.reply_text)
        binding = self.aliases.resolve("oc_test", "王哈")
        self.assertEqual(binding.open_id, "ou_test")
        self.assertEqual(binding.source, "self_command")

    def test_identity_command_can_be_classified_without_applying_it(self) -> None:
        message = self._ingest_command("@_user_1 绑定姓名：王哈")

        self.assertTrue(is_identity_command_message(message))
        self.assertIsNone(self.aliases.resolve("oc_test", "王哈"))

    def test_natural_my_name_form_also_binds_sender(self) -> None:
        message = self._ingest_command("@_user_1 我的姓名 王政")

        result = self._processor().handle(message)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.kind, IdentityCommandKind.SELF_BIND)
        self.assertEqual(
            self.aliases.resolve("oc_test", "王政").open_id, "ou_test"
        )

    def test_query_returns_current_preferred_name(self) -> None:
        self._ingest_normal("ou_test")
        self.aliases.bind("oc_test", "ou_test", "王哈")
        message = self._ingest_command("@_user_1 我的姓名？")

        result = self._processor().handle(message)

        self.assertTrue(result.succeeded)
        self.assertIn("王哈", result.reply_text)

    def test_query_explains_how_to_bind_when_missing(self) -> None:
        message = self._ingest_command("@_user_1 我的姓名")

        result = self._processor().handle(message)

        self.assertTrue(result.succeeded)
        self.assertIn("还没有", result.reply_text)

    def test_plain_text_without_bot_mention_is_ignored(self) -> None:
        message = self._ingest_command(
            "绑定姓名：王哈",
            mentions=[],
        )

        self.assertIsNone(self._processor().handle(message))

    def test_mentioning_another_user_does_not_trigger_command(self) -> None:
        human_mention = deepcopy(BOT_MENTION)
        human_mention["id"]["open_id"] = "ou_someone"
        human_mention["mentioned_type"] = "user"
        message = self._ingest_command(
            "@_user_1 绑定姓名：王哈",
            mentions=[human_mention],
        )

        self.assertIsNone(self._processor().handle(message))

    def test_bot_mention_must_be_at_start(self) -> None:
        message = self._ingest_command("请 @_user_1 绑定姓名：王哈")

        self.assertIsNone(self._processor().handle(message))

    def test_empty_name_returns_usage_error(self) -> None:
        message = self._ingest_command("@_user_1 绑定姓名：")

        result = self._processor().handle(message)

        self.assertFalse(result.succeeded)
        self.assertEqual(result.kind, IdentityCommandKind.INVALID)

    def test_conflicting_self_binding_is_rejected(self) -> None:
        self._ingest_normal("ou_other")
        self.aliases.bind("oc_test", "ou_other", "王哈")
        message = self._ingest_command("@_user_1 绑定姓名：王哈")

        result = self._processor().handle(message)

        self.assertFalse(result.succeeded)
        self.assertIn("其他成员", result.reply_text)

    def test_authorized_admin_can_bind_explicitly_mentioned_member(self) -> None:
        self._ingest_normal("ou_target")
        target = self._target_mention("ou_target")
        message = self._ingest_command(
            "@_user_1 绑定成员 @_user_2 为 王政",
            mentions=[BOT_MENTION, target],
        )
        processor = self._processor(admins=frozenset({"ou_test"}))

        result = processor.handle(message)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.kind, IdentityCommandKind.ADMIN_BIND)
        self.assertEqual(
            self.aliases.resolve("oc_test", "王政").open_id, "ou_target"
        )

    def test_non_admin_cannot_bind_another_member(self) -> None:
        self._ingest_normal("ou_target")
        message = self._ingest_command(
            "@_user_1 绑定成员 @_user_2 为 王政",
            mentions=[BOT_MENTION, self._target_mention("ou_target")],
        )

        result = self._processor().handle(message)

        self.assertFalse(result.succeeded)
        self.assertIn("身份管理员", result.reply_text)
        self.assertIsNone(self.aliases.resolve("oc_test", "王政"))

    def test_admin_target_must_have_a_stored_message_in_chat(self) -> None:
        message = self._ingest_command(
            "@_user_1 绑定成员 @_user_2 为 新成员",
            mentions=[BOT_MENTION, self._target_mention("ou_unknown")],
        )

        result = self._processor(
            admins=frozenset({"ou_test"})
        ).handle(message)

        self.assertFalse(result.succeeded)
        self.assertIn("尚未在本群留下消息", result.reply_text)

    def test_private_chat_command_is_ignored(self) -> None:
        message = self._ingest_command(
            "@_user_1 绑定姓名：王哈", chat_type="p2p"
        )

        self.assertIsNone(self._processor().handle(message))

    def _processor(
        self, *, admins: frozenset[str] = frozenset()
    ) -> IdentityCommandProcessor:
        return IdentityCommandProcessor(
            self.aliases,
            bot_open_id=BOT_OPEN_ID,
            admin_open_ids=admins,
        )

    def _ingest_normal(self, open_id: str) -> None:
        payload = self._event("普通消息", open_id=open_id, mentions=[])
        self.ingestion.process_payload(payload, received_at=self.received_at)

    def _ingest_command(
        self,
        text: str,
        *,
        mentions: list[dict] | None = None,
        chat_type: str = "group",
    ):
        payload = self._event(
            text,
            open_id="ou_test",
            mentions=mentions if mentions is not None else [BOT_MENTION],
            chat_type=chat_type,
        )
        return self.ingestion.process_payload(
            payload, received_at=self.received_at
        ).message

    def _event(
        self,
        text: str,
        *,
        open_id: str,
        mentions: list[dict],
        chat_type: str = "group",
    ) -> dict:
        self.counter += 1
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = f"evt_command_{self.counter}"
        payload["event"]["message"]["message_id"] = f"om_command_{self.counter}"
        payload["event"]["message"]["chat_type"] = chat_type
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["message"]["mentions"] = deepcopy(mentions)
        payload["event"]["sender"]["sender_id"]["open_id"] = open_id
        payload["event"]["sender"]["sender_id"]["union_id"] = f"on_{open_id}"
        return payload

    @staticmethod
    def _target_mention(open_id: str) -> dict:
        return {
            "key": "@_user_2",
            "id": {"open_id": open_id},
            "mentioned_type": "user",
            "name": "目标成员",
            "tenant_key": "tenant_test",
        }


if __name__ == "__main__":
    unittest.main()
