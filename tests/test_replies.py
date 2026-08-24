"""Tests for Feishu identity resolution and text replies."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import FeishuSettings
from app.feishu.replies import (
    BotIdentityError,
    FeishuMessageReplier,
    FeishuReplyError,
    resolve_bot_open_id,
)


class FeishuMessageReplierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret",
            }
        )

    def test_builds_idempotent_text_reply(self) -> None:
        client = MagicMock()
        client.im.v1.message.reply.return_value = SimpleNamespace(
            success=lambda: True
        )
        replier = FeishuMessageReplier(self.settings, client=client)

        replier.reply_text("om_test", "绑定成功")

        request = client.im.v1.message.reply.call_args.args[0]
        self.assertEqual(request.message_id, "om_test")
        self.assertEqual(request.request_body.msg_type, "text")
        self.assertEqual(
            json.loads(request.request_body.content), {"text": "绑定成功"}
        )
        self.assertTrue(request.request_body.uuid.startswith("identity-"))

    def test_raises_when_reply_is_rejected(self) -> None:
        client = MagicMock()
        client.im.v1.message.reply.return_value = SimpleNamespace(
            success=lambda: False,
            code=999,
            msg="denied",
        )
        replier = FeishuMessageReplier(self.settings, client=client)

        with self.assertRaisesRegex(FeishuReplyError, "code=999"):
            replier.reply_text("om_test", "绑定失败")

    def test_builds_idempotent_interactive_card_reply(self) -> None:
        client = MagicMock()
        client.im.v1.message.reply.return_value = SimpleNamespace(
            success=lambda: True
        )
        replier = FeishuMessageReplier(self.settings, client=client)
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "我的任务"}
            },
            "elements": [],
        }

        sent_as_card = replier.reply_card(
            "om_test", card, fallback_text="我的任务"
        )

        self.assertTrue(sent_as_card)
        request = client.im.v1.message.reply.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "interactive")
        self.assertEqual(json.loads(request.request_body.content), card)
        self.assertTrue(request.request_body.uuid.startswith("task-card-"))

    def test_card_rejection_falls_back_to_text(self) -> None:
        client = MagicMock()
        client.im.v1.message.reply.side_effect = (
            SimpleNamespace(success=lambda: False, code=230001, msg="bad card"),
            SimpleNamespace(success=lambda: True),
        )
        replier = FeishuMessageReplier(self.settings, client=client)

        sent_as_card = replier.reply_card(
            "om_test", {"elements": []}, fallback_text="文本任务列表"
        )

        self.assertFalse(sent_as_card)
        requests = [call.args[0] for call in client.im.v1.message.reply.call_args_list]
        self.assertEqual(
            [request.request_body.msg_type for request in requests],
            ["interactive", "text"],
        )
        self.assertEqual(
            json.loads(requests[1].request_body.content),
            {"text": "文本任务列表"},
        )

    def test_resolves_bot_open_id(self) -> None:
        client = SimpleNamespace(config=object())
        with (
            patch("app.feishu.replies.build_api_client", return_value=client),
            patch(
                "app.feishu.replies.fetch_bot_identity",
                new=AsyncMock(return_value=SimpleNamespace(open_id="ou_bot")),
            ),
        ):
            open_id = resolve_bot_open_id(self.settings)

        self.assertEqual(open_id, "ou_bot")

    def test_rejects_missing_bot_identity(self) -> None:
        client = SimpleNamespace(config=object())
        with (
            patch("app.feishu.replies.build_api_client", return_value=client),
            patch(
                "app.feishu.replies.fetch_bot_identity",
                new=AsyncMock(return_value=None),
            ),
            self.assertRaises(BotIdentityError),
        ):
            resolve_bot_open_id(self.settings)


if __name__ == "__main__":
    unittest.main()
