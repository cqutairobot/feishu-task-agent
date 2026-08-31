"""Strict grounding tests for Phase 9C-2 task-note output."""

from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from app.agent.context import ContextMessage, TaskDetectionContext
from app.lifecycle.context import LifecycleDetectionContext, LifecycleTaskReference
from app.tasks.note_contracts import (
    TaskNoteOutputError,
    parse_task_note_detection_json,
)


class TaskNoteContractTest(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime(2026, 8, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.context = LifecycleDetectionContext(
            conversation=TaskDetectionContext(
                chat_id="oc_dm",
                trigger_message_id="om_note",
                timezone="Asia/Shanghai",
                reference_time=now,
                participants=(),
                messages=(
                    ContextMessage(
                        "om_note",
                        "ou_owner",
                        "王政",
                        "T-1A 进度：两组实验已完成，日志在 /srv/run.log。",
                        now,
                    ),
                ),
                focus_message_ids=("om_note",),
            ),
            tasks=(
                LifecycleTaskReference(
                    1,
                    "ou_owner",
                    "王政",
                    "补充 baseline 实验",
                    "完成不同随机种子的实验并记录结果",
                    None,
                    "todo",
                ),
            ),
            scope="private_authorized_task",
            actor_open_id="ou_owner",
        )

    def test_accepts_one_grounded_note_and_normalizes_content(self) -> None:
        result = parse_task_note_detection_json(
            '{"notes":[{"task_id":1,"note_type":"progress",'
            '"content":"  两组实验已完成。 ","confidence":0.94,'
            '"evidence_message_ids":["om_note"]}]}',
            self.context,
        )
        self.assertEqual(result.notes[0].task_id, 1)
        self.assertEqual(result.notes[0].content, "两组实验已完成。")
        self.assertEqual(result.notes[0].confidence, 0.94)

    def test_empty_notes_is_valid_for_questions_and_lifecycle_only_text(self) -> None:
        result = parse_task_note_detection_json('{"notes":[]}', self.context)
        self.assertEqual(result.notes, ())

    def test_rejects_invented_task_or_unrelated_evidence(self) -> None:
        invented = (
            '{"notes":[{"task_id":2,"note_type":"general",'
            '"content":"说明","confidence":0.9,'
            '"evidence_message_ids":["om_note"]}]}'
        )
        with self.assertRaises(TaskNoteOutputError):
            parse_task_note_detection_json(invented, self.context)

        unrelated = (
            '{"notes":[{"task_id":1,"note_type":"general",'
            '"content":"说明","confidence":0.9,'
            '"evidence_message_ids":["om_unknown"]}]}'
        )
        with self.assertRaises(TaskNoteOutputError):
            parse_task_note_detection_json(unrelated, self.context)

    def test_rejects_extra_fields_duplicate_fields_and_invalid_confidence(self) -> None:
        extra = (
            '{"notes":[{"task_id":1,"note_type":"general",'
            '"content":"说明","confidence":0.9,'
            '"evidence_message_ids":["om_note"],"extra":true}]}'
        )
        with self.assertRaises(TaskNoteOutputError):
            parse_task_note_detection_json(extra, self.context)

        duplicate = (
            '{"notes":[],"notes":[]}'
        )
        with self.assertRaises(TaskNoteOutputError):
            parse_task_note_detection_json(duplicate, self.context)

        invalid_confidence = (
            '{"notes":[{"task_id":1,"note_type":"general",'
            '"content":"说明","confidence":1.1,'
            '"evidence_message_ids":["om_note"]}]}'
        )
        with self.assertRaises(TaskNoteOutputError):
            parse_task_note_detection_json(invalid_confidence, self.context)


if __name__ == "__main__":
    unittest.main()
