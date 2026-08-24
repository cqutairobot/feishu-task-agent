"""Environment-backed application configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class SettingsError(ValueError):
    """Raised when required application settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class FeishuSettings:
    """Settings needed by the Phase 1 WebSocket listener."""

    app_id: str
    app_secret: str
    allowed_chat_ids: frozenset[str]
    identity_admin_open_ids: frozenset[str]
    task_admin_open_ids: frozenset[str]
    private_task_cards_enabled: bool
    task_card_actions_enabled: bool
    log_level: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "FeishuSettings":
        app_id = values.get("FEISHU_APP_ID", "").strip()
        app_secret = values.get("FEISHU_APP_SECRET", "").strip()
        if not app_id:
            raise SettingsError("FEISHU_APP_ID is missing")
        if not app_secret:
            raise SettingsError("FEISHU_APP_SECRET is missing")

        raw_chat_ids = values.get("FEISHU_ALLOWED_CHAT_IDS", "")
        allowed_chat_ids = frozenset(
            chat_id.strip()
            for chat_id in raw_chat_ids.split(",")
            if chat_id.strip()
        )

        raw_admin_ids = values.get("FEISHU_IDENTITY_ADMIN_OPEN_IDS", "")
        identity_admin_open_ids = frozenset(
            open_id.strip()
            for open_id in raw_admin_ids.split(",")
            if open_id.strip()
        )

        raw_task_admin_ids = values.get("FEISHU_TASK_ADMIN_OPEN_IDS", "")
        task_admin_open_ids = frozenset(
            open_id.strip()
            for open_id in raw_task_admin_ids.split(",")
            if open_id.strip()
        )

        raw_private_cards = values.get(
            "FEISHU_PRIVATE_TASK_CARDS_ENABLED", "false"
        ).strip().lower()
        if raw_private_cards not in {"true", "false"}:
            raise SettingsError(
                "FEISHU_PRIVATE_TASK_CARDS_ENABLED must be true or false"
            )
        raw_card_actions = values.get(
            "FEISHU_TASK_CARD_ACTIONS_ENABLED", "false"
        ).strip().lower()
        if raw_card_actions not in {"true", "false"}:
            raise SettingsError(
                "FEISHU_TASK_CARD_ACTIONS_ENABLED must be true or false"
            )
        if raw_card_actions == "true" and raw_private_cards != "true":
            raise SettingsError(
                "FEISHU_TASK_CARD_ACTIONS_ENABLED requires "
                "FEISHU_PRIVATE_TASK_CARDS_ENABLED=true"
            )

        level_name = values.get("FEISHU_LOG_LEVEL", "INFO").strip().upper()
        log_level = LOG_LEVELS.get(level_name)
        if log_level is None:
            raise SettingsError(
                "FEISHU_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            allowed_chat_ids=allowed_chat_ids,
            identity_admin_open_ids=identity_admin_open_ids,
            task_admin_open_ids=task_admin_open_ids,
            private_task_cards_enabled=raw_private_cards == "true",
            task_card_actions_enabled=raw_card_actions == "true",
            log_level=log_level,
        )


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Settings for the local message database."""

    url: str
    echo: bool

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "DatabaseSettings":
        url = values.get(
            "DATABASE_URL", "sqlite:///data/feishu_task_agent.db"
        ).strip()
        if not url:
            raise SettingsError("DATABASE_URL must not be empty")

        raw_echo = values.get("DATABASE_ECHO", "false").strip().lower()
        if raw_echo not in {"true", "false"}:
            raise SettingsError("DATABASE_ECHO must be true or false")

        return cls(url=url, echo=raw_echo == "true")


@dataclass(frozen=True, slots=True)
class ManagementWebSettings:
    """Management login and API listener settings."""

    enabled: bool = False
    public_base_url: str = "http://127.0.0.1:8000"
    frontend_url: str = "http://127.0.0.1:3000"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    login_ttl_minutes: int = 5
    session_ttl_hours: int = 12
    cookie_secure: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "ManagementWebSettings":
        raw_enabled = values.get("MANAGEMENT_WEB_ENABLED", "false").strip().lower()
        raw_secure = values.get(
            "MANAGEMENT_WEB_COOKIE_SECURE", "false"
        ).strip().lower()
        if raw_enabled not in {"true", "false"}:
            raise SettingsError("MANAGEMENT_WEB_ENABLED must be true or false")
        if raw_secure not in {"true", "false"}:
            raise SettingsError(
                "MANAGEMENT_WEB_COOKIE_SECURE must be true or false"
            )
        public_base_url = _plain_origin(
            values.get(
                "MANAGEMENT_WEB_PUBLIC_BASE_URL",
                "http://127.0.0.1:8000",
            ),
            "MANAGEMENT_WEB_PUBLIC_BASE_URL",
        )
        frontend_url = _plain_origin(
            values.get(
                "MANAGEMENT_WEB_FRONTEND_URL",
                "http://127.0.0.1:3000",
            ),
            "MANAGEMENT_WEB_FRONTEND_URL",
        )
        bind_host = values.get(
            "MANAGEMENT_WEB_BIND_HOST", "127.0.0.1"
        ).strip()
        if bind_host not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "0.0.0.0",
            "::",
        }:
            raise SettingsError(
                "MANAGEMENT_WEB_BIND_HOST must be a loopback or wildcard address"
            )
        try:
            port = int(values.get("MANAGEMENT_WEB_PORT", "8000").strip())
            login_ttl_minutes = int(
                values.get("MANAGEMENT_LOGIN_TTL_MINUTES", "5").strip()
            )
            session_ttl_hours = int(
                values.get("MANAGEMENT_SESSION_TTL_HOURS", "12").strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "MANAGEMENT_WEB port and TTL settings must be integers"
            ) from exc
        if not 1024 <= port <= 65535:
            raise SettingsError("MANAGEMENT_WEB_PORT must be between 1024 and 65535")
        if not 1 <= login_ttl_minutes <= 30:
            raise SettingsError(
                "MANAGEMENT_LOGIN_TTL_MINUTES must be between 1 and 30"
            )
        if not 1 <= session_ttl_hours <= 168:
            raise SettingsError(
                "MANAGEMENT_SESSION_TTL_HOURS must be between 1 and 168"
            )
        cookie_secure = raw_secure == "true"
        if cookie_secure and urlparse(public_base_url).scheme != "https":
            raise SettingsError(
                "MANAGEMENT_WEB_COOKIE_SECURE=true requires an HTTPS public URL"
            )
        return cls(
            enabled=raw_enabled == "true",
            public_base_url=public_base_url,
            frontend_url=frontend_url,
            bind_host=bind_host,
            port=port,
            login_ttl_minutes=login_ttl_minutes,
            session_ttl_hours=session_ttl_hours,
            cookie_secure=cookie_secure,
        )


@dataclass(frozen=True, slots=True)
class TaskLlmSettings:
    """Settings for the OpenAI-compatible Phase 3B detector."""

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float
    max_retries: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "TaskLlmSettings":
        api_key = values.get("TASK_LLM_API_KEY", "").strip()
        base_url = values.get("TASK_LLM_BASE_URL", "").strip().rstrip("/")
        model = values.get("TASK_LLM_MODEL", "").strip()
        if not api_key:
            raise SettingsError("TASK_LLM_API_KEY is missing")
        if not base_url:
            raise SettingsError("TASK_LLM_BASE_URL is missing")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise SettingsError("TASK_LLM_BASE_URL must be an HTTP(S) URL")
        if parsed_url.query or parsed_url.fragment:
            raise SettingsError(
                "TASK_LLM_BASE_URL must not contain a query or fragment"
            )
        if not model:
            raise SettingsError("TASK_LLM_MODEL is missing")

        try:
            timeout_seconds = float(
                values.get("TASK_LLM_TIMEOUT_SECONDS", "60").strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "TASK_LLM_TIMEOUT_SECONDS must be a number"
            ) from exc
        if not 1 <= timeout_seconds <= 300:
            raise SettingsError(
                "TASK_LLM_TIMEOUT_SECONDS must be between 1 and 300"
            )

        try:
            max_retries = int(
                values.get("TASK_LLM_MAX_RETRIES", "2").strip()
            )
        except ValueError as exc:
            raise SettingsError("TASK_LLM_MAX_RETRIES must be an integer") from exc
        if not 0 <= max_retries <= 5:
            raise SettingsError("TASK_LLM_MAX_RETRIES must be between 0 and 5")

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )


@dataclass(frozen=True, slots=True)
class DetectionSettings:
    """Settings for automatic detection scheduling."""

    debounce_seconds: int = 20

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "DetectionSettings":
        try:
            debounce_seconds = int(
                values.get("DETECTION_DEBOUNCE_SECONDS", "20").strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "DETECTION_DEBOUNCE_SECONDS must be an integer"
            ) from exc
        if not 1 <= debounce_seconds <= 60:
            raise SettingsError(
                "DETECTION_DEBOUNCE_SECONDS must be between 1 and 60"
            )
        return cls(debounce_seconds=debounce_seconds)


@dataclass(frozen=True, slots=True)
class DetectionWorkerSettings:
    """Safety and retry settings for one task-detection Worker process."""

    context_limit: int = 30
    lease_seconds: int = 300
    retry_base_seconds: int = 30
    poll_seconds: float = 2.0

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str]
    ) -> "DetectionWorkerSettings":
        try:
            context_limit = int(
                values.get("DETECTION_WORKER_CONTEXT_LIMIT", "30").strip()
            )
            lease_seconds = int(
                values.get("DETECTION_WORKER_LEASE_SECONDS", "300").strip()
            )
            retry_base_seconds = int(
                values.get(
                    "DETECTION_WORKER_RETRY_BASE_SECONDS", "30"
                ).strip()
            )
            poll_seconds = float(
                values.get("DETECTION_WORKER_POLL_SECONDS", "2").strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "DETECTION_WORKER settings must be numeric values"
            ) from exc
        if not 1 <= context_limit <= 100:
            raise SettingsError(
                "DETECTION_WORKER_CONTEXT_LIMIT must be between 1 and 100"
            )
        if not 10 <= lease_seconds <= 3_600:
            raise SettingsError(
                "DETECTION_WORKER_LEASE_SECONDS must be between 10 and 3600"
            )
        if not 1 <= retry_base_seconds <= 3_600:
            raise SettingsError(
                "DETECTION_WORKER_RETRY_BASE_SECONDS must be between 1 and 3600"
            )
        if not 0.1 <= poll_seconds <= 60:
            raise SettingsError(
                "DETECTION_WORKER_POLL_SECONDS must be between 0.1 and 60"
            )
        return cls(
            context_limit=context_limit,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            poll_seconds=poll_seconds,
        )


@dataclass(frozen=True, slots=True)
class TaskSettings:
    """Settings for converting validated candidates into task records."""

    auto_todo_confidence: float = 0.85

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "TaskSettings":
        try:
            threshold = float(
                values.get("TASK_AUTO_TODO_CONFIDENCE", "0.85").strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "TASK_AUTO_TODO_CONFIDENCE must be a number"
            ) from exc
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise SettingsError(
                "TASK_AUTO_TODO_CONFIDENCE must be between 0 and 1"
            )
        return cls(auto_todo_confidence=threshold)


@dataclass(frozen=True, slots=True)
class LifecycleSettings:
    """Safety gate for private natural-language lifecycle writes."""

    private_writes_enabled: bool = False
    context_limit: int = 20
    minimum_confidence: float = 0.9

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "LifecycleSettings":
        raw_enabled = values.get(
            "LIFECYCLE_PRIVATE_WRITES_ENABLED", "false"
        ).strip().lower()
        if raw_enabled not in {"true", "false"}:
            raise SettingsError(
                "LIFECYCLE_PRIVATE_WRITES_ENABLED must be true or false"
            )
        try:
            context_limit = int(
                values.get("LIFECYCLE_PRIVATE_CONTEXT_LIMIT", "20").strip()
            )
            minimum_confidence = float(
                values.get(
                    "LIFECYCLE_MUTATION_MIN_CONFIDENCE", "0.9"
                ).strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "LIFECYCLE context limit and confidence must be numeric"
            ) from exc
        if not 1 <= context_limit <= 50:
            raise SettingsError(
                "LIFECYCLE_PRIVATE_CONTEXT_LIMIT must be between 1 and 50"
            )
        if (
            not math.isfinite(minimum_confidence)
            or not 0 <= minimum_confidence <= 1
        ):
            raise SettingsError(
                "LIFECYCLE_MUTATION_MIN_CONFIDENCE must be between 0 and 1"
            )
        return cls(
            private_writes_enabled=raw_enabled == "true",
            context_limit=context_limit,
            minimum_confidence=minimum_confidence,
        )


@dataclass(frozen=True, slots=True)
class ReminderSettings:
    """Phase 5 reminder planning and future delivery safety settings."""

    due_day_hour: int = 9
    overdue_grace_minutes: int = 1
    max_attempts: int = 3
    test_mode: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "ReminderSettings":
        try:
            due_day_hour = int(
                values.get("REMINDER_DUE_DAY_HOUR", "9").strip()
            )
            overdue_grace_minutes = int(
                values.get("REMINDER_OVERDUE_GRACE_MINUTES", "1").strip()
            )
            max_attempts = int(
                values.get("REMINDER_MAX_ATTEMPTS", "3").strip()
            )
        except ValueError as exc:
            raise SettingsError("REMINDER settings must be integers") from exc
        raw_test_mode = values.get(
            "REMINDER_TEST_MODE", "false"
        ).strip().lower()
        if raw_test_mode not in {"true", "false"}:
            raise SettingsError("REMINDER_TEST_MODE must be true or false")
        if not 0 <= due_day_hour <= 23:
            raise SettingsError(
                "REMINDER_DUE_DAY_HOUR must be between 0 and 23"
            )
        if not 0 <= overdue_grace_minutes <= 1_440:
            raise SettingsError(
                "REMINDER_OVERDUE_GRACE_MINUTES must be between 0 and 1440"
            )
        if not 1 <= max_attempts <= 10:
            raise SettingsError(
                "REMINDER_MAX_ATTEMPTS must be between 1 and 10"
            )
        return cls(
            due_day_hour=due_day_hour,
            overdue_grace_minutes=overdue_grace_minutes,
            max_attempts=max_attempts,
            test_mode=raw_test_mode == "true",
        )


@dataclass(frozen=True, slots=True)
class ReminderWorkerSettings:
    """Lease, retry, and polling settings for reminder delivery."""

    lease_seconds: int = 120
    retry_base_seconds: int = 30
    poll_seconds: float = 5.0

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str]
    ) -> "ReminderWorkerSettings":
        try:
            lease_seconds = int(
                values.get("REMINDER_WORKER_LEASE_SECONDS", "120").strip()
            )
            retry_base_seconds = int(
                values.get(
                    "REMINDER_WORKER_RETRY_BASE_SECONDS", "30"
                ).strip()
            )
            poll_seconds = float(
                values.get("REMINDER_WORKER_POLL_SECONDS", "5").strip()
            )
        except ValueError as exc:
            raise SettingsError(
                "REMINDER_WORKER settings must be numeric values"
            ) from exc
        if not 10 <= lease_seconds <= 3_600:
            raise SettingsError(
                "REMINDER_WORKER_LEASE_SECONDS must be between 10 and 3600"
            )
        if not 1 <= retry_base_seconds <= 3_600:
            raise SettingsError(
                "REMINDER_WORKER_RETRY_BASE_SECONDS must be between 1 and 3600"
            )
        if not 0.1 <= poll_seconds <= 60:
            raise SettingsError(
                "REMINDER_WORKER_POLL_SECONDS must be between 0.1 and 60"
            )
        return cls(
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            poll_seconds=poll_seconds,
        )


def load_settings(env_file: Path | str = ".env") -> FeishuSettings:
    """Load a local .env file without overriding exported environment values."""

    load_dotenv(dotenv_path=env_file, override=False)
    return FeishuSettings.from_mapping(os.environ)


def load_database_settings(env_file: Path | str = ".env") -> DatabaseSettings:
    """Load database settings without requiring Feishu credentials."""

    load_dotenv(dotenv_path=env_file, override=False)
    return DatabaseSettings.from_mapping(os.environ)


def load_management_web_settings(
    env_file: Path | str = ".env",
) -> ManagementWebSettings:
    """Load local management login and HTTP settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return ManagementWebSettings.from_mapping(os.environ)


