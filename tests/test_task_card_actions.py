"""Phase 6C-2 task card callback processor tests."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.lifecycle.contracts import LifecycleAction
from app.lifecycle.mutations import LifecycleMutationError
from app.tasks.card_actions import (
    TaskCardActionProcessor,
    TaskCardActionRequest,
)
from app.tasks.repository import CrossChatTaskListPage


class TaskCardActionProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 23, 5, 0, tzinfo=timezone.utc)
        self.tasks = Mock()
        self.tasks.list_open_tasks_across_chats.return_value = (
            CrossChatTaskListPage(total_count=0, entries=())
        )
        self.mutations = Mock()
        self.mutations.apply_card_action.return_value = SimpleNamespace(
            task_code="T-1A",
            already_applied=False,
            deadline_after=None,
        )

    def test_owner_completion_uses_signed_actor_and_refreshes_own_list(self) -> None:
        result = self._processor().handle(self._request())

        self.assertTrue(result.succeeded)
        self.assertEqual(result.toast_type, "success")
        self.assertIn("已完成", result.toast_text)
        self.mutations.apply_card_action.assert_called_once_with(
            LifecycleAction.COMPLETE,
            actor_open_id="ou_owner",
            callback_id="evt_card",
            card_message_id="om_card",
            card_chat_id="oc_dm",
            task_code="T-1A",
            applied_at=self.now,
            new_deadline=None,
        )
        self.tasks.list_open_tasks_across_chats.assert_called_once_with(
            owner_open_id="ou_owner",
            chat_ids=frozenset({"oc_lab"}),
            limit=20,
        )
        self.assertIn("没有未完成任务", str(result.replacement_card))

    def test_administrator_refreshes_allowed_groups_without_owner_filter(self) -> None:
        result = self._processor(admins={"ou_admin"}).handle(
            self._request(actor="ou_admin", action="cancel")
        )

        self.assertTrue(result.succeeded)
        self.assertIn("已取消", result.toast_text)
        self.tasks.list_open_tasks_across_chats.assert_called_once_with(
            owner_open_id=None,
            chat_ids=frozenset({"oc_lab"}),
            limit=20,
        )

    def test_persisted_administrator_refreshes_only_its_groups(self) -> None:
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset(
            {"oc_lab"}
        )

        result = self._processor(
            chat_administrators=chat_administrators
        ).handle(self._request(actor="ou_admin", action="cancel"))

        self.assertTrue(result.succeeded)
        self.tasks.list_open_tasks_across_chats.assert_called_once_with(
            owner_open_id=None,
            chat_ids=frozenset({"oc_lab"}),
            limit=20,
        )

    def test_owner_card_refresh_includes_self_service_groups(self) -> None:
        chat_administrators = Mock()
        chat_administrators.chat_ids_for_administrator.return_value = frozenset()
        chat_administrators.admitted_chat_ids.return_value = frozenset(
            {"oc_lab", "oc_dynamic"}
        )

        result = self._processor(
            chat_administrators=chat_administrators
        ).handle(self._request(actor="ou_owner", action="complete"))

        self.assertTrue(result.succeeded)
        self.tasks.list_open_tasks_across_chats.assert_called_once_with(
            owner_open_id="ou_owner",
            chat_ids=frozenset({"oc_lab", "oc_dynamic"}),
            limit=20,
        )

    def test_malformed_or_unknown_button_value_never_reaches_mutation(self) -> None:
        values = (
            {"command": "task_lifecycle"},
            {**self._value(), "extra": "forged"},
            {**self._value(), "version": "99"},
        )
        for value in values:
            with self.subTest(value=value):
                self.mutations.reset_mock()
                result = self._processor().handle(
                    self._request(value=value)
                )
                self.assertFalse(result.succeeded)
                self.assertEqual(result.toast_type, "warning")
                self.mutations.apply_card_action.assert_not_called()

    def test_picker_reschedule_parses_offset_and_refreshes_card(self) -> None:
        deadline = datetime(2026, 9, 5, 10, 30, tzinfo=timezone.utc)
        self.mutations.apply_card_action.return_value = SimpleNamespace(
            task_code="T-1A",
            already_applied=False,
            deadline_after=deadline,
        )

        result = self._processor().handle(
            self._request(
                action="reschedule",
                option="2026-09-05 18:30 +0800",
                actor_timezone="Asia/Shanghai",
            )
        )

        self.assertTrue(result.succeeded)
        self.assertIn("已延期至 2026-09-05 18:30", result.toast_text)
        self.mutations.apply_card_action.assert_called_once_with(
            LifecycleAction.RESCHEDULE,
            actor_open_id="ou_owner",
            callback_id="evt_card",
            card_message_id="om_card",
            card_chat_id="oc_dm",
            task_code="T-1A",
            applied_at=self.now,
            new_deadline=deadline,
        )

    def test_picker_reschedule_uses_callback_timezone_for_naive_value(self) -> None:
        deadline = datetime(2026, 9, 5, 10, 30, tzinfo=timezone.utc)
        self.mutations.apply_card_action.return_value = SimpleNamespace(
            task_code="T-1A",
            already_applied=False,
            deadline_after=deadline,
        )

        result = self._processor().handle(
            self._request(
                action="reschedule",
                option="2026-09-05 18:30",
                actor_timezone="Asia/Shanghai",
            )
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(
            self.mutations.apply_card_action.call_args.kwargs["new_deadline"],
            deadline,
        )

    def test_reschedule_rejects_wrong_tag_or_invalid_datetime(self) -> None:
        requests = (
            self._request(
                action="reschedule",
                action_tag="button",
                option="2026-09-05 18:30 +0800",
            ),
            self._request(
                action="reschedule",
                option="not-a-time",
                actor_timezone="Asia/Shanghai",
            ),
            self._request(action="reschedule", option=None),
        )
        for request in requests:
            with self.subTest(request=request):
                self.mutations.reset_mock()
                result = self._processor().handle(request)
                self.assertFalse(result.succeeded)
                self.mutations.apply_card_action.assert_not_called()

    def test_authorization_or_stale_state_failure_returns_generic_refresh(self) -> None:
        self.mutations.apply_card_action.side_effect = LifecycleMutationError(
            "actor is neither the task owner nor an administrator"
        )

        result = self._processor().handle(self._request())

        self.assertFalse(result.succeeded)
        self.assertNotIn("owner", result.toast_text)
        self.assertIn("没有操作权限", result.toast_text)
        self.tasks.list_open_tasks_across_chats.assert_called_once()

    def test_retry_is_reported_as_already_processed(self) -> None:
        self.mutations.apply_card_action.return_value = SimpleNamespace(
            task_code="T-1A",
            already_applied=True,
            deadline_after=None,
        )

        result = self._processor().handle(self._request())

        self.assertTrue(result.succeeded)
        self.assertIn("已处理", result.toast_text)

    def _processor(
        self,
        *,
        admins: set[str] | frozenset[str] = frozenset(),
        chat_administrators=None,
    ) -> TaskCardActionProcessor:
        return TaskCardActionProcessor(
            self.tasks,
            self.mutations,
            administrator_open_ids=frozenset(admins),
            allowed_chat_ids=frozenset({"oc_lab"}),
            now=lambda: self.now,
            chat_administrators=chat_administrators,
        )

    def _request(
        self,
        *,
        actor: str = "ou_owner",
        action: str = "complete",
        value: dict[str, str] | None = None,
        action_tag: str | None = None,
        option: str | None = None,
        actor_timezone: str | None = None,
    ) -> TaskCardActionRequest:
        return TaskCardActionRequest(
            callback_id="evt_card",
            actor_open_id=actor,
            card_message_id="om_card",
            card_chat_id="oc_dm",
            action_tag=(
                action_tag
                if action_tag is not None
                else (
                    "picker_datetime"
                    if action == "reschedule"
                    else "button"
                )
            ),
            value=value if value is not None else self._value(action=action),
            option=option,
            actor_timezone=actor_timezone,
        )

    @staticmethod
    def _value(*, action: str = "complete") -> dict[str, str]:
        return {
            "command": "task_lifecycle",
            "version": "1",
            "task_code": "T-1A",
            "action": action,
        }


if __name__ == "__main__":
    unittest.main()
