"""Chat-isolated context plus exact existing task choices for Phase 6A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.agent.context import (
    ContextParticipant,
    SHANGHAI_TZ,
    TaskDetectionContext,
)
from app.identity.aliases import AliasRepository
from app.tasks.codes import format_task_code
from app.tasks.repository import (
    CrossChatTaskEntry,
    TaskRepository,
    TaskSnapshot,
)


@dataclass(frozen=True, slots=True)
class LifecycleTaskReference:
    task_id: int
    owner_open_id: str
    owner_name: str
    title: str
    description: str
    deadline: datetime | None
    status: str
    source_chat_id: str | None = None
    source_chat_name: str | None = None
    assignees: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_snapshot(
        cls, task: TaskSnapshot, *, chat_name: str | None = None
    ) -> "LifecycleTaskReference":
        return cls(
            task_id=task.task_id,
            owner_open_id=task.owner_open_id,
            owner_name=task.owner_name,
            title=task.title,
            description=task.description,
            deadline=task.deadline,
            status=task.status.value,
            source_chat_id=task.chat_id,
            source_chat_name=chat_name,
            assignees=tuple(
                (member.name, member.open_id)
                for member in task.responsible_members
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "task_code": format_task_code(self.task_id),
            "owner": {
                "name": self.owner_name,
                "open_id": self.owner_open_id,
            },
            "owners": [
                {"name": name, "open_id": open_id}
                for name, open_id in (
                    self.assignees
                    or ((self.owner_name, self.owner_open_id),)
                )
            ],
            "title": self.title,
            "description": self.description,
            "deadline": (
                None
                if self.deadline is None
                else self.deadline.astimezone(SHANGHAI_TZ).isoformat()
            ),
            "status": self.status,
            "source_chat": {
                "chat_id": self.source_chat_id,
                "name": self.source_chat_name,
            },
        }


@dataclass(frozen=True, slots=True)
class LifecycleDetectionContext:
    conversation: TaskDetectionContext
    tasks: tuple[LifecycleTaskReference, ...]
    scope: str = "same_chat"
    actor_open_id: str | None = None
    eligible_owners: tuple[ContextParticipant, ...] = ()

    @property
    def chat_id(self) -> str:
        return self.conversation.chat_id

    @property
    def reference_time(self) -> datetime:
        return self.conversation.reference_time

    def to_dict(self) -> dict[str, object]:
        result = self.conversation.to_dict()
        result["lifecycle_context_version"] = "1.1"
        result["task_scope"] = {
            "mode": self.scope,
            "actor_open_id": self.actor_open_id,
        }
        result["eligible_owners"] = [
            owner.to_dict() for owner in self.eligible_owners
        ]
        result["open_tasks"] = [task.to_dict() for task in self.tasks]
        return result


class LifecycleDetectionContextBuilder:
    def __init__(
        self,
        conversation_builder: object,
        tasks: TaskRepository,
        *,
        task_limit: int = 50,
    ) -> None:
        if not 1 <= task_limit <= 100:
            raise ValueError("task_limit must be between 1 and 100")
        self._conversation_builder = conversation_builder
        self._tasks = tasks
        self._task_limit = task_limit

    def build(
        self,
        chat_id: str,
        trigger_message_id: str,
        *,
        message_limit: int = 30,
    ) -> LifecycleDetectionContext:
        conversation = self._conversation_builder.build(
            chat_id,
            trigger_message_id,
            limit=message_limit,
        )
        targets = self._tasks.list_lifecycle_targets(
            chat_id,
            limit=self._task_limit,
        )
        return LifecycleDetectionContext(
            conversation=conversation,
            tasks=tuple(
                LifecycleTaskReference.from_snapshot(task)
                for task in targets
            ),
            eligible_owners=conversation.participants,
        )


class PrivateLifecycleDetectionContextBuilder:
    """Build a P2P conversation around one pre-authorized task code."""

    def __init__(
        self,
        conversation_builder: object,
        aliases: AliasRepository | None = None,
    ) -> None:
        self._conversation_builder = conversation_builder
        self._aliases = aliases

    def build(
        self,
        chat_id: str,
        trigger_message_id: str,
        *,
        actor_open_id: str,
        task: CrossChatTaskEntry,
        message_limit: int = 20,
    ) -> LifecycleDetectionContext:
        if not actor_open_id.strip():
            raise ValueError("actor_open_id must not be empty")
        conversation = self._conversation_builder.build(
            chat_id,
            trigger_message_id,
            limit=message_limit,
        )
        if self._aliases is None:
            eligible_owners = tuple(
                ContextParticipant(open_id=open_id, name=name)
                for name, open_id in (
                    task.task.assignees
                    and tuple(
                        (member.name, member.open_id)
                        for member in task.task.assignees
                    )
                    or ((task.task.owner_name, task.task.owner_open_id),)
                )
            )
        else:
            eligible_owners = tuple(
                ContextParticipant(
                    open_id=binding.open_id,
                    name=binding.alias,
                )
                for binding in self._aliases.list_for_chat(task.task.chat_id)
            )
        return LifecycleDetectionContext(
            conversation=conversation,
            tasks=(
                LifecycleTaskReference.from_snapshot(
                    task.task, chat_name=task.chat_name
                ),
            ),
            scope="private_authorized_task",
            actor_open_id=actor_open_id.strip(),
            eligible_owners=eligible_owners,
        )
