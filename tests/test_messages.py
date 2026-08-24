"""Offline tests for Feishu ``im.message.receive_v1`` normalization."""

from copy import deepcopy
from datetime import datetime
import unittest

from app.feishu.messages import MessageEventError, normalize_message_event


TEXT_EVENT = {
    "schema": "2.0",
    "header": {
        "event_id": "evt_test",
        "event_type": "im.message.receive_v1",
        "create_time": "1787383930000",
        "token": "event-token-must-not-be-persisted",
        "tenant_key": "tenant_test",
    },
    "event": {
        "sender": {
            "sender_id": {
                "union_id": "on_test",
                "user_id": "user_test",
                "open_id": "ou_test",
            },
            "sender_type": "user",
            "tenant_key": "tenant_test",
        },
        "message": {
            "message_id": "om_test",
            "root_id": "",
            "parent_id": "",
            "create_time": "1787383930000",
            "chat_id": "oc_test",
            "chat_type": "group",
            "message_type": "text",
            "content": '{"text":"今天实验结果出来了吗？"}',
            "mentions": [],
        },
    },
}


class NormalizeMessageEventTest(unittest.TestCase):
    def test_normalizes_text_event(self) -> None:
        message = normalize_message_event(TEXT_EVENT)

        self.assertEqual(message.event_id, "evt_test")
        self.assertEqual(message.tenant_key, "tenant_test")
        self.assertEqual(message.message_id, "om_test")
        self.assertEqual(message.chat_id, "oc_test")
        self.assertEqual(message.sender_open_id, "ou_test")
        self.assertEqual(message.text, "今天实验结果出来了吗？")
        self.assertEqual(message.sender_union_id, "on_test")
        self.assertNotIn("event-token-must-not-be-persisted", message.raw_event_json)
        self.assertNotIn('"token"', message.raw_event_json)
        self.assertIn("message_id: om_test", message.terminal_output())
        self.assertIn("message: 今天实验结果出来了吗？", message.terminal_output())

    def test_marks_non_text_without_parsing_its_content(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["message_type"] = "image"
        payload["event"]["message"]["content"] = '{"image_key":"img_test"}'

        message = normalize_message_event(payload)

        self.assertEqual(message.text, "<image message>")

    def test_normalizes_mention_identity(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["mentions"] = [
            {
                "key": "@_user_1",
                "id": {"open_id": "ou_bot"},
                "mentioned_type": "bot",
                "name": "任务机器人",
                "tenant_key": "tenant_test",
            }
        ]

        message = normalize_message_event(payload)

        self.assertEqual(len(message.mentions), 1)
        self.assertEqual(message.mentions[0].open_id, "ou_bot")
        self.assertEqual(message.mentions[0].mentioned_type, "bot")

    def test_rejects_missing_sender_open_id(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        del payload["event"]["sender"]["sender_id"]["open_id"]

        with self.assertRaisesRegex(MessageEventError, "open_id"):
            normalize_message_event(payload)

    def test_rejects_invalid_text_content(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["content"] = "not-json"

        with self.assertRaisesRegex(MessageEventError, "valid JSON"):
            normalize_message_event(payload)

    def test_rejects_naive_received_at(self) -> None:
        with self.assertRaisesRegex(MessageEventError, "received_at"):
            normalize_message_event(
                TEXT_EVENT,
                received_at=datetime(2026, 8, 22, 18, 30),
            )


if __name__ == "__main__":
    unittest.main()
