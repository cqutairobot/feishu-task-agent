"""Private, code-anchored lifecycle command orchestration tests."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.feishu.messages import normalize_message_event
from app.lifecycle.contracts import (
    LifecycleAction,
    LifecycleCandidate,
    LifecycleDetectionResult,
)
from app.lifecycle.mutations import (
    LifecycleAuthorizationRole,
    LifecycleMutationError,
    LifecycleMutationResult,
)
from app.lifecycle.private_commands import (
    PrivateLifecycleCommandProcessor,
    is_private_lifecycle_command_message,
)
from app.tasks.repository import (
    CrossChatTaskEntry,
    TaskSnapshot,
    TaskStatus,
)
from tests.test_messages import TEXT_EVENT


SHANGHAI = ZoneInfo("Asia/Shanghai")


class PrivateLifecycleCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        timestamp = datetime(2026, 8, 23, 10, 0, tzinfo=SHANGHAI)
        self.entry = CrossChatTaskEntry(
            task=TaskSnapshot(
                task_id=1,
                chat_id="oc_lab",
                owner_open_id="ou_owner",
                owner_name="王政",
                title="验收记录",
                description="完成验收记录",
                deadline=datetime(2026, 8, 30, 18, 0, tzinfo=SHANGHAI),
                status=TaskStatus.TODO,
                confidence=0.95,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            chat_name="实验群",
        )
        self.tasks = MagicMock()
        self.tasks.find_lifecycle_target_across_chats.return_value = self.entry
        self.context_builder = MagicMock()
        self.context_builder.build.return_value = SimpleNamespace()
        self.detector = MagicMock()
        self.mutations = MagicMock()
        self.mutations.apply_candidate.return_value = self._mutation_result()

    def test_successful_completion_uses_owner_scope_and_model_audit(self) -> None:
        candidate = self._candidate(LifecycleAction.COMPLETE)
        self.detector.detect_lifecycle.return_value = self._call(candidate)
        message = self._message(
            "T-1A 已完成，结果和日志已经上传实验服务器。"
        )

        result = self._processor().handle(message)

        self.assertTrue(result.succeeded)
        self.assertIn("任务已标记完成", result.reply_text)
        self.assertIn("[T-1A] 验收记录", result.reply_text)
        self.assertIn("完成说明与来源证据已保存", result.reply_text)
        self.assertIn("等待本群管理员复核", result.reply_text)
        self.tasks.find_lifecycle_target_across_chats.assert_called_once_with(
            1,
            owner_open_id=None,
            chat_ids=frozenset({"oc_lab"}),
        )
        self.context_builder.build.assert_called_once_with(
            "oc_dm",
            "om_private",
            actor_open_id="ou_owner",
            task=self.entry,
            message_limit=12,
        )
        kwargs = self.mutations.apply_candidate.call_args.kwargs
        self.assertEqual(kwargs["task_code"], "T-1A")
        self.assertEqual(kwargs["model_audit"].model, "qwen-test")
        self.assertEqual(kwargs["model_audit"].total_tokens, 120)

    def test_administrator_preflight_does_not_restrict_to_owner(self) -> None:
        self.detector.detect_lifecycle.return_value = self._call(
            self._candidate(LifecycleAction.CANCEL)
        )
        message = self._message("取消 T-1A", sender="ou_admin")

        self._processor(admins={"ou_admin"}).handle(message)

        self.tasks.find_lifecycle_target_across_chats.assert_called_once_with(
            1,
            owner_open_id=None,
            chat_ids=frozenset({"oc_lab"}),
        )

    def test_persisted_group_administrator_can_update_another_members_task(self) -> None:
        chat_administrators = MagicMock()
        chat_administrators.is_administrator.return_value = True
        chat_administrators.admitted_chat_ids.return_value = frozenset(
            {"oc_lab", "oc_dynamic"}
        )
        self.detector.detect_lifecycle.return_value = self._call(
            self._candidate(LifecycleAction.CANCEL)
        )

        result = self._processor(
            chat_administrators=chat_administrators
        ).handle(self._message("取消 T-1A", sender="ou_admin"))

        self.assertTrue(result.succeeded)
        self.tasks.find_lifecycle_target_across_chats.assert_called_once_with(
            1,
            owner_open_id=None,
            chat_ids=frozenset({"oc_lab", "oc_dynamic"}),
        )
        chat_administrators.is_administrator.assert_called_once_with(
            "oc_lab", "ou_admin"
        )

    def test_corrections_are_rejected_for_member_and_allowed_for_admin(self) -> None:
        self.detector.detect_lifecycle.return_value = self._call(
            self._candidate(LifecycleAction.RENAME)
        )
        member_result = self._processor().handle(
            self._message("T-1A 标题改为最终验收记录")
        )
        self.assertFalse(member_result.succeeded)
        self.assertIn("仅允许本群任务管理员", member_result.reply_text)
        self.mutations.apply_candidate.assert_not_called()

        self.mutations.reset_mock()
        admin_result = self._processor(admins={"ou_admin"}).handle(
            self._message(
                "T-1A 标题改为最终验收记录",
                sender="ou_admin",
            )
        )
        self.assertTrue(admin_result.succeeded)
        self.mutations.apply_candidate.assert_called_once()

    def test_invalid_or_multiple_codes_are_rejected_without_model_call(self) -> None:
        for text in ("T-1B 已完成", "1A 已完成，T-2T 取消"):
            with self.subTest(text=text):
                result = self._processor().handle(self._message(text))
                self.assertFalse(result.succeeded)
        self.detector.detect_lifecycle.assert_not_called()
        self.mutations.apply_candidate.assert_not_called()

    def test_no_code_or_group_message_is_not_a_lifecycle_command(self) -> None:
        private = self._message("Phase 4B 已完成")
        group = self._message("1A 已完成", chat_type="group")

        self.assertIsNone(self._processor().handle(private))
        self.assertIsNone(self._processor().handle(group))
        self.assertFalse(is_private_lifecycle_command_message(private))
        self.assertFalse(is_private_lifecycle_command_message(group))

    def test_unauthorized_or_terminal_task_is_rejected_before_model(self) -> None:
        self.tasks.find_lifecycle_target_across_chats.return_value = None

        result = self._processor().handle(self._message("1A 已完成"))

        self.assertFalse(result.succeeded)
        self.assertIn("找不到可操作", result.reply_text)
        self.detector.detect_lifecycle.assert_not_called()

    def test_ambiguous_model_result_makes_no_change(self) -> None:
        self.detector.detect_lifecycle.return_value = self._call()

        result = self._processor().handle(self._message("1A 完成了吗"))

        self.assertFalse(result.succeeded)
        self.assertIn("没有识别到", result.reply_text)
        self.mutations.apply_candidate.assert_not_called()

    def test_model_or_mutation_failure_returns_safe_no_change_reply(self) -> None:
        self.detector.detect_lifecycle.side_effect = RuntimeError("provider")
        model_failure = self._processor().handle(self._message("1A 已完成"))
        self.assertIn("任务没有修改", model_failure.reply_text)

        self.detector.reset_mock(side_effect=True)
        self.detector.detect_lifecycle.return_value = self._call(
            self._candidate(LifecycleAction.COMPLETE)
        )
        self.mutations.apply_candidate.side_effect = LifecycleMutationError(
            "stale"
        )
        mutation_failure = self._processor().handle(
            self._message("1A 已完成")
        )
        self.assertIn("操作未执行", mutation_failure.reply_text)

    def _processor(
        self,
        *,
        admins: set[str] | frozenset[str] = frozenset(),
        chat_administrators=None,
    ) -> PrivateLifecycleCommandProcessor:
        return PrivateLifecycleCommandProcessor(
            self.tasks,
            self.context_builder,
            self.detector,
            self.mutations,
            administrator_open_ids=frozenset(admins),
            allowed_chat_ids=frozenset({"oc_lab"}),
            context_limit=12,
            clock=lambda: self.now,
            chat_administrators=chat_administrators,
        )

    def _candidate(
        self, action: LifecycleAction
    ) -> LifecycleCandidate:
        return LifecycleCandidate(
            action=action,
            confidence=0.98,
            task_id=1,
            new_deadline=None,
            evidence_message_ids=("om_private",),
        )

    def _call(self, *updates: LifecycleCandidate) -> SimpleNamespace:
        return SimpleNamespace(
            result=LifecycleDetectionResult(updates=tuple(updates)),
            model="qwen-test",
            response_format="json_schema",
            request_id="req_private",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        )

    def _mutation_result(self) -> LifecycleMutationResult:
        return LifecycleMutationResult(
            event_id=1,
            task_id=1,
            task_code="T-1A",
            action=LifecycleAction.COMPLETE,
            authorization_role=LifecycleAuthorizationRole.OWNER,
            previous_status=TaskStatus.TODO,
            new_status=TaskStatus.DONE,
            deadline_before=self.entry.task.deadline,
            deadline_after=self.entry.task.deadline,
            reminders_created=0,
            reminders_cancelled=4,
            already_applied=False,
            applied_at=self.now,
        )

    def _message(
        self,
        text: str,
        *,
        sender: str = "ou_owner",
        chat_type: str = "p2p",
    ):
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = "evt_private"
        payload["event"]["message"]["message_id"] = "om_private"
        payload["event"]["message"]["chat_id"] = "oc_dm"
        payload["event"]["message"]["chat_type"] = chat_type
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["sender"]["sender_id"]["open_id"] = sender
        return normalize_message_event(payload, received_at=self.now)


if __name__ == "__main__":
    unittest.main()
