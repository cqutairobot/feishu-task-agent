"""Phase 5B Feishu private-first reminder sender tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.config import FeishuSettings, ReminderSettings
from app.feishu.reminder_sender import (
    FeishuReminderSender,
    ReminderDeliveryError,
)
from app.reminders.repository import ReminderLease
from app.reminders.schedule import ReminderKind


class ReminderSenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret",
            }
        )
        self.lease = ReminderLease(
            reminder_id=7,
            task_id=3,
            kind=ReminderKind.DUE_24H,
            deadline=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
            scheduled_for=datetime(
                2026, 8, 29, 10, 0, tzinfo=timezone.utc
            ),
            attempt=1,
            max_attempts=3,
            worker_id="worker",
            lease_expires_at=datetime(
                2026, 8, 29, 10, 2, tzinfo=timezone.utc
            ),
            chat_id="oc_test",
            owner_open_id="ou_wang",
            owner_name="王政",
            title="完成前端页面",
            task_status="todo",
        )

    def test_private_delivery_is_first_and_contains_no_group_mention(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = _success("om_private")
        sender = FeishuReminderSender(self.settings, client=client)

        receipt = sender.deliver(self.lease)

        self.assertEqual(receipt.receive_id_type, "open_id")
        self.assertEqual(receipt.receive_id, "ou_wang")
        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "open_id")
        self.assertEqual(request.request_body.receive_id, "ou_wang")
        content = json.loads(request.request_body.content)["text"]
        self.assertIn("第二提醒（截止前 24 小时）", content)
        self.assertIn("王政", content)
        self.assertNotIn("<at ", content)

    def test_known_private_chat_id_is_preferred_over_open_id(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = _success("om_private_chat")
        sender = FeishuReminderSender(self.settings, client=client)
        lease = replace(
            self.lease,
            owner_private_chat_id="oc_private_wang",
        )

        receipt = sender.deliver(lease)

        self.assertEqual(receipt.receive_id_type, "chat_id")
        self.assertEqual(receipt.receive_id, "oc_private_wang")
        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "chat_id")
        self.assertEqual(request.request_body.receive_id, "oc_private_wang")

    def test_test_mode_labels_do_not_claim_production_intervals(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = _success("om_test_mode")
        sender = FeishuReminderSender(
            self.settings,
            client=client,
            reminder_settings=ReminderSettings(test_mode=True),
        )

        sender.deliver(self.lease)

        request = client.im.v1.message.create.call_args.args[0]
        content = json.loads(request.request_body.content)["text"]
        self.assertIn("【测试提醒 2/4】", content)
        self.assertIn("4 分钟后截止", content)
        self.assertNotIn("24 小时后截止", content)

    def test_production_text_uses_the_persisted_custom_offset(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = _success("om_custom")
        sender = FeishuReminderSender(self.settings, client=client)
        custom = replace(
            self.lease,
            scheduled_for=self.lease.deadline - timedelta(hours=36),
        )

        sender.deliver(custom)

        request = client.im.v1.message.create.call_args.args[0]
        content = json.loads(request.request_body.content)["text"]
        self.assertIn("第二提醒（截止前 36 小时）", content)
        self.assertNotIn("截止前 24 小时", content)

    def test_private_failure_falls_back_to_group_with_real_at(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.side_effect = (
            _failure(230002, "cannot initiate chat"),
            _success("om_group"),
        )
        sender = FeishuReminderSender(self.settings, client=client)

        receipt = sender.deliver(self.lease)

        self.assertEqual(receipt.receive_id_type, "chat_id")
        self.assertEqual(receipt.receive_id, "oc_test")
        self.assertEqual(receipt.private_error_code, "230002")
        requests = [
            call.args[0]
            for call in client.im.v1.message.create.call_args_list
        ]
        self.assertEqual(
            [request.receive_id_type for request in requests],
            ["open_id", "chat_id"],
        )
        group_text = json.loads(requests[1].request_body.content)["text"]
        self.assertIn('<at user_id="ou_wang">王政</at>', group_text)
        self.assertNotEqual(
            requests[0].request_body.uuid,
            requests[1].request_body.uuid,
        )

    def test_both_failures_raise_one_bounded_delivery_error(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.side_effect = (
            _failure(1, "private denied"),
            _failure(2, "group denied"),
        )
        sender = FeishuReminderSender(self.settings, client=client)

        with self.assertRaises(ReminderDeliveryError) as caught:
            sender.deliver(self.lease)

        self.assertEqual(caught.exception.code, "all_delivery_failed")
        self.assertIn("private denied", str(caught.exception))
        self.assertIn("group denied", str(caught.exception))

    def test_uncertain_transport_failure_waits_for_a_retry(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.side_effect = ConnectionError(
            "temporary network failure"
        )
        sender = FeishuReminderSender(self.settings, client=client)

        with self.assertRaises(ReminderDeliveryError) as caught:
            sender.deliver(self.lease)

        self.assertEqual(caught.exception.code, "transport_error")
        self.assertEqual(client.im.v1.message.create.call_count, 1)

    def test_uncertain_private_error_falls_back_on_final_attempt(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.side_effect = (
            _failure(230101, "temporarily unavailable"),
            _success("om_group"),
        )
        sender = FeishuReminderSender(self.settings, client=client)

        receipt = sender.deliver(replace(self.lease, attempt=3))

        self.assertEqual(receipt.receive_id_type, "chat_id")
        self.assertEqual(receipt.private_error_code, "230101")
        self.assertEqual(client.im.v1.message.create.call_count, 2)

    def test_same_reminder_reuses_channel_uuid_across_retries(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = _success("om_private")
        sender = FeishuReminderSender(self.settings, client=client)

        sender.deliver(self.lease)
        first_uuid = (
            client.im.v1.message.create.call_args.args[0]
            .request_body.uuid
        )
        sender.deliver(self.lease)
        second_uuid = (
            client.im.v1.message.create.call_args.args[0]
            .request_body.uuid
        )

        self.assertEqual(first_uuid, second_uuid)


def _success(message_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message_id=message_id),
    )


def _failure(code: int, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        success=lambda: False,
        code=code,
        msg=message,
    )


if __name__ == "__main__":
    unittest.main()
