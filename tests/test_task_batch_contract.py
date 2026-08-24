"""Strict zero-to-many task candidate contract tests."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

from app.agent.context import (
    ContextMessage,
    ContextParticipant,
    TaskDetectionContext,
)
from app.agent.contracts import (
    TaskOutputError,
    parse_task_detection_batch_json,
    task_detection_batch_json_schema,
)


class TaskDetectionBatchContractTest(unittest.TestCase):
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
                ContextParticipant("ou_li", "李四"),
            ),
            messages=(
                ContextMessage(
                    "om_1",
                    "ou_teacher",
                    "老师",
                    "王政周四前补完 baseline，李四周五前整理数据字典。",
                    timestamp,
                ),
                ContextMessage("om_2", "ou_wang", "王政", "收到", timestamp),
                ContextMessage("om_3", "ou_li", "李四", "好的", timestamp),
            ),
        )

    def test_accepts_multiple_grounded_candidates(self) -> None:
        result = self._parse(
            {
                "candidates": [
                    self._candidate(),
                    self._candidate(
                        owner_name="李四",
                        owner_open_id="ou_li",
                        title="整理数据字典",
                        deadline="2026-08-28T23:59:59+08:00",
                    ),
                ]
            }
        )

        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0].owner.open_id, "ou_wang")
        self.assertEqual(result.candidates[1].owner.open_id, "ou_li")

    def test_accepts_empty_candidates_for_no_task(self) -> None:
        result = self._parse({"candidates": []})

        self.assertEqual(result.candidates, ())

    def test_allows_distinct_tasks_to_share_evidence(self) -> None:
        first = self._candidate(evidence=["om_1"])
        second = self._candidate(
            owner_name="李四",
            owner_open_id="ou_li",
            title="整理数据字典",
            evidence=["om_1"],
        )

        result = self._parse({"candidates": [first, second]})

        self.assertEqual(len(result.candidates), 2)

    def test_accepts_one_shared_task_with_ordered_co_owners(self) -> None:
        candidate = self._candidate(
            assignment_mode="shared",
            co_owners=[{"name": "李四", "open_id": "ou_li"}],
        )

        result = self._parse({"candidates": [candidate]})

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            [owner.open_id for owner in result.candidates[0].owners],
            ["ou_wang", "ou_li"],
        )

    def test_rejects_shared_mode_without_a_co_owner(self) -> None:
        candidate = self._candidate(assignment_mode="shared")

        with self.assertRaisesRegex(TaskOutputError, "requires co_owners"):
            self._parse({"candidates": [candidate]})

    def test_rejects_single_mode_with_a_co_owner(self) -> None:
        candidate = self._candidate(
            co_owners=[{"name": "李四", "open_id": "ou_li"}],
        )

        with self.assertRaisesRegex(TaskOutputError, "cannot have co_owners"):
            self._parse({"candidates": [candidate]})

    def test_rejects_duplicate_responsible_member(self) -> None:
        candidate = self._candidate(
            assignment_mode="shared",
            co_owners=[{"name": "王政", "open_id": "ou_wang"}],
        )

        with self.assertRaisesRegex(TaskOutputError, "duplicate responsible"):
            self._parse({"candidates": [candidate]})

    def test_rejects_duplicate_candidate(self) -> None:
        candidate = self._candidate()

        with self.assertRaisesRegex(TaskOutputError, "duplicate task candidate"):
            self._parse({"candidates": [candidate, dict(candidate)]})

    def test_rejects_owner_outside_known_participants(self) -> None:
        candidate = self._candidate(owner_open_id="ou_invented")

        with self.assertRaisesRegex(TaskOutputError, "known participant"):
            self._parse({"candidates": [candidate]})

    def test_rejects_owner_name_mismatched_to_open_id(self) -> None:
        candidate = self._candidate(owner_name="李四")

        with self.assertRaisesRegex(TaskOutputError, "confirmed names"):
            self._parse({"candidates": [candidate]})

    def test_rejects_evidence_outside_current_context(self) -> None:
        candidate = self._candidate(evidence=["om_other_chat"])

        with self.assertRaisesRegex(TaskOutputError, "outside the current context"):
            self._parse({"candidates": [candidate]})

    def test_rejects_old_context_evidence_without_a_batch_focus_message(self) -> None:
        context = TaskDetectionContext(
            chat_id=self.context.chat_id,
            trigger_message_id=self.context.trigger_message_id,
            timezone=self.context.timezone,
            reference_time=self.context.reference_time,
            participants=self.context.participants,
            messages=self.context.messages,
            focus_message_ids=("om_3",),
        )
        candidate = self._candidate(evidence=["om_1"])

        with self.assertRaisesRegex(TaskOutputError, "batch-focus"):
            parse_task_detection_batch_json(
                json.dumps({"candidates": [candidate]}, ensure_ascii=False),
                context,
            )

    def test_rejects_empty_candidate_evidence(self) -> None:
        candidate = self._candidate(evidence=[])

        with self.assertRaisesRegex(TaskOutputError, "must not be empty"):
            self._parse({"candidates": [candidate]})

    def test_rejects_top_level_and_candidate_extra_fields(self) -> None:
        with self.assertRaisesRegex(TaskOutputError, "batch output fields"):
            self._parse({"candidates": [], "reasoning": "not allowed"})

        candidate = self._candidate()
        candidate["is_task"] = True
        with self.assertRaisesRegex(TaskOutputError, r"candidate\[0\] fields"):
            self._parse({"candidates": [candidate]})

    def test_rejects_more_than_ten_candidates(self) -> None:
        candidates = [self._candidate(title=f"任务 {index}") for index in range(11)]

        with self.assertRaisesRegex(TaskOutputError, "at most 10"):
            self._parse({"candidates": candidates})

    def test_provider_schema_is_closed_at_every_object_level(self) -> None:
        schema = task_detection_batch_json_schema()
        candidate_schema = schema["properties"]["candidates"]["items"]

        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(candidate_schema["additionalProperties"])
        self.assertFalse(
            candidate_schema["properties"]["owner"]["additionalProperties"]
        )

    def _parse(self, payload: dict):
        return parse_task_detection_batch_json(
            json.dumps(payload, ensure_ascii=False), self.context
        )

    @staticmethod
    def _candidate(
        *,
        owner_name: str = "王政",
        owner_open_id: str = "ou_wang",
        title: str = "补充 baseline 实验",
        deadline: str | None = "2026-08-27T23:59:59+08:00",
        evidence: list[str] | None = None,
        assignment_mode: str = "single",
        co_owners: list[dict[str, str]] | None = None,
    ) -> dict:
        return {
            "assignment_mode": assignment_mode,
            "confidence": 0.96,
            "co_owners": [] if co_owners is None else co_owners,
            "owner": {"name": owner_name, "open_id": owner_open_id},
            "title": title,
            "description": title,
            "deadline": deadline,
            "evidence_message_ids": ["om_1"] if evidence is None else evidence,
        }


if __name__ == "__main__":
    unittest.main()
