"""Phase 9C-1A append-only task-note service tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import func, select

from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import upgrade_database
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatMemberAlias,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskNote,
    User,
)
from app.tasks.notes import (
    MAX_TASK_NOTE_CONTENT_LENGTH,
    TaskNoteAccessDenied,
    TaskNoteConflict,
    TaskNoteModelAudit,
    TaskNoteService,
    TaskNoteType,
    build_task_note_idempotency_key,
)


class TaskNoteServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "notes.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.service = TaskNoteService(self.session_factory)
        self.now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)

        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Chat(
                        chat_id="oc_lab",
                        tenant_key="tenant",
                        name="实验群",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Chat(
                        chat_id="oc_other",
                        tenant_key="tenant",
                        name="其他群",
                        chat_type="group",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Chat(
                        chat_id="oc_dm",
                        tenant_key="tenant",
                        name=None,
                        chat_type="p2p",
                        enabled=True,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
            for open_id, name in (
                ("ou_admin", "林老师"),
                ("ou_owner", "王政"),
                ("ou_coowner", "李明"),
                ("ou_outsider", "周悦"),
                ("ou_departed", "已离群成员"),
            ):
                session.add(
                    User(
                        open_id=open_id,
                        union_id=None,
                        name=name,
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
            session.flush()
            for open_id, name, active in (
                ("ou_admin", "林老师", True),
                ("ou_owner", "王政", True),
                ("ou_coowner", "李明", True),
                ("ou_outsider", "周悦", True),
                ("ou_departed", "已离群成员", False),
            ):
                session.add(
                    ChatMembership(
                        chat_id="oc_lab",
                        open_id=open_id,
                        display_name_snapshot=name,
                        active=active,
                        is_owner=open_id == "ou_admin",
                        first_synced_at=self.now,
                        last_synced_at=self.now,
                        left_at=None if active else self.now,
                    )
                )
            session.add_all(
                (
                    ChatAdministrator(
                        chat_id="oc_lab",
                        open_id="ou_admin",
                        granted_by_open_id=None,
                        source="bootstrap",
                        created_at=self.now,
                    ),
                    ChatMemberAlias(
                        chat_id="oc_lab",
                        open_id="ou_owner",
                        alias="王政",
                        normalized_alias="王政",
                        source="self_command",
                        confidence=1.0,
                        verified_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
            task = Task(
                chat_id="oc_lab",
                owner_open_id="ou_owner",
                owner_name_snapshot="王政",
                title="整理任务溯源测试",
                normalized_title="整理任务溯源测试",
                description="验证过程说明以追加方式保存",
                deadline=self.now + timedelta(days=2),
                status="todo",
                confidence=0.98,
                completion_cycle=2,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(task)
            session.flush()
            self.task_id = task.id
            session.add_all(
                (
                    TaskAssignee(
                        task_id=task.id,
                        open_id="ou_owner",
                        name_snapshot="王政",
                        position=0,
                        created_at=self.now,
                    ),
                    TaskAssignee(
                        task_id=task.id,
                        open_id="ou_coowner",
                        name_snapshot="李明",
                        position=1,
                        created_at=self.now,
                    ),
                    Message(
                        tenant_key="tenant",
                        event_id="event-owner",
                        message_id="om_owner",
                        chat_id="oc_lab",
                        sender_open_id="ou_owner",
                        sender_name_snapshot="王政",
                        message_type="text",
                        text_content="T-1A 训练已完成一半",
                        raw_content='{"text":"progress"}',
                        raw_event_json="{}",
                        message_created_at=self.now,
                        received_at=self.now,
                        is_from_bot=False,
                    ),
                    Message(
                        tenant_key="tenant",
                        event_id="event-other-sender",
                        message_id="om_other_sender",
                        chat_id="oc_lab",
                        sender_open_id="ou_coowner",
                        sender_name_snapshot="李明",
                        message_type="text",
                        text_content="他人的说明",
                        raw_content='{"text":"other"}',
                        raw_event_json="{}",
                        message_created_at=self.now,
                        received_at=self.now,
                        is_from_bot=False,
                    ),
                    Message(
                        tenant_key="tenant",
                        event_id="event-private-note",
                        message_id="om_private_note",
                        chat_id="oc_dm",
                        sender_open_id="ou_owner",
                        sender_name_snapshot="王政",
                        message_type="text",
                        text_content="T-1A 进度：两组实验已完成。",
                        raw_content='{"text":"private progress"}',
                        raw_event_json="{}",
                        message_created_at=self.now,
                        received_at=self.now,
                        is_from_bot=False,
                    ),
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_owner_and_coassignee_append_multiple_notes_without_overwrite(self) -> None:
        progress = self._append(
            actor="ou_owner",
            note_type=TaskNoteType.PROGRESS,
            content="训练已完成一半，当前指标正常。",
            token="progress-1",
            source_message_id="om_owner",
        )
        blocker = self._append(
            actor="ou_coowner",
            note_type=TaskNoteType.BLOCKER,
            content="实验服务器磁盘空间不足，需要管理员协助。",
            token="blocker-1",
        )

        self.assertFalse(progress.already_created)
        self.assertEqual(progress.author_name, "王政")
        self.assertEqual(progress.completion_cycle, 2)
        self.assertEqual(progress.source_message_id, "om_owner")
        self.assertEqual(blocker.note_type, TaskNoteType.BLOCKER)
        with session_scope(self.session_factory) as session:
            notes = session.scalars(
                select(TaskNote)
                .where(TaskNote.task_id == self.task_id)
                .order_by(TaskNote.id)
            ).all()
        self.assertEqual(len(notes), 2)
        self.assertEqual(
            [note.note_type for note in notes], ["progress", "blocker"]
        )
        self.assertEqual(
            [note.content for note in notes],
            [
                "训练已完成一半，当前指标正常。",
                "实验服务器磁盘空间不足，需要管理员协助。",
            ],
        )

    def test_administrator_only_note_types_are_enforced(self) -> None:
        with self.assertRaises(TaskNoteAccessDenied):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.REOPEN,
                content="我自行决定重开。",
                token="owner-reopen",
            )

        reopened = self._append(
            actor="ou_admin",
            note_type=TaskNoteType.REOPEN,
            content="验收结果不完整，需要补充失败样例分析。",
            token="admin-reopen",
        )
        corrected = self._append(
            actor="ou_admin",
            note_type=TaskNoteType.CORRECTION,
            content="将说明中的服务器路径修正为 /srv/results。",
            token="admin-correction",
        )
        self.assertEqual(reopened.author_name, "林老师")
        self.assertEqual(corrected.note_type, TaskNoteType.CORRECTION)

    def test_outsider_and_departed_assignee_are_rejected(self) -> None:
        for actor in ("ou_outsider", "ou_departed"):
            with self.subTest(actor=actor), self.assertRaises(
                TaskNoteAccessDenied
            ):
                self._append(
                    actor=actor,
                    note_type=TaskNoteType.GENERAL,
                    content="不应允许写入的说明。",
                    token=f"denied-{actor}",
                )

    def test_idempotent_replay_returns_original_and_conflict_is_rejected(self) -> None:
        first = self._append(
            actor="ou_owner",
            note_type=TaskNoteType.PROGRESS,
            content="第一轮训练完成。",
            token="same-request",
        )
        replay = self._append(
            actor="ou_owner",
            note_type=TaskNoteType.PROGRESS,
            content="第一轮训练完成。",
            token="same-request",
        )
        self.assertEqual(replay.note_id, first.note_id)
        self.assertTrue(replay.already_created)

        with self.assertRaises(TaskNoteConflict):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.PROGRESS,
                content="同一请求却修改了内容。",
                token="same-request",
            )
        with session_scope(self.session_factory) as session:
            count = session.scalar(select(func.count(TaskNote.id)))
        self.assertEqual(count, 1)

    def test_source_message_must_belong_to_same_actor_and_chat(self) -> None:
        with self.assertRaises(TaskNoteConflict):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.PROGRESS,
                content="尝试引用他人的消息。",
                token="wrong-source",
                source_message_id="om_other_sender",
            )
        with self.assertRaises(TaskNoteConflict):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.PROGRESS,
                content="尝试引用不存在的消息。",
                token="missing-source",
                source_message_id="om_missing",
            )

    def test_private_source_chat_and_model_audit_are_persisted(self) -> None:
        note = self._append(
            actor="ou_owner",
            note_type=TaskNoteType.PROGRESS,
            content="两组实验已完成。",
            token="private-audit",
            source_message_id="om_private_note",
            source_chat_id="oc_dm",
            model_audit=TaskNoteModelAudit(
                provider="openai_compatible",
                model="qwen-test",
                response_format="json_schema",
                request_id="req-note",
                prompt_tokens=30,
                completion_tokens=12,
                total_tokens=42,
                confidence=0.96,
            ),
        )
        self.assertEqual(note.source_chat_id, "oc_dm")
        self.assertEqual(note.model, "qwen-test")
        self.assertEqual(note.total_tokens, 42)
        with session_scope(self.session_factory) as session:
            stored = session.get(TaskNote, note.note_id)
            assert stored is not None
            self.assertEqual(stored.source_chat_id, "oc_dm")
            self.assertEqual(stored.confidence, 0.96)

    def test_model_audit_respects_persisted_field_lengths(self) -> None:
        with self.assertRaises(ValueError):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.PROGRESS,
                content="模型审计字段边界校验。",
                token="audit-provider-too-long",
                model_audit=TaskNoteModelAudit(
                    provider="p" * 33,
                    model="qwen-test",
                    response_format="json_schema",
                ),
            )
        with self.assertRaises(ValueError):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.PROGRESS,
                content="请求编号也需要受长度约束。",
                token="audit-request-too-long",
                model_audit=TaskNoteModelAudit(
                    provider="openai_compatible",
                    model="qwen-test",
                    response_format="json_schema",
                    request_id="r" * 129,
                ),
            )

    def test_merged_task_cannot_receive_notes(self) -> None:
        with session_scope(self.session_factory) as session:
            target = Task(
                chat_id="oc_lab",
                owner_open_id="ou_owner",
                owner_name_snapshot="王政",
                title="合并目标",
                normalized_title="合并目标",
                description="合并目标",
                deadline=None,
                status="todo",
                confidence=1.0,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(target)
            session.flush()
            task = session.get(Task, self.task_id)
            assert task is not None
            task.merged_into_task_id = target.id
            task.merged_at = self.now

        with self.assertRaises(TaskNoteConflict):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.GENERAL,
                content="合并后的旧任务不再接收说明。",
                token="merged-task",
            )

    def test_validation_rejects_invalid_type_content_and_time(self) -> None:
        with self.assertRaises(ValueError):
            self._append(
                actor="ou_owner",
                note_type="unknown",
                content="非法类型",
                token="invalid-type",
            )
        with self.assertRaises(ValueError):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.GENERAL,
                content=" ",
                token="blank",
            )
        with self.assertRaises(ValueError):
            self._append(
                actor="ou_owner",
                note_type=TaskNoteType.GENERAL,
                content="x" * (MAX_TASK_NOTE_CONTENT_LENGTH + 1),
                token="too-long",
            )
        with self.assertRaises(ValueError):
            self.service.append(
                actor_open_id="ou_owner",
                chat_id="oc_lab",
                task_id=self.task_id,
                note_type=TaskNoteType.GENERAL,
                content="时间必须带时区",
                idempotency_key=build_task_note_idempotency_key(
                    "test", "naive-time"
                ),
                created_at=datetime(2026, 8, 30, 8, 0),
            )

    def _append(
        self,
        *,
        actor: str,
        note_type: TaskNoteType | str,
        content: str,
        token: str,
        source_message_id: str | None = None,
        source_chat_id: str | None = None,
        model_audit: TaskNoteModelAudit | None = None,
    ):
        return self.service.append(
            actor_open_id=actor,
            chat_id="oc_lab",
            task_id=self.task_id,
            note_type=note_type,
            content=content,
            source_message_id=source_message_id,
            source_chat_id=source_chat_id,
            model_audit=model_audit,
            idempotency_key=build_task_note_idempotency_key("test", token),
            created_at=self.now,
        )


if __name__ == "__main__":
    unittest.main()
