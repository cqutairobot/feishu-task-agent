"""Phase 4C deterministic, chat-isolated task query tests."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import upgrade_database
from app.database.models import Chat, Task, TaskAssignee, User
from app.feishu.messages import normalize_message_event
from app.tasks.commands import (
    TaskCommandKind,
    TaskCommandProcessor,
    is_task_command_message,
)
from app.tasks.repository import TaskRepository, TaskStatus
from app.tasks.query_contracts import TaskQueryIntent, TaskQueryScope
from tests.test_messages import TEXT_EVENT


BOT_OPEN_ID = "ou_bot"
BOT_MENTION = {
    "key": "@_user_1",
    "id": {"open_id": BOT_OPEN_ID},
    "mentioned_type": "bot",
    "name": "任务机器人",
    "tenant_key": "tenant_test",
}


class TaskCommandProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "tasks.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.repository = TaskRepository(self.session_factory)
        self.now = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
        self._seed_tasks()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_list_returns_only_open_tasks_from_command_chat(self) -> None:
        result = self._processor().handle(self._message("oc_a"))

        self.assertIsNotNone(result)
        self.assertEqual(result.kind, TaskCommandKind.LIST)
        self.assertIn("逾期报告", result.reply_text)
        self.assertIn("前端页面", result.reply_text)
        self.assertIn("无截止任务", result.reply_text)
        self.assertNotIn("已经完成", result.reply_text)
        self.assertNotIn("已取消任务", result.reply_text)
        self.assertNotIn("李四的任务", result.reply_text)
        self.assertNotIn("另一个群的秘密任务", result.reply_text)
        self.assertNotIn("负责人：", result.reply_text)

    def test_repository_enforces_exact_chat_and_open_statuses(self) -> None:
        page = self.repository.list_open_tasks(
            "oc_a", owner_open_id="ou_wang"
        )

        self.assertEqual(page.chat_id, "oc_a")
        self.assertEqual(page.total_count, 3)
        self.assertEqual(
            [task.title for task in page.tasks],
            ["逾期报告", "前端页面", "无截止任务"],
        )
        self.assertTrue(
            all(task.chat_id == "oc_a" for task in page.tasks)
        )

    def test_lifecycle_targets_exclude_pending_terminal_and_other_chat(self) -> None:
        tasks = self.repository.list_lifecycle_targets("oc_a")

        self.assertEqual(
            {task.title for task in tasks},
            {"逾期报告", "前端页面", "李四的任务"},
        )
        self.assertTrue(
            all(
                task.chat_id == "oc_a"
                and task.status in {TaskStatus.TODO, TaskStatus.OVERDUE}
                for task in tasks
            )
        )

    def test_exact_private_lifecycle_target_enforces_owner_and_chat_scope(self) -> None:
        own = self.repository.find_lifecycle_target_across_chats(
            2,
            owner_open_id="ou_wang",
            chat_ids=frozenset({"oc_a"}),
        )
        wrong_owner = self.repository.find_lifecycle_target_across_chats(
            2,
            owner_open_id="ou_li",
            chat_ids=frozenset({"oc_a"}),
        )
        wrong_chat = self.repository.find_lifecycle_target_across_chats(
            2,
            owner_open_id="ou_wang",
            chat_ids=frozenset({"oc_b"}),
        )
        terminal = self.repository.find_lifecycle_target_across_chats(5)

        self.assertEqual(own.task.title, "前端页面")
        self.assertEqual(own.chat_name, "实验A")
        self.assertIsNone(wrong_owner)
        self.assertIsNone(wrong_chat)
        self.assertIsNone(terminal)

    def test_exact_review_target_requires_completed_reviewable_task(self) -> None:
        target = self.repository.find_review_target_across_chats(
            5,
            chat_ids=frozenset({"oc_a"}),
        )
        wrong_chat = self.repository.find_review_target_across_chats(
            5,
            chat_ids=frozenset({"oc_b"}),
        )
        open_task = self.repository.find_review_target_across_chats(2)

        self.assertEqual(target.task.title, "已经完成")
        self.assertEqual(target.task.review_status, "pending")
        self.assertEqual(target.task.completion_cycle, 1)
        self.assertIsNone(wrong_chat)
        self.assertIsNone(open_task)

    def test_deadline_is_rendered_in_shanghai_time(self) -> None:
        result = self._processor().handle(self._message("oc_a"))

        self.assertRegex(result.reply_text, r"\[T-[0-9A-Z]+\]")
        self.assertNotIn("[#", result.reply_text)
        self.assertIn("2026-08-30 18:00", result.reply_text)
        self.assertIn("截止：未设置", result.reply_text)
        self.assertIn("状态：已逾期", result.reply_text)

    def test_empty_chat_has_an_explicit_empty_reply(self) -> None:
        result = self._processor().handle(self._message("oc_empty"))

        self.assertEqual(
            result.reply_text, "📋 你在本群当前没有未完成任务。"
        )

    def test_reply_limit_reports_hidden_count(self) -> None:
        result = self._processor(reply_limit=2).handle(self._message("oc_a"))

        self.assertIn("你在本群的未完成任务（3 项）", result.reply_text)
        self.assertIn("另有 1 项未显示", result.reply_text)
        self.assertNotIn("无截止任务", result.reply_text)

    def test_command_can_be_classified_without_executing_query(self) -> None:
        message = self._message("oc_a", text="@_user_1 本群任务？")

        self.assertTrue(is_task_command_message(message))

    def test_plain_text_without_bot_mention_is_ignored(self) -> None:
        message = self._message("oc_a", text="任务列表", mentions=[])

        self.assertIsNone(self._processor().handle(message))
        self.assertTrue(is_task_command_message(message))

    def test_another_bot_mention_is_ignored(self) -> None:
        mention = deepcopy(BOT_MENTION)
        mention["id"]["open_id"] = "ou_other_bot"
        message = self._message("oc_a", mentions=[mention])

        self.assertIsNone(self._processor().handle(message))

    def test_group_administrator_sees_everyones_tasks_in_that_group(self) -> None:
        message = self._message("oc_a", sender_open_id="ou_admin")

        result = self._processor(admins={"ou_admin"}).handle(message)

        self.assertIn("管理员视图，共 4 项", result.reply_text)
        self.assertIn("李四的任务", result.reply_text)
        self.assertIn("负责人：李四", result.reply_text)
        self.assertNotIn("另一个群的秘密任务", result.reply_text)

    def test_persisted_administrator_is_limited_to_exact_groups(self) -> None:
        chat_administrators = Mock()
        chat_administrators.is_administrator.return_value = True
        chat_administrators.chat_ids_for_administrator.return_value = frozenset(
            {"oc_a"}
        )

        group_result = self._processor(
            chat_administrators=chat_administrators
        ).handle(self._message("oc_a", sender_open_id="ou_admin"))
        private_result = self._processor(
            allowed_chats={"oc_a", "oc_b"},
            chat_administrators=chat_administrators,
        ).handle(
            self._message(
                "oc_direct",
                text="任务列表",
                mentions=[],
                chat_type="p2p",
                sender_open_id="ou_admin",
            )
        )

        self.assertIn("管理员视图", group_result.reply_text)
        self.assertIn("李四的任务", group_result.reply_text)
        self.assertIn("管理员视图", private_result.reply_text)
        self.assertIn("李四的任务", private_result.reply_text)
        self.assertNotIn("另一个群的秘密任务", private_result.reply_text)

    def test_private_member_scope_includes_self_service_groups(self) -> None:
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset()
        chat_administrators.admitted_chat_ids.return_value = frozenset(
            {"oc_a", "oc_b"}
        )

        result = self._processor(
            allowed_chats={"oc_a"},
            chat_administrators=chat_administrators,
        ).handle(
            self._message(
                "oc_direct",
                text="任务列表",
                mentions=[],
                chat_type="p2p",
            )
        )

        chat_administrators.admitted_chat_ids.assert_called_once_with(
            frozenset({"oc_a"})
        )
        self.assertIn("群聊：实验A", result.reply_text)
        self.assertIn("群聊：实验B", result.reply_text)

    def test_private_chat_lists_only_sender_tasks_across_groups(self) -> None:
        message = self._message(
            "oc_direct",
            text="任务列表",
            mentions=[],
            chat_type="p2p",
        )

        result = self._processor().handle(message)

        self.assertIn("你的未完成任务（共 4 项）", result.reply_text)
        self.assertIn("群聊：实验A", result.reply_text)
        self.assertIn("群聊：实验B", result.reply_text)
        self.assertIn("另一个群的秘密任务", result.reply_text)
        self.assertNotIn("李四的任务", result.reply_text)
        self.assertNotIn("负责人：", result.reply_text)
        self.assertTrue(is_task_command_message(message))

    def test_shared_task_is_one_record_visible_to_every_assignee(self) -> None:
        with session_scope(self.session_factory) as session:
            task = self._task(
                "oc_a", "联合回归报告", TaskStatus.TODO
            )
            session.add(task)
            session.flush()
            session.add_all(
                (
                    TaskAssignee(
                        task_id=task.id,
                        open_id="ou_wang",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    ),
                    TaskAssignee(
                        task_id=task.id,
                        open_id="ou_li",
                        name_snapshot="李四",
                        position=1,
                        created_at=self.now,
                    ),
                )
            )
            shared_task_id = task.id

        wang = self.repository.list_open_tasks(
            "oc_a", owner_open_id="ou_wang"
        )
        li = self.repository.list_open_tasks(
            "oc_a", owner_open_id="ou_li"
        )

        wang_shared = next(
            task for task in wang.tasks if task.task_id == shared_task_id
        )
        li_shared = next(
            task for task in li.tasks if task.task_id == shared_task_id
        )
        self.assertEqual(wang_shared.public_code, li_shared.public_code)
        self.assertEqual(
            [member.name for member in wang_shared.responsible_members],
            ["王政", "李四"],
        )

    def test_private_card_is_opt_in_and_uses_same_authorized_page(self) -> None:
        message = self._message(
            "oc_direct",
            text="任务列表",
            mentions=[],
            chat_type="p2p",
        )

        result = self._processor(private_cards=True).handle(message)

        self.assertIsNotNone(result.reply_card)
        rendered = json.dumps(result.reply_card, ensure_ascii=False)
        self.assertIn("我的未完成任务", rendered)
        self.assertIn("前端页面", rendered)
        self.assertIn("另一个群的秘密任务", rendered)
        self.assertNotIn("李四的任务", rendered)

    def test_private_card_refreshes_mutable_chat_names(self) -> None:
        message = self._message(
            "oc_direct",
            text="任务列表",
            mentions=[],
            chat_type="p2p",
        )
        refreshed = {"oc_a": "实验A新名", "oc_b": "实验B新名"}

        result = self._processor(
            private_cards=True,
            chat_name_refresher=lambda chat_id: refreshed[chat_id],
        ).handle(message)

        rendered = json.dumps(result.reply_card, ensure_ascii=False)
        self.assertIn("群聊：实验A新名", rendered)
        self.assertIn("群聊：实验B新名", rendered)
        self.assertNotIn("群聊：实验A\n", rendered)

    def test_private_card_hides_chat_name_when_refresh_fails(self) -> None:
        message = self._message(
            "oc_direct",
            text="任务列表",
            mentions=[],
            chat_type="p2p",
        )

        result = self._processor(
            private_cards=True,
            allowed_chats={"oc_a"},
            chat_name_refresher=lambda _chat_id: None,
        ).handle(message)

        rendered = json.dumps(result.reply_card, ensure_ascii=False)
        self.assertNotIn("群聊：", rendered)

    def test_group_query_remains_text_when_private_cards_are_enabled(self) -> None:
        result = self._processor(private_cards=True).handle(
            self._message("oc_a")
        )

        self.assertIsNone(result.reply_card)

    def test_private_card_actions_are_separately_gated(self) -> None:
        message = self._message(
            "oc_direct",
            text="任务列表",
            mentions=[],
            chat_type="p2p",
        )

        result = self._processor(
            private_cards=True, card_actions=True
        ).handle(message)
        rendered = json.dumps(result.reply_card, ensure_ascii=False)

        self.assertIn('"tag": "action"', rendered)
        self.assertIn('"action": "complete"', rendered)
        self.assertIn('"action": "cancel"', rendered)

    def test_card_actions_require_card_transport(self) -> None:
        with self.assertRaisesRegex(ValueError, "require"):
            self._processor(card_actions=True)

    def test_private_admin_is_restricted_to_configured_groups(self) -> None:
        message = self._message(
            "oc_direct",
            text="任务列表",
            mentions=[],
            chat_type="p2p",
            sender_open_id="ou_admin",
        )

        result = self._processor(
            admins={"ou_admin"}, allowed_chats={"oc_a"}
        ).handle(message)

        self.assertIn("管理员视图，共 4 项", result.reply_text)
        self.assertIn("李四的任务", result.reply_text)
        self.assertNotIn("另一个群的秘密任务", result.reply_text)

    def test_private_non_command_is_ignored(self) -> None:
        message = self._message(
            "oc_direct",
            text="请问我有什么任务",
            mentions=[],
            chat_type="p2p",
        )

        self.assertIsNone(self._processor().handle(message))

    def test_natural_private_query_lists_only_senders_open_tasks(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.SELF,
                target_name=None,
                status="open",
                confidence=0.98,
            )
        )
        message = self._message(
            "oc_direct",
            text="还有哪些任务没完成？",
            mentions=[],
            chat_type="p2p",
        )
        processor = self._processor(query_detector=detector)

        self.assertTrue(processor.matches(message))
        result = processor.handle(message)

        self.assertIsNotNone(result)
        self.assertIn("你的未完成任务（共 4 项）", result.reply_text)
        self.assertIn("另一个群的秘密任务", result.reply_text)
        self.assertNotIn("李四的任务", result.reply_text)
        detector.detect_task_query.assert_called_once()

    def test_natural_private_query_keeps_admin_sender_scope(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.SELF,
                target_name=None,
                status="open",
                confidence=0.99,
            )
        )
        message = self._message(
            "oc_direct",
            text="帮我看看我的待办",
            mentions=[],
            chat_type="p2p",
            sender_open_id="ou_wang",
        )
        result = self._processor(
            admins={"ou_wang"}, query_detector=detector
        ).handle(message)

        self.assertIn("你的未完成任务（共 4 项）", result.reply_text)
        self.assertNotIn("管理员视图", result.reply_text)
        self.assertNotIn("李四的任务", result.reply_text)

    def test_admin_unqualified_natural_query_lists_all_managed_tasks(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.SELF,
                target_name=None,
                status="open",
                confidence=0.99,
            )
        )
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset(
            {"oc_a"}
        )
        message = self._message(
            "oc_direct",
            text="现在还有什么事没做？",
            mentions=[],
            chat_type="p2p",
            sender_open_id="ou_admin",
        )

        result = self._processor(
            chat_administrators=chat_administrators,
            query_detector=detector,
        ).handle(message)

        self.assertIn("全部未完成任务（管理员视图，共 4 项）", result.reply_text)
        self.assertIn("李四的任务", result.reply_text)
        self.assertNotIn("另一个群的秘密任务", result.reply_text)

    def test_admin_natural_person_query_resolves_name_and_limits_chat_scope(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.PERSON,
                target_name="李四",
                status="open",
                confidence=0.98,
            )
        )
        aliases = Mock()
        aliases.resolve_open_ids_across_chats.return_value = frozenset(
            {"ou_li"}
        )
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset(
            {"oc_a"}
        )
        message = self._message(
            "oc_direct",
            text="李四还有哪些任务没完成？",
            mentions=[],
            chat_type="p2p",
            sender_open_id="ou_admin",
        )

        result = self._processor(
            chat_administrators=chat_administrators,
            alias_repository=aliases,
            query_detector=detector,
            private_cards=True,
        ).handle(message)

        self.assertIsNotNone(result)
        self.assertTrue(result.succeeded)
        self.assertIn("李四的未完成任务（共 1 项）", result.reply_text)
        self.assertIn("李四的任务", result.reply_text)
        self.assertIn("负责人：李四", result.reply_text)
        self.assertNotIn("另一个群的秘密任务", result.reply_text)
        rendered = json.dumps(result.reply_card, ensure_ascii=False)
        self.assertIn("李四的未完成任务", rendered)
        aliases.resolve_open_ids_across_chats.assert_called_with(
            "李四", chat_ids=frozenset({"oc_a"})
        )

    def test_admin_person_query_rejects_unknown_name_without_listing_tasks(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.PERSON,
                target_name="不存在的人",
                status="open",
                confidence=0.98,
            )
        )
        aliases = Mock()
        aliases.resolve_open_ids_across_chats.return_value = frozenset()
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset(
            {"oc_a"}
        )

        result = self._processor(
            chat_administrators=chat_administrators,
            alias_repository=aliases,
            query_detector=detector,
        ).handle(
            self._message(
                "oc_direct",
                text="不存在的人还有哪些任务没完成？",
                mentions=[],
                chat_type="p2p",
                sender_open_id="ou_admin",
            )
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.succeeded)
        self.assertIn("未找到当前管理范围内", result.reply_text)

    def test_admin_person_query_rejects_ambiguous_name(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.PERSON,
                target_name="王政",
                status="open",
                confidence=0.98,
            )
        )
        aliases = Mock()
        aliases.resolve_open_ids_across_chats.return_value = frozenset(
            {"ou_wang", "ou_other_wang"}
        )
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset(
            {"oc_a", "oc_b"}
        )

        result = self._processor(
            chat_administrators=chat_administrators,
            alias_repository=aliases,
            query_detector=detector,
        ).handle(
            self._message(
                "oc_direct",
                text="王政还有哪些任务没完成？",
                mentions=[],
                chat_type="p2p",
                sender_open_id="ou_admin",
            )
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.succeeded)
        self.assertIn("对应多个不同成员", result.reply_text)

    def test_person_scope_is_not_exposed_to_non_administrators(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.PERSON,
                target_name="李四",
                status="open",
                confidence=0.99,
            )
        )
        aliases = Mock()

        message = self._message(
            "oc_direct",
            text="李四还有哪些任务没完成？",
            mentions=[],
            chat_type="p2p",
        )
        processor = self._processor(
            alias_repository=aliases,
            query_detector=detector,
        )

        self.assertFalse(processor.matches(message))
        self.assertIsNone(processor.handle(message))
        aliases.resolve_open_ids_across_chats.assert_not_called()

    def test_admin_all_scope_lists_all_managed_tasks(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.ALL,
                target_name=None,
                status="open",
                confidence=0.99,
            )
        )
        message = self._message(
            "oc_direct",
            text="所有人的未完成任务有哪些？",
            mentions=[],
            chat_type="p2p",
            sender_open_id="ou_admin",
        )
        processor = self._processor(
            admins={"ou_admin"},
            allowed_chats={"oc_a"},
            query_detector=detector,
        )

        self.assertTrue(processor.matches(message))
        result = processor.handle(message)

        self.assertIn("全部未完成任务（管理员视图，共 4 项）", result.reply_text)
        self.assertIn("李四的任务", result.reply_text)
        self.assertNotIn("另一个群的秘密任务", result.reply_text)

    def test_all_scope_is_not_exposed_to_non_administrators(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.ALL,
                target_name=None,
                status="open",
                confidence=0.99,
            )
        )
        message = self._message(
            "oc_direct",
            text="所有人的未完成任务有哪些？",
            mentions=[],
            chat_type="p2p",
            sender_open_id="ou_wang",
        )
        processor = self._processor(query_detector=detector)

        self.assertFalse(processor.matches(message))
        self.assertIsNone(processor.handle(message))

    def test_natural_private_query_does_not_execute_other_scopes(self) -> None:
        detector = Mock()
        detector.detect_task_query.return_value = SimpleNamespace(
            result=TaskQueryIntent(
                is_query=True,
                scope=TaskQueryScope.PERSON,
                target_name="王政",
                status="open",
                confidence=0.99,
            )
        )
        message = self._message(
            "oc_direct",
            text="王政还有哪些任务没完成？",
            mentions=[],
            chat_type="p2p",
        )
        processor = self._processor(query_detector=detector)

        self.assertFalse(processor.matches(message))
        self.assertIsNone(processor.handle(message))

    def test_natural_query_classifier_failure_does_not_match(self) -> None:
        detector = Mock()
        detector.detect_task_query.side_effect = RuntimeError("provider down")
        message = self._message(
            "oc_direct",
            text="我还有什么待办？",
            mentions=[],
            chat_type="p2p",
        )

        processor = self._processor(query_detector=detector)

        self.assertFalse(processor.matches(message))
        self.assertIsNone(processor.handle(message))
        self.assertEqual(detector.detect_task_query.call_count, 1)

    def test_limit_validation_rejects_bool_and_out_of_range(self) -> None:
        for limit in (True, 0, 101):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                    self.repository.list_open_tasks("oc_a", limit=limit)

    def _processor(
        self,
        *,
        reply_limit: int = 20,
        admins: set[str] | frozenset[str] = frozenset(),
        allowed_chats: set[str] | frozenset[str] = frozenset(),
        private_cards: bool = False,
        card_actions: bool = False,
        chat_name_refresher=None,
        chat_administrators=None,
        alias_repository=None,
        query_detector=None,
    ) -> TaskCommandProcessor:
        return TaskCommandProcessor(
            self.repository,
            bot_open_id=BOT_OPEN_ID,
            task_admin_open_ids=frozenset(admins),
            allowed_chat_ids=frozenset(allowed_chats),
            reply_limit=reply_limit,
            private_cards_enabled=private_cards,
            card_actions_enabled=card_actions,
            chat_name_refresher=chat_name_refresher,
            chat_administrators=chat_administrators,
            alias_repository=alias_repository,
            query_detector=query_detector,
        )

    def _message(
        self,
        chat_id: str,
        *,
        text: str = "@_user_1 任务列表",
        mentions: list[dict] | None = None,
        chat_type: str = "group",
        sender_open_id: str = "ou_wang",
    ):
        payload = deepcopy(TEXT_EVENT)
        payload["header"]["event_id"] = f"evt_{chat_id}_{chat_type}"
        payload["event"]["message"]["message_id"] = (
            f"om_{chat_id}_{chat_type}"
        )
        payload["event"]["message"]["chat_id"] = chat_id
        payload["event"]["message"]["chat_type"] = chat_type
        payload["event"]["message"]["content"] = json.dumps(
            {"text": text}, ensure_ascii=False
        )
        payload["event"]["message"]["mentions"] = deepcopy(
            mentions if mentions is not None else [BOT_MENTION]
        )
        payload["event"]["sender"]["sender_id"][
            "open_id"
        ] = sender_open_id
        return normalize_message_event(payload, received_at=self.now)

    def _seed_tasks(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                [
                    Chat(
                        chat_id=chat_id,
                        tenant_key="tenant_test",
                        name={
                            "oc_a": "实验A",
                            "oc_b": "实验B",
                            "oc_empty": "空群",
                        }[chat_id],
                        chat_type="group",
                    )
                    for chat_id in ("oc_a", "oc_b", "oc_empty")
                ]
            )
            session.add_all(
                [
                    User(
                        open_id="ou_wang",
                        union_id="on_wang",
                        name="王政",
                        tenant_key="tenant_test",
                        last_seen_at=self.now,
                    ),
                    User(
                        open_id="ou_li",
                        union_id="on_li",
                        name="李四",
                        tenant_key="tenant_test",
                        last_seen_at=self.now,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    self._task(
                        "oc_a",
                        "逾期报告",
                        TaskStatus.OVERDUE,
                        deadline=datetime(
                            2026, 8, 29, 10, 0, tzinfo=timezone.utc
                        ),
                    ),
                    self._task(
                        "oc_a",
                        "前端页面",
                        TaskStatus.TODO,
                        deadline=datetime(
                            2026, 8, 30, 10, 0, tzinfo=timezone.utc
                        ),
                    ),
                    self._task(
                        "oc_a", "无截止任务", TaskStatus.PENDING
                    ),
                    self._task(
                        "oc_a",
                        "李四的任务",
                        TaskStatus.TODO,
                        owner_open_id="ou_li",
                        owner_name="李四",
                    ),
                    self._task(
                        "oc_a",
                        "已经完成",
                        TaskStatus.DONE,
                        completed_at=self.now,
                        review_status="pending",
                        completion_cycle=1,
                    ),
                    self._task(
                        "oc_a",
                        "已取消任务",
                        TaskStatus.CANCELLED,
                        cancelled_at=self.now,
                    ),
                    self._task(
                        "oc_b",
                        "另一个群的秘密任务",
                        TaskStatus.TODO,
                    ),
                ]
            )

    def _task(
        self,
        chat_id: str,
        title: str,
        status: TaskStatus,
        *,
        deadline: datetime | None = None,
        completed_at: datetime | None = None,
        cancelled_at: datetime | None = None,
        owner_open_id: str = "ou_wang",
        owner_name: str = "王政",
        review_status: str = "none",
        completion_cycle: int = 0,
    ) -> Task:
        return Task(
            chat_id=chat_id,
            owner_open_id=owner_open_id,
            owner_name_snapshot=owner_name,
            title=title,
            normalized_title=title.casefold(),
            description=f"{title}的说明",
            deadline=deadline,
            status=status.value,
            confidence=0.95,
            completed_at=completed_at,
            cancelled_at=cancelled_at,
            review_status=review_status,
            completion_cycle=completion_cycle,
            created_at=self.now,
            updated_at=self.now,
        )


if __name__ == "__main__":
    unittest.main()
