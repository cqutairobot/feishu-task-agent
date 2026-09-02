"""Phase 7B loopback HTTP authentication and read-API adapter tests."""

from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlencode
import json
import socket
import unittest

from sqlalchemy import select

from app.config import ManagementWebSettings
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
    Task,
    TaskCreationEvent,
    TaskCompletionSubmission,
    TaskLifecycleEvent,
    TaskNotification,
    TaskNote,
    TaskReminder,
    User,
)
from app.management.access import ChatAdministratorRepository
from app.management.auth import ManagementAuthRepository
from app.management.queries import ManagementReadApi
from app.management.settings import ChatSettingsRepository
from app.management.web import ManagementRequestHandler
from app.identity.aliases import AliasRepository
from app.lifecycle.mutations import LifecycleMutationService
from app.tasks.manual_creation import ManagementTaskCreationService
from app.tasks.notes import TaskNoteService


class ManagementHttpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "http.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime.now(timezone.utc)
        with session_scope(self.session_factory) as session:
            session.add(
                Chat(
                    chat_id="oc_lab",
                    tenant_key="tenant",
                    name="实验群",
                    chat_type="group",
                    enabled=True,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.add_all(
                (
                    User(
                        open_id="ou_admin",
                        union_id=None,
                        name="莉莉",
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    User(
                        open_id="ou_member",
                        union_id=None,
                        name="王政",
                        tenant_key="tenant",
                        last_seen_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
            session.flush()
            session.add(
                Task(
                    chat_id="oc_lab",
                    owner_open_id="ou_member",
                    owner_name_snapshot="王政",
                    title="后台改期验收",
                    normalized_title="后台改期验收",
                    description="验证管理后台修改截止时间",
                    deadline=self.now + timedelta(days=2),
                    status="todo",
                    confidence=0.96,
                    created_at=self.now,
                    updated_at=self.now,
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
                    ChatMembership(
                        chat_id="oc_lab",
                        open_id="ou_admin",
                        display_name_snapshot="莉莉",
                        active=True,
                        is_owner=True,
                        first_synced_at=self.now,
                        last_synced_at=self.now,
                        left_at=None,
                    ),
                    ChatMembership(
                        chat_id="oc_lab",
                        open_id="ou_member",
                        display_name_snapshot="王政",
                        active=True,
                        is_owner=False,
                        first_synced_at=self.now,
                        last_synced_at=self.now,
                        left_at=None,
                    ),
                    ChatMemberAlias(
                        chat_id="oc_lab",
                        open_id="ou_admin",
                        alias="莉莉",
                        normalized_alias="莉莉",
                        source="self_command",
                        confidence=1.0,
                        verified_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    ChatMemberAlias(
                        chat_id="oc_lab",
                        open_id="ou_member",
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
        self.auth = ManagementAuthRepository(self.session_factory)
        self.aliases = AliasRepository(self.session_factory)
        self.administrators = ChatAdministratorRepository(self.session_factory)
        self.refresh_calls: list[str] = []
        self.settings = ManagementWebSettings(
            enabled=True,
            bind_host="127.0.0.1",
            port=0,
            public_base_url="http://127.0.0.1:8000",
            frontend_url="http://127.0.0.1:3000",
            cookie_secure=False,
        )
        self.server = SimpleNamespace(
            settings=self.settings,
            auth=self.auth,
            reads=ManagementReadApi(self.session_factory),
            administrators=self.administrators,
            lifecycle_mutations=LifecycleMutationService(
                self.session_factory
            ),
            task_creation=ManagementTaskCreationService(
                self.session_factory
            ),
            task_notes=TaskNoteService(self.session_factory),
            chat_settings=ChatSettingsRepository(self.session_factory),
            directory_refresher=lambda chat_id: self.refresh_calls.append(chat_id),
            aliases=self.aliases,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_get_preview_does_not_consume_then_post_creates_cookie_and_api_works(self) -> None:
        ticket = self.auth.create_login_ticket(
            "ou_admin",
            public_base_url="http://127.0.0.1:8000",
        )
        preview_status, preview_headers, preview_body = self._request(
            "GET", f"/auth/start?token={ticket.raw_token}"
        )
        self.assertEqual(preview_status, 200)
        self.assertIn("进入 Lab Task Console", preview_body)
        self.assertIn("/auth/login.js", preview_body)
        self.assertIn(
            self.settings.frontend_url,
            preview_headers["content-security-policy"],
        )

        script_status, script_headers, script_body = self._request(
            "GET", "/auth/login.js"
        )
        self.assertEqual(script_status, 200)
        self.assertEqual(
            script_headers["content-type"], "text/javascript; charset=utf-8"
        )
        self.assertIn("submitted", script_body)
        self.assertIn("aria-disabled", script_body)
        self.assertNotIn("button.disabled", script_body)

        body = urlencode({"token": ticket.raw_token})
        consumed_status, consumed_headers, _ = self._request(
            "POST",
            "/auth/consume",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(consumed_status, 303)
        self.assertEqual(consumed_headers["location"], self.settings.frontend_url)
        cookie = SimpleCookie()
        cookie.load(consumed_headers["set-cookie"])
        raw_session = cookie["lab_task_session"].value

        api_status, api_headers, api_body = self._request(
            "GET",
            "/api/chats",
            headers={
                "Origin": self.settings.frontend_url,
                "Cookie": f"lab_task_session={raw_session}",
            },
        )
        payload = json.loads(api_body)
        self.assertEqual(api_status, 200)
        self.assertEqual(api_headers["access-control-allow-origin"], self.settings.frontend_url)
        self.assertEqual(payload[0]["chat_id"], "oc_lab")

        recovered_status, recovered_headers, _ = self._request(
            "POST",
            "/auth/consume",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": f"lab_task_session={raw_session}",
            },
        )
        self.assertEqual(recovered_status, 303)
        self.assertEqual(
            recovered_headers["location"], self.settings.frontend_url
        )

        reused_status, _, _ = self._request(
            "POST",
            "/auth/consume",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(reused_status, 401)

    def test_api_rejects_missing_session_and_unexpected_origin(self) -> None:
        missing_status, _, _ = self._request(
            "GET",
            "/api/chats",
            headers={"Origin": self.settings.frontend_url},
        )
        self.assertEqual(missing_status, 401)

        forbidden_status, forbidden_headers, _ = self._request(
            "GET",
            "/api/chats",
            headers={"Origin": "http://malicious.example"},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertNotIn("access-control-allow-origin", forbidden_headers)

    def test_chat_settings_api_is_admin_only_persistent_and_audited(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }

        default_status, _, default_body = self._request(
            "GET",
            "/api/chats/oc_lab/settings",
            headers=headers,
        )
        blocked_write_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps({"auto_todo_confidence": 0.95}),
            headers={key: value for key, value in headers.items() if key != "Origin"},
        )
        update_status, _, update_body = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps(
                {
                    "detection_enabled": False,
                    "auto_todo_confidence": 0.95,
                    "task_scope": "work_only",
                    "reminder_due_72h_enabled": False,
                    "reminder_due_24h_enabled": True,
                    "reminder_due_today_enabled": False,
                    "reminder_overdue_enabled": True,
                    "reminder_due_72h_offset_hours": 96,
                    "reminder_due_24h_offset_hours": 36,
                    "reminder_due_today_hour": 8,
                    "reminder_overdue_grace_minutes": 15,
                    "missing_deadline_owner_enabled": False,
                    "missing_deadline_admin_enabled": True,
                    "missing_deadline_owner_delay_hours": 12,
                    "missing_deadline_admin_delay_hours": 48,
                    "administrator_notification_mode": "selected",
                    "administrator_notification_open_ids": ["ou_admin"],
                }
            ),
            headers=headers,
        )
        invalid_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps({"auto_todo_confidence": 1.1}),
            headers=headers,
        )
        invalid_scope_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps({"task_scope": "personal_only"}),
            headers=headers,
        )
        invalid_reminder_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps({"reminder_due_today_enabled": "yes"}),
            headers=headers,
        )
        invalid_timing_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps(
                {
                    "reminder_due_72h_offset_hours": 12,
                    "reminder_due_24h_offset_hours": 24,
                }
            ),
            headers=headers,
        )
        invalid_missing_deadline_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps(
                {
                    "missing_deadline_owner_delay_hours": 72,
                    "missing_deadline_admin_delay_hours": 48,
                }
            ),
            headers=headers,
        )
        invalid_notification_recipient_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/settings",
            body=json.dumps(
                {
                    "administrator_notification_mode": "selected",
                    "administrator_notification_open_ids": ["ou_member"],
                }
            ),
            headers=headers,
        )
        events_status, _, events_body = self._request(
            "GET",
            "/api/chats/oc_lab/settings/events",
            headers=headers,
        )

        self.assertEqual(default_status, 200)
        self.assertEqual(json.loads(default_body)["auto_todo_confidence"], 0.85)
        self.assertEqual(json.loads(default_body)["task_scope"], "broad")
        self.assertTrue(json.loads(default_body)["reminder_due_72h_enabled"])
        self.assertEqual(blocked_write_status, 403)
        self.assertEqual(update_status, 200)
        self.assertFalse(json.loads(update_body)["detection_enabled"])
        self.assertEqual(json.loads(update_body)["auto_todo_confidence"], 0.95)
        self.assertEqual(json.loads(update_body)["task_scope"], "work_only")
        self.assertEqual(invalid_scope_status, 400)
        self.assertFalse(json.loads(update_body)["reminder_due_72h_enabled"])
        self.assertTrue(json.loads(update_body)["reminder_due_24h_enabled"])
        self.assertEqual(
            json.loads(update_body)["administrator_notification_mode"],
            "selected",
        )
        self.assertEqual(
            json.loads(update_body)[
                "administrator_notification_open_ids"
            ],
            ["ou_admin"],
        )
        self.assertFalse(json.loads(update_body)["reminder_due_today_enabled"])
        self.assertTrue(json.loads(update_body)["reminder_overdue_enabled"])
        self.assertEqual(
            json.loads(update_body)["reminder_due_72h_offset_hours"], 96
        )
        self.assertEqual(
            json.loads(update_body)["reminder_due_24h_offset_hours"], 36
        )
        self.assertEqual(json.loads(update_body)["reminder_due_today_hour"], 8)
        self.assertEqual(
            json.loads(update_body)["reminder_overdue_grace_minutes"], 15
        )
        self.assertFalse(
            json.loads(update_body)["missing_deadline_owner_enabled"]
        )
        self.assertTrue(
            json.loads(update_body)["missing_deadline_admin_enabled"]
        )
        self.assertEqual(
            json.loads(update_body)["missing_deadline_owner_delay_hours"], 12
        )
        self.assertEqual(
            json.loads(update_body)["missing_deadline_admin_delay_hours"], 48
        )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_reminder_status, 400)
        self.assertEqual(invalid_timing_status, 400)
        self.assertEqual(invalid_missing_deadline_status, 400)
        self.assertEqual(invalid_notification_recipient_status, 400)
        self.assertEqual(events_status, 200)
        self.assertEqual(len(json.loads(events_body)), 1)

    def test_administrator_writes_require_origin_membership_and_keep_one_admin(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        cookie = f"lab_task_session={credential.raw_session}"
        payload = json.dumps({"open_id": "ou_member"})

        rejected_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/administrators",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        granted_status, _, granted_body = self._request(
            "POST",
            "/api/chats/oc_lab/administrators",
            body=payload,
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        members_status, _, members_body = self._request(
            "GET",
            "/api/chats/oc_lab/members",
            headers={
                "Origin": self.settings.frontend_url,
                "Cookie": cookie,
            },
        )
        events_status, _, events_body = self._request(
            "GET",
            "/api/chats/oc_lab/administrator-events",
            headers={
                "Origin": self.settings.frontend_url,
                "Cookie": cookie,
            },
        )
        revoked_status, _, _ = self._request(
            "DELETE",
            "/api/chats/oc_lab/administrators/ou_member",
            headers={
                "Origin": self.settings.frontend_url,
                "Cookie": cookie,
            },
        )
        last_status, _, last_body = self._request(
            "DELETE",
            "/api/chats/oc_lab/administrators/ou_admin",
            headers={
                "Origin": self.settings.frontend_url,
                "Cookie": cookie,
            },
        )

        self.assertEqual(rejected_status, 403)
        self.assertEqual(granted_status, 201)
        self.assertTrue(json.loads(granted_body)["changed"])
        self.assertEqual(members_status, 200)
        self.assertEqual(
            sum(item["is_administrator"] for item in json.loads(members_body)),
            2,
        )
        self.assertEqual(events_status, 200)
        self.assertEqual(len(json.loads(events_body)), 1)
        self.assertEqual(revoked_status, 200)
        self.assertEqual(last_status, 409)
        self.assertIn("last administrator", json.loads(last_body)["error"])
        self.assertEqual(
            self.refresh_calls,
            ["oc_lab", "oc_lab", "oc_lab", "oc_lab"],
        )

    def test_administrator_can_set_member_alias_without_self_binding(self) -> None:
        with session_scope(self.session_factory) as session:
            session.delete(
                session.scalar(
                    select(ChatMemberAlias).where(
                        ChatMemberAlias.chat_id == "oc_lab",
                        ChatMemberAlias.open_id == "ou_member",
                    )
                )
            )

        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        status, _, body = self._request(
            "POST",
            "/api/chats/oc_lab/members/ou_member/alias",
            body=json.dumps({"alias": "王政"}),
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": f"lab_task_session={credential.raw_session}",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["alias"], "王政")
        self.assertEqual(
            self.aliases.for_member("oc_lab", "ou_member").source,
            "administrator_page",
        )

    def test_member_read_fails_closed_when_live_refresh_revokes_actor(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )

        def revoke_departed_administrator(_chat_id: str) -> None:
            with session_scope(self.session_factory) as session:
                membership = session.scalar(
                    select(ChatAdministrator).where(
                        ChatAdministrator.chat_id == "oc_lab",
                        ChatAdministrator.open_id == "ou_admin",
                    )
                )
                assert membership is not None
                session.delete(membership)

        self.server.directory_refresher = revoke_departed_administrator

        status, _, body = self._request(
            "GET",
            "/api/chats/oc_lab/members",
            headers={
                "Origin": self.settings.frontend_url,
                "Cookie": (
                    f"lab_task_session={credential.raw_session}"
                ),
            },
        )

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "sign in again")

    def test_task_note_api_is_append_only_idempotent_and_readable(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        payload = json.dumps(
            {
                "note_type": "progress",
                "content": "管理后台记录：已完成数据清洗。",
                "request_id": "management-task-note-1",
            }
        )
        path = "/api/chats/oc_lab/tasks/1/notes"

        missing_origin_status, _, _ = self._request(
            "POST",
            path,
            body=payload,
            headers={key: value for key, value in headers.items() if key != "Origin"},
        )
        created_status, _, created_body = self._request(
            "POST", path, body=payload, headers=headers
        )
        replay_status, _, replay_body = self._request(
            "POST", path, body=payload, headers=headers
        )
        conflict_status, _, _ = self._request(
            "POST",
            path,
            body=json.dumps(
                {
                    "note_type": "progress",
                    "content": "同一请求号不能换成另一段内容。",
                    "request_id": "management-task-note-1",
                }
            ),
            headers=headers,
        )
        invalid_status, _, _ = self._request(
            "POST",
            path,
            body=json.dumps(
                {
                    "note_type": "unsupported",
                    "content": "非法类型",
                    "request_id": "management-task-note-invalid",
                }
            ),
            headers=headers,
        )

        created = json.loads(created_body)
        replayed = json.loads(replay_body)
        self.assertEqual(missing_origin_status, 403)
        self.assertEqual(created_status, 201)
        self.assertFalse(created["note_replayed"])
        self.assertEqual(replay_status, 200)
        self.assertTrue(replayed["note_replayed"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(invalid_status, 400)
        self.assertEqual(len(created["notes"]), 1)
        self.assertEqual(created["notes"][0]["note_type"], "progress")
        self.assertEqual(
            created["notes"][0]["content"],
            "管理后台记录：已完成数据清洗。",
        )
        self.assertEqual(created["notes"][0]["author_name"], "莉莉")
        with session_scope(self.session_factory) as session:
            notes = session.scalars(select(TaskNote)).all()
        self.assertEqual(len(notes), 1)

    def test_management_reschedule_requires_origin_and_is_idempotent(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        cookie = f"lab_task_session={credential.raw_session}"
        deadline = self.now + timedelta(days=5)
        payload = json.dumps(
            {
                "deadline": deadline.isoformat(),
                "request_id": "management-deadline-request-1",
            }
        )
        path = "/api/chats/oc_lab/tasks/1/deadline"

        rejected_status, _, _ = self._request(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        changed_status, _, changed_body = self._request(
            "POST",
            path,
            body=payload,
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        replay_status, _, _ = self._request(
            "POST",
            path,
            body=payload,
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        conflict_payload = json.dumps(
            {
                "deadline": (deadline + timedelta(days=1)).isoformat(),
                "request_id": "management-deadline-request-1",
            }
        )
        conflict_status, _, _ = self._request(
            "POST",
            path,
            body=conflict_payload,
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )

        self.assertEqual(rejected_status, 403)
        self.assertEqual(changed_status, 200)
        self.assertEqual(replay_status, 200)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(
            json.loads(changed_body)["task"]["deadline"],
            deadline.isoformat(),
        )
        with session_scope(self.session_factory) as session:
            events = session.scalars(select(TaskLifecycleEvent)).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].trigger_source, "management_page")
            self.assertEqual(
                events[0].trigger_management_request_id,
                "management-deadline-request-1",
            )
        self.assertEqual(self.refresh_calls, ["oc_lab", "oc_lab", "oc_lab"])

    def test_management_title_and_assignee_corrections_use_bound_members(
        self,
    ) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }

        title_status, _, title_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/title",
            body=json.dumps(
                {
                    "title": "后台纠错后的标题",
                    "request_id": "management-title-request",
                }
            ),
            headers=headers,
        )
        assignee_status, _, assignee_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/assignees",
            body=json.dumps(
                {
                    "open_ids": ["ou_member", "ou_admin"],
                    "request_id": "management-assignees-request",
                }
            ),
            headers=headers,
        )
        rejected_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/assignees",
            body=json.dumps(
                {
                    "open_ids": ["ou_departed"],
                    "request_id": "management-departed-request",
                }
            ),
            headers=headers,
        )

        self.assertEqual(title_status, 200)
        self.assertEqual(
            json.loads(title_body)["task"]["title"],
            "后台纠错后的标题",
        )
        self.assertEqual(assignee_status, 200)
        self.assertEqual(
            [
                item["open_id"]
                for item in json.loads(assignee_body)["task"]["assignees"]
            ],
            ["ou_member", "ou_admin"],
        )
        self.assertEqual(rejected_status, 409)
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(
                    TaskLifecycleEvent.trigger_source == "management_page"
                )
                .order_by(TaskLifecycleEvent.id)
            ).all()
        self.assertEqual([event.action for event in events], ["rename", "reassign"])

    def test_management_status_actions_are_strict_and_audited(self) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Task(
                        chat_id="oc_lab",
                        owner_open_id="ou_member",
                        owner_name_snapshot="王政",
                        title="后台取消验收",
                        normalized_title="后台取消验收",
                        description="验证管理后台取消任务",
                        deadline=self.now + timedelta(days=3),
                        status="todo",
                        confidence=0.96,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Task(
                        chat_id="oc_lab",
                        owner_open_id="ou_member",
                        owner_name_snapshot="王政",
                        title="后台误识别撤销验收",
                        normalized_title="后台误识别撤销验收",
                        description="验证管理后台撤销误识别",
                        deadline=self.now + timedelta(days=4),
                        status="todo",
                        confidence=0.96,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        results: list[tuple[int, dict[str, object]]] = []
        for task_id, action in ((1, "complete"), (2, "cancel"), (3, "invalidate")):
            status, _, body = self._request(
                "POST",
                f"/api/chats/oc_lab/tasks/{task_id}/status",
                body=json.dumps(
                    {
                        "action": action,
                        "request_id": f"management-{action}-request",
                    }
                ),
                headers=headers,
            )
            results.append((status, json.loads(body)))

        replay_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=json.dumps(
                {
                    "action": "complete",
                    "request_id": "management-complete-request",
                }
            ),
            headers=headers,
        )
        invalid_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=json.dumps(
                {"action": "reopen", "request_id": "management-reopen-request"}
            ),
            headers=headers,
        )

        self.assertEqual([status for status, _ in results], [200, 200, 200])
        self.assertEqual(
            [payload["task"]["status"] for _, payload in results],
            ["done", "cancelled", "cancelled"],
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(invalid_status, 400)
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.trigger_source == "management_page")
                .order_by(TaskLifecycleEvent.id)
            ).all()
        self.assertEqual([event.action for event in events], ["complete", "cancel", "invalidate"])

    def test_management_accepts_completion_and_returns_review_snapshot(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        complete_status, _, complete_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=json.dumps(
                {
                    "action": "complete",
                    "request_id": "management-complete-before-accept",
                }
            ),
            headers=headers,
        )
        accept_payload = json.dumps(
            {
                "action": "accept",
                "request_id": "management-accept-http",
            }
        )
        accept_status, _, accept_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=accept_payload,
            headers=headers,
        )
        replay_status, _, replay_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=accept_payload,
            headers=headers,
        )

        completed_detail = json.loads(complete_body)
        accepted_detail = json.loads(accept_body)
        completed_task = completed_detail["task"]
        accepted_task = accepted_detail["task"]
        replayed_task = json.loads(replay_body)["task"]
        self.assertEqual(complete_status, 200)
        self.assertEqual(completed_task["status"], "done")
        self.assertEqual(completed_task["review_status"], "pending")
        self.assertEqual(accept_status, 200)
        self.assertEqual(replay_status, 200)
        self.assertEqual(accepted_task["status"], "done")
        self.assertEqual(accepted_task["review_status"], "accepted")
        self.assertEqual(accepted_task["reviewed_by_open_id"], "ou_admin")
        self.assertEqual(accepted_task["reviewed_by_name"], "莉莉")
        self.assertEqual(accepted_task["completion_cycle"], 1)
        self.assertEqual(
            accepted_detail["responsibility"]["latest_reviewer_name"],
            "莉莉",
        )
        self.assertEqual(len(accepted_detail["completion_submissions"]), 1)
        self.assertEqual(
            accepted_detail["completion_submissions"][0]["review_status"],
            "accepted",
        )
        self.assertEqual(
            [
                item["action"]
                for item in accepted_detail["timeline"]
                if item["event_type"] == "lifecycle"
            ],
            ["complete", "accept"],
        )
        self.assertEqual(replayed_task["review_status"], "accepted")
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id == 1)
                .order_by(TaskLifecycleEvent.id)
            ).all()
            submission = session.scalar(
                select(TaskCompletionSubmission).where(
                    TaskCompletionSubmission.task_id == 1,
                    TaskCompletionSubmission.cycle == 1,
                )
            )
        self.assertEqual([event.action for event in events], ["complete", "accept"])
        self.assertEqual(events[-1].from_review_status, "pending")
        self.assertEqual(events[-1].to_review_status, "accepted")
        self.assertIsNotNone(submission)
        assert submission is not None
        self.assertEqual(submission.review_status, "accepted")
        self.assertEqual(submission.reviewed_by_open_id, "ou_admin")

    def test_management_reopens_completion_with_required_reason(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        complete_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=json.dumps(
                {
                    "action": "complete",
                    "request_id": "management-complete-before-reopen",
                }
            ),
            headers=headers,
        )
        missing_reason_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=json.dumps(
                {
                    "action": "reopen",
                    "request_id": "management-reopen-missing-reason",
                }
            ),
            headers=headers,
        )
        reason = "当前交付缺少实验日志，请补齐日志路径后重新提交。"
        reopen_payload = json.dumps(
            {
                "action": "reopen",
                "request_id": "management-reopen-http",
                "reason": reason,
            }
        )
        reopen_status, _, reopen_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=reopen_payload,
            headers=headers,
        )
        replay_status, _, replay_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=reopen_payload,
            headers=headers,
        )

        reopened = json.loads(reopen_body)
        replayed = json.loads(replay_body)
        self.assertEqual(complete_status, 200)
        self.assertEqual(missing_reason_status, 400)
        self.assertEqual(reopen_status, 200)
        self.assertEqual(replay_status, 200)
        self.assertEqual(reopened["task"]["status"], "todo")
        self.assertEqual(
            reopened["task"]["review_status"], "rework_required"
        )
        self.assertEqual(reopened["task"]["completion_cycle"], 1)
        self.assertEqual(reopened["lifecycle"][-1]["action"], "reopen")
        self.assertEqual(reopened["lifecycle"][-1]["reason"], reason)
        self.assertEqual(replayed["task"]["status"], "todo")
        with session_scope(self.session_factory) as session:
            submission = session.scalar(
                select(TaskCompletionSubmission).where(
                    TaskCompletionSubmission.task_id == 1,
                    TaskCompletionSubmission.cycle == 1,
                )
            )
        assert submission is not None
        self.assertEqual(submission.review_status, "rework_required")
        self.assertEqual(submission.review_reason, reason)

    def test_management_pending_review_is_strict_audited_and_notifies_owner(
        self,
    ) -> None:
        with session_scope(self.session_factory) as session:
            session.add_all(
                (
                    Task(
                        chat_id="oc_lab",
                        owner_open_id="ou_member",
                        owner_name_snapshot="王政",
                        title="待审核确认验收",
                        normalized_title="待审核确认验收",
                        description="验证管理员确认待审核任务",
                        deadline=self.now + timedelta(days=2),
                        status="pending",
                        confidence=0.72,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Task(
                        chat_id="oc_lab",
                        owner_open_id="ou_member",
                        owner_name_snapshot="王政",
                        title="待审核撤销验收",
                        normalized_title="待审核撤销验收",
                        description="验证管理员撤销待审核误识别",
                        deadline=None,
                        status="pending",
                        confidence=0.68,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                )
            )
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        confirmed_status, _, confirmed_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/2/status",
            body=json.dumps(
                {
                    "action": "confirm",
                    "request_id": "management-confirm-pending-http",
                }
            ),
            headers=headers,
        )
        invalidated_status, _, invalidated_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/3/status",
            body=json.dumps(
                {
                    "action": "invalidate",
                    "request_id": "management-invalidate-pending-http",
                }
            ),
            headers=headers,
        )

        self.assertEqual(confirmed_status, 200)
        self.assertEqual(json.loads(confirmed_body)["task"]["status"], "todo")
        self.assertEqual(invalidated_status, 200)
        self.assertEqual(
            json.loads(invalidated_body)["task"]["status"], "cancelled"
        )
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id.in_((2, 3)))
                .order_by(TaskLifecycleEvent.id)
            ).all()
            notification = session.scalar(
                select(TaskNotification).where(
                    TaskNotification.task_id == 2,
                    TaskNotification.kind == "task_created_assignee",
                )
            )
        self.assertEqual([event.action for event in events], ["confirm", "invalidate"])
        self.assertIsNotNone(notification)

    def test_management_manual_task_creation_is_origin_checked_and_idempotent(
        self,
    ) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        cookie = f"lab_task_session={credential.raw_session}"
        deadline = self.now + timedelta(days=10)
        payload = json.dumps(
            {
                "title": "后台手动补建验收",
                "description": "验证管理页创建、通知与提醒",
                "deadline": deadline.isoformat(),
                "open_ids": ["ou_member"],
                "request_id": "management-create-task-request",
            }
        )
        path = "/api/chats/oc_lab/tasks"

        rejected_status, _, _ = self._request(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        created_status, _, created_body = self._request(
            "POST",
            path,
            body=payload,
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )
        replay_status, _, replay_body = self._request(
            "POST",
            path,
            body=payload,
            headers={
                "Origin": self.settings.frontend_url,
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
        )

        self.assertEqual(rejected_status, 403)
        self.assertEqual(created_status, 201)
        self.assertEqual(replay_status, 200)
        created = json.loads(created_body)
        replayed = json.loads(replay_body)
        self.assertFalse(created["creation_replayed"])
        self.assertTrue(replayed["creation_replayed"])
        self.assertEqual(created["task"]["task_id"], 2)
        self.assertEqual(created["task"]["creation_source"], "management_page")
        self.assertEqual(
            created["task"]["assignees"],
            [{"open_id": "ou_member", "name": "王政", "position": 0}],
        )
        with session_scope(self.session_factory) as session:
            events = session.scalars(select(TaskCreationEvent)).all()
            notifications = session.scalars(
                select(TaskNotification).where(TaskNotification.task_id == 2)
            ).all()
            reminders = session.scalars(
                select(TaskReminder).where(TaskReminder.task_id == 2)
            ).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(len(reminders), 4)
        self.assertEqual(self.refresh_calls, ["oc_lab", "oc_lab"])

    def test_management_restore_requires_confirmation_request_and_replans(self) -> None:
        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        complete_status, _, _ = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=json.dumps(
                {
                    "action": "complete",
                    "request_id": "management-restore-http-complete",
                }
            ),
            headers=headers,
        )
        restore_payload = json.dumps(
            {
                "action": "restore",
                "request_id": "management-restore-http",
            }
        )
        restore_status, _, restore_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=restore_payload,
            headers=headers,
        )
        replay_status, _, replay_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/1/status",
            body=restore_payload,
            headers=headers,
        )

        self.assertEqual(complete_status, 200)
        self.assertEqual(restore_status, 200)
        self.assertEqual(replay_status, 200)
        self.assertEqual(json.loads(restore_body)["task"]["status"], "todo")
        self.assertEqual(json.loads(replay_body)["task"]["status"], "todo")
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id == 1)
                .order_by(TaskLifecycleEvent.id)
            ).all()
            reminders = session.scalars(
                select(TaskReminder).where(
                    TaskReminder.task_id == 1,
                    TaskReminder.status == "scheduled",
                )
            ).all()
        self.assertEqual([event.action for event in events], ["complete", "restore"])
        self.assertEqual(len(reminders), 4)

    def test_management_merge_marks_duplicate_and_preserves_target(self) -> None:
        with session_scope(self.session_factory) as session:
            task = Task(
                chat_id="oc_lab",
                owner_open_id="ou_member",
                owner_name_snapshot="王政",
                title="重复后台任务",
                normalized_title="重复后台任务",
                description="用于合并验收",
                deadline=self.now + timedelta(days=3),
                status="todo",
                confidence=0.94,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(task)

        credential = self.auth.consume_login_token(
            self.auth.create_login_ticket(
                "ou_admin", public_base_url="http://127.0.0.1:8000"
            ).raw_token
        )
        headers = {
            "Origin": self.settings.frontend_url,
            "Content-Type": "application/json",
            "Cookie": f"lab_task_session={credential.raw_session}",
        }
        payload = json.dumps(
            {
                "action": "merge",
                "target_task_id": 1,
                "request_id": "management-merge-http",
            }
        )
        merge_status, _, merge_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/2/status",
            body=payload,
            headers=headers,
        )
        replay_status, _, replay_body = self._request(
            "POST",
            "/api/chats/oc_lab/tasks/2/status",
            body=payload,
            headers=headers,
        )

        self.assertEqual(merge_status, 200)
        self.assertEqual(replay_status, 200)
        merged = json.loads(merge_body)
        replayed = json.loads(replay_body)
        self.assertEqual(merged["task"]["status"], "merged")
        self.assertEqual(merged["task"]["merged_into_task_id"], 1)
        self.assertEqual(merged["task"]["merged_into_task_code"], "T-1A")
        self.assertEqual(replayed["task"]["status"], "merged")
        with session_scope(self.session_factory) as session:
            events = session.scalars(
                select(TaskLifecycleEvent)
                .where(TaskLifecycleEvent.task_id == 2)
                .order_by(TaskLifecycleEvent.id)
            ).all()
        self.assertEqual([event.action for event in events], ["merge"])
        self.assertEqual(events[0].merge_target_task_id, 1)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> tuple[int, dict[str, str], str]:
        headers = dict(headers or {})
        encoded_body = body.encode("utf-8")
        headers.setdefault("Host", "127.0.0.1:8000")
        headers.setdefault("Connection", "close")
        if encoded_body:
            headers["Content-Length"] = str(len(encoded_body))
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            + "\r\n"
        ).encode("utf-8") + encoded_body
        client_socket, handler_socket = socket.socketpair()
        try:
            # Task detail responses intentionally include provenance timelines.
            # Keep the in-process socket writer from blocking before this test
            # helper starts reading the complete response.
            handler_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, 1_048_576
            )
            client_socket.sendall(request)
            client_socket.shutdown(socket.SHUT_WR)
            ManagementRequestHandler(
                handler_socket,
                ("127.0.0.1", 12345),
                self.server,
            )
            handler_socket.close()
            chunks: list[bytes] = []
            while chunk := client_socket.recv(65536):
                chunks.append(chunk)
        finally:
            client_socket.close()
            handler_socket.close()
        header_bytes, response_body = b"".join(chunks).split(b"\r\n\r\n", 1)
        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        response_headers = {
            name.lower(): value.strip()
            for name, value in (line.split(":", 1) for line in lines[1:])
        }
        return status, response_headers, response_body.decode("utf-8")


if __name__ == "__main__":
    unittest.main()
