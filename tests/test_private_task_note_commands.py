"""Private task-note command orchestration tests."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.feishu.messages import normalize_message_event
from app.tasks.note_commands import (
    PrivateTaskNoteCommandProcessor,
    is_private_task_note_message,
)
from app.tasks.note_contracts import TaskNoteCandidate, TaskNoteDetectionResult
from app.tasks.notes import TaskNoteResult, TaskNoteType
from app.tasks.repository import CrossChatTaskEntry, TaskSnapshot, TaskStatus
from tests.test_messages import TEXT_EVENT


class PrivateTaskNoteCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        timestamp = datetime(2026, 8, 30, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.entry = CrossChatTaskEntry(
            task=TaskSnapshot(
                task_id=1,
                chat_id="oc_lab",
                owner_open_id="ou_owner",
                owner_name="王政",
                title="补充 baseline 实验",
                description="完成不同随机种子的实验",
                deadline=None,
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
        self.notes = MagicMock()
        self.notes.append.return_value = TaskNoteResult(
            note_id=9,
            task_id=1,
            task_code="T-1A",
            author_open_id="ou_owner",
            author_name="王政",
            note_type=TaskNoteType.PROGRESS,
            content="两组实验已完成。",
            source_message_id="om_private",
            source_chat_id="oc_dm",
            completion_cycle=0,
            idempotency_key="task-note:private:hash",
            confidence=0.96,
            provider="openai_compatible",
            model="qwen-test",
            response_format="json_schema",
            model_request_id="req_note",
            prompt_tokens=30,
            completion_tokens=12,
            total_tokens=42,
            already_created=False,
            created_at=self.now,
        )

    def test_owner_note_is_appended_with_private_source_and_model_audit(self) -> None:
        self.detector.detect_note.return_value = self._call()
        result = self._processor().handle(self._message("T-1A 进度：两组实验已完成。"))

        self.assertTrue(result.succeeded)
        self.assertIn("已记录任务说明", result.reply_text)
        self.assertIn("任务状态未改变", result.reply_text)
        kwargs = self.notes.append.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], "oc_lab")
        self.assertEqual(kwargs["source_chat_id"], "oc_dm")
        self.assertEqual(kwargs["source_message_id"], "om_private")
        self.assertEqual(kwargs["model_audit"].model, "qwen-test")
        self.assertEqual(kwargs["model_audit"].total_tokens, 42)

    def test_lifecycle_only_text_is_not_misclassified_as_a_note(self) -> None:
        lifecycle = self._message("T-1A 已完成")
        self.assertFalse(is_private_task_note_message(lifecycle))
        self.assertIsNone(self._processor().handle(lifecycle))
        self.detector.detect_note.assert_not_called()

    def test_completion_with_details_is_reserved_for_lifecycle_processor(
        self,
    ) -> None:
        lifecycle = self._message(
            "T-1A 已完成，结果和日志已经上传实验服务器。"
        )

        self.assertFalse(is_private_task_note_message(lifecycle))
        self.assertIsNone(self._processor().handle(lifecycle))
        self.detector.detect_note.assert_not_called()
        self.notes.append.assert_not_called()

    def test_partial_progress_stays_note_only(self) -> None:
        self.detector.detect_note.return_value = self._call()
        message = self._message(
            "T-1A 进度：已经完成两组实验，第三组仍在运行。"
        )

        self.assertTrue(is_private_task_note_message(message))
        result = self._processor().handle(message)

        self.assertTrue(result.succeeded)
        self.detector.detect_note.assert_called_once()
        self.notes.append.assert_called_once()

    def test_question_or_multiple_codes_does_not_write_a_note(self) -> None:
        question = self._message("T-1A 进度怎么样？")
        self.detector.detect_note.return_value = SimpleNamespace(
            result=TaskNoteDetectionResult(notes=())
        )
        result = self._processor().handle(question)
        self.assertFalse(result.succeeded)
        self.notes.append.assert_not_called()
        self.detector.detect_note.assert_called_once()

        self.detector.reset_mock()
        multiple = self._message("T-1A 进度：完成；T-2T 进度：未开始")
        result = self._processor().handle(multiple)
        self.assertFalse(result.succeeded)
        self.detector.detect_note.assert_not_called()

    def test_unknown_task_is_rejected_before_model_call(self) -> None:
        self.tasks.find_lifecycle_target_across_chats.return_value = None
        result = self._processor().handle(self._message("T-1A 备注：需要确认路径。"))
        self.assertFalse(result.succeeded)
        self.detector.detect_note.assert_not_called()
        self.notes.append.assert_not_called()

    def _processor(self) -> PrivateTaskNoteCommandProcessor:
        return PrivateTaskNoteCommandProcessor(
            self.tasks,
            self.context_builder,
            self.detector,
            self.notes,
            allowed_chat_ids=frozenset({"oc_lab"}),
            context_limit=12,
            clock=lambda: self.now,
        )

    def _call(self) -> SimpleNamespace:
        return SimpleNamespace(
            result=TaskNoteDetectionResult(
                notes=(
                    TaskNoteCandidate(
                        task_id=1,
                        note_type=TaskNoteType.PROGRESS,
                        content="两组实验已完成。",
                        confidence=0.96,
                        evidence_message_ids=("om_private",),
                    ),
                )
            ),
            model="qwen-test",
            response_format="json_schema",
            request_id="req_note",
            usage={
                "prompt_tokens": 30,
                "completion_tokens": 12,
                "total_tokens": 42,
            },
        )

    def _message(self, text: str):
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = "evt_private_note"
        payload["event"]["message"]["message_id"] = "om_private"
        payload["event"]["message"]["chat_id"] = "oc_dm"
        payload["event"]["message"]["chat_type"] = "p2p"
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["sender"]["sender_id"]["open_id"] = "ou_owner"
        return normalize_message_event(payload, received_at=self.now)


if __name__ == "__main__":
    unittest.main()
