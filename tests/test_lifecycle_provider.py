"""Phase 6A OpenAI-compatible lifecycle detector tests."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

import httpx

from app.agent.context import ContextMessage, TaskDetectionContext
from app.agent.provider import ModelProviderError, OpenAICompatibleTaskDetector
from app.config import TaskLlmSettings
from app.lifecycle.context import (
    LifecycleDetectionContext,
    LifecycleTaskReference,
)


class LifecycleProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(
            2026, 8, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        self.context = LifecycleDetectionContext(
            conversation=TaskDetectionContext(
                chat_id="oc_lab",
                trigger_message_id="om_done",
                timezone="Asia/Shanghai",
                reference_time=self.now,
                participants=(),
                messages=(
                    ContextMessage(
                        "om_done",
                        "ou_wang",
                        "王政",
                        "验收记录已经完成。",
                        self.now,
                    ),
                ),
                focus_message_ids=("om_done",),
            ),
            tasks=(
                LifecycleTaskReference(
                    1,
                    "ou_wang",
                    "王政",
                    "完成验收记录",
                    "完成 Phase 4B 验收记录",
                    None,
                    "todo",
                ),
            ),
        )
        self.settings = TaskLlmSettings(
            api_key="test-key",
            base_url="https://llm.example.test/v1",
            model="qwen-test",
            timeout_seconds=10,
            max_retries=0,
        )

    def test_uses_lifecycle_schema_and_parses_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            schema = body["response_format"]["json_schema"]
            self.assertEqual(schema["name"], "task_lifecycle_detection")
            self.assertEqual(body["max_tokens"], 1_500)
            user_input = json.loads(body["messages"][1]["content"])
            self.assertEqual(
                user_input["context"]["open_tasks"][0]["task_id"], 1
            )
            return self._response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            call = OpenAICompatibleTaskDetector(
                self.settings, client=client
            ).detect_lifecycle(self.context)

        self.assertEqual(call.result.updates[0].task_id, 1)
        self.assertEqual(call.response_format, "json_schema")

    def test_rejects_invented_task_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            result = self._result()
            result["updates"][0]["task_id"] = 999
            return self._response(request, result=result)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            with self.assertRaisesRegex(ModelProviderError, "本地契约校验"):
                detector.detect_lifecycle(self.context)

    def _response(
        self, request: httpx.Request, *, result: dict | None = None
    ) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "req_lifecycle"},
            json={
                "model": "qwen-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                result or self._result(),
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 180},
            },
            request=request,
        )

    @staticmethod
    def _result() -> dict:
        return {
            "updates": [
                {
                    "action": "complete",
                    "confidence": 0.97,
                    "task_id": 1,
                    "new_deadline": None,
                    "new_title": None,
                    "new_owners": [],
                    "evidence_message_ids": ["om_done"],
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
