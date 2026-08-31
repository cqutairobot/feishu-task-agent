"""Strictly-private Feishu task-notification sender tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.config import FeishuSettings, ReminderSettings
from app.feishu.task_notification_sender import (
    FeishuTaskNotificationSender,
    TaskNotificationDeliveryError,
    format_task_notification_text,
)
from app.notifications.repository import (
    TaskNotificationKind,
    TaskNotificationLease,
)


class TaskNotificationSenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret",
            }
        )
        self.now = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)

    def test_owner_deadline_prompt_uses_known_private_chat(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = _success("om_prompt")
        sender = FeishuTaskNotificationSender(
            self.settings,
            reminder_settings=ReminderSettings(test_mode=True),
            client=client,
        )

        receipt = sender.deliver(
            self._lease(TaskNotificationKind.MISSING_DEADLINE_OWNER)
        )

        self.assertEqual(receipt.receive_id_type, "chat_id")
        self.assertEqual(receipt.receive_id, "oc_private")
        request = client.im.v1.message.create.call_args.args[0]
        text = json.loads(request.request_body.content)["text"]
        self.assertIn("请设置任务截止时间", text)
        self.assertIn("超过 2 分钟", text)
        self.assertIn("T-1A 截止时间设为", text)

    def test_admin_status_and_overdue_texts_contain_task_identity(self) -> None:
        cases = (
            (
                TaskNotificationKind.TASK_CREATED_ASSIGNEE,
                "你有一个新任务",
            ),
            (
                TaskNotificationKind.TASK_DONE_ADMIN,
                "任务完成待复核",
            ),
            (
                TaskNotificationKind.TASK_CANCELLED_ADMIN,
                "任务取消通知",
            ),
            (
                TaskNotificationKind.TASK_OVERDUE_ADMIN,
                "任务逾期通知",
            ),
            (
                TaskNotificationKind.TASK_RESCHEDULED_ADMIN,
                "任务延期通知",
            ),
            (
                TaskNotificationKind.TASK_DONE_COASSIGNEE,
                "共同任务已提交完成",
            ),
            (
                TaskNotificationKind.TASK_CANCELLED_COASSIGNEE,
                "共同任务状态更新",
            ),
            (
                TaskNotificationKind.TASK_RESCHEDULED_COASSIGNEE,
                "共同任务截止时间已更新",
            ),
            (
                TaskNotificationKind.TASK_RENAMED_ASSIGNEE,
                "任务标题已纠正",
            ),
            (
                TaskNotificationKind.TASK_ASSIGNEE_ADDED,
                "你已被设为该任务的负责人",
            ),
            (
                TaskNotificationKind.TASK_ASSIGNEE_REMOVED,
                "你已不再负责该任务",
            ),
            (
                TaskNotificationKind.TASK_ASSIGNEES_CHANGED,
                "共同负责人已调整",
            ),
            (
                TaskNotificationKind.TASK_INVALIDATED_ASSIGNEE,
                "误识别任务已撤销",
            ),
            (
                TaskNotificationKind.TASK_RENAMED_ADMIN,
                "管理员任务纠错通知",
            ),
            (
                TaskNotificationKind.TASK_REASSIGNED_ADMIN,
                "管理员任务纠错通知",
            ),
            (
                TaskNotificationKind.TASK_INVALIDATED_ADMIN,
                "管理员任务纠错通知",
            ),
            (
                TaskNotificationKind.TASK_RESTORED_COASSIGNEE,
                "任务已恢复",
            ),
            (
                TaskNotificationKind.TASK_RESTORED_ADMIN,
                "管理员恢复任务通知",
            ),
            (
                TaskNotificationKind.TASK_REOPENED_COASSIGNEE,
                "任务已要求返工",
            ),
            (
                TaskNotificationKind.TASK_REOPENED_ADMIN,
                "任务返工通知",
            ),
            (
                TaskNotificationKind.TASK_ACCEPTED_COASSIGNEE,
                "任务验收通过",
            ),
            (
                TaskNotificationKind.TASK_ACCEPTED_ADMIN,
                "任务验收通过通知",
            ),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind):
                client = MagicMock()
                client.im.v1.message.create.return_value = _success(
                    f"om_{kind.value}"
                )
                sender = FeishuTaskNotificationSender(
                    self.settings, client=client
                )

                sender.deliver(self._lease(kind))

                request = client.im.v1.message.create.call_args.args[0]
                text = json.loads(request.request_body.content)["text"]
                self.assertIn(expected, text)
                self.assertIn("T-1A", text)
                self.assertIn("完成前端页面", text)
                self.assertIn("王政", text)
                if kind in {
                    TaskNotificationKind.TASK_DONE_ADMIN,
                    TaskNotificationKind.TASK_DONE_COASSIGNEE,
                }:
                    self.assertIn("等待管理员复核", text)
                if kind in {
                    TaskNotificationKind.TASK_REOPENED_COASSIGNEE,
                    TaskNotificationKind.TASK_REOPENED_ADMIN,
                }:
                    self.assertIn("当前交付缺少实验日志", text)

    def test_missing_deadline_text_uses_the_actual_scheduled_delay(self) -> None:
        cases = (
            (
                TaskNotificationKind.MISSING_DEADLINE_OWNER,
                timedelta(hours=1),
                "超过 1 小时",
            ),
            (
                TaskNotificationKind.MISSING_DEADLINE_ADMIN,
                timedelta(hours=2),
                "超过 2 小时",
            ),
            (
                TaskNotificationKind.MISSING_DEADLINE_ADMIN,
                timedelta(days=3),
                "超过 3 天",
            ),
        )
        for kind, delay, expected in cases:
            with self.subTest(kind=kind, delay=delay):
                text = format_task_notification_text(
                    self._lease(kind, created_delay=delay)
                )
                self.assertIn(expected, text)

    def test_private_failure_never_falls_back_to_group(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.return_value = SimpleNamespace(
            success=lambda: False,
            code=230002,
            msg="cannot initiate chat",
        )
        sender = FeishuTaskNotificationSender(self.settings, client=client)

        with self.assertRaises(TaskNotificationDeliveryError) as caught:
            sender.deliver(
                self._lease(
                    TaskNotificationKind.MISSING_DEADLINE_ADMIN,
                    private_chat_id=None,
                )
            )

        self.assertEqual(caught.exception.code, "230002")
        self.assertEqual(client.im.v1.message.create.call_count, 1)
        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "open_id")

    def test_delivery_uuid_is_stable_across_retry_attempts(self) -> None:
        client = MagicMock()
        client.im.v1.message.create.side_effect = (
            _success("om_first"),
            _success("om_same_delivery"),
        )
        sender = FeishuTaskNotificationSender(
            self.settings, client=client
        )
        first = self._lease(
            TaskNotificationKind.TASK_ACCEPTED_COASSIGNEE
        )
        retry = replace(
            first,
            attempt=2,
            worker_id="worker-after-restart",
            lease_expires_at=first.lease_expires_at + timedelta(minutes=2),
        )

        sender.deliver(first)
        sender.deliver(retry)

        requests = [
            call.args[0]
            for call in client.im.v1.message.create.call_args_list
        ]
        self.assertEqual(
            requests[0].request_body.uuid,
            requests[1].request_body.uuid,
        )
        self.assertTrue(
            requests[0].request_body.uuid.startswith("notification-")
        )

    def _lease(
        self,
        kind: TaskNotificationKind,
        *,
        private_chat_id: str | None = "oc_private",
        created_delay: timedelta = timedelta(minutes=2),
    ) -> TaskNotificationLease:
        return TaskNotificationLease(
            notification_id=7,
            task_id=1,
            kind=kind,
            recipient_open_id="ou_recipient",
            recipient_private_chat_id=private_chat_id,
            task_code="T-1A",
            owner_open_id="ou_owner",
            owner_name="王政",
            title="完成前端页面",
            status_snapshot="overdue",
            deadline=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
            deadline_before=datetime(
                2026, 8, 29, 10, 0, tzinfo=timezone.utc
            ),
            reason="当前交付缺少实验日志，请补齐后重新提交。",
            task_created_at=self.now - created_delay,
            scheduled_for=self.now,
            attempt=1,
            max_attempts=3,
            worker_id="worker",
            lease_expires_at=self.now,
        )


def _success(message_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(message_id=message_id),
    )


if __name__ == "__main__":
    unittest.main()
