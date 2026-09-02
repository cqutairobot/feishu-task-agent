"""OpenAI-compatible natural-language task query provider tests."""

import json
import unittest

import httpx

from app.agent.provider import ModelProviderError, OpenAICompatibleTaskDetector
from app.config import TaskLlmSettings
from app.tasks.query_contracts import TaskQueryScope

class TaskQueryProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = TaskLlmSettings(
            api_key="query-test-key",
            base_url="https://llm.example.test/v1",
            model="qwen-test",
            timeout_seconds=10,
            max_retries=0,
        )

    def test_uses_query_schema_and_parses_self_scope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(
                request.headers["Authorization"], "Bearer query-test-key"
            )
            body = json.loads(request.content)
            response_format = body["response_format"]
            self.assertEqual(response_format["type"], "json_schema")
            self.assertEqual(
                response_format["json_schema"]["name"],
                "task_query_detection",
            )
            self.assertFalse(
                response_format["json_schema"]["schema"]["additionalProperties"]
            )
            self.assertEqual(body["max_tokens"], 600)
            model_input = json.loads(body["messages"][1]["content"])
            self.assertEqual(model_input["message"]["chat_type"], "p2p")
            return self._response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            call = OpenAICompatibleTaskDetector(
                self.settings, client=client
            ).detect_task_query(
                "我还有什么待办？",
                chat_type="p2p",
                sender_name="王政",
            )

        self.assertTrue(call.result.is_query)
        self.assertIs(call.result.scope, TaskQueryScope.SELF)
        self.assertEqual(call.response_format, "json_schema")
        self.assertEqual(call.request_id, "req_query")
        self.assertEqual(call.usage["total_tokens"], 35)

    def test_parses_person_scope_without_authorizing_the_target(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return self._response(
                request,
                result={
                    "is_task_query": True,
                    "scope": "person",
                    "target_name": "王政",
                    "status": "open",
                    "confidence": 0.96,
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            call = OpenAICompatibleTaskDetector(
                self.settings, client=client
            ).detect_task_query(
                "王政还有哪些任务没完成？",
                chat_type="p2p",
                sender_name="林老师",
            )

        self.assertTrue(call.result.is_query)
        self.assertIs(call.result.scope, TaskQueryScope.PERSON)
        self.assertEqual(call.result.target_name, "王政")

    def test_falls_back_to_json_object(self) -> None:
        formats: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            mode = body["response_format"]["type"]
            formats.append(mode)
            if mode == "json_schema":
                return httpx.Response(
                    400,
                    json={"error": {"message": "unsupported schema"}},
                    request=request,
                )
            return self._response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            call = OpenAICompatibleTaskDetector(
                self.settings, client=client
            ).detect_task_query("还有哪些任务？", chat_type="p2p")

        self.assertEqual(formats, ["json_schema", "json_object"])
        self.assertEqual(call.response_format, "json_object")

    def test_rejects_malformed_query_output(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return self._response(
                request,
                result={
                    "is_task_query": True,
                    "scope": "self",
                    "target_name": "someone",
                    "status": "open",
                    "confidence": 0.99,
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            with self.assertRaisesRegex(ModelProviderError, "本地契约校验"):
                detector.detect_task_query("我还有什么待办？", chat_type="p2p")

    def _response(
        self,
        request: httpx.Request,
        *,
        result: dict | None = None,
    ) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "req_query"},
            json={
                "model": "qwen-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                result
                                or {
                                    "is_task_query": True,
                                    "scope": "self",
                                    "target_name": None,
                                    "status": "open",
                                    "confidence": 0.97,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 35},
            },
            request=request,
        )


if __name__ == "__main__":
    unittest.main()
