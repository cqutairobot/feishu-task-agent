"""Receive Feishu events over the official SDK WebSocket client."""

from __future__ import annotations

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from app.config import FeishuSettings
from app.feishu.directory import DirectoryService
from app.feishu.messages import MessageEventError, normalize_message_event
from app.feishu.replies import (
    BotIdentityError,
    FeishuMessageReplier,
    FeishuReplyError,
    resolve_bot_open_id,
)
from app.ingestion.service import MessageIngestionService
from app.identity.aliases import AliasRepository
from app.identity.commands import (
    IdentityCommandProcessor,
    is_identity_command_message,
)
from app.lifecycle.private_commands import (
    PrivateLifecycleCommandProcessor,
    is_private_lifecycle_command_message,
)
from app.lifecycle.review_commands import PrivateReviewCommandProcessor
from app.tasks.note_commands import (
    PrivateTaskNoteCommandProcessor,
    is_private_task_note_message,
)
from app.management.access import ChatAdministratorRepository
from app.management.commands import (
    ManagementCommandProcessor,
    is_management_command_message,
)
from app.management.group_commands import (
    GroupManagementCommandProcessor,
    is_group_management_command_message,
)
from app.tasks.commands import (
    TaskCommandProcessor,
    is_task_command_message,
)
from app.tasks.card_actions import (
    TaskCardActionProcessor,
    TaskCardActionRequest,
)
from app.tasks.repository import TaskRepository


LOGGER = logging.getLogger(__name__)

# The SDK's INFO connection message contains short-lived WebSocket query
# credentials. Keep SDK logging at WARNING while retaining our own configured
# application log level.
SDK_LOG_LEVEL = lark.LogLevel.WARNING


def _on_message(
    data: P2ImMessageReceiveV1,
    allowed_chat_ids: frozenset[str],
    ingestion_service: MessageIngestionService | None = None,
    directory_service: DirectoryService | None = None,
    identity_commands: IdentityCommandProcessor | None = None,
    message_replier: FeishuMessageReplier | None = None,
    task_commands: TaskCommandProcessor | None = None,
    lifecycle_commands: PrivateLifecycleCommandProcessor | None = None,
    management_commands: ManagementCommandProcessor | None = None,
    group_management_commands: GroupManagementCommandProcessor | None = None,
    note_commands: PrivateTaskNoteCommandProcessor | None = None,
    review_commands: PrivateReviewCommandProcessor | None = None,
) -> None:
    """Normalize and print one SDK message event without exposing credentials."""

    try:
        payload = json.loads(lark.JSON.marshal(data))
        message = normalize_message_event(payload)
    except (json.JSONDecodeError, MessageEventError, TypeError) as exc:
        LOGGER.exception("Unable to normalize Feishu message event: %s", exc)
        return

    if (
        allowed_chat_ids
        and message.chat_type == "group"
        and message.chat_id not in allowed_chat_ids
        and (
            group_management_commands is None
            or not group_management_commands.allows_chat(message)
        )
    ):
        LOGGER.debug(
            "Ignoring message from an uninitialized chat outside the allowlist"
        )
        return

    if directory_service is not None:
        message = directory_service.enrich(
            message,
            force=is_identity_command_message(message),
        )

    output = message.terminal_output()
    inserted = False
    if ingestion_service is not None:
        try:
            identity_command = is_identity_command_message(message) or (
                identity_commands is not None
                and identity_commands.matches(message)
            )
            task_command = is_task_command_message(message) or (
                task_commands is not None and task_commands.matches(message)
            )
            lifecycle_command = is_private_lifecycle_command_message(
                message
            ) or (
                lifecycle_commands is not None
                and lifecycle_commands.matches(message)
            )
            note_command = is_private_task_note_message(message) or (
                note_commands is not None and note_commands.matches(message)
            )
            review_command = (
                review_commands is not None
                and review_commands.matches(message)
            )
            management_command = is_management_command_message(message) or (
                management_commands is not None
                and management_commands.matches(message)
            )
            group_management_command = (
                is_group_management_command_message(message)
                or (
                    group_management_commands is not None
                    and group_management_commands.matches(message)
                )
            )
            outcome = ingestion_service.process_message(
                message,
                enqueue_detection=not (
                    identity_command
                    or task_command
                    or lifecycle_command
                    or note_command
                    or review_command
                    or management_command
                    or group_management_command
                ),
            )
        except Exception:
            LOGGER.exception("Unable to persist Feishu message event")
            return
        output = f"{output}\nstorage: {outcome.persistence.status}"
        detection = getattr(outcome.persistence, "detection", None)
        if detection is not None and detection.job_id is not None:
            output = (
                f"{output}\ndetection_queue: {detection.status} "
                f"job_id={detection.job_id} "
                f"trigger={detection.trigger_message_id}"
            )
        inserted = bool(
            getattr(
                outcome.persistence,
                "inserted",
                str(outcome.persistence.status) == "inserted",
            )
        )

    print(output, flush=True)

    if not inserted or message_replier is None:
        return
    try:
        processors = (
            ("identity_command", identity_commands),
            ("group_management_command", group_management_commands),
            ("task_command", task_commands),
            ("review_intent_read_only", review_commands),
            ("task_note_command", note_commands),
            ("lifecycle_command", lifecycle_commands),
            ("management_command", management_commands),
        )
        for log_name, processor in processors:
            if processor is None:
                continue
            command_result = processor.handle(message)
            if command_result is None:
                continue
            reply_card = getattr(command_result, "reply_card", None)
            if reply_card is None:
                reply_kind = "text"
                message_replier.reply_text(
                    message.message_id, command_result.reply_text
                )
            else:
                card_sent = message_replier.reply_card(
                    message.message_id,
                    reply_card,
                    fallback_text=command_result.reply_text,
                )
                reply_kind = "card" if card_sent else "text_fallback"
            print(
                f"{log_name}: {command_result.kind} / "
                f"{'success' if command_result.succeeded else 'rejected'} / "
                f"reply={reply_kind}",
                flush=True,
            )
            return
    except FeishuReplyError as exc:
        LOGGER.error("Command was handled but reply failed: %s", exc)
    except Exception:
        LOGGER.exception("Unable to process bot command")


