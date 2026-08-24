"""Phase 6C private task-list card rendering tests."""

from datetime import datetime, timezone
import json
import unittest

from app.tasks.cards import build_private_task_list_card
from app.tasks.repository import (
    CrossChatTaskEntry,
    CrossChatTaskListPage,
    TaskAssigneeSnapshot,
    TaskSnapshot,
    TaskStatus,
)


class PrivateTaskListCardTest(unittest.TestCase):
    def test_personal_card_contains_stable_code_and_shanghai_deadline(self) -> None:
        page = CrossChatTaskListPage(
            total_count=1,
            entries=(
                CrossChatTaskEntry(
                    task=self._task(),
                    chat_name="前端实验组",
                ),
            ),
        )

        card = build_private_task_list_card(page, is_admin=False)
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertEqual(card["header"]["title"]["content"], "我的未完成任务")
        self.assertIn("[T-1A]", rendered)
        self.assertIn("2026-08-30 18:00", rendered)
        self.assertIn("前端实验组", rendered)
        self.assertNotIn("负责人：", rendered)
        self.assertFalse(card["config"]["enable_forward"])

    def test_admin_card_shows_owner_and_hidden_count(self) -> None:
        page = CrossChatTaskListPage(
            total_count=3,
            entries=(
                CrossChatTaskEntry(task=self._task(), chat_name="实验组"),
            ),
        )

        card = build_private_task_list_card(page, is_admin=True)
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertIn("全部未完成任务", rendered)
        self.assertIn("负责人：王政", rendered)
        self.assertIn("另有 **2** 项未显示", rendered)

    def test_shared_task_shows_all_responsible_members_in_personal_card(self) -> None:
        task = self._task()
        shared = TaskSnapshot(
            task_id=task.task_id,
            chat_id=task.chat_id,
            owner_open_id=task.owner_open_id,
            owner_name=task.owner_name,
            title=task.title,
            description=task.description,
            deadline=task.deadline,
            status=task.status,
            confidence=task.confidence,
            created_at=task.created_at,
            updated_at=task.updated_at,
            assignees=(
                TaskAssigneeSnapshot("ou_wang", "王政", 0),
                TaskAssigneeSnapshot("ou_li", "李四", 1),
            ),
        )

        card = build_private_task_list_card(
            CrossChatTaskListPage(
                total_count=1,
                entries=(
                    CrossChatTaskEntry(shared, chat_name="实验组"),
                ),
            ),
            is_admin=False,
        )
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertIn("共同负责人：王政、李四", rendered)

    def test_empty_card_is_explicit_and_has_no_update_hint(self) -> None:
        card = build_private_task_list_card(
            CrossChatTaskListPage(total_count=0, entries=()),
            is_admin=False,
        )
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertIn("你当前没有未完成任务", rendered)
        self.assertNotIn("延期到", rendered)

    def test_nonempty_card_has_no_broken_natural_language_footer(self) -> None:
        card = build_private_task_list_card(
            CrossChatTaskListPage(
                total_count=1,
                entries=(
                    CrossChatTaskEntry(
                        task=self._task(), chat_name="实验组"
                    ),
                ),
            ),
            is_admin=False,
            actions_enabled=True,
        )
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertNotIn("更新任务时直接回复", rendered)
        self.assertNotIn("1A已完成", rendered)

    def test_stored_text_cannot_inject_mentions_or_card_markdown(self) -> None:
        unsafe = self._task(
            title="<at id=all>全体</at> **危险** `代码`",
            owner_name="<at id=ou_other>某人</at>",
        )
        card = build_private_task_list_card(
            CrossChatTaskListPage(
                total_count=1,
                entries=(CrossChatTaskEntry(task=unsafe, chat_name="<测试群>"),),
            ),
            is_admin=True,
        )
        rendered = json.dumps(card, ensure_ascii=False)
        task_content = card["elements"][1]["text"]["content"]

        self.assertNotIn("<at", rendered)
        self.assertIn("‹at id=all›", rendered)
        self.assertIn(r"\*\*危险\*\*", task_content)

    def test_action_buttons_use_only_versioned_code_and_explicit_action(self) -> None:
        card = build_private_task_list_card(
            CrossChatTaskListPage(
                total_count=1,
                entries=(
                    CrossChatTaskEntry(task=self._task(), chat_name="实验组"),
                ),
            ),
            is_admin=False,
            actions_enabled=True,
        )

        action_element = next(
            element for element in card["elements"] if element["tag"] == "action"
        )
        actions = action_element["actions"]
        self.assertEqual(
            [action["value"]["action"] for action in actions],
            ["complete", "reschedule", "cancel"],
        )
        self.assertEqual(
            [action["tag"] for action in actions],
            ["button", "picker_datetime", "button"],
        )
        for action in actions:
            self.assertEqual(
                set(action["value"]),
                {"command", "version", "task_code", "action"},
            )
            self.assertEqual(action["value"]["task_code"], "T-1A")
            self.assertNotIn("owner_open_id", action["value"])
        picker = actions[1]
        self.assertEqual(picker["name"], "new_deadline")
        self.assertEqual(picker["initial_datetime"], "2026-08-30 18:00")
        self.assertIn("confirm", picker)
        self.assertIn("confirm", actions[2])

    def test_pending_task_has_no_lifecycle_buttons(self) -> None:
        pending = self._task()
        pending = TaskSnapshot(
            task_id=pending.task_id,
            chat_id=pending.chat_id,
            owner_open_id=pending.owner_open_id,
            owner_name=pending.owner_name,
            title=pending.title,
            description=pending.description,
            deadline=pending.deadline,
            status=TaskStatus.PENDING,
            confidence=pending.confidence,
            created_at=pending.created_at,
            updated_at=pending.updated_at,
        )
        card = build_private_task_list_card(
            CrossChatTaskListPage(
                total_count=1,
                entries=(CrossChatTaskEntry(task=pending, chat_name="实验组"),),
            ),
            is_admin=False,
            actions_enabled=True,
        )

        self.assertFalse(
            any(element["tag"] == "action" for element in card["elements"])
        )

    @staticmethod
    def _task(
        *,
        title: str = "完成前端页面",
        owner_name: str = "王政",
    ) -> TaskSnapshot:
        now = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)
        return TaskSnapshot(
            task_id=1,
            chat_id="oc_group",
            owner_open_id="ou_wang",
            owner_name=owner_name,
            title=title,
            description="完成开发",
            deadline=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
            status=TaskStatus.TODO,
            confidence=0.96,
            created_at=now,
            updated_at=now,
        )


if __name__ == "__main__":
    unittest.main()
