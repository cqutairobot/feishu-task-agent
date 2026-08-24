"""Strict task detection JSON and grounding validation tests."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

from app.agent.context import (
    ContextMessage,
    ContextParticipant,
    TaskDetectionContext,
)
from app.agent.contracts import TaskOutputError, parse_task_detection_json


class TaskDetectionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        timestamp = datetime(
            2026, 8, 22, 15, 3, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        self.context = TaskDetectionContext(
            chat_id="oc_a",
            trigger_message_id="om_3",
            timezone="Asia/Shanghai",
            reference_time=timestamp,
            participants=(
                ContextParticipant("ou_teacher", "老师"),
                ContextParticipant("ou_wang", "王政"),
            ),
            messages=(
                ContextMessage(
                    "om_1",
                    "ou_teacher",
                    "老师",
                    "这个实验还缺一个 baseline",
                    timestamp,
                ),
                ContextMessage(
                    "om_2", "ou_wang", "王政", "我来补吧", timestamp
                ),
                ContextMessage(
                    "om_3",
                    "ou_teacher",
                    "老师",
                    "好，周四之前跑出来",
                    timestamp,
                ),
            ),
        )

    def test_accepts_grounded_task_json(self) -> None:
        result = parse_task_detection_json(
            json.dumps(self._valid_payload(), ensure_ascii=False), self.context
        )

        self.assertTrue(result.is_task)
        self.assertEqual(result.owner.open_id, "ou_wang")
        self.assertEqual(result.owner.name, "王政")
        self.assertEqual(result.deadline.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(result.evidence_message_ids, ("om_1", "om_2", "om_3"))

    def test_rejects_previous_name_after_member_renames(self) -> None:
        payload = self._valid_payload()
        payload["owner"]["name"] = "小王"

        with self.assertRaisesRegex(TaskOutputError, "confirmed names"):
            parse_task_detection_json(
                json.dumps(payload, ensure_ascii=False), self.context
            )

    def test_rejects_unknown_owner_open_id(self) -> None:
        payload = self._valid_payload()
        payload["owner"]["open_id"] = "ou_invented"

        with self.assertRaisesRegex(TaskOutputError, "known participant"):
            self._parse(payload)

    def test_rejects_name_that_does_not_match_open_id(self) -> None:
        payload = self._valid_payload()
        payload["owner"]["name"] = "老师"

        with self.assertRaisesRegex(TaskOutputError, "confirmed names"):
            self._parse(payload)

    def test_rejects_evidence_outside_current_chat_context(self) -> None:
        payload = self._valid_payload()
        payload["evidence_message_ids"].append("om_other_chat")

        with self.assertRaisesRegex(TaskOutputError, "outside the current context"):
            self._parse(payload)

    def test_rejects_unknown_top_level_field(self) -> None:
        payload = self._valid_payload()
        payload["reasoning"] = "hidden chain"

        with self.assertRaisesRegex(TaskOutputError, "fields do not match"):
            self._parse(payload)

    def test_rejects_naive_deadline(self) -> None:
        payload = self._valid_payload()
        payload["deadline"] = "2026-08-27T23:59:59"

        with self.assertRaisesRegex(TaskOutputError, "timezone"):
            self._parse(payload)

    def test_allows_task_without_deadline(self) -> None:
        payload = self._valid_payload()
        payload["deadline"] = None

        result = self._parse(payload)

        self.assertIsNone(result.deadline)

    def test_non_task_requires_null_fields_and_no_evidence(self) -> None:
        payload = {
            "is_task": False,
            "confidence": 0.97,
            "owner": None,
            "title": None,
            "description": None,
            "deadline": None,
            "evidence_message_ids": [],
        }

        result = self._parse(payload)

        self.assertFalse(result.is_task)

    def test_rejects_duplicate_json_fields(self) -> None:
        payload = (
            '{"is_task":false,"is_task":true,"confidence":1,'
            '"owner":null,"title":null,"description":null,'
            '"deadline":null,"evidence_message_ids":[]}'
        )

        with self.assertRaisesRegex(TaskOutputError, "duplicate JSON field"):
            parse_task_detection_json(payload, self.context)

    def test_rejects_duplicate_evidence(self) -> None:
        payload = self._valid_payload()
        payload["evidence_message_ids"] = ["om_1", "om_1"]

        with self.assertRaisesRegex(TaskOutputError, "duplicate evidence"):
            self._parse(payload)

    def _parse(self, payload: dict):
        return parse_task_detection_json(
            json.dumps(payload, ensure_ascii=False), self.context
        )

    @staticmethod
    def _valid_payload() -> dict:
        return {
            "is_task": True,
            "confidence": 0.96,
            "owner": {"name": "王政", "open_id": "ou_wang"},
            "title": "补充 baseline 实验",
            "description": "完成 ResNet50 baseline 实验",
            "deadline": "2026-08-27T23:59:59+08:00",
            "evidence_message_ids": ["om_1", "om_2", "om_3"],
        }


if __name__ == "__main__":
    unittest.main()