def _on_card_action(
    data: P2CardActionTrigger,
    processor: TaskCardActionProcessor,
) -> P2CardActionTriggerResponse:
    """Normalize one signed Feishu card callback and return card refresh data."""

    try:
        header = data.header
        event = data.event
        if header is None or event is None:
            raise ValueError("card callback has no header or event")
        operator = event.operator
        action = event.action
        context = event.context
        if operator is None or action is None or context is None:
            raise ValueError("card callback is missing required context")
        result = processor.handle(
            TaskCardActionRequest(
                callback_id=header.event_id or "",
                actor_open_id=operator.open_id or "",
                card_message_id=context.open_message_id or "",
                card_chat_id=context.open_chat_id or "",
                action_tag=action.tag or "",
                value=action.value or {},
                option=action.option,
                actor_timezone=action.timezone,
            )
        )
        print(
            "task_card_action: "
            f"{'success' if result.succeeded else 'rejected'}",
            flush=True,
        )
        return P2CardActionTriggerResponse(
            {
                "toast": {
                    "type": result.toast_type,
                    "content": result.toast_text,
                },
                "card": {
                    "type": "raw",
                    "data": result.replacement_card,
                },
            }
        )
    except Exception:
        LOGGER.exception("Unable to process task card action")
        return P2CardActionTriggerResponse(
            {
                "toast": {
                    "type": "error",
                    "content": "操作失败，任务没有修改。",
                }
            }
        )