def _plain_origin(value: str, name: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise SettingsError(f"{name} must be a plain HTTP(S) origin")
    return value


def load_task_llm_settings(
    env_file: Path | str = ".env",
) -> TaskLlmSettings:
    """Load task-detector settings without requiring Feishu credentials."""

    load_dotenv(dotenv_path=env_file, override=False)
    return TaskLlmSettings.from_mapping(os.environ)


def load_detection_settings(
    env_file: Path | str = ".env",
) -> DetectionSettings:
    """Load automatic detection scheduling settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return DetectionSettings.from_mapping(os.environ)


def load_detection_worker_settings(
    env_file: Path | str = ".env",
) -> DetectionWorkerSettings:
    """Load task-detection Worker settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return DetectionWorkerSettings.from_mapping(os.environ)


def load_task_settings(env_file: Path | str = ".env") -> TaskSettings:
    """Load task-materialization settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return TaskSettings.from_mapping(os.environ)


def load_lifecycle_settings(
    env_file: Path | str = ".env",
) -> LifecycleSettings:
    """Load the separately gated private lifecycle-write settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return LifecycleSettings.from_mapping(os.environ)


def load_reminder_settings(
    env_file: Path | str = ".env",
) -> ReminderSettings:
    """Load durable reminder planning settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return ReminderSettings.from_mapping(os.environ)


def load_reminder_worker_settings(
    env_file: Path | str = ".env",
) -> ReminderWorkerSettings:
    """Load reminder delivery Worker settings."""

    load_dotenv(dotenv_path=env_file, override=False)
    return ReminderWorkerSettings.from_mapping(os.environ)
