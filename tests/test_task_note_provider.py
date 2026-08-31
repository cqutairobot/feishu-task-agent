"""OpenAI-compatible provider tests for task-note detection."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

import httpx

from app.agent.context import ContextMessage, TaskDetectionContext
from app.agent.provider import ModelProviderError, OpenAICompatibleTaskDetector
from app.config import TaskLlmSettings
from app.lifecycle.context import LifecycleDetectionContext, LifecycleTaskReference


class TaskNoteProviderTest(unittest.TestCase):
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
                        "T-1A 进度：两组实验已完成。",
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
                    "完成实验",
                    None,
                    "todo",
                ),
            ),
            scope="private_authorized_task",
            actor_open_id="ou_owner",
        )
        self.settings = TaskLlmSettings(
            api_key="test-key",
            base_url="https://llm.example.test/v1",
            model="qwen-test",
            timeout_seconds=10,
            max_retries=0,
        )

    def test_uses_note_schema_and_parses_model_audit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            schema = body["response_format"]["json_schema"]
            self.assertEqual(schema["name"], "task_note_detection")
            self.assertEqual(body["max_tokens"], 1_500)
            user_input = json.loads(body["messages"][1]["content"])
            self.assertEqual(user_input["context"]["open_tasks"][0]["task_id"], 1)
            return self._response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            call = OpenAICompatibleTaskDetector(
                self.settings, client=client
            ).detect_note(self.context)

        self.assertEqual(call.result.notes[0].task_id, 1)
        self.assertEqual(call.result.notes[0].note_type.value, "progress")
        self.assertEqual(call.request_id, "req_note")
        self.assertEqual(call.usage["total_tokens"], 42)

    def test_rejects_model_note_for_unknown_task(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            result = self._result()
            result["notes"][0]["task_id"] = 999
            return self._response(request, result=result)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(self.settings, client=client)
            with self.assertRaisesRegex(ModelProviderError, "本地契约校验"):
                detector.detect_note(self.context)

    def _response(
        self, request: httpx.Request, *, result: dict | None = None
    ) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "req_note"},
            json={
                "model": "qwen-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                result or self._result(), ensure_ascii=False
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 12,
                    "total_tokens": 42,
                },
            },
            request=request,
        )

    @staticmethod
    def _result() -> dict:
        return {
            "notes": [
                {
                    "task_id": 1,
                    "note_type": "progress",
                    "content": "两组实验已完成。",
                    "confidence": 0.96,
                    "evidence_message_ids": ["om_note"],
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
