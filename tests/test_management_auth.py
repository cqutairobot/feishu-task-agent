"""Phase 7B one-time login, browser-session, and private-command tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select

from app.database.engine import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.database.migrate import upgrade_database
from app.database.models import (
    Chat,
    ChatAdministrator,
    ManagementLoginToken,
    ManagementSession,
    User,
)
from app.feishu.messages import IncomingMessage
from app.management.auth import ManagementAuthError, ManagementAuthRepository
from app.management.commands import ManagementCommandProcessor


class _Secrets:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, _size: int) -> str:
        self.index += 1
        return f"secret-{self.index:02d}-" + "x" * 32


class _FailingAuth:
    def __init__(self, fallback: ManagementAuthRepository, *, always: bool) -> None:
        self.fallback = fallback
        self.always = always
        self.calls = 0

    def create_login_ticket(self, *args, **kwargs):
        self.calls += 1
        if self.always or self.calls == 1:
            raise RuntimeError("temporary database failure")
        return self.fallback.create_login_ticket(*args, **kwargs)


class ManagementAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "auth.db"
        database_url = f"sqlite:///{database_path}"
        upgrade_database(database_url)
        self.engine = create_database_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)
        self.secrets = _Secrets()
        self.auth = ManagementAuthRepository(
            self.session_factory,
            token_factory=self.secrets,
        )
        self._seed()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_token_is_hashed_preview_does_not_consume_and_use_is_once(self) -> None:
        ticket = self.auth.create_login_ticket(
            "ou_admin",
            public_base_url="http://127.0.0.1:8000",
            created_at=self.now,
        )

        preview = self.auth.inspect_login_token(
            ticket.raw_token, inspected_at=self.now + timedelta(minutes=1)
        )
        with session_scope(self.session_factory) as session:
            stored = session.scalar(select(ManagementLoginToken))
            self.assertIsNotNone(stored)
            self.assertNotEqual(stored.token_hash, ticket.raw_token)
            self.assertNotIn(ticket.raw_token, stored.token_hash)
            self.assertIsNone(stored.consumed_at)

        credential = self.auth.consume_login_token(
            ticket.raw_token, consumed_at=self.now + timedelta(minutes=2)
        )
        principal = self.auth.authenticate_session(
            credential.raw_session,
            authenticated_at=self.now + timedelta(minutes=3),
        )

        self.assertEqual(preview.actor_open_id, "ou_admin")
        self.assertEqual(principal.actor_open_id, "ou_admin")
        with self.assertRaisesRegex(ManagementAuthError, "invalid or expired"):
            self.auth.consume_login_token(
                ticket.raw_token,
                consumed_at=self.now + timedelta(minutes=2, seconds=1),
            )
        with session_scope(self.session_factory) as session:
            stored_session = session.scalar(select(ManagementSession))
            self.assertIsNotNone(stored_session)
            self.assertNotEqual(stored_session.session_hash, credential.raw_session)

    def test_non_admin_expiry_logout_and_revoked_admin_are_rejected(self) -> None:
        with self.assertRaisesRegex(ManagementAuthError, "administrator"):
            self.auth.create_login_ticket(
                "ou_member",
                public_base_url="http://127.0.0.1:8000",
                created_at=self.now,
            )

        expired = self.auth.create_login_ticket(
            "ou_admin",
            public_base_url="http://127.0.0.1:8000",
            created_at=self.now,
        )
        with self.assertRaisesRegex(ManagementAuthError, "invalid or expired"):
            self.auth.inspect_login_token(
                expired.raw_token,
                inspected_at=self.now + timedelta(minutes=5),
            )

        live = self.auth.create_login_ticket(
            "ou_admin",
            public_base_url="http://127.0.0.1:8000",
            created_at=self.now,
        )
        credential = self.auth.consume_login_token(
            live.raw_token, consumed_at=self.now + timedelta(minutes=1)
        )
        self.assertTrue(
            self.auth.revoke_session(
                credential.raw_session,
                revoked_at=self.now + timedelta(minutes=2),
            )
        )
        self.assertFalse(self.auth.revoke_session(credential.raw_session))
        with self.assertRaisesRegex(ManagementAuthError, "not valid"):
            self.auth.authenticate_session(
                credential.raw_session,
                authenticated_at=self.now + timedelta(minutes=3),
            )

        another = self.auth.create_login_ticket(
            "ou_admin",
            public_base_url="http://127.0.0.1:8000",
            created_at=self.now,
        )
        another_session = self.auth.consume_login_token(
            another.raw_token, consumed_at=self.now + timedelta(minutes=1)
        )
        with session_scope(self.session_factory) as session:
            administrator = session.scalar(select(ChatAdministrator))
            session.delete(administrator)
        with self.assertRaisesRegex(ManagementAuthError, "not valid"):
            self.auth.authenticate_session(
                another_session.raw_session,
                authenticated_at=self.now + timedelta(minutes=2),
            )

    def test_private_management_command_issues_link_but_group_message_does_not(self) -> None:
        processor = ManagementCommandProcessor(
            self.auth,
            public_base_url="http://127.0.0.1:8000",
            clock=lambda: self.now,
        )

        result = processor.handle(self._message("ou_admin", "p2p", "管理后台"))

        self.assertIsNotNone(result)
        self.assertTrue(result.succeeded)
        self.assertIn("/auth/start?token=", result.reply_text)
        self.assertIn("5 分钟", result.reply_text)
        self.assertIsNone(
            processor.handle(self._message("ou_admin", "group", "管理后台"))
        )
        denied = processor.handle(
            self._message("ou_member", "p2p", "打开管理后台")
        )
        self.assertIsNotNone(denied)
        self.assertFalse(denied.succeeded)

    def test_private_management_command_retries_and_never_fails_silently(self) -> None:
        message = self._message("ou_admin", "p2p", "管理后台")
        flaky = _FailingAuth(self.auth, always=False)
        recovered = ManagementCommandProcessor(
            flaky,
            public_base_url="http://127.0.0.1:8000",
            clock=lambda: self.now,
        ).handle(message)

        self.assertIsNotNone(recovered)
        self.assertTrue(recovered.succeeded)
        self.assertEqual(flaky.calls, 2)

        failing = _FailingAuth(self.auth, always=True)
        rejected = ManagementCommandProcessor(
            failing,
            public_base_url="http://127.0.0.1:8000",
            clock=lambda: self.now,
        ).handle(message)
        self.assertIsNotNone(rejected)
        self.assertFalse(rejected.succeeded)
        self.assertIn("暂时不可用", rejected.reply_text)

    def _seed(self) -> None:
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
                ChatAdministrator(
                    chat_id="oc_lab",
                    open_id="ou_admin",
                    granted_by_open_id=None,
                    source="bootstrap",
                    created_at=self.now,
                )
            )

    def _message(
        self, sender_open_id: str, chat_type: str, text: str
    ) -> IncomingMessage:
        return IncomingMessage(
            event_id="ev_test",
            tenant_key="tenant",
            message_id="om_test",
            chat_id="oc_private" if chat_type == "p2p" else "oc_lab",
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            sender_union_id=None,
            sender_name=None,
            sender_type="user",
            message_type="text",
            text=text,
            mentions=(),
            raw_content="{}",
            raw_event_json="{}",
            root_id=None,
            parent_id=None,
            created_at=self.now,
            received_at=self.now,
        )


if __name__ == "__main__":
    unittest.main()
