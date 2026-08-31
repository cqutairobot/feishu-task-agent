"""Phase 9E-1 provider wiring for read-only review detection."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

import httpx

from app.agent.context import ContextMessage, TaskDetectionContext
from app.agent.provider import OpenAICompatibleTaskDetector
from app.config import TaskLlmSettings
from app.lifecycle.context import LifecycleTaskReference
from app.lifecycle.review_context import (
    ReviewDetectionContext,
    ReviewTaskReference,
)


class ReviewProviderTest(unittest.TestCase):
    def test_uses_dedicated_schema_and_parses_reopen_reason(self) -> None:
        now = datetime(
            2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        context = ReviewDetectionContext(
            conversation=TaskDetectionContext(
                chat_id="oc_dm",
                trigger_message_id="om_review",
                timezone="Asia/Shanghai",
                reference_time=now,
                participants=(),
                messages=(
                    ContextMessage(
                        "om_review",
                        "ou_admin",
                        "林老师",
                        "T-1A 重新开启，原因是缺少回滚步骤。",
                        now,
                    ),
                ),
                focus_message_ids=("om_review",),
            ),
            tasks=(
                ReviewTaskReference(
                    LifecycleTaskReference(
                        1,
                        "ou_owner",
                        "王政",
                        "制定演示方案",
                        "制定演示方案",
                        None,
                        "done",
                    ),
                    "pending",
                    1,
                ),
            ),
            actor_open_id="ou_admin",
        )
        settings = TaskLlmSettings(
            api_key="test-key",
            base_url="https://llm.example.test/v1",
            model="qwen-test",
            timeout_seconds=10,
            max_retries=0,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            schema = body["response_format"]["json_schema"]
            self.assertEqual(schema["name"], "task_review_action_detection")
            self.assertEqual(body["max_tokens"], 1_200)
            model_context = json.loads(body["messages"][1]["content"])[
                "context"
            ]
            self.assertEqual(
                model_context["reviewable_tasks"][0]["review_status"],
                "pending",
            )
            return httpx.Response(
                200,
                headers={"x-request-id": "req_review"},
                json={
                    "model": "qwen-test",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intents": [
                                            {
                                                "action": "reopen",
                                                "confidence": 0.98,
                                                "task_id": 1,
                                                "reason": "缺少回滚步骤",
                                                "evidence_message_ids": [
                                                    "om_review"
                                                ],
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                    "usage": {"total_tokens": 120},
                },
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            call = OpenAICompatibleTaskDetector(
                settings, client=client
            ).detect_review(context)

        self.assertEqual(call.result.intents[0].reason, "缺少回滚步骤")
        self.assertEqual(call.response_format, "json_schema")


if __name__ == "__main__":
    unittest.main()
