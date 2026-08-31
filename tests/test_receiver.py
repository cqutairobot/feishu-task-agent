"""Tests for the SDK-to-normalizer callback boundary."""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
)

from app.feishu.receiver import _on_card_action, _on_message
from app.identity.commands import IdentityCommandKind, IdentityCommandResult
from app.lifecycle.private_commands import (
    PrivateLifecycleCommandKind,
    PrivateLifecycleCommandResult,
)
from app.lifecycle.review_commands import (
    PrivateReviewCommandKind,
    PrivateReviewCommandResult,
)
from app.tasks.commands import TaskCommandKind, TaskCommandResult
from app.tasks.card_actions import TaskCardActionResult
from tests.test_messages import TEXT_EVENT


class ReceiverCallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sdk_event = lark.JSON.unmarshal(
            json.dumps(TEXT_EVENT), P2ImMessageReceiveV1
        )

    def test_prints_normalized_sdk_event(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            _on_message(self.sdk_event, frozenset())

        self.assertIn("[15:32:10]", output.getvalue())
        self.assertIn("chat_id: oc_test", output.getvalue())
        self.assertIn("message: 今天实验结果出来了吗？", output.getvalue())

    def test_ignores_chat_outside_allowlist(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            _on_message(self.sdk_event, frozenset({"oc_another"}))

        self.assertEqual(output.getvalue(), "")

    def test_outside_allowlist_accepts_only_dynamically_admitted_group(self) -> None:
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        group_commands = Mock()
        group_commands.allows_chat.return_value = True
        group_commands.matches.return_value = False
        group_commands.handle.return_value = None

        with redirect_stdout(io.StringIO()):
            _on_message(
                self.sdk_event,
                frozenset({"oc_another"}),
                ingestion_service=ingestion,
                group_management_commands=group_commands,
            )

        group_commands.allows_chat.assert_called_once()
        ingestion.process_message.assert_called_once()

    def test_direct_message_is_not_blocked_by_group_allowlist(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["chat_id"] = "oc_direct"
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "任务列表"}, ensure_ascii=False
        )
        sdk_event = lark.JSON.unmarshal(
            json.dumps(payload), P2ImMessageReceiveV1
        )
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        task_commands = Mock()
        task_commands.matches.return_value = True
        task_commands.handle.return_value = TaskCommandResult(
            kind=TaskCommandKind.LIST,
            succeeded=True,
            reply_text="你的任务",
        )
        replier = Mock()

        with redirect_stdout(io.StringIO()):
            _on_message(
                sdk_event,
                frozenset({"oc_group"}),
                ingestion,
                None,
                None,
                replier,
                task_commands,
            )

        self.assertFalse(
            ingestion.process_message.call_args.kwargs["enqueue_detection"]
        )
        replier.reply_text.assert_called_once_with("om_test", "你的任务")

    def test_persists_before_printing_storage_status(self) -> None:
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted")
        )
        output = io.StringIO()

        with redirect_stdout(output):
            _on_message(self.sdk_event, frozenset(), ingestion)

        ingestion.process_message.assert_called_once()
        self.assertTrue(
            ingestion.process_message.call_args.kwargs["enqueue_detection"]
        )
        self.assertIn("storage: inserted", output.getvalue())

    def test_enriches_name_before_persistence(self) -> None:
        directory = Mock()
        directory.enrich.side_effect = lambda message, **_kwargs: replace(
            message, sender_name="张三"
        )
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted")
        )

        output = io.StringIO()
        with redirect_stdout(output):
            _on_message(self.sdk_event, frozenset(), ingestion, directory)

        persisted = ingestion.process_message.call_args.args[0]
        self.assertEqual(persisted.sender_name, "张三")
        self.assertIn("sender_name: 张三", output.getvalue())

    def test_identity_command_forces_directory_refresh_before_binding(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "@_user_1 我的姓名：李明"}, ensure_ascii=False
        )
        payload["event"]["message"]["mentions"] = [
            {
                "key": "@_user_1",
                "id": {"open_id": "ou_bot"},
                "mentioned_type": "bot",
                "name": "任务机器人",
                "tenant_key": "tenant_test",
            }
        ]
        sdk_event = lark.JSON.unmarshal(
            json.dumps(payload), P2ImMessageReceiveV1
        )
        directory = Mock()
        directory.enrich.side_effect = lambda message, **_kwargs: message

        with redirect_stdout(io.StringIO()):
            _on_message(
                sdk_event,
                frozenset(),
                directory_service=directory,
            )

        directory.enrich.assert_called_once()
        self.assertTrue(directory.enrich.call_args.kwargs["force"])

    def test_inserted_identity_command_is_processed_and_replied(self) -> None:
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        commands = Mock()
        commands.matches.return_value = True
        commands.handle.return_value = IdentityCommandResult(
            kind=IdentityCommandKind.SELF_BIND,
            succeeded=True,
            reply_text="绑定成功",
        )
        replier = Mock()

        with redirect_stdout(io.StringIO()):
            _on_message(
                self.sdk_event,
                frozenset(),
                ingestion,
                None,
                commands,
                replier,
            )

        commands.handle.assert_called_once()
        self.assertFalse(
            ingestion.process_message.call_args.kwargs["enqueue_detection"]
        )
        replier.reply_text.assert_called_once_with("om_test", "绑定成功")

    def test_duplicate_event_does_not_repeat_identity_command(self) -> None:
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="duplicate", inserted=False)
        )
        commands = Mock()
        replier = Mock()

        with redirect_stdout(io.StringIO()):
            _on_message(
                self.sdk_event,
                frozenset(),
                ingestion,
                None,
                commands,
                replier,
            )

        commands.handle.assert_not_called()
        replier.reply_text.assert_not_called()

    def test_inserted_task_command_is_replied_and_not_detected(self) -> None:
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        identity_commands = Mock()
        identity_commands.matches.return_value = False
        identity_commands.handle.return_value = None
        task_commands = Mock()
        task_commands.matches.return_value = True
        task_commands.handle.return_value = TaskCommandResult(
            kind=TaskCommandKind.LIST,
            succeeded=True,
            reply_text="本群任务",
        )
        replier = Mock()

        with redirect_stdout(io.StringIO()):
            _on_message(
                self.sdk_event,
                frozenset(),
                ingestion,
                None,
                identity_commands,
                replier,
                task_commands,
            )

        self.assertFalse(
            ingestion.process_message.call_args.kwargs["enqueue_detection"]
        )
        identity_commands.handle.assert_called_once()
        task_commands.handle.assert_called_once()
        replier.reply_text.assert_called_once_with("om_test", "本群任务")

    def test_private_task_card_is_preferred_over_text(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["chat_id"] = "oc_direct"
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "任务列表"}, ensure_ascii=False
        )
        sdk_event = lark.JSON.unmarshal(
            json.dumps(payload), P2ImMessageReceiveV1
        )
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        task_commands = Mock()
        task_commands.matches.return_value = True
        card = {"header": {}, "elements": []}
        task_commands.handle.return_value = TaskCommandResult(
            kind=TaskCommandKind.LIST,
            succeeded=True,
            reply_text="文本降级",
            reply_card=card,
        )
        replier = Mock()
        replier.reply_card.return_value = True

        with redirect_stdout(io.StringIO()):
            _on_message(
                sdk_event,
                frozenset({"oc_group"}),
                ingestion,
                None,
                None,
                replier,
                task_commands,
            )

        replier.reply_card.assert_called_once_with(
            "om_test", card, fallback_text="文本降级"
        )
        replier.reply_text.assert_not_called()

    def test_card_callback_is_normalized_and_returns_raw_replacement(self) -> None:
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": "evt_card_action",
                "event_type": "card.action.trigger",
                "tenant_key": "tenant_test",
            },
            "event": {
                "operator": {"open_id": "ou_owner"},
                "action": {
                    "tag": "button",
                    "value": {
                        "command": "task_lifecycle",
                        "version": "1",
                        "task_code": "T-1A",
                        "action": "complete",
                    },
                },
                "context": {
                    "open_message_id": "om_card",
                    "open_chat_id": "oc_dm",
                },
            },
        }
        data = lark.JSON.unmarshal(
            json.dumps(payload), P2CardActionTrigger
        )
        processor = Mock()
        replacement = {"header": {}, "elements": []}
        processor.handle.return_value = TaskCardActionResult(
            succeeded=True,
            toast_type="success",
            toast_text="任务已完成",
            replacement_card=replacement,
        )

        with redirect_stdout(io.StringIO()):
            response = _on_card_action(data, processor)

        request = processor.handle.call_args.args[0]
        self.assertEqual(request.callback_id, "evt_card_action")
        self.assertEqual(request.actor_open_id, "ou_owner")
        self.assertEqual(request.card_message_id, "om_card")
        self.assertEqual(request.card_chat_id, "oc_dm")
        self.assertEqual(request.action_tag, "button")
        self.assertIsNone(request.option)
        self.assertIsNone(request.actor_timezone)
        self.assertEqual(request.value["task_code"], "T-1A")
        self.assertEqual(response.toast.type, "success")
        self.assertEqual(response.toast.content, "任务已完成")
        self.assertEqual(response.card.type, "raw")
        self.assertEqual(response.card.data, replacement)

    def test_datetime_picker_callback_includes_option_and_timezone(self) -> None:
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": "evt_reschedule",
                "event_type": "card.action.trigger",
            },
            "event": {
                "operator": {"open_id": "ou_owner"},
                "action": {
                    "tag": "picker_datetime",
                    "name": "new_deadline",
                    "option": "2026-09-05 18:30 +0800",
                    "timezone": "Asia/Shanghai",
                    "value": {
                        "command": "task_lifecycle",
                        "version": "1",
                        "task_code": "T-1A",
                        "action": "reschedule",
                    },
                },
                "context": {
                    "open_message_id": "om_card",
                    "open_chat_id": "oc_dm",
                },
            },
        }
        data = lark.JSON.unmarshal(
            json.dumps(payload), P2CardActionTrigger
        )
        processor = Mock()
        processor.handle.return_value = TaskCardActionResult(
            succeeded=True,
            toast_type="success",
            toast_text="任务已延期",
            replacement_card={"header": {}, "elements": []},
        )

        with redirect_stdout(io.StringIO()):
            _on_card_action(data, processor)

        request = processor.handle.call_args.args[0]
        self.assertEqual(request.action_tag, "picker_datetime")
        self.assertEqual(request.option, "2026-09-05 18:30 +0800")
        self.assertEqual(request.actor_timezone, "Asia/Shanghai")

    def test_malformed_card_callback_returns_error_without_processor_call(self) -> None:
        data = lark.JSON.unmarshal(
            json.dumps(
                {
                    "schema": "2.0",
                    "header": {
                        "event_id": "evt_bad",
                        "event_type": "card.action.trigger",
                    },
                    "event": {},
                }
            ),
            P2CardActionTrigger,
        )
        processor = Mock()

        response = _on_card_action(data, processor)

        processor.handle.assert_not_called()
        self.assertEqual(response.toast.type, "error")
        self.assertIsNone(response.card)

    def test_inserted_private_lifecycle_command_is_replied(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["chat_id"] = "oc_direct"
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "1A 已完成"}, ensure_ascii=False
        )
        sdk_event = lark.JSON.unmarshal(
            json.dumps(payload), P2ImMessageReceiveV1
        )
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        identity_commands = Mock()
        identity_commands.matches.return_value = False
        identity_commands.handle.return_value = None
        task_commands = Mock()
        task_commands.matches.return_value = False
        task_commands.handle.return_value = None
        lifecycle_commands = Mock()
        lifecycle_commands.matches.return_value = True
        lifecycle_commands.handle.return_value = PrivateLifecycleCommandResult(
            kind=PrivateLifecycleCommandKind.UPDATE,
            succeeded=True,
            reply_text="任务已完成",
        )
        replier = Mock()

        with redirect_stdout(io.StringIO()):
            _on_message(
                sdk_event,
                frozenset({"oc_group"}),
                ingestion,
                None,
                identity_commands,
                replier,
                task_commands,
                lifecycle_commands,
            )

        self.assertFalse(
            ingestion.process_message.call_args.kwargs["enqueue_detection"]
        )
        lifecycle_commands.handle.assert_called_once()
        replier.reply_text.assert_called_once_with("om_test", "任务已完成")

    def test_read_only_review_processor_runs_before_lifecycle_processor(self) -> None:
        payload = deepcopy(TEXT_EVENT)
        payload["event"]["message"]["chat_id"] = "oc_direct"
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "T-1A 重新开启，原因是缺少回滚步骤"},
            ensure_ascii=False,
        )
        sdk_event = lark.JSON.unmarshal(
            json.dumps(payload), P2ImMessageReceiveV1
        )
        ingestion = Mock()
        ingestion.process_message.return_value = SimpleNamespace(
            persistence=SimpleNamespace(status="inserted", inserted=True)
        )
        lifecycle_commands = Mock()
        lifecycle_commands.matches.return_value = True
        lifecycle_commands.handle.return_value = PrivateLifecycleCommandResult(
            kind=PrivateLifecycleCommandKind.UPDATE,
            succeeded=False,
            reply_text="旧生命周期回复",
        )
        review_commands = Mock()
        review_commands.matches.return_value = True
        review_commands.handle.return_value = PrivateReviewCommandResult(
            kind=PrivateReviewCommandKind.DETECT,
            succeeded=True,
            reply_text="只读识别成功，任务未修改",
        )
        replier = Mock()

        with redirect_stdout(io.StringIO()):
            _on_message(
                sdk_event,
                frozenset({"oc_group"}),
                ingestion_service=ingestion,
                message_replier=replier,
                lifecycle_commands=lifecycle_commands,
                review_commands=review_commands,
            )

        self.assertFalse(
            ingestion.process_message.call_args.kwargs["enqueue_detection"]
        )
        review_commands.handle.assert_called_once()
        lifecycle_commands.handle.assert_not_called()
        replier.reply_text.assert_called_once_with(
            "om_test", "只读识别成功，任务未修改"
        )


if __name__ == "__main__":
    unittest.main()
