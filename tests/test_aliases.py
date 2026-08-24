"""Phase 2E-A chat-scoped member alias tests."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from app.database.engine import create_database_engine, create_session_factory
from app.database.migrate import upgrade_database
from app.database.models import User
from app.database.repository import MessageRepository
from app.identity.aliases import AliasConflictError, AliasError, AliasRepository
from app.ingestion.service import MessageIngestionService
from tests.test_messages import TEXT_EVENT


class AliasRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "aliases.db"
        self.database_url = f"sqlite:///{database_path}"
        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        self.session_factory = create_session_factory(self.engine)
        self.messages = MessageRepository(self.session_factory)
        self.aliases = AliasRepository(self.session_factory)
        self.ingestion = MessageIngestionService(self.messages)
        self.received_at = datetime(
            2026, 8, 22, 18, 32, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        self.ingestion.process_payload(TEXT_EVENT, received_at=self.received_at)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_binds_and_resolves_normalized_alias(self) -> None:
        binding = self.aliases.bind("oc_test", "ou_test", "  王政  ")
        resolved = self.aliases.resolve("oc_test", "王政")

        self.assertEqual(binding.alias, "王政")
        self.assertEqual(resolved, binding)
        self.assertEqual(binding.source, "manual")

    def test_alias_takes_precedence_in_conversation_context(self) -> None:
        self.messages.apply_directory_snapshot(
            "oc_test",
            "实验群",
            {"ou_test": "飞书用户0976BV"},
            updated_at=self.received_at,
        )
        self.aliases.bind("oc_test", "ou_test", "王政")

        conversation = self.messages.conversation("oc_test")

        self.assertEqual(conversation[0].sender_name, "王政")

    def test_renaming_releases_the_previous_name(self) -> None:
        self.aliases.bind("oc_test", "ou_test", "王政")
        renamed = self.aliases.bind("oc_test", "ou_test", "王哈")

        self.assertEqual(renamed.alias, "王哈")
        self.assertIsNone(self.aliases.resolve("oc_test", "王政"))
        self.assertEqual(self.aliases.resolve("oc_test", "王哈").open_id, "ou_test")
        self.assertEqual(len(self.aliases.list_for_chat("oc_test")), 1)
        self.assertEqual(self.messages.conversation("oc_test")[0].sender_name, "王哈")

    def test_released_name_can_be_claimed_by_another_member(self) -> None:
        second = self._event(
            event_id="evt_second",
            message_id="om_second",
            open_id="ou_second",
        )
        self.ingestion.process_payload(second, received_at=self.received_at)
        self.aliases.bind("oc_test", "ou_test", "王政")
        self.aliases.bind("oc_test", "ou_test", "王哈")

        binding = self.aliases.bind("oc_test", "ou_second", "王政")

        self.assertEqual(binding.open_id, "ou_second")
        self.assertEqual(self.aliases.resolve("oc_test", "王政").open_id, "ou_second")

    def test_failed_conflicting_rename_keeps_current_name(self) -> None:
        second = self._event(
            event_id="evt_second",
            message_id="om_second",
            open_id="ou_second",
        )
        self.ingestion.process_payload(second, received_at=self.received_at)
        self.aliases.bind("oc_test", "ou_test", "王政")
        self.aliases.bind("oc_test", "ou_second", "李四")

        with self.assertRaises(AliasConflictError):
            self.aliases.bind("oc_test", "ou_test", "李四")

        self.assertEqual(self.aliases.for_member("oc_test", "ou_test").alias, "王政")

    def test_same_name_cannot_map_to_two_people_in_one_chat(self) -> None:
        second = self._event(
            event_id="evt_second",
            message_id="om_second",
            open_id="ou_second",
        )
        self.ingestion.process_payload(second, received_at=self.received_at)
        self.aliases.bind("oc_test", "ou_test", "王政")

        with self.assertRaisesRegex(AliasConflictError, "already mapped"):
            self.aliases.bind("oc_test", "ou_second", "王政")

    def test_same_name_can_map_differently_in_another_chat(self) -> None:
        other_chat = self._event(
            event_id="evt_other_chat",
            message_id="om_other_chat",
            open_id="ou_second",
            chat_id="oc_other",
        )
        self.ingestion.process_payload(other_chat, received_at=self.received_at)

        first = self.aliases.bind("oc_test", "ou_test", "王政")
        second = self.aliases.bind("oc_other", "ou_second", "王政")

        self.assertNotEqual(first.open_id, second.open_id)

    def test_rejects_user_not_observed_in_chat(self) -> None:
        with self.session_factory() as session:
            session.add(
                User(
                    open_id="ou_unobserved",
                    union_id=None,
                    name="未发言成员",
                    tenant_key="tenant_test",
                    last_seen_at=self.received_at,
                )
            )
            session.commit()

        with self.assertRaisesRegex(AliasError, "has not sent"):
            self.aliases.bind("oc_test", "ou_unobserved", "未发言成员")

    def test_can_find_sender_from_copied_message_id(self) -> None:
        sender = self.aliases.sender_for_message("om_test")

        self.assertEqual(sender.chat_id, "oc_test")
        self.assertEqual(sender.open_id, "ou_test")

    @staticmethod
    def _event(
        *,
        event_id: str,
        message_id: str,
        open_id: str,
        chat_id: str = "oc_test",
    ) -> dict:
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = event_id
        payload["event"]["message"]["message_id"] = message_id
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["sender"]["sender_id"]["open_id"] = open_id
        payload["event"]["sender"]["sender_id"]["union_id"] = f"on_{open_id}"
        return payload


if __name__ == "__main__":
    unittest.main()
