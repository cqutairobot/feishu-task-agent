"""Phase 3A chat-isolated task detection context tests."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.agent.context import TaskDetectionContextBuilder
from app.config import DatabaseSettings
from app.database.migrate import upgrade_database
from app.database.repository import MessageLookupError
from app.database.runtime import open_database_runtime
from app.main import main
from tests.test_messages import TEXT_EVENT


class TaskDetectionContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "context.db"
        self.settings = DatabaseSettings(
            url=f"sqlite:///{database_path}", echo=False
        )
        upgrade_database(self.settings.url)
        self.runtime_manager = open_database_runtime(self.settings)
        self.runtime = self.runtime_manager.__enter__()
        self.received_at = datetime(
            2026, 8, 22, 18, 32, tzinfo=ZoneInfo("Asia/Shanghai")
        )

        self._ingest(
            event_id="evt_a1",
            message_id="om_a1",
            chat_id="oc_a",
            open_id="ou_teacher",
            text="这个实验还缺一个 baseline",
            timestamp="1787382060000",
        )
        self._ingest(
            event_id="evt_a2",
            message_id="om_a2",
            chat_id="oc_a",
            open_id="ou_wang",
            text="我来补吧",
            timestamp="1787382120000",
        )
        self._ingest(
            event_id="evt_b1",
            message_id="om_b1",
            chat_id="oc_b",
            open_id="ou_other",
            text="另一个群的秘密任务",
            timestamp="1787382150000",
        )
        self._ingest(
            event_id="evt_a3",
            message_id="om_a3",
            chat_id="oc_a",
            open_id="ou_teacher",
            text="好，周四之前跑出来",
            timestamp="1787382180000",
        )
        self._ingest(
            event_id="evt_a4",
            message_id="om_a4",
            chat_id="oc_a",
            open_id="ou_teacher",
            text="这是触发消息之后的内容",
            timestamp="1787382240000",
        )
        self.runtime.aliases.bind("oc_a", "ou_teacher", "老师")
        self.runtime.aliases.bind("oc_a", "ou_wang", "王政")

    def tearDown(self) -> None:
        self.runtime_manager.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_context_is_same_chat_and_ends_at_trigger(self) -> None:
        context = self._builder().build("oc_a", "om_a3")

        self.assertEqual(
            [message.message_id for message in context.messages],
            ["om_a1", "om_a2", "om_a3"],
        )
        self.assertNotIn(
            "另一个群的秘密任务",
            [message.content for message in context.messages],
        )
        self.assertEqual(context.trigger_message_id, "om_a3")
        self.assertEqual(context.timezone, "Asia/Shanghai")
        self.assertEqual(context.focus_message_ids, ("om_a3",))

    def test_batch_focus_uses_arrival_time_not_old_context(self) -> None:
        focus_since = self.received_at

        context = self._builder().build(
            "oc_a", "om_a3", focus_since=focus_since
        )

        self.assertEqual(
            context.focus_message_ids,
            ("om_a1", "om_a2", "om_a3"),
        )

    def test_confirmed_names_are_exposed_as_owner_candidates(self) -> None:
        context = self._builder().build("oc_a", "om_a3")
        participants = {
            participant.open_id: participant
            for participant in context.participants
        }

        self.assertEqual(participants["ou_wang"].name, "王政")
        self.assertEqual(participants["ou_teacher"].name, "老师")
        self.assertEqual(context.messages[1].sender_name, "王政")

    def test_context_preserves_exact_feishu_mention_mapping(self) -> None:
        self._ingest(
            event_id="evt_a5",
            message_id="om_a5",
            chat_id="oc_a",
            open_id="ou_teacher",
            text="@_user_1 今天 21:00 前主持智能体讨论会议",
            timestamp="1787382300000",
            mentions=[
                {
                    "id": {"open_id": "ou_wang", "union_id": "on_ou_wang"},
                    "key": "@_user_1",
                    "mentioned_type": "user",
                    "name": "那也就丶",
                }
            ],
        )

        message = self._builder().build("oc_a", "om_a5").messages[-1]
        self.assertEqual(len(message.mentions), 1)
        self.assertEqual(message.mentions[0].key, "@_user_1")
        self.assertEqual(message.mentions[0].open_id, "ou_wang")
        self.assertEqual(message.mentions[0].name, "王政")
        self.assertEqual(message.to_dict()["mentions"][0]["open_id"], "ou_wang")
        self.assertNotEqual(message.mentions[0].name, "那也就丶")

    def test_content_budget_keeps_trigger_and_drops_oldest(self) -> None:
        context = TaskDetectionContextBuilder(
            self.runtime.repository,
            self.runtime.aliases,
            max_content_characters=10,
        ).build("oc_a", "om_a3")

        self.assertEqual(
            [message.message_id for message in context.messages], ["om_a3"]
        )

    def test_chat_task_scope_is_included_in_model_context(self) -> None:
        context = TaskDetectionContextBuilder(
            self.runtime.repository,
            self.runtime.aliases,
            task_scope_resolver=lambda chat_id: (
                "work_only" if chat_id == "oc_a" else "broad"
            ),
        ).build("oc_a", "om_a3")

        self.assertEqual(context.task_scope, "work_only")
        self.assertEqual(context.to_dict()["task_scope"], "work_only")
        self.assertEqual(context.to_dict()["context_version"], "1.3")

    def test_unknown_or_cross_chat_trigger_is_rejected(self) -> None:
        with self.assertRaises(MessageLookupError):
            self._builder().build("oc_a", "om_b1")

    def test_non_positive_limit_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit"):
            self._builder().build("oc_a", "om_a3", limit=0)

    def test_cli_prints_model_input_as_json(self) -> None:
        output = io.StringIO()
        with (
            patch("app.main.load_database_settings", return_value=self.settings),
            redirect_stdout(output),
        ):
            status = main(
                [
                    "task-context",
                    "--chat-id",
                    "oc_a",
                    "--message-id",
                    "om_a3",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["context"]["chat_id"], "oc_a")
        self.assertEqual(
            payload["context"]["messages"][-1]["message_id"], "om_a3"
        )

    def _builder(self) -> TaskDetectionContextBuilder:
        return TaskDetectionContextBuilder(
            self.runtime.repository,
            self.runtime.aliases,
        )

    def _ingest(
        self,
        *,
        event_id: str,
        message_id: str,
        chat_id: str,
        open_id: str,
        text: str,
        timestamp: str,
        mentions: list[dict[str, object]] | None = None,
    ) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = event_id
        payload["header"]["create_time"] = timestamp
        payload["event"]["message"]["message_id"] = message_id
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["message"]["create_time"] = timestamp
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["sender"]["sender_id"]["open_id"] = open_id
        payload["event"]["sender"]["sender_id"]["union_id"] = f"on_{open_id}"
        payload["event"]["message"]["mentions"] = mentions or []
        self.runtime.ingestion.process_payload(
            payload, received_at=self.received_at
        )


if __name__ == "__main__":
    unittest.main()
