"""OpenAI-compatible task provider tests without network access."""

from datetime import datetime
from dataclasses import replace
import json
import unittest
from zoneinfo import ZoneInfo

import httpx

from app.agent.context import (
    ContextMessage,
    ContextParticipant,
    TaskDetectionContext,
)
from app.agent.provider import ModelProviderError, OpenAICompatibleTaskDetector
from app.agent.prompt import build_task_batch_detection_input
from app.config import TaskLlmSettings


class OpenAICompatibleTaskDetectorTest(unittest.TestCase):
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
                    "om_1", "ou_teacher", "老师", "还缺 baseline", timestamp
                ),
                ContextMessage(
                    "om_2", "ou_wang", "王政", "我来补", timestamp
                ),
                ContextMessage(
                    "om_3", "ou_teacher", "老师", "周四前完成", timestamp
                ),
            ),
        )
        self.settings = TaskLlmSettings(
            api_key="private-test-key",
            base_url="https://llm.example.test/v1",
            model="qwen-test",
            timeout_seconds=10,
            max_retries=0,
        )

    def test_uses_strict_json_schema_and_parses_grounded_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(
                request.headers["Authorization"], "Bearer private-test-key"
            )
            body = json.loads(request.content)
            response_format = body["response_format"]
            self.assertEqual(response_format["type"], "json_schema")
            schema = response_format["json_schema"]["schema"]
            self.assertFalse(schema["additionalProperties"])
            return self._success_response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            call = detector.detect(self.context)

        self.assertTrue(call.result.is_task)
        self.assertEqual(call.result.owner.open_id, "ou_wang")
        self.assertEqual(call.response_format, "json_schema")
        self.assertEqual(call.request_id, "req_test")
        self.assertEqual(call.usage["total_tokens"], 150)

    def test_falls_back_to_json_object_when_schema_is_unsupported(self) -> None:
        response_formats = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            mode = body["response_format"]["type"]
            response_formats.append(mode)
            if mode == "json_schema":
                return httpx.Response(
                    400,
                    json={"error": {"message": "unsupported response_format"}},
                    request=request,
                )
            return self._success_response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            call = detector.detect(self.context)

        self.assertEqual(response_formats, ["json_schema", "json_object"])
        self.assertEqual(call.response_format, "json_object")

    def test_rejects_model_output_with_invented_owner(self) -> None:
        invalid = self._valid_result()
        invalid["owner"] = {"name": "陌生人", "open_id": "ou_invented"}

        def handler(request: httpx.Request) -> httpx.Response:
            return self._success_response(request, result=invalid)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            with self.assertRaisesRegex(ModelProviderError, "本地契约校验"):
                detector.detect(self.context)

    def test_detect_batch_uses_strict_schema_and_returns_all_candidates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            response_format = body["response_format"]
            self.assertEqual(response_format["type"], "json_schema")
            self.assertEqual(
                response_format["json_schema"]["name"],
                "task_detection_batch",
            )
            schema = response_format["json_schema"]["schema"]
            self.assertEqual(schema["properties"]["candidates"]["maxItems"], 10)
            self.assertEqual(body["max_tokens"], 2_500)
            return self._batch_success_response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            call = detector.detect_batch(self.context)

        self.assertEqual(len(call.result.candidates), 2)
        self.assertEqual(call.result.candidates[0].owner.open_id, "ou_wang")
        self.assertEqual(call.result.candidates[1].owner.open_id, "ou_li")
        self.assertEqual(call.response_format, "json_schema")

    def test_task_scope_changes_batch_prompt_policy(self) -> None:
        broad = build_task_batch_detection_input(self.context)
        work_only = build_task_batch_detection_input(
            replace(self.context, task_scope="work_only")
        )

        self.assertIn("生活需求和个人跑腿都可以是任务", broad["instructions"])
        self.assertIn("必须排除", work_only["instructions"])
        self.assertEqual(broad["context"]["task_scope"], "broad")
        self.assertEqual(work_only["context"]["task_scope"], "work_only")

    def test_batch_prompt_defines_date_only_deadline_as_end_of_day(self) -> None:
        payload = build_task_batch_detection_input(self.context)
        instructions = payload["instructions"]
        self.assertIn("周五 23:59:59", instructions)
        self.assertIn("不能解析成周五 00:00", instructions)
        self.assertIn("两天内", instructions)
        self.assertIn("reference_time 加 48 小时", instructions)
        self.assertIn("不能改成后天 23:59:59", instructions)

    def test_detect_batch_falls_back_to_json_object(self) -> None:
        response_formats = []

        def handler(request: httpx.Request) -> httpx.Response:
            mode = json.loads(request.content)["response_format"]["type"]
            response_formats.append(mode)
            if mode == "json_schema":
                return httpx.Response(
                    422,
                    json={"error": {"message": "schema unsupported"}},
                    request=request,
                )
            return self._batch_success_response(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            call = detector.detect_batch(self.context)

        self.assertEqual(response_formats, ["json_schema", "json_object"])
        self.assertEqual(call.response_format, "json_object")

    def test_detect_batch_rejects_ungrounded_candidate(self) -> None:
        invalid = self._valid_batch_result()
        invalid["candidates"][0]["evidence_message_ids"] = ["om_other_chat"]

        def handler(request: httpx.Request) -> httpx.Response:
            return self._batch_success_response(request, result=invalid)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            with self.assertRaisesRegex(ModelProviderError, "本地契约校验"):
                detector.detect_batch(self.context)

    def test_lists_models(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [{"id": "qwen-a"}, {"id": "qwen-b"}]},
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            detector = OpenAICompatibleTaskDetector(
                self.settings, client=client
            )
            models = detector.list_models()

        self.assertEqual(models, ("qwen-a", "qwen-b"))

    def _success_response(
        self,
        request: httpx.Request,
        *,
        result: dict | None = None,
    ) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "req_test"},
            json={
                "model": "qwen-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                result or self._valid_result(),
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            },
            request=request,
        )

    def _batch_success_response(
        self,
        request: httpx.Request,
        *,
        result: dict | None = None,
    ) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "req_batch_test"},
            json={
                "model": "qwen-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                result or self._valid_batch_result(),
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 220},
            },
            request=request,
        )

    @staticmethod
    def _valid_result() -> dict:
        return {
            "is_task": True,
            "confidence": 0.96,
            "owner": {"name": "王政", "open_id": "ou_wang"},
            "title": "补充 baseline 实验",
            "description": "完成 baseline 实验",
            "deadline": "2026-08-27T23:59:59+08:00",
            "evidence_message_ids": ["om_1", "om_2", "om_3"],
        }

    @classmethod
    def _valid_batch_result(cls) -> dict:
        first = cls._valid_result()
        first.pop("is_task")
        first["assignment_mode"] = "single"
        first["co_owners"] = []
        second = {
            **first,
            "owner": {"name": "李四", "open_id": "ou_li"},
            "title": "整理数据字典",
            "description": "整理数据字典",
        }
        return {"candidates": [first, second]}


if __name__ == "__main__":
    unittest.main()
