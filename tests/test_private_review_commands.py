"""Phase 9E-1 private read-only review command tests."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.feishu.messages import normalize_message_event
from app.lifecycle.mutations import (
    LifecycleAuthorizationRole,
    LifecycleMutationError,
    LifecycleMutationResult,
)
from app.lifecycle.review_commands import PrivateReviewCommandProcessor
from app.lifecycle.review_contracts import (
    ReviewAction,
    ReviewActionCandidate,
    ReviewDetectionResult,
)
from app.tasks.repository import CrossChatTaskEntry, TaskSnapshot, TaskStatus
from tests.test_messages import TEXT_EVENT


class PrivateReviewCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
        self.entry = CrossChatTaskEntry(
            task=TaskSnapshot(
                task_id=1,
                chat_id="oc_lab",
                owner_open_id="ou_owner",
                owner_name="王政",
                title="制定下一轮答辩演示方案",
                description="补充完整演示方案",
                deadline=None,
                status=TaskStatus.DONE,
                confidence=0.98,
                created_at=self.now,
                updated_at=self.now,
                review_status="pending",
                completion_cycle=1,
            ),
            chat_name="完整演示群1",
        )
        self.tasks = MagicMock()
        self.tasks.find_review_target_across_chats.return_value = self.entry
        self.context_builder = MagicMock()
        self.context_builder.build.return_value = SimpleNamespace()
        self.detector = MagicMock()
        self.mutations = MagicMock()

    def test_admin_accept_is_reported_as_read_only(self) -> None:
        self.detector.detect_review.return_value = self._call(
            ReviewAction.ACCEPT
        )

        result = self._processor(admins={"ou_admin"}).handle(
            self._message("T-1A 验收通过", sender="ou_admin")
        )

        self.assertTrue(result.succeeded)
        self.assertIn("识别动作：验收通过", result.reply_text)
        self.assertIn("没有执行任何任务修改", result.reply_text)
        self.tasks.find_review_target_across_chats.assert_called_once_with(
            1,
            chat_ids=frozenset({"oc_lab"}),
        )
        self.context_builder.build.assert_called_once_with(
            "oc_dm",
            "om_review",
            actor_open_id="ou_admin",
            task=self.entry,
            message_limit=12,
        )

    def test_admin_reopen_displays_grounded_reason_without_writing(self) -> None:
        self.detector.detect_review.return_value = self._call(
            ReviewAction.REOPEN,
            reason="缺少失败场景和回滚步骤",
        )

        result = self._processor(admins={"ou_admin"}).handle(
            self._message(
                "T-1A 重新开启，原因是缺少失败场景和回滚步骤。",
                sender="ou_admin",
            )
        )

        self.assertTrue(result.succeeded)
        self.assertIn("识别动作：重新开启", result.reply_text)
        self.assertIn("返工原因：缺少失败场景和回滚步骤", result.reply_text)
        self.assertNotIn("当前状态：待办", result.reply_text)

    def test_writes_require_explicit_confirmation(self) -> None:
        self.detector.detect_review.return_value = self._call(
            ReviewAction.ACCEPT
        )

        result = self._processor(
            admins={"ou_admin"}, writes_enabled=True
        ).handle(self._message("T-1A 验收通过", sender="ou_admin"))

        self.assertFalse(result.succeeded)
        self.assertIn("高风险操作", result.reply_text)
        self.assertIn("确认执行 T-1A 验收通过", result.reply_text)
        self.mutations.apply_private_review_action.assert_not_called()

    def test_confirmed_accept_is_applied_and_reported(self) -> None:
        self.detector.detect_review.return_value = self._call(
            ReviewAction.ACCEPT
        )
        self.mutations.apply_private_review_action.return_value = (
            LifecycleMutationResult(
                event_id=9,
                task_id=1,
                task_code="T-1A",
                action=ReviewAction.ACCEPT,
                authorization_role=LifecycleAuthorizationRole.ADMINISTRATOR,
                previous_status=TaskStatus.DONE,
                new_status=TaskStatus.DONE,
                deadline_before=None,
                deadline_after=None,
                reminders_created=0,
                reminders_cancelled=0,
                already_applied=False,
                applied_at=self.now,
            )
        )

        result = self._processor(
            admins={"ou_admin"}, writes_enabled=True
        ).handle(
            self._message("确认执行 T-1A 验收通过", sender="ou_admin")
        )

        self.assertTrue(result.succeeded)
        self.assertIn("任务已验收", result.reply_text)
        self.assertIn("溯源记录", result.reply_text)
        self.assertNotIn("只读", result.reply_text)
        self.mutations.apply_private_review_action.assert_called_once()
        kwargs = self.mutations.apply_private_review_action.call_args.kwargs
        self.assertEqual(kwargs["actor_open_id"], "ou_admin")
        self.assertEqual(kwargs["trigger_message_id"], "om_review")
        self.assertEqual(kwargs["task_code"], "T-1A")

    def test_confirmed_reopen_preserves_reason_in_success_reply(self) -> None:
        reason = "缺少失败场景和回滚步骤"
        self.detector.detect_review.return_value = self._call(
            ReviewAction.REOPEN, reason=reason
        )
        self.mutations.apply_private_review_action.return_value = (
            LifecycleMutationResult(
                event_id=10,
                task_id=1,
                task_code="T-1A",
                action=ReviewAction.REOPEN,
                authorization_role=LifecycleAuthorizationRole.ADMINISTRATOR,
                previous_status=TaskStatus.DONE,
                new_status=TaskStatus.TODO,
                deadline_before=None,
                deadline_after=None,
                reminders_created=4,
                reminders_cancelled=0,
                already_applied=False,
                applied_at=self.now,
            )
        )

        result = self._processor(
            admins={"ou_admin"}, writes_enabled=True
        ).handle(
            self._message(
                f"确认执行 T-1A 重新开启，原因是 {reason}",
                sender="ou_admin",
            )
        )

        self.assertTrue(result.succeeded)
        self.assertIn("任务已重新开启", result.reply_text)
        self.assertIn(f"返工原因：{reason}", result.reply_text)

    def test_mutation_error_returns_safe_no_change_reply(self) -> None:
        self.detector.detect_review.return_value = self._call(
            ReviewAction.ACCEPT
        )
        self.mutations.apply_private_review_action.side_effect = (
            LifecycleMutationError("state changed")
        )

        result = self._processor(
            admins={"ou_admin"}, writes_enabled=True
        ).handle(
            self._message("确认执行 T-1A 验收通过", sender="ou_admin")
        )

        self.assertFalse(result.succeeded)
        self.assertIn("未执行", result.reply_text)

    def test_non_admin_is_rejected_before_model(self) -> None:
        result = self._processor().handle(
            self._message("T-1A 验收通过", sender="ou_owner")
        )

        self.assertFalse(result.succeeded)
        self.assertIn("不是该任务来源群的管理员", result.reply_text)
        self.detector.detect_review.assert_not_called()

    def test_persisted_admin_and_dynamic_chat_scope_are_supported(self) -> None:
        chat_administrators = MagicMock()
        chat_administrators.admitted_chat_ids.return_value = frozenset(
            {"oc_lab", "oc_dynamic"}
        )
        chat_administrators.is_administrator.return_value = True
        self.detector.detect_review.return_value = self._call(
            ReviewAction.ACCEPT
        )

        result = self._processor(
            chat_administrators=chat_administrators
        ).handle(self._message("T-1A 确认验收通过", sender="ou_admin"))

        self.assertTrue(result.succeeded)
        self.tasks.find_review_target_across_chats.assert_called_once_with(
            1,
            chat_ids=frozenset({"oc_lab", "oc_dynamic"}),
        )
        chat_administrators.is_administrator.assert_called_once_with(
            "oc_lab", "ou_admin"
        )

    def test_question_or_reopen_without_reason_produces_no_intent(self) -> None:
        self.detector.detect_review.return_value = self._call()

        result = self._processor(admins={"ou_admin"}).handle(
            self._message("T-1A 能否验收通过？", sender="ou_admin")
        )

        self.assertFalse(result.succeeded)
        self.assertIn("没有识别到", result.reply_text)
        self.assertIn("重新开启必须同时给出具体原因", result.reply_text)
        self.assertIn("任务没有修改", result.reply_text)

    def test_open_or_unreviewable_task_falls_through_to_existing_processors(self) -> None:
        self.tasks.find_review_target_across_chats.return_value = None

        result = self._processor(admins={"ou_admin"}).handle(
            self._message("T-1A 已完成", sender="ou_admin")
        )

        self.assertIsNone(result)
        self.detector.detect_review.assert_not_called()

    def _processor(
        self,
        *,
        admins: set[str] | frozenset[str] = frozenset(),
        chat_administrators=None,
        writes_enabled: bool = False,
    ) -> PrivateReviewCommandProcessor:
        return PrivateReviewCommandProcessor(
            self.tasks,
            self.context_builder,
            self.detector,
            administrator_open_ids=frozenset(admins),
            allowed_chat_ids=frozenset({"oc_lab"}),
            context_limit=12,
            chat_administrators=chat_administrators,
            mutations=self.mutations if writes_enabled else None,
            review_writes_enabled=writes_enabled,
            clock=lambda: self.now,
        )

    def _call(
        self,
        action: ReviewAction | None = None,
        *,
        reason: str | None = None,
    ) -> SimpleNamespace:
        intents = ()
        if action is not None:
            intents = (
                ReviewActionCandidate(
                    action=action,
                    confidence=0.98,
                    task_id=1,
                    reason=reason,
                    evidence_message_ids=("om_review",),
                ),
            )
        return SimpleNamespace(
            result=ReviewDetectionResult(intents=intents),
            model="qwen-test",
            response_format="json_schema",
            request_id="req_review",
            usage={"total_tokens": 120},
        )

    def _message(self, text: str, *, sender: str):
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = "evt_review"
        payload["event"]["message"]["message_id"] = "om_review"
        payload["event"]["message"]["chat_id"] = "oc_dm"
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["sender"]["sender_id"]["open_id"] = sender
        return normalize_message_event(payload, received_at=self.now)


if __name__ == "__main__":
    unittest.main()
