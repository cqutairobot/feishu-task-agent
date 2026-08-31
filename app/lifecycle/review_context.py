"""Exact, pre-authorized context for task acceptance and reopen detection."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.context import TaskDetectionContext
from app.lifecycle.context import LifecycleTaskReference
from app.tasks.repository import CrossChatTaskEntry


@dataclass(frozen=True, slots=True)
class ReviewTaskReference:
    """One completed task exposed to the review-intent model."""

    task: LifecycleTaskReference
    review_status: str
    completion_cycle: int

    @classmethod
    def from_entry(cls, entry: CrossChatTaskEntry) -> "ReviewTaskReference":
        return cls(
            task=LifecycleTaskReference.from_snapshot(
                entry.task,
                chat_name=entry.chat_name,
            ),
            review_status=entry.task.review_status,
            completion_cycle=entry.task.completion_cycle,
        )

    @property
    def task_id(self) -> int:
        return self.task.task_id

    def to_dict(self) -> dict[str, object]:
        result = self.task.to_dict()
        result["review_status"] = self.review_status
        result["completion_cycle"] = self.completion_cycle
        return result


@dataclass(frozen=True, slots=True)
class ReviewDetectionContext:
    """P2P conversation plus exactly one reviewable group task."""

    conversation: TaskDetectionContext
    tasks: tuple[ReviewTaskReference, ...]
    actor_open_id: str

    def to_dict(self) -> dict[str, object]:
        result = self.conversation.to_dict()
        result["review_context_version"] = "1.0"
        result["task_scope"] = {
            "mode": "private_authorized_review_task",
            "actor_open_id": self.actor_open_id,
        }
        result["reviewable_tasks"] = [task.to_dict() for task in self.tasks]
        return result


class PrivateReviewDetectionContextBuilder:
    """Build a P2P context around one locally authorized task code."""

    def __init__(self, conversation_builder: object) -> None:
        self._conversation_builder = conversation_builder

    def build(
        self,
        chat_id: str,
        trigger_message_id: str,
        *,
        actor_open_id: str,
        task: CrossChatTaskEntry,
        message_limit: int = 20,
    ) -> ReviewDetectionContext:
        actor_open_id = actor_open_id.strip()
        if not actor_open_id:
            raise ValueError("actor_open_id must not be empty")
        conversation = self._conversation_builder.build(
            chat_id,
            trigger_message_id,
            limit=message_limit,
        )
        return ReviewDetectionContext(
            conversation=conversation,
            tasks=(ReviewTaskReference.from_entry(task),),
            actor_open_id=actor_open_id,
        )
