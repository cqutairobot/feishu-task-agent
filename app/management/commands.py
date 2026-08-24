"""Private Feishu command that issues a one-time management login link."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import logging
from typing import Callable

from app.feishu.messages import IncomingMessage
from app.management.auth import ManagementAuthError, ManagementAuthRepository


LOGGER = logging.getLogger(__name__)


class ManagementCommandKind(StrEnum):
    LOGIN = "management_login"


@dataclass(frozen=True, slots=True)
class ManagementCommandResult:
    kind: ManagementCommandKind
    succeeded: bool
    reply_text: str


class ManagementCommandProcessor:
    """Issue login links only through an authenticated Feishu P2P message."""

    def __init__(
        self,
        auth: ManagementAuthRepository,
        *,
        public_base_url: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not public_base_url.strip():
            raise ValueError("public_base_url must not be empty")
        self._auth = auth
        self._public_base_url = public_base_url.rstrip("/")
        self._clock = clock

    def matches(self, message: IncomingMessage) -> bool:
        return _parse_management_command(message)

    def handle(
        self, message: IncomingMessage
    ) -> ManagementCommandResult | None:
        if not _parse_management_command(message):
            return None
        ticket = None
        for attempt in range(2):
            try:
                ticket = self._auth.create_login_ticket(
                    message.sender_open_id,
                    public_base_url=self._public_base_url,
                    created_at=self._clock(),
                )
                break
            except ManagementAuthError:
                return ManagementCommandResult(
                    kind=ManagementCommandKind.LOGIN,
                    succeeded=False,
                    reply_text=(
                        "⛔ 你当前不是任何群的任务管理员，无法进入管理后台。"
                    ),
                )
            except Exception:
                if attempt == 0:
                    LOGGER.warning(
                        "Management login issuance failed; retrying once",
                        exc_info=True,
                    )
                    continue
                LOGGER.exception("Management login issuance failed after retry")
                return ManagementCommandResult(
                    kind=ManagementCommandKind.LOGIN,
                    succeeded=False,
                    reply_text=(
                        "⚠️ 管理后台暂时不可用，请稍后重新发送“管理后台”。"
                    ),
                )
        assert ticket is not None
        return ManagementCommandResult(
            kind=ManagementCommandKind.LOGIN,
            succeeded=True,
            reply_text=(
                "🔐 管理后台登录\n\n"
                "请在 5 分钟内打开下面的私人链接，并点击一次“进入后台”。\n"
                "链接只能使用一次，请勿转发。\n\n"
                f"{ticket.login_url}"
            ),
        )


def is_management_command_message(message: IncomingMessage) -> bool:
    return _parse_management_command(message)


def _parse_management_command(message: IncomingMessage) -> bool:
    return (
        message.chat_type == "p2p"
        and message.message_type == "text"
        and message.sender_type != "bot"
        and message.text.strip().rstrip("?？").strip()
        in {"管理后台", "打开管理后台"}
    )
