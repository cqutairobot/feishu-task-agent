"""Phase 6A read-only lifecycle detector CLI tests."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.agent.provider import TaskLifecycleDetectionCall
from app.config import DatabaseSettings, TaskLlmSettings
from app.lifecycle.contracts import (
    LifecycleAction,
    LifecycleCandidate,
    LifecycleDetectionResult,
)
from app.identity.aliases import MessageSender
from app.tasks.repository import CrossChatTaskEntry, TaskSnapshot, TaskStatus
from app.main import main


class LifecycleCliTest(unittest.TestCase):
    def test_prints_read_only_structured_candidate(self) -> None:
        timestamp = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)
        context = object()
        context_builder = MagicMock()
        context_builder.build.return_value = context
        detector = MagicMock()
        detector.detect_lifecycle.return_value = TaskLifecycleDetectionCall(
            result=LifecycleDetectionResult(
                updates=(
                    LifecycleCandidate(
                        action=LifecycleAction.COMPLETE,
                        confidence=0.97,
                        task_id=1,
                        new_deadline=None,
                        evidence_message_ids=("om_done",),
                    ),
                )
            ),
            model="qwen-test",
            response_format="json_schema",
            request_id="req_lifecycle",
            usage={"total_tokens": 180},
        )
        provider_context = MagicMock()
        provider_context.__enter__.return_value = detector
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            repository=MagicMock(),
            aliases=MagicMock(),
            tasks=MagicMock(),
        )
        output = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.load_task_llm_settings",
                return_value=TaskLlmSettings(
                    api_key="test-key",
                    base_url="https://llm.example.test/v1",
                    model="qwen-test",
                    timeout_seconds=10,
                    max_retries=0,
                ),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            patch(
                "app.lifecycle.context.LifecycleDetectionContextBuilder",
                return_value=context_builder,
            ),
            patch(
                "app.agent.provider.OpenAICompatibleTaskDetector",
                return_value=provider_context,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "lifecycle-detect",
                    "--chat-id",
                    "oc_lab",
                    "--message-id",
                    "om_done",
                    "--limit",
                    "20",
                ]
            )

        self.assertEqual(exit_code, 0)
        context_builder.build.assert_called_once_with(
            "oc_lab", "om_done", message_limit=20
        )
        detector.detect_lifecycle.assert_called_once_with(context)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["updates"][0]["task_id"], 1)
        self.assertEqual(payload["updates"][0]["action"], "complete")

    def test_private_command_detect_is_explicitly_read_only(self) -> None:
        timestamp = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)
        context = object()
        context_builder = MagicMock()
        context_builder.build.return_value = context
        detector = MagicMock()
        detector.detect_lifecycle.return_value = TaskLifecycleDetectionCall(
            result=LifecycleDetectionResult(
                updates=(
                    LifecycleCandidate(
                        action=LifecycleAction.COMPLETE,
                        confidence=0.98,
                        task_id=1,
                        new_deadline=None,
                        evidence_message_ids=("om_private",),
                    ),
                )
            ),
            model="qwen-test",
            response_format="json_schema",
            request_id="req_private",
            usage={"total_tokens": 120},
        )
        provider_context = MagicMock()
        provider_context.__enter__.return_value = detector
        aliases = MagicMock()
        aliases.sender_for_message.return_value = MessageSender(
            message_id="om_private",
            chat_id="oc_dm",
            open_id="ou_owner",
            chat_type="p2p",
        )
        tasks = MagicMock()
        entry = CrossChatTaskEntry(
            task=TaskSnapshot(
                task_id=1,
                chat_id="oc_lab",
                owner_open_id="ou_owner",
                owner_name="王政",
                title="验收记录",
                description="完成验收记录",
                deadline=None,
                status=TaskStatus.TODO,
                confidence=0.95,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            chat_name="实验群",
        )
        tasks.find_lifecycle_target_across_chats.return_value = entry
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            repository=MagicMock(),
            aliases=aliases,
            tasks=tasks,
        )
        output = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.load_task_llm_settings",
                return_value=TaskLlmSettings(
                    api_key="test-key",
                    base_url="https://llm.example.test/v1",
                    model="qwen-test",
                    timeout_seconds=10,
                    max_retries=0,
                ),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            patch(
                "app.lifecycle.context.PrivateLifecycleDetectionContextBuilder",
                return_value=context_builder,
            ),
            patch(
                "app.agent.provider.OpenAICompatibleTaskDetector",
                return_value=provider_context,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "private-lifecycle-detect",
                    "--message-id",
                    "om_private",
                    "--task-code",
                    "1A",
                    "--limit",
                    "12",
                ]
            )

        self.assertEqual(exit_code, 0)
        tasks.find_lifecycle_target_across_chats.assert_called_once_with(
            1, owner_open_id="ou_owner"
        )
        context_builder.build.assert_called_once_with(
            "oc_dm",
            "om_private",
            actor_open_id="ou_owner",
            task=entry,
            message_limit=12,
        )
        detector.detect_lifecycle.assert_called_once_with(context)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["updates"][0]["task_id"], 1)


if __name__ == "__main__":
    unittest.main()