def start_listener(
    settings: FeishuSettings,
    ingestion_service: MessageIngestionService | None = None,
    directory_service: DirectoryService | None = None,
    alias_repository: AliasRepository | None = None,
    task_repository: TaskRepository | None = None,
    lifecycle_commands: PrivateLifecycleCommandProcessor | None = None,
    card_actions: TaskCardActionProcessor | None = None,
    chat_administrator_repository: ChatAdministratorRepository | None = None,
    management_commands: ManagementCommandProcessor | None = None,
    note_commands: PrivateTaskNoteCommandProcessor | None = None,
    review_commands: PrivateReviewCommandProcessor | None = None,
    query_detector: object | None = None,
) -> None:
    """Start the blocking Feishu WebSocket listener."""

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    identity_commands: IdentityCommandProcessor | None = None
    task_commands: TaskCommandProcessor | None = None
    group_management_commands: GroupManagementCommandProcessor | None = None
    message_replier: FeishuMessageReplier | None = None
    if (
        alias_repository is not None
        or task_repository is not None
        or chat_administrator_repository is not None
    ):
        try:
            bot_open_id = resolve_bot_open_id(settings)
            if alias_repository is not None:
                identity_commands = IdentityCommandProcessor(
                    alias_repository,
                    bot_open_id=bot_open_id,
                    admin_open_ids=settings.identity_admin_open_ids,
                )
            if task_repository is not None:
                task_commands = TaskCommandProcessor(
                    task_repository,
                    bot_open_id=bot_open_id,
                    task_admin_open_ids=settings.task_admin_open_ids,
                    allowed_chat_ids=settings.allowed_chat_ids,
                    private_cards_enabled=(
                        settings.private_task_cards_enabled
                    ),
                    card_actions_enabled=(
                        settings.task_card_actions_enabled
                    ),
                    chat_name_refresher=(
                        directory_service.refresh_chat_name
                        if directory_service is not None
                        else None
                    ),
                    chat_administrators=chat_administrator_repository,
                    alias_repository=alias_repository,
                    query_detector=query_detector,
                )
            if (
                chat_administrator_repository is not None
                and directory_service is not None
            ):
                group_management_commands = GroupManagementCommandProcessor(
                    chat_administrator_repository,
                    directory_service,
                    bot_open_id=bot_open_id,
                )
            message_replier = FeishuMessageReplier(settings)
        except BotIdentityError as exc:
            LOGGER.warning("Bot commands disabled: %s", exc)

    event_builder = lark.EventDispatcherHandler.builder("", "", SDK_LOG_LEVEL)
    event_builder.register_p2_im_message_receive_v1(
        lambda data: _on_message(
            data,
            settings.allowed_chat_ids,
            ingestion_service,
            directory_service,
            identity_commands,
            message_replier,
            task_commands,
            lifecycle_commands,
            management_commands,
            group_management_commands,
            note_commands,
            review_commands,
        )
    )
    if card_actions is not None:
        event_builder.register_p2_card_action_trigger(
            lambda data: _on_card_action(data, card_actions)
        )
    event_handler = event_builder.build()
    client = lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        log_level=SDK_LOG_LEVEL,
        event_handler=event_handler,
        auto_reconnect=True,
    )

    allowlist_note = (
        f"{len(settings.allowed_chat_ids)} configured chat(s)"
        if settings.allowed_chat_ids
        else "all chats visible to the app"
    )
    persistence_note = (
        "SQLite persistence enabled"
        if ingestion_service is not None
        else "persistence disabled"
    )
    print(
        f"Connecting to Feishu via WebSocket; listening to {allowlist_note}; "
        f"{persistence_note}."
    )
    if identity_commands is not None:
        admin_note = (
            f"{len(settings.identity_admin_open_ids)} administrator(s)"
            if settings.identity_admin_open_ids
            else "administrator delegation disabled"
        )
        print(f"Identity commands enabled; {admin_note}.")
    if task_commands is not None:
        admin_note = "chat-scoped task administration enabled"
        print(
            "Task query commands enabled for group and direct messages; "
            f"personal view by default; {admin_note}."
        )
        if settings.private_task_cards_enabled:
            print(
                "Private task-list interactive cards enabled with text "
                "fallback."
            )
        if card_actions is not None:
            print(
                "Private task-card complete/reschedule/cancel callbacks "
                "enabled."
            )
    if lifecycle_commands is not None:
        print(
            "Private lifecycle commands enabled; one valid task code per "
            "message; authorized writes active."
        )
    if note_commands is not None:
        print(
            "Private task-note commands enabled; responsible members and "
            "administrators may append natural-language notes."
        )
    if review_commands is not None:
        if getattr(review_commands, "review_writes_enabled", False):
            print(
                "Private review commands enabled; explicit confirmation "
                "is required before accept/reopen writes."
            )
        else:
            print(
                "Private review-intent detection enabled in read-only mode; "
                "accept/reopen decisions never mutate tasks."
            )
    if management_commands is not None:
        print(
            "Private management login command enabled; one-time links active."
        )
    if group_management_commands is not None:
        print(
            "Group-owner administration commands enabled; live owner "
            "verification required."
        )
    if directory_service is not None:
        for chat_id in settings.allowed_chat_ids:
            snapshot = directory_service.refresh(chat_id, force=True)
            if snapshot is not None:
                print(
                    "Directory synchronized: "
                    f"chat name and {len(snapshot.members)} member(s)."
                )
    client.start()
