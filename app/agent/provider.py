"""OpenAI-compatible provider for strict task detection."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Callable

import httpx

from app.agent.context import TaskDetectionContext
from app.agent.contracts import (
    TaskDetectionBatchResult,
    TaskDetectionResult,
    TaskOutputError,
    parse_task_detection_batch_json,
    parse_task_detection_json,
    task_detection_batch_json_schema,
    task_detection_json_schema,
)
from app.agent.prompt import (
    build_task_batch_detection_input,
    build_task_detection_input,
)
from app.config import TaskLlmSettings
from app.lifecycle.context import LifecycleDetectionContext
from app.lifecycle.contracts import (
    LifecycleDetectionResult,
    LifecycleOutputError,
    lifecycle_detection_json_schema,
    parse_lifecycle_detection_json,
)
from app.lifecycle.prompt import build_lifecycle_detection_input
from app.lifecycle.review_context import ReviewDetectionContext
from app.lifecycle.review_contracts import (
    ReviewDetectionResult,
    ReviewOutputError,
    parse_review_detection_json,
    review_detection_json_schema,
)
from app.lifecycle.review_prompt import build_review_detection_input
from app.tasks.note_contracts import (
    TaskNoteDetectionResult,
    TaskNoteOutputError,
    parse_task_note_detection_json,
    task_note_detection_json_schema,
)
from app.tasks.note_prompt import build_task_note_detection_input


class ModelProviderError(RuntimeError):
    """Raised when the configured model service cannot provide a result."""


@dataclass(frozen=True, slots=True)
class TaskDetectionCall:
    result: TaskDetectionResult
    model: str
    response_format: str
    request_id: str | None
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class TaskBatchDetectionCall:
    result: TaskDetectionBatchResult
    model: str
    response_format: str
    request_id: str | None
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class TaskLifecycleDetectionCall:
    result: LifecycleDetectionResult
    model: str
    response_format: str
    request_id: str | None
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class TaskReviewDetectionCall:
    result: ReviewDetectionResult
    model: str
    response_format: str
    request_id: str | None
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class TaskNoteDetectionCall:
    result: TaskNoteDetectionResult
    model: str
    response_format: str
    request_id: str | None
    usage: dict[str, int]


class OpenAICompatibleTaskDetector:
    """Call Chat Completions with structured output and a safe fallback."""

    def __init__(
        self,
        settings: TaskLlmSettings,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds)
        )
        self._sleeper = sleeper

    def __enter__(self) -> "OpenAICompatibleTaskDetector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def list_models(self) -> tuple[str, ...]:
        response = self._request("GET", "models")
        payload = self._response_json(response)
        records = payload.get("data")
        if not isinstance(records, list):
            raise ModelProviderError("模型列表响应缺少 data 数组")
        model_ids = []
        for record in records:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                model_ids.append(record["id"])
        return tuple(model_ids)

    def detect(self, context: TaskDetectionContext) -> TaskDetectionCall:
        """Detect a task, preferring JSON Schema and falling back to JSON mode."""

        model_input = build_task_detection_input(context)
        response = self._chat_completion(
            mode="json_schema",
            schema_name="task_detection",
            schema=task_detection_json_schema(),
            model_input=model_input,
            max_tokens=1_000,
        )
        mode = "json_schema"
        if response.status_code in {400, 422}:
            response = self._chat_completion(
                mode="json_object",
                schema_name="task_detection",
                schema=task_detection_json_schema(),
                model_input=model_input,
                max_tokens=1_000,
            )
            mode = "json_object"
        if not response.is_success:
            raise self._http_error(response)

        payload = self._response_json(response)
        content = self._message_content(payload)
        try:
            result = parse_task_detection_json(content, context)
        except TaskOutputError as exc:
            raise ModelProviderError(f"模型输出未通过本地契约校验：{exc}") from exc

        return TaskDetectionCall(
            result=result,
            model=self._response_model(payload),
            response_format=mode,
            request_id=response.headers.get("x-request-id"),
            usage=self._usage(payload),
        )

    def detect_batch(
        self, context: TaskDetectionContext
    ) -> TaskBatchDetectionCall:
        """Detect zero or more independent tasks in one context window."""

        model_input = build_task_batch_detection_input(context)
        response = self._chat_completion(
            mode="json_schema",
            schema_name="task_detection_batch",
            schema=task_detection_batch_json_schema(),
            model_input=model_input,
            max_tokens=2_500,
        )
        mode = "json_schema"
        if response.status_code in {400, 422}:
            response = self._chat_completion(
                mode="json_object",
                schema_name="task_detection_batch",
                schema=task_detection_batch_json_schema(),
                model_input=model_input,
                max_tokens=2_500,
            )
            mode = "json_object"
        if not response.is_success:
            raise self._http_error(response)

        payload = self._response_json(response)
        content = self._message_content(payload)
        try:
            result = parse_task_detection_batch_json(content, context)
        except TaskOutputError as exc:
            raise ModelProviderError(f"模型输出未通过本地契约校验：{exc}") from exc

        return TaskBatchDetectionCall(
            result=result,
            model=self._response_model(payload),
            response_format=mode,
            request_id=response.headers.get("x-request-id"),
            usage=self._usage(payload),
        )

    def detect_lifecycle(
        self, context: LifecycleDetectionContext
    ) -> TaskLifecycleDetectionCall:
        """Read lifecycle intents without mutating any task record."""

        model_input = build_lifecycle_detection_input(context)
        schema = lifecycle_detection_json_schema()
        response = self._chat_completion(
            mode="json_schema",
            schema_name="task_lifecycle_detection",
            schema=schema,
            model_input=model_input,
            max_tokens=1_500,
        )
        mode = "json_schema"
        if response.status_code in {400, 422}:
            response = self._chat_completion(
                mode="json_object",
                schema_name="task_lifecycle_detection",
                schema=schema,
                model_input=model_input,
                max_tokens=1_500,
            )
            mode = "json_object"
        if not response.is_success:
            raise self._http_error(response)

        payload = self._response_json(response)
        content = self._message_content(payload)
        try:
            result = parse_lifecycle_detection_json(content, context)
        except LifecycleOutputError as exc:
            raise ModelProviderError(
                f"模型生命周期输出未通过本地契约校验：{exc}"
            ) from exc
        return TaskLifecycleDetectionCall(
            result=result,
            model=self._response_model(payload),
            response_format=mode,
            request_id=response.headers.get("x-request-id"),
            usage=self._usage(payload),
        )

    def detect_review(
        self, context: ReviewDetectionContext
    ) -> TaskReviewDetectionCall:
        """Detect an accept/reopen intent without applying either action."""

        model_input = build_review_detection_input(context)
        schema = review_detection_json_schema()
        response = self._chat_completion(
            mode="json_schema",
            schema_name="task_review_action_detection",
            schema=schema,
            model_input=model_input,
            max_tokens=1_200,
        )
        mode = "json_schema"
        if response.status_code in {400, 422}:
            response = self._chat_completion(
                mode="json_object",
                schema_name="task_review_action_detection",
                schema=schema,
                model_input=model_input,
                max_tokens=1_200,
            )
            mode = "json_object"
        if not response.is_success:
            raise self._http_error(response)

        payload = self._response_json(response)
        content = self._message_content(payload)
        try:
            result = parse_review_detection_json(content, context)
        except ReviewOutputError as exc:
            raise ModelProviderError(
                f"模型复核动作输出未通过本地契约校验：{exc}"
            ) from exc
        return TaskReviewDetectionCall(
            result=result,
            model=self._response_model(payload),
            response_format=mode,
            request_id=response.headers.get("x-request-id"),
            usage=self._usage(payload),
        )

    def detect_note(
        self, context: LifecycleDetectionContext
    ) -> TaskNoteDetectionCall:
        """Detect at most one factual task note without changing task state."""

        model_input = build_task_note_detection_input(context)
        schema = task_note_detection_json_schema()
        response = self._chat_completion(
            mode="json_schema",
            schema_name="task_note_detection",
            schema=schema,
            model_input=model_input,
            max_tokens=1_500,
        )
        mode = "json_schema"
        if response.status_code in {400, 422}:
            response = self._chat_completion(
                mode="json_object",
                schema_name="task_note_detection",
                schema=schema,
                model_input=model_input,
                max_tokens=1_500,
            )
            mode = "json_object"
        if not response.is_success:
            raise self._http_error(response)
        payload = self._response_json(response)
        content = self._message_content(payload)
        try:
            result = parse_task_note_detection_json(content, context)
        except TaskNoteOutputError as exc:
            raise ModelProviderError(
                f"模型任务说明输出未通过本地契约校验：{exc}"
            ) from exc
        return TaskNoteDetectionCall(
            result=result,
            model=self._response_model(payload),
            response_format=mode,
            request_id=response.headers.get("x-request-id"),
            usage=self._usage(payload),
        )

    def _chat_completion(
        self,
        *,
        mode: str,
        schema_name: str,
        schema: dict[str, object],
        model_input: dict[str, object],
        max_tokens: int,
    ) -> httpx.Response:
        if mode == "json_schema":
            response_format: dict[str, object] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            response_format = {"type": "json_object"}

        payload = {
            "model": self._settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你只输出严格 JSON，不输出 Markdown。",
                },
                {
                    "role": "user",
                    "content": json.dumps(model_input, ensure_ascii=False),
                },
            ],
            "response_format": response_format,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        return self._request("POST", "chat/completions", json_payload=payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        url = f"{self._settings.base_url}/{path}"
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        attempts = self._settings.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_payload,
                )
            except httpx.HTTPError as exc:
                if attempt + 1 == attempts:
                    raise ModelProviderError(
                        f"模型服务网络请求失败：{type(exc).__name__}"
                    ) from exc
                self._sleeper(0.5 * (2**attempt))
                continue

            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt + 1 < attempts:
                self._sleeper(0.5 * (2**attempt))
                continue
            return response
        raise AssertionError("request retry loop ended unexpectedly")

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, object]:
        if not response.is_success:
            raise OpenAICompatibleTaskDetector._http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelProviderError("模型服务返回了非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise ModelProviderError("模型服务响应必须是 JSON 对象")
        return payload

    @staticmethod
    def _http_error(response: httpx.Response) -> ModelProviderError:
        code: str | None = None
        message: str | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                if isinstance(error.get("code"), str):
                    code = error["code"]
                if isinstance(error.get("message"), str):
                    message = error["message"].strip()[:300]
        details = [f"HTTP {response.status_code}"]
        if code:
            details.append(f"code={code}")
        if message:
            details.append(message)
        return ModelProviderError("模型服务请求失败：" + "；".join(details))

    @staticmethod
    def _message_content(payload: dict[str, object]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelProviderError("模型响应缺少 choices")
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise ModelProviderError("模型响应缺少 message")
        message = first["message"]
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            refusal = message.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                raise ModelProviderError("模型拒绝了任务识别请求")
            raise ModelProviderError("模型响应缺少文本 content")
        return content

    def _response_model(self, payload: dict[str, object]) -> str:
        model = payload.get("model")
        return model if isinstance(model, str) else self._settings.model

    @staticmethod
    def _usage(payload: dict[str, object]) -> dict[str, int]:
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            return {}
        result = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                result[field] = value
        return result
