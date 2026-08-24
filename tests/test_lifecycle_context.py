"""Phase 6A lifecycle context and task target selection tests."""

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.agent.context import TaskDetectionContext
from app.lifecycle.context import (
    LifecycleDetectionContextBuilder,
    PrivateLifecycleDetectionContextBuilder,
)
from app.identity.aliases import AliasBinding
from app.tasks.repository import (
    CrossChatTaskEntry,
    TaskSnapshot,
    TaskStatus,
)


class LifecycleContextTest(unittest.TestCase):
    def test_combines_one_chat_context_with_actionable_tasks(self) -> None:
        timestamp = datetime(
            2026, 8, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        conversation = TaskDetectionContext(
            chat_id="oc_lab",
            trigger_message_id="om_done",
            timezone="Asia/Shanghai",
            reference_time=timestamp,
            participants=(),
            messages=(),
            focus_message_ids=("om_done",),
        )
        conversation_builder = MagicMock()
        conversation_builder.build.return_value = conversation
        tasks = MagicMock()
        tasks.list_lifecycle_targets.return_value = (
            TaskSnapshot(
                task_id=1,
                chat_id="oc_lab",
                owner_open_id="ou_wang",
                owner_name="王政",
                title="验收记录",
                description="完成验收记录",
                deadline=None,
                status=TaskStatus.TODO,
                confidence=0.95,
                created_at=timestamp,
                updated_at=timestamp,
            ),
        )

        context = LifecycleDetectionContextBuilder(
            conversation_builder, tasks
        ).build("oc_lab", "om_done", message_limit=20)

        conversation_builder.build.assert_called_once_with(
            "oc_lab", "om_done", limit=20
        )
        tasks.list_lifecycle_targets.assert_called_once_with(
            "oc_lab", limit=50
        )
        self.assertEqual(context.tasks[0].task_id, 1)
        self.assertEqual(context.to_dict()["open_tasks"][0]["status"], "todo")
        self.assertEqual(
            context.to_dict()["open_tasks"][0]["task_code"], "T-1A"
        )

    def test_private_context_contains_only_the_pre_authorized_task(self) -> None:
        timestamp = datetime(
            2026, 8, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        conversation = TaskDetectionContext(
            chat_id="oc_dm",
            trigger_message_id="om_update",
            timezone="Asia/Shanghai",
            reference_time=timestamp,
            participants=(),
            messages=(),
            focus_message_ids=("om_update",),
        )
        conversation_builder = MagicMock()
        conversation_builder.build.return_value = conversation
        entry = CrossChatTaskEntry(
            task=TaskSnapshot(
                task_id=1,
                chat_id="oc_lab",
                owner_open_id="ou_wang",
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

        aliases = MagicMock()
        aliases.list_for_chat.return_value = [
            AliasBinding("oc_lab", "ou_wang", "王政", "self_command", 1.0),
            AliasBinding("oc_lab", "ou_ha", "王哈", "self_command", 1.0),
        ]
        context = PrivateLifecycleDetectionContextBuilder(
            conversation_builder,
            aliases,
        ).build(
            "oc_dm",
            "om_update",
            actor_open_id="ou_wang",
            task=entry,
            message_limit=12,
        )

        conversation_builder.build.assert_called_once_with(
            "oc_dm", "om_update", limit=12
        )
        payload = context.to_dict()
        self.assertEqual(
            payload["task_scope"],
            {
                "mode": "private_authorized_task",
                "actor_open_id": "ou_wang",
            },
        )
        self.assertEqual(len(payload["open_tasks"]), 1)
        self.assertEqual(
            payload["open_tasks"][0]["source_chat"],
            {"chat_id": "oc_lab", "name": "实验群"},
        )
        self.assertEqual(
            payload["eligible_owners"],
            [
                {"open_id": "ou_wang", "name": "王政"},
                {"open_id": "ou_ha", "name": "王哈"},
            ],
        )
        aliases.list_for_chat.assert_called_once_with("oc_lab")


if __name__ == "__main__":
    unittest.main()
