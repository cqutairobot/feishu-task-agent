"""Authorized task lifecycle handling for Feishu card callbacks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent.context import SHANGHAI_TZ
from app.lifecycle.contracts import LifecycleAction
from app.lifecycle.mutations import (
    LifecycleMutationError,
    LifecycleMutationService,
)
from app.management.access import ChatAdministratorRepository
from app.tasks.cards import CardPayload, build_private_task_list_card
from app.tasks.commands import _refresh_page_chat_names
from app.tasks.repository import TaskRepository


LOGGER = logging.getLogger(__name__)
ACTION_VALUE_FIELDS = frozenset(
    {"command", "version", "task_code", "action"}
)


@dataclass(frozen=True, slots=True)
class TaskCardActionRequest:
    callback_id: str
    actor_open_id: str
    card_message_id: str
    card_chat_id: str
    action_tag: str
    value: Mapping[str, Any]
    option: str | None = None
    actor_timezone: str | None = None


@dataclass(frozen=True, slots=True)
class TaskCardActionResult:
    succeeded: bool
    toast_type: str
    toast_text: str
    replacement_card: CardPayload


class TaskCardActionProcessor:
    """Apply explicit card actions and return an actor-scoped fresh card."""

    def __init__(
        self,
        tasks: TaskRepository,
        mutations: LifecycleMutationService,
        *,
        administrator_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        reply_limit: int = 20,
        now: Callable[[], datetime] | None = None,
        chat_name_refresher: Callable[[str], str | None] | None = None,
        chat_administrators: ChatAdministratorRepository | None = None,
    ) -> None:
        if not 1 <= reply_limit <= 20:
            raise ValueError("reply_limit must be between 1 and 20")
        if any(not item.strip() for item in administrator_open_ids):
            raise ValueError("administrator Open IDs must not be empty")
        if any(not item.strip() for item in allowed_chat_ids):
            raise ValueError("allowed chat IDs must not be empty")
        self._tasks = tasks
        self._mutations = mutations
        self._administrator_open_ids = frozenset(administrator_open_ids)
        self._allowed_chat_ids = frozenset(allowed_chat_ids)
        self._reply_limit = reply_limit
        self._now = now or _utc_now
        self._chat_name_refresher = chat_name_refresher
        self._chat_administrators = chat_administrators

    def handle(self, request: TaskCardActionRequest) -> TaskCardActionResult:
        actor_open_id = _required(request.actor_open_id, "actor_open_id")
        try:
            action, task_code, new_deadline = _parse_action_request(request)
            mutation = self._mutations.apply_card_action(
                action,
                actor_open_id=actor_open_id,
                callback_id=request.callback_id,
                card_message_id=request.card_message_id,
                card_chat_id=request.card_chat_id,
                task_code=task_code,
                applied_at=self._now(),
                new_deadline=new_deadline,
            )
        except (TaskCardActionValueError, LifecycleMutationError) as exc:
            LOGGER.warning("Rejected task card action: %s", exc)
            return TaskCardActionResult(
                succeeded=False,
                toast_type="warning",
                toast_text=(
                    "操作未执行：任务状态可能已变化，或你没有操作权限。"
                ),
                replacement_card=self._fresh_card(actor_open_id),
            )

        verb = {
            LifecycleAction.COMPLETE: "完成",
            LifecycleAction.CANCEL: "取消",
            LifecycleAction.RESCHEDULE: "延期",
        }[action]
        replay_note = "（该操作已处理）" if mutation.already_applied else ""
        deadline_note = ""
        if action is LifecycleAction.RESCHEDULE:
            assert mutation.deadline_after is not None
            deadline_note = mutation.deadline_after.astimezone(
                SHANGHAI_TZ
            ).strftime("至 %Y-%m-%d %H:%M")
        return TaskCardActionResult(
            succeeded=True,
            toast_type="success",
            toast_text=(
                f"任务 [{mutation.task_code}] 已{verb}{deadline_note}{replay_note}"
            ),
            replacement_card=self._fresh_card(actor_open_id),
        )

    def _fresh_card(self, actor_open_id: str) -> CardPayload:
        admin_chat_ids = self._administrator_chat_ids(actor_open_id)
        is_admin = admin_chat_ids is None or bool(admin_chat_ids)
        page = self._tasks.list_open_tasks_across_chats(
            owner_open_id=None if is_admin else actor_open_id,
            chat_ids=(
                admin_chat_ids
                if is_admin
                else self._admitted_chat_ids()
            ),
            limit=self._reply_limit,
        )
        page = _refresh_page_chat_names(page, self._chat_name_refresher)
        return build_private_task_list_card(
            page,
            is_admin=is_admin,
            actions_enabled=True,
        )

    def _administrator_chat_ids(
        self, actor_open_id: str
    ) -> frozenset[str] | None:
        if actor_open_id in self._administrator_open_ids:
            return self._allowed_chat_ids or None
        if self._chat_administrators is None:
            return frozenset()
        return self._chat_administrators.chat_ids_for_administrator(
            actor_open_id
        )

    def _admitted_chat_ids(self) -> frozenset[str] | None:
        if self._chat_administrators is None:
            return self._allowed_chat_ids or None
        return self._chat_administrators.admitted_chat_ids(
            self._allowed_chat_ids
        )


class TaskCardActionValueError(ValueError):
    """Raised when a callback carries an unknown or malformed button value."""


def _parse_action_request(
    request: TaskCardActionRequest,
) -> tuple[LifecycleAction, str, datetime | None]:
    value = request.value
    if not isinstance(value, Mapping) or frozenset(value) != ACTION_VALUE_FIELDS:
        raise TaskCardActionValueError("card action fields are invalid")
    if value.get("command") != "task_lifecycle" or value.get("version") != "1":
        raise TaskCardActionValueError("card action version is invalid")
    task_code = value.get("task_code")
    raw_action = value.get("action")
    if not isinstance(task_code, str) or not task_code.strip():
        raise TaskCardActionValueError("card task code is invalid")
    try:
        action = LifecycleAction(raw_action)
    except (TypeError, ValueError) as exc:
        raise TaskCardActionValueError("card lifecycle action is invalid") from exc
    if action in {LifecycleAction.COMPLETE, LifecycleAction.CANCEL}:
        if request.action_tag != "button":
            raise TaskCardActionValueError("button action tag is invalid")
        if request.option not in {None, ""}:
            raise TaskCardActionValueError("button option must be empty")
        return action, task_code.strip(), None
    if action is LifecycleAction.RESCHEDULE:
        if request.action_tag != "picker_datetime":
            raise TaskCardActionValueError("reschedule action tag is invalid")
        return (
            action,
            task_code.strip(),
            _parse_picker_deadline(request.option, request.actor_timezone),
        )
    raise TaskCardActionValueError("card lifecycle action is not enabled")


def _parse_picker_deadline(
    option: str | None,
    actor_timezone: str | None,
) -> datetime:
    raw = _required(option or "", "picker option")
    if len(raw) > 64:
        raise TaskCardActionValueError("picker option is too long")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TaskCardActionValueError(
            "picker option is not a valid datetime"
        ) from exc
    if parsed.tzinfo is None:
        timezone_name = _required(actor_timezone or "", "actor_timezone")
        if len(timezone_name) > 64:
            raise TaskCardActionValueError("actor_timezone is too long")
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError as exc:
            raise TaskCardActionValueError(
                "actor_timezone is not recognized"
            ) from exc
    return parsed.astimezone(timezone.utc)


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskCardActionValueError(f"{field} must not be empty")
    return value.strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
