"""Loopback-only HTTP adapter for the authenticated management page."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.config import ManagementWebSettings
from app.lifecycle.contracts import LifecycleAction
from app.lifecycle.mutations import (
    LifecycleMutationError,
    LifecycleMutationService,
)
from app.tasks.manual_creation import (
    ManagementTaskCreationError,
    ManagementTaskCreationService,
)
from app.tasks.notes import (
    TaskNoteAccessDenied,
    TaskNoteConflict,
    TaskNoteService,
    TaskNoteType,
    build_task_note_idempotency_key,
)
from app.management.auth import ManagementAuthError, ManagementAuthRepository
from app.management.access import (
    AdministratorSource,
    ChatAdministratorError,
    ChatAdministratorRepository,
)
from app.management.queries import (
    ManagementAccessDenied,
    ManagementQueryError,
    ManagementReadApi,
)
from app.identity.aliases import AliasConflictError, AliasError, AliasRepository
from app.management.settings import ChatSettingsError, ChatSettingsRepository


SESSION_COOKIE = "lab_task_session"
MAX_FORM_BYTES = 4096
MAX_JSON_BYTES = 16_384


class ManagementHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        settings: ManagementWebSettings,
        auth: ManagementAuthRepository,
        reads: ManagementReadApi,
        administrators: ChatAdministratorRepository,
        lifecycle_mutations: LifecycleMutationService,
        task_creation: ManagementTaskCreationService,
        task_notes: TaskNoteService,
        chat_settings: ChatSettingsRepository,
        directory_refresher: Any,
        aliases: AliasRepository | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self.reads = reads
        self.administrators = administrators
        self.lifecycle_mutations = lifecycle_mutations
        self.task_creation = task_creation
        self.task_notes = task_notes
        self.chat_settings = chat_settings
        self.directory_refresher = directory_refresher
        self.aliases = aliases
        super().__init__(
            (settings.bind_host, settings.port), ManagementRequestHandler
        )


class ManagementRequestHandler(BaseHTTPRequestHandler):
    server: ManagementHttpServer

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self._security_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._json(
                HTTPStatus.OK,
                {"status": "ok", "administrator_management": True},
            )
            return
        if parsed.path == "/auth/start":
            self._login_confirmation(parse_qs(parsed.query).get("token", [""])[0])
            return
        if parsed.path == "/auth/login.js":
            self._login_script()
            return
        if parsed.path.startswith("/api/"):
            self._api_get(parsed.path, parse_qs(parsed.query))
            return
        self._json_error(HTTPStatus.NOT_FOUND, "route not found")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/auth/consume":
            self._consume_login()
            return
        if parsed.path == "/auth/logout":
            self._logout()
            return
        if parsed.path.startswith("/api/"):
            self._api_post(parsed.path)
            return
        self._json_error(HTTPStatus.NOT_FOUND, "route not found")

    def do_DELETE(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            self._api_delete(parsed.path)
            return
        self._json_error(HTTPStatus.NOT_FOUND, "route not found")

    def _login_confirmation(self, raw_token: str) -> None:
        try:
            preview = self.server.auth.inspect_login_token(raw_token)
        except ManagementAuthError:
            self._html(
                HTTPStatus.UNAUTHORIZED,
                _login_page(
                    title="链接不可用",
                    body="该登录链接可能已经使用或超过 5 分钟，请返回飞书重新发送“管理后台”。",
                    token=None,
                ),
            )
            return
        remaining = max(
            1,
            int(
                (preview.expires_at - datetime.now(timezone.utc)).total_seconds()
                // 60
            )
            + 1,
        )
        self._html(
            HTTPStatus.OK,
            _login_page(
                title="进入 Lab Task Console",
                body=f"已验证飞书管理员身份。该链接约 {remaining} 分钟后失效。",
                token=raw_token,
            ),
        )

    def _consume_login(self) -> None:
        try:
            form = self._form_body()
            credential = self.server.auth.consume_login_token(
                form.get("token", [""])[0]
            )
        except (ManagementAuthError, ValueError):
            if self._has_valid_session():
                self._redirect_to_frontend()
                return
            self._html(
                HTTPStatus.UNAUTHORIZED,
                _login_page(
                    title="登录失败",
                    body="链接无效、已使用或已经过期，请返回飞书重新获取。",
                    token=None,
                ),
            )
            return
        max_age = max(
            1,
            int(
                (credential.expires_at - datetime.now(timezone.utc)).total_seconds()
            ),
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", self.server.settings.frontend_url)
        cookie = (
            f"{SESSION_COOKIE}={credential.raw_session}; Path=/; "
            f"Max-Age={max_age}; HttpOnly; SameSite=Strict"
        )
        if self.server.settings.cookie_secure:
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _redirect_to_frontend(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", self.server.settings.frontend_url)
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _has_valid_session(self) -> bool:
        raw_session = self._session_cookie()
        if not raw_session:
            return False
        try:
            self.server.auth.authenticate_session(raw_session)
        except ManagementAuthError:
            return False
        return True

    def _login_script(self) -> None:
        body = b"""(() => {
  const form = document.getElementById('login-form');
  const button = document.getElementById('login-submit');
  if (!form || !button) return;
  let submitted = false;
  form.addEventListener('submit', (event) => {
    if (submitted) {
      event.preventDefault();
      return;
    }
    submitted = true;
    button.setAttribute('aria-disabled', 'true');
    button.style.pointerEvents = 'none';
    button.textContent = '\\u6b63\\u5728\\u8fdb\\u5165\\u2026';
  });
})();
"""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _logout(self) -> None:
        if not self._origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        raw_session = self._session_cookie()
        if raw_session:
            try:
                self.server.auth.revoke_session(raw_session)
            except ManagementAuthError:
                pass
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if not self._origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            actor = principal.actor_open_id
            if path == "/api/chats":
                result: Any = self.server.reads.list_chats(actor)
            else:
                detail_match = re.fullmatch(
                    r"/api/chats/([^/]+)/tasks/([1-9][0-9]*)", path
                )
                tasks_match = re.fullmatch(r"/api/chats/([^/]+)/tasks", path)
                dashboard_match = re.fullmatch(
                    r"/api/chats/([^/]+)/dashboard", path
                )
                members_match = re.fullmatch(
                    r"/api/chats/([^/]+)/members", path
                )
                administrator_events_match = re.fullmatch(
                    r"/api/chats/([^/]+)/administrator-events", path
                )
                setting_events_match = re.fullmatch(
                    r"/api/chats/([^/]+)/settings/events", path
                )
                settings_match = re.fullmatch(
                    r"/api/chats/([^/]+)/settings", path
                )
                if detail_match:
                    result = self.server.reads.task_detail(
                        actor, detail_match.group(1), int(detail_match.group(2))
                    )
                elif tasks_match:
                    result = self.server.reads.list_tasks(
                        actor,
                        tasks_match.group(1),
                        statuses=tuple(query.get("status", ())),
                        owner_open_id=_first(query, "owner_open_id"),
                        query=_first(query, "query"),
                        missing_deadline=_optional_bool(
                            _first(query, "missing_deadline")
                        ),
                        deadline_before=_optional_datetime(
                            _first(query, "deadline_before")
                        ),
                        limit=_integer(query, "limit", 10),
                        offset=_integer(query, "offset", 0),
                    )
                elif dashboard_match:
                    result = self.server.reads.dashboard(
                        actor, dashboard_match.group(1)
                    )
                elif members_match:
                    chat_id = members_match.group(1)
                    self.server.directory_refresher(chat_id)
                    principal = self.server.auth.authenticate_session(
                        self._session_cookie()
                    )
                    actor = principal.actor_open_id
                    result = self.server.reads.list_members(
                        actor, chat_id
                    )
                elif administrator_events_match:
                    result = self.server.reads.list_administrator_events(
                        actor,
                        administrator_events_match.group(1),
                        limit=_integer(query, "limit", 100),
                    )
                elif setting_events_match:
                    result = self.server.chat_settings.list_events_for_administrator(
                        actor,
                        setting_events_match.group(1),
                        limit=_integer(query, "limit", 20),
                    )
                elif settings_match:
                    result = self.server.chat_settings.get_for_administrator(
                        actor, settings_match.group(1)
                    )
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "route not found")
                    return
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except ManagementAccessDenied:
            self._json_error(HTTPStatus.FORBIDDEN, "not authorized")
            return
        except ChatSettingsError as exc:
            status = (
                HTTPStatus.FORBIDDEN
                if str(exc) == "actor must be an administrator of this group"
                else HTTPStatus.BAD_REQUEST
            )
            self._json_error(status, "settings cannot be read")
            return
        except (ManagementQueryError, ValueError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(HTTPStatus.OK, _json_value(result), cors=True)

    def _api_post(self, path: str) -> None:
        settings_match = re.fullmatch(r"/api/chats/([^/]+)/settings", path)
        if settings_match is not None:
            self._api_update_settings(settings_match.group(1))
            return
        alias_match = re.fullmatch(
            r"/api/chats/([^/]+)/members/([^/]+)/alias", path
        )
        if alias_match is not None:
            self._api_update_member_alias(
                alias_match.group(1), alias_match.group(2)
            )
            return
        task_note_match = re.fullmatch(
            r"/api/chats/([^/]+)/tasks/([1-9][0-9]*)/notes",
            path,
        )
        if task_note_match is not None:
            self._api_append_task_note(
                task_note_match.group(1),
                int(task_note_match.group(2)),
            )
            return
        task_action_match = re.fullmatch(
            r"/api/chats/([^/]+)/tasks/([1-9][0-9]*)/"
            r"(deadline|title|assignees|status)",
            path,
        )
        if task_action_match is not None:
            action = {
                "deadline": LifecycleAction.RESCHEDULE,
                "title": LifecycleAction.RENAME,
                "assignees": LifecycleAction.REASSIGN,
                "status": None,
            }[task_action_match.group(3)]
            self._api_management_task_action(
                task_action_match.group(1),
                int(task_action_match.group(2)),
                action,
            )
            return
        task_create_match = re.fullmatch(r"/api/chats/([^/]+)/tasks", path)
        if task_create_match is not None:
            self._api_create_task(task_create_match.group(1))
            return
        match = re.fullmatch(r"/api/chats/([^/]+)/administrators", path)
        if match is None:
            self._json_error(HTTPStatus.NOT_FOUND, "route not found")
            return
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            payload = self._json_body()
            if set(payload) != {"open_id"}:
                raise ValueError("invalid fields")
            chat_id = match.group(1)
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            result = self.server.administrators.grant(
                chat_id,
                payload["open_id"],
                source=AdministratorSource.MANAGEMENT_PAGE,
                actor_open_id=principal.actor_open_id,
            )
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except ChatAdministratorError as exc:
            status = (
                HTTPStatus.FORBIDDEN
                if "actor must be" in str(exc)
                else HTTPStatus.CONFLICT
            )
            self._json_error(status, str(exc))
            return
        except (ManagementQueryError, TypeError, ValueError, KeyError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(
            HTTPStatus.CREATED if result.changed else HTTPStatus.OK,
            _json_value(result),
            cors=True,
        )

    def _api_update_settings(self, chat_id: str) -> None:
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            payload = self._json_body()
            reminder_fields = {
                "reminder_due_72h_enabled",
                "reminder_due_24h_enabled",
                "reminder_due_today_enabled",
                "reminder_overdue_enabled",
            }
            timing_fields = {
                "reminder_due_72h_offset_hours",
                "reminder_due_24h_offset_hours",
                "reminder_due_today_hour",
                "reminder_overdue_grace_minutes",
            }
            missing_deadline_switch_fields = {
                "missing_deadline_owner_enabled",
                "missing_deadline_admin_enabled",
            }
            missing_deadline_timing_fields = {
                "missing_deadline_owner_delay_hours",
                "missing_deadline_admin_delay_hours",
            }
            allowed = {
                "detection_enabled",
                "auto_todo_confidence",
                "task_scope",
                "administrator_notification_mode",
                "administrator_notification_open_ids",
                *reminder_fields,
                *timing_fields,
                *missing_deadline_switch_fields,
                *missing_deadline_timing_fields,
            }
            if not payload or set(payload) - allowed:
                raise ValueError("invalid fields")
            enabled = payload.get("detection_enabled")
            confidence = payload.get("auto_todo_confidence")
            task_scope = payload.get("task_scope")
            administrator_notification_mode = payload.get(
                "administrator_notification_mode"
            )
            administrator_notification_open_ids = payload.get(
                "administrator_notification_open_ids"
            )
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError("invalid detection_enabled")
            if confidence is not None and not isinstance(confidence, (int, float)):
                raise ValueError("invalid auto_todo_confidence")
            if task_scope is not None and not isinstance(task_scope, str):
                raise ValueError("invalid task_scope")
            if (
                administrator_notification_mode is not None
                and not isinstance(administrator_notification_mode, str)
            ):
                raise ValueError("invalid administrator_notification_mode")
            if administrator_notification_open_ids is not None and (
                not isinstance(administrator_notification_open_ids, list)
                or any(
                    not isinstance(item, str)
                    for item in administrator_notification_open_ids
                )
            ):
                raise ValueError(
                    "invalid administrator_notification_open_ids"
                )
            for field in reminder_fields:
                value = payload.get(field)
                if value is not None and not isinstance(value, bool):
                    raise ValueError(f"invalid {field}")
            for field in timing_fields:
                value = payload.get(field)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                ):
                    raise ValueError(f"invalid {field}")
            for field in missing_deadline_switch_fields:
                value = payload.get(field)
                if value is not None and not isinstance(value, bool):
                    raise ValueError(f"invalid {field}")
            for field in missing_deadline_timing_fields:
                value = payload.get(field)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                ):
                    raise ValueError(f"invalid {field}")
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            result = self.server.chat_settings.update_for_administrator(
                principal.actor_open_id,
                chat_id,
                detection_enabled=enabled,
                auto_todo_confidence=confidence,
                task_scope=task_scope,
                reminder_due_72h_enabled=payload.get(
                    "reminder_due_72h_enabled"
                ),
                reminder_due_24h_enabled=payload.get(
                    "reminder_due_24h_enabled"
                ),
                reminder_due_today_enabled=payload.get(
                    "reminder_due_today_enabled"
                ),
                reminder_overdue_enabled=payload.get(
                    "reminder_overdue_enabled"
                ),
                reminder_due_72h_offset_hours=payload.get(
                    "reminder_due_72h_offset_hours"
                ),
                reminder_due_24h_offset_hours=payload.get(
                    "reminder_due_24h_offset_hours"
                ),
                reminder_due_today_hour=payload.get(
                    "reminder_due_today_hour"
                ),
                reminder_overdue_grace_minutes=payload.get(
                    "reminder_overdue_grace_minutes"
                ),
                missing_deadline_owner_enabled=payload.get(
                    "missing_deadline_owner_enabled"
                ),
                missing_deadline_admin_enabled=payload.get(
                    "missing_deadline_admin_enabled"
                ),
                missing_deadline_owner_delay_hours=payload.get(
                    "missing_deadline_owner_delay_hours"
                ),
                missing_deadline_admin_delay_hours=payload.get(
                    "missing_deadline_admin_delay_hours"
                ),
                administrator_notification_mode=(
                    administrator_notification_mode
                ),
                administrator_notification_open_ids=(
                    None
                    if administrator_notification_open_ids is None
                    else tuple(administrator_notification_open_ids)
                ),
                updated_at=datetime.now(timezone.utc),
            )
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except ChatSettingsError as exc:
            status = (
                HTTPStatus.FORBIDDEN
                if str(exc) == "actor must be an administrator of this group"
                else HTTPStatus.BAD_REQUEST
            )
            self._json_error(status, "settings cannot be updated")
            return
        except (TypeError, ValueError, KeyError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(HTTPStatus.OK, _json_value(result), cors=True)

    def _api_update_member_alias(self, chat_id: str, open_id: str) -> None:
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        if self.server.aliases is None:
            self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "alias management is unavailable")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            payload = self._json_body()
            if set(payload) != {"alias"} or not isinstance(payload["alias"], str):
                raise ValueError("invalid fields")
            # Refresh first so a departed member cannot retain a stale alias.
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            self.server.chat_settings.get_for_administrator(
                principal.actor_open_id, chat_id
            )
            result = self.server.aliases.bind_for_administrator(
                chat_id,
                open_id,
                payload["alias"],
            )
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except AliasConflictError as exc:
            self._json_error(HTTPStatus.CONFLICT, str(exc))
            return
        except (ChatSettingsError, AliasError) as exc:
            status = (
                HTTPStatus.FORBIDDEN
                if str(exc) == "actor must be an administrator of this group"
                else HTTPStatus.BAD_REQUEST
            )
            self._json_error(status, str(exc))
            return
        except (TypeError, ValueError, KeyError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(HTTPStatus.OK, _json_value(result), cors=True)

    def _api_management_task_action(
        self,
        chat_id: str,
        task_id: int,
        action: LifecycleAction | None,
    ) -> None:
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            payload = self._json_body()
            request_id = payload.get("request_id")
            if not isinstance(request_id, str):
                raise ValueError("invalid request_id")
            if action is None:
                if "action" not in payload or payload["action"] not in {
                    "confirm",
                    "complete",
                    "accept",
                    "reopen",
                    "cancel",
                    "invalidate",
                    "restore",
                    "merge",
                }:
                    raise ValueError("invalid lifecycle action")
                action = LifecycleAction(payload["action"])
            deadline = None
            title = None
            owner_open_ids: tuple[str, ...] = ()
            merge_target_task_id: int | None = None
            reason: str | None = None
            if action is LifecycleAction.RESCHEDULE:
                if set(payload) != {"deadline", "request_id"} or not isinstance(
                    payload["deadline"], str
                ):
                    raise ValueError("invalid fields")
                deadline = datetime.fromisoformat(payload["deadline"])
            elif action is LifecycleAction.RENAME:
                if set(payload) != {"title", "request_id"} or not isinstance(
                    payload["title"], str
                ):
                    raise ValueError("invalid fields")
                title = payload["title"]
            elif action is LifecycleAction.REASSIGN:
                if set(payload) != {"open_ids", "request_id"}:
                    raise ValueError("invalid fields")
                raw_open_ids = payload["open_ids"]
                if not isinstance(raw_open_ids, list) or any(
                    not isinstance(item, str) for item in raw_open_ids
                ):
                    raise ValueError("invalid open_ids")
                owner_open_ids = tuple(raw_open_ids)
            elif action is LifecycleAction.MERGE:
                if set(payload) != {
                    "action",
                    "request_id",
                    "target_task_id",
                } or (
                    isinstance(payload["target_task_id"], bool)
                    or not isinstance(payload["target_task_id"], int)
                ):
                    raise ValueError("invalid merge target")
                merge_target_task_id = payload["target_task_id"]
            elif action is LifecycleAction.REOPEN:
                if set(payload) != {
                    "action",
                    "request_id",
                    "reason",
                } or not isinstance(payload["reason"], str):
                    raise ValueError("invalid reopen reason")
                reason = payload["reason"]
            elif set(payload) != {"action", "request_id"}:
                raise ValueError("invalid fields")
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            self.server.reads.dashboard(principal.actor_open_id, chat_id)
            self.server.lifecycle_mutations.apply_management_action(
                action,
                actor_open_id=principal.actor_open_id,
                request_id=request_id,
                chat_id=chat_id,
                task_id=task_id,
                new_deadline=deadline,
                new_title=title,
                new_owner_open_ids=owner_open_ids,
                merge_target_task_id=merge_target_task_id,
                reason=reason,
                applied_at=datetime.now(timezone.utc),
            )
            result = self.server.reads.task_detail(
                principal.actor_open_id, chat_id, task_id
            )
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except ManagementAccessDenied:
            self._json_error(HTTPStatus.FORBIDDEN, "not authorized")
            return
        except LifecycleMutationError:
            self._json_error(
                HTTPStatus.CONFLICT,
                "task cannot be updated from its current state",
            )
            return
        except (ManagementQueryError, TypeError, ValueError, KeyError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(HTTPStatus.OK, _json_value(result), cors=True)

    def _api_create_task(self, chat_id: str) -> None:
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            payload = self._json_body()
            if set(payload) != {
                "title",
                "description",
                "deadline",
                "open_ids",
                "request_id",
            }:
                raise ValueError("invalid fields")
            if not isinstance(payload["title"], str) or not isinstance(
                payload["description"], str
            ):
                raise ValueError("invalid task text")
            if not isinstance(payload["request_id"], str):
                raise ValueError("invalid request_id")
            raw_open_ids = payload["open_ids"]
            if not isinstance(raw_open_ids, list) or any(
                not isinstance(item, str) for item in raw_open_ids
            ):
                raise ValueError("invalid open_ids")
            raw_deadline = payload["deadline"]
            if raw_deadline is not None and not isinstance(raw_deadline, str):
                raise ValueError("invalid deadline")
            deadline = (
                None
                if raw_deadline is None
                else datetime.fromisoformat(raw_deadline)
            )
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            self.server.reads.dashboard(principal.actor_open_id, chat_id)
            creation = self.server.task_creation.create(
                actor_open_id=principal.actor_open_id,
                request_id=payload["request_id"],
                chat_id=chat_id,
                title=payload["title"],
                description=payload["description"],
                deadline=deadline,
                owner_open_ids=tuple(raw_open_ids),
                created_at=datetime.now(timezone.utc),
            )
            detail = self.server.reads.task_detail(
                principal.actor_open_id,
                chat_id,
                creation.task_id,
            )
            result = _json_value(detail)
            assert isinstance(result, dict)
            result["creation_replayed"] = creation.already_created
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except ManagementAccessDenied:
            self._json_error(HTTPStatus.FORBIDDEN, "not authorized")
            return
        except ManagementTaskCreationError as exc:
            status = (
                HTTPStatus.FORBIDDEN
                if "authorized administrator" in str(exc)
                else HTTPStatus.CONFLICT
            )
            self._json_error(status, "task cannot be created with these values")
            return
        except (ManagementQueryError, TypeError, ValueError, KeyError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(
            HTTPStatus.OK if creation.already_created else HTTPStatus.CREATED,
            result,
            cors=True,
        )

    def _api_append_task_note(self, chat_id: str, task_id: int) -> None:
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            payload = self._json_body()
            if set(payload) != {"note_type", "content", "request_id"}:
                raise ValueError("invalid fields")
            if not all(
                isinstance(payload[field], str)
                for field in ("note_type", "content", "request_id")
            ):
                raise ValueError("invalid note values")
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            self.server.reads.dashboard(principal.actor_open_id, chat_id)
            note = self.server.task_notes.append(
                actor_open_id=principal.actor_open_id,
                chat_id=chat_id,
                task_id=task_id,
                note_type=TaskNoteType(payload["note_type"]),
                content=payload["content"],
                source_message_id=None,
                idempotency_key=build_task_note_idempotency_key(
                    "management",
                    payload["request_id"],
                ),
                created_at=datetime.now(timezone.utc),
            )
            result = _json_value(
                self.server.reads.task_detail(
                    principal.actor_open_id,
                    chat_id,
                    task_id,
                )
            )
            assert isinstance(result, dict)
            result["note_replayed"] = note.already_created
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except (ManagementAccessDenied, TaskNoteAccessDenied):
            self._json_error(HTTPStatus.FORBIDDEN, "not authorized")
            return
        except TaskNoteConflict:
            self._json_error(
                HTTPStatus.CONFLICT,
                "task note conflicts with its current task or request",
            )
            return
        except (ManagementQueryError, TypeError, ValueError, KeyError):
            self._json_error(HTTPStatus.BAD_REQUEST, "request is invalid")
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(
            HTTPStatus.OK if note.already_created else HTTPStatus.CREATED,
            result,
            cors=True,
        )

    def _api_delete(self, path: str) -> None:
        match = re.fullmatch(
            r"/api/chats/([^/]+)/administrators/([^/]+)", path
        )
        if match is None:
            self._json_error(HTTPStatus.NOT_FOUND, "route not found")
            return
        if not self._write_origin_allowed():
            self._json_error(HTTPStatus.FORBIDDEN, "origin is not allowed")
            return
        try:
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            chat_id, target_open_id = match.groups()
            self.server.directory_refresher(chat_id)
            principal = self.server.auth.authenticate_session(
                self._session_cookie()
            )
            result = self.server.administrators.revoke(
                chat_id,
                target_open_id,
                source=AdministratorSource.MANAGEMENT_PAGE,
                actor_open_id=principal.actor_open_id,
            )
        except ManagementAuthError:
            self._json_error(HTTPStatus.UNAUTHORIZED, "sign in again")
            return
        except ChatAdministratorError as exc:
            status = (
                HTTPStatus.FORBIDDEN
                if "actor must be" in str(exc)
                else HTTPStatus.CONFLICT
            )
            self._json_error(status, str(exc))
            return
        except Exception:
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unable to verify current Feishu membership",
            )
            return
        self._json(HTTPStatus.OK, _json_value(result), cors=True)

    def _form_body(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if not 1 <= length <= MAX_FORM_BYTES:
            raise ValueError("invalid form size")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("invalid form content type")
        return parse_qs(self.rfile.read(length).decode("utf-8"))

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if not 1 <= length <= MAX_JSON_BYTES:
            raise ValueError("invalid JSON size")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise ValueError("invalid JSON content type")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) for key in payload
        ):
            raise ValueError("invalid JSON object")
        return payload

    def _session_cookie(self) -> str:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return "" if morsel is None else morsel.value

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin == self.server.settings.frontend_url

    def _write_origin_allowed(self) -> bool:
        return self.headers.get("Origin") == self.server.settings.frontend_url

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin == self.server.settings.frontend_url:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def _json(self, status: HTTPStatus, payload: object, *, cors: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if cors:
            self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message}, cors=True)

    def _html(self, status: HTTPStatus, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; "
            f"form-action 'self' {self.server.settings.frontend_url}; "
            "base-uri 'none'",
        )
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def log_message(self, format: str, *args: object) -> None:
        safe_path = urlsplit(self.path).path
        print(f"management_http: {self.command} {safe_path}", flush=True)


def run_management_server(
    settings: ManagementWebSettings,
    auth: ManagementAuthRepository,
    reads: ManagementReadApi,
    administrators: ChatAdministratorRepository,
    lifecycle_mutations: LifecycleMutationService,
    task_creation: ManagementTaskCreationService,
    task_notes: TaskNoteService,
    chat_settings: ChatSettingsRepository,
    directory_refresher: Any,
    aliases: AliasRepository | None = None,
) -> None:
    if not settings.enabled:
        raise ValueError("MANAGEMENT_WEB_ENABLED is false")
    server = ManagementHttpServer(
        settings,
        auth,
        reads,
        administrators,
        lifecycle_mutations,
        task_creation,
        task_notes,
        chat_settings,
        directory_refresher,
        aliases,
    )
    print(
        f"Management server started at {settings.public_base_url}; "
        f"bound to {settings.bind_host}:{settings.port}; "
        "press Ctrl+C to stop.",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def _login_page(*, title: str, body: str, token: str | None) -> str:
    action = (
        "<form id='login-form' method='post' action='/auth/consume'>"
        f"<input type='hidden' name='token' value='{escape(token, quote=True)}'>"
        "<button id='login-submit' type='submit'>进入后台</button></form>"
        "<script src='/auth/login.js' defer></script>"
        if token is not None
        else "<p class='hint'>返回飞书私聊机器人即可重新获取。</p>"
    )
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{escape(title)}</title><style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f4f6fa;color:#17223b;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}}
.card{{width:min(430px,100%);padding:34px;border:1px solid #e1e6ef;border-radius:20px;background:#fff;box-shadow:0 20px 55px rgba(29,45,78,.09)}}
.mark{{width:44px;height:44px;display:grid;place-items:center;border-radius:13px;background:#315eff;color:#fff;font-weight:800}} h1{{font-size:22px;margin:24px 0 10px}} p{{color:#6f7c94;font-size:14px;line-height:1.7;margin:0}} button{{width:100%;border:0;border-radius:11px;margin-top:26px;padding:13px;background:#315eff;color:#fff;font-weight:700;cursor:pointer}} button:disabled{{opacity:.72;cursor:wait}} .hint{{margin-top:20px}}
</style></head><body><main class='card'><div class='mark'>LT</div><h1>{escape(title)}</h1><p>{escape(body)}</p>{action}</main></body></html>"""


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return None if not values else values[0]


def _integer(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = _first(query, name)
    return default if raw is None else int(raw)


def _optional_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError("invalid boolean")


def _optional_datetime(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)
