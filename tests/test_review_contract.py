"""Phase 9E-1 strict review-intent contract tests."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

from app.agent.context import ContextMessage, TaskDetectionContext
from app.lifecycle.context import LifecycleTaskReference
from app.lifecycle.review_context import (
    ReviewDetectionContext,
    ReviewTaskReference,
)
from app.lifecycle.review_contracts import (
    ReviewAction,
    ReviewOutputError,
    parse_review_detection_json,
    review_detection_json_schema,
)


class ReviewContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(
            2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        self.context = self._context(review_status="pending")

    def test_accepts_explicit_accept_and_requires_null_reason(self) -> None:
        result = self._parse(self._intent("accept", reason=None))

        self.assertEqual(result.intents[0].action, ReviewAction.ACCEPT)
        self.assertIsNone(result.intents[0].reason)

        payload = self._intent("accept", reason="不应出现")
        with self.assertRaisesRegex(ReviewOutputError, "reason to be null"):
            self._parse(payload)

    def test_reopen_requires_and_normalizes_reason(self) -> None:
        result = self._parse(
            self._intent("reopen", reason="  缺少失败场景\n和回滚步骤  ")
        )

        self.assertEqual(result.intents[0].action, ReviewAction.REOPEN)
        self.assertEqual(result.intents[0].reason, "缺少失败场景 和回滚步骤")

        with self.assertRaisesRegex(ReviewOutputError, "non-empty reason"):
            self._parse(self._intent("reopen", reason=None))

    def test_accept_requires_pending_but_reopen_allows_accepted(self) -> None:
        accepted = self._context(review_status="accepted")
        with self.assertRaisesRegex(ReviewOutputError, "pending"):
            parse_review_detection_json(
                json.dumps(self._intent("accept", reason=None)), accepted
            )
        result = parse_review_detection_json(
            json.dumps(self._intent("reopen", reason="补充现场分工表")),
            accepted,
        )
        self.assertEqual(result.intents[0].action, ReviewAction.REOPEN)

    def test_rejects_invented_task_and_ungrounded_evidence(self) -> None:
        payload = self._intent("reopen", reason="缺少回滚步骤")
        payload["intents"][0]["task_id"] = 999
        with self.assertRaisesRegex(ReviewOutputError, "authorized"):
            self._parse(payload)

        payload = self._intent("reopen", reason="缺少回滚步骤")
        payload["intents"][0]["evidence_message_ids"] = ["om_unknown"]
        with self.assertRaisesRegex(ReviewOutputError, "outside"):
            self._parse(payload)

    def test_contract_allows_empty_result_and_only_review_actions(self) -> None:
        self.assertEqual(self._parse({"intents": []}).intents, ())
        actions = review_detection_json_schema()["properties"]["intents"][
            "items"
        ]["properties"]["action"]["enum"]
        self.assertEqual(actions, ["accept", "reopen"])

    def _context(self, *, review_status: str) -> ReviewDetectionContext:
        conversation = TaskDetectionContext(
            chat_id="oc_dm",
            trigger_message_id="om_review",
            timezone="Asia/Shanghai",
            reference_time=self.now,
            participants=(),
            messages=(
                ContextMessage(
                    "om_review",
                    "ou_admin",
                    "林老师",
                    "T-1A 重新开启，原因是缺少失败场景和回滚步骤。",
                    self.now,
                ),
            ),
            focus_message_ids=("om_review",),
        )
        return ReviewDetectionContext(
            conversation=conversation,
            tasks=(
                ReviewTaskReference(
                    task=LifecycleTaskReference(
                        task_id=1,
                        owner_open_id="ou_owner",
                        owner_name="王政",
                        title="制定答辩演示方案",
                        description="补充完整演示方案",
                        deadline=None,
                        status="done",
                        source_chat_id="oc_lab",
                        source_chat_name="实验群",
                    ),
                    review_status=review_status,
                    completion_cycle=1,
                ),
            ),
            actor_open_id="ou_admin",
        )

    def _parse(self, payload: dict):
        return parse_review_detection_json(
            json.dumps(payload, ensure_ascii=False), self.context
        )

    @staticmethod
    def _intent(action: str, *, reason: str | None) -> dict:
        return {
            "intents": [
                {
                    "action": action,
                    "confidence": 0.98,
                    "task_id": 1,
                    "reason": reason,
                    "evidence_message_ids": ["om_review"],
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
