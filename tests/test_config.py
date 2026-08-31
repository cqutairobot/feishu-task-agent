"""Tests for safe environment configuration."""

import logging
import unittest

from app.config import (
    DatabaseSettings,
    DetectionSettings,
    DetectionWorkerSettings,
    FeishuSettings,
    LifecycleSettings,
    ManagementWebSettings,
    ReminderSettings,
    ReminderWorkerSettings,
    SettingsError,
    TaskSettings,
    TaskLlmSettings,
)


class FeishuSettingsTest(unittest.TestCase):
    def test_loads_credentials_allowlist_and_log_level(self) -> None:
        settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-value",
                "FEISHU_ALLOWED_CHAT_IDS": "oc_one, oc_two,oc_one",
                "FEISHU_LOG_LEVEL": "debug",
            }
        )

        self.assertEqual(settings.app_id, "cli_test")
        self.assertEqual(settings.app_secret, "secret-value")
        self.assertEqual(settings.allowed_chat_ids, {"oc_one", "oc_two"})
        self.assertEqual(settings.identity_admin_open_ids, frozenset())
        self.assertEqual(settings.task_admin_open_ids, frozenset())
        self.assertFalse(settings.private_task_cards_enabled)
        self.assertFalse(settings.task_card_actions_enabled)
        self.assertEqual(settings.log_level, logging.DEBUG)

    def test_loads_identity_administrator_allowlist(self) -> None:
        settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-value",
                "FEISHU_IDENTITY_ADMIN_OPEN_IDS": "ou_one, ou_two,ou_one",
            }
        )

        self.assertEqual(
            settings.identity_admin_open_ids, {"ou_one", "ou_two"}
        )

    def test_loads_task_administrator_allowlist(self) -> None:
        settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-value",
                "FEISHU_TASK_ADMIN_OPEN_IDS": "ou_one, ou_two,ou_one",
            }
        )

        self.assertEqual(settings.task_admin_open_ids, {"ou_one", "ou_two"})

    def test_loads_private_task_card_gate(self) -> None:
        settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-value",
                "FEISHU_PRIVATE_TASK_CARDS_ENABLED": "true",
            }
        )

        self.assertTrue(settings.private_task_cards_enabled)
        self.assertFalse(settings.task_card_actions_enabled)

        with self.assertRaisesRegex(SettingsError, "true or false"):
            FeishuSettings.from_mapping(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret-value",
                    "FEISHU_PRIVATE_TASK_CARDS_ENABLED": "yes",
                }
            )

    def test_card_action_gate_requires_private_cards(self) -> None:
        settings = FeishuSettings.from_mapping(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-value",
                "FEISHU_PRIVATE_TASK_CARDS_ENABLED": "true",
                "FEISHU_TASK_CARD_ACTIONS_ENABLED": "true",
            }
        )

        self.assertTrue(settings.task_card_actions_enabled)

        with self.assertRaisesRegex(SettingsError, "requires"):
            FeishuSettings.from_mapping(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret-value",
                    "FEISHU_TASK_CARD_ACTIONS_ENABLED": "true",
                }
            )
        with self.assertRaisesRegex(SettingsError, "true or false"):
            FeishuSettings.from_mapping(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret-value",
                    "FEISHU_TASK_CARD_ACTIONS_ENABLED": "yes",
                }
            )

    def test_missing_app_secret_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "FEISHU_APP_SECRET"):
            FeishuSettings.from_mapping({"FEISHU_APP_ID": "cli_test"})

    def test_invalid_log_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "FEISHU_LOG_LEVEL"):
            FeishuSettings.from_mapping(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret-value",
                    "FEISHU_LOG_LEVEL": "LOUD",
                }
            )


class DatabaseSettingsTest(unittest.TestCase):
    def test_defaults_to_local_sqlite_file(self) -> None:
        settings = DatabaseSettings.from_mapping({})

        self.assertEqual(settings.url, "sqlite:///data/feishu_task_agent.db")
        self.assertFalse(settings.echo)

    def test_rejects_invalid_echo_value(self) -> None:
        with self.assertRaisesRegex(SettingsError, "DATABASE_ECHO"):
            DatabaseSettings.from_mapping({"DATABASE_ECHO": "sometimes"})


class ManagementWebSettingsTest(unittest.TestCase):
    def test_loads_loopback_management_settings(self) -> None:
        settings = ManagementWebSettings.from_mapping(
            {
                "MANAGEMENT_WEB_ENABLED": "true",
                "MANAGEMENT_WEB_PUBLIC_BASE_URL": "http://127.0.0.1:8000/",
                "MANAGEMENT_WEB_FRONTEND_URL": "http://127.0.0.1:3000/",
                "MANAGEMENT_WEB_BIND_HOST": "127.0.0.1",
                "MANAGEMENT_WEB_PORT": "8000",
                "MANAGEMENT_LOGIN_TTL_MINUTES": "5",
                "MANAGEMENT_SESSION_TTL_HOURS": "12",
            }
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.public_base_url, "http://127.0.0.1:8000")
        self.assertEqual(settings.frontend_url, "http://127.0.0.1:3000")

    def test_allows_container_wildcard_bind(self) -> None:
        settings = ManagementWebSettings.from_mapping(
            {"MANAGEMENT_WEB_BIND_HOST": "0.0.0.0"}
        )

        self.assertEqual(settings.bind_host, "0.0.0.0")

    def test_rejects_unknown_bind_and_non_origin_urls(self) -> None:
        with self.assertRaisesRegex(SettingsError, "loopback or wildcard"):
            ManagementWebSettings.from_mapping(
                {"MANAGEMENT_WEB_BIND_HOST": "192.0.2.10"}
            )
        with self.assertRaisesRegex(SettingsError, "plain HTTP"):
            ManagementWebSettings.from_mapping(
                {
                    "MANAGEMENT_WEB_FRONTEND_URL": (
                        "http://127.0.0.1:3000/path"
                    )
                }
            )


class DetectionSettingsTest(unittest.TestCase):
    def test_defaults_to_twenty_second_window(self) -> None:
        settings = DetectionSettings.from_mapping({})

        self.assertEqual(settings.debounce_seconds, 20)

    def test_loads_configured_window(self) -> None:
        settings = DetectionSettings.from_mapping(
            {"DETECTION_DEBOUNCE_SECONDS": "15"}
        )

        self.assertEqual(settings.debounce_seconds, 15)

    def test_rejects_window_outside_safe_range(self) -> None:
        with self.assertRaisesRegex(SettingsError, "between 1 and 60"):
            DetectionSettings.from_mapping(
                {"DETECTION_DEBOUNCE_SECONDS": "0"}
            )


class DetectionWorkerSettingsTest(unittest.TestCase):
    def test_defaults_are_bounded(self) -> None:
        settings = DetectionWorkerSettings.from_mapping({})

        self.assertEqual(settings.context_limit, 30)
        self.assertEqual(settings.lease_seconds, 300)
        self.assertEqual(settings.retry_base_seconds, 30)
        self.assertEqual(settings.poll_seconds, 2.0)

    def test_loads_worker_settings(self) -> None:
        settings = DetectionWorkerSettings.from_mapping(
            {
                "DETECTION_WORKER_CONTEXT_LIMIT": "40",
                "DETECTION_WORKER_LEASE_SECONDS": "600",
                "DETECTION_WORKER_RETRY_BASE_SECONDS": "45",
                "DETECTION_WORKER_POLL_SECONDS": "1.5",
            }
        )

        self.assertEqual(settings.context_limit, 40)
        self.assertEqual(settings.lease_seconds, 600)
        self.assertEqual(settings.retry_base_seconds, 45)
        self.assertEqual(settings.poll_seconds, 1.5)

    def test_rejects_unsafe_worker_settings(self) -> None:
        with self.assertRaisesRegex(SettingsError, "CONTEXT_LIMIT"):
            DetectionWorkerSettings.from_mapping(
                {"DETECTION_WORKER_CONTEXT_LIMIT": "0"}
            )


class TaskLlmSettingsTest(unittest.TestCase):
    def test_loads_openai_compatible_settings(self) -> None:
        settings = TaskLlmSettings.from_mapping(
            {
                "TASK_LLM_API_KEY": "test-key",
                "TASK_LLM_BASE_URL": "https://llm.example.test/v1/",
                "TASK_LLM_MODEL": "qwen-test",
                "TASK_LLM_TIMEOUT_SECONDS": "45.5",
                "TASK_LLM_MAX_RETRIES": "3",
            }
        )

        self.assertEqual(settings.api_key, "test-key")
        self.assertEqual(settings.base_url, "https://llm.example.test/v1")
        self.assertEqual(settings.model, "qwen-test")
        self.assertEqual(settings.timeout_seconds, 45.5)
        self.assertEqual(settings.max_retries, 3)

    def test_missing_model_setting_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "TASK_LLM_MODEL"):
            TaskLlmSettings.from_mapping(
                {
                    "TASK_LLM_API_KEY": "test-key",
                    "TASK_LLM_BASE_URL": "https://llm.example.test/v1",
                }
            )


class TaskSettingsTest(unittest.TestCase):
    def test_defaults_to_conservative_auto_todo_threshold(self) -> None:
        settings = TaskSettings.from_mapping({})

        self.assertEqual(settings.auto_todo_confidence, 0.85)

    def test_loads_threshold_and_rejects_non_finite_value(self) -> None:
        settings = TaskSettings.from_mapping(
            {"TASK_AUTO_TODO_CONFIDENCE": "0.9"}
        )
        self.assertEqual(settings.auto_todo_confidence, 0.9)

        with self.assertRaisesRegex(SettingsError, "between 0 and 1"):
            TaskSettings.from_mapping(
                {"TASK_AUTO_TODO_CONFIDENCE": "NaN"}
            )


class LifecycleSettingsTest(unittest.TestCase):
    def test_defaults_keep_private_writes_disabled(self) -> None:
        settings = LifecycleSettings.from_mapping({})

        self.assertFalse(settings.private_writes_enabled)
        self.assertFalse(settings.review_writes_enabled)
        self.assertEqual(settings.context_limit, 20)
        self.assertEqual(settings.minimum_confidence, 0.9)

    def test_loads_and_rejects_unsafe_values(self) -> None:
        settings = LifecycleSettings.from_mapping(
            {
                "LIFECYCLE_PRIVATE_WRITES_ENABLED": "true",
                "LIFECYCLE_REVIEW_WRITES_ENABLED": "true",
                "LIFECYCLE_PRIVATE_CONTEXT_LIMIT": "12",
                "LIFECYCLE_MUTATION_MIN_CONFIDENCE": "0.95",
            }
        )
        self.assertTrue(settings.private_writes_enabled)
        self.assertTrue(settings.review_writes_enabled)
        self.assertEqual(settings.context_limit, 12)
        self.assertEqual(settings.minimum_confidence, 0.95)

        with self.assertRaisesRegex(SettingsError, "true or false"):
            LifecycleSettings.from_mapping(
                {"LIFECYCLE_PRIVATE_WRITES_ENABLED": "yes"}
            )
        with self.assertRaisesRegex(SettingsError, "requires"):
            LifecycleSettings.from_mapping(
                {
                    "LIFECYCLE_REVIEW_WRITES_ENABLED": "true",
                }
            )
        with self.assertRaisesRegex(SettingsError, "between 0 and 1"):
            LifecycleSettings.from_mapping(
                {"LIFECYCLE_MUTATION_MIN_CONFIDENCE": "NaN"}
            )


class ReminderSettingsTest(unittest.TestCase):
    def test_defaults_match_phase_five_policy(self) -> None:
        settings = ReminderSettings.from_mapping({})

        self.assertEqual(settings.due_day_hour, 9)
        self.assertEqual(settings.overdue_grace_minutes, 1)
        self.assertEqual(settings.max_attempts, 3)
        self.assertFalse(settings.test_mode)

    def test_loads_and_validates_reminder_settings(self) -> None:
        settings = ReminderSettings.from_mapping(
            {
                "REMINDER_DUE_DAY_HOUR": "8",
                "REMINDER_OVERDUE_GRACE_MINUTES": "5",
                "REMINDER_MAX_ATTEMPTS": "4",
                "REMINDER_TEST_MODE": "true",
            }
        )

        self.assertEqual(settings.due_day_hour, 8)
        self.assertEqual(settings.overdue_grace_minutes, 5)
        self.assertEqual(settings.max_attempts, 4)
        self.assertTrue(settings.test_mode)

        with self.assertRaisesRegex(SettingsError, "between 0 and 23"):
            ReminderSettings.from_mapping(
                {"REMINDER_DUE_DAY_HOUR": "24"}
            )
        with self.assertRaisesRegex(SettingsError, "true or false"):
            ReminderSettings.from_mapping(
                {"REMINDER_TEST_MODE": "yes"}
            )


class ReminderWorkerSettingsTest(unittest.TestCase):
    def test_defaults_match_delivery_policy(self) -> None:
        settings = ReminderWorkerSettings.from_mapping({})

        self.assertEqual(settings.lease_seconds, 120)
        self.assertEqual(settings.retry_base_seconds, 30)
        self.assertEqual(settings.poll_seconds, 5.0)

    def test_loads_and_validates_worker_settings(self) -> None:
        settings = ReminderWorkerSettings.from_mapping(
            {
                "REMINDER_WORKER_LEASE_SECONDS": "180",
                "REMINDER_WORKER_RETRY_BASE_SECONDS": "45",
                "REMINDER_WORKER_POLL_SECONDS": "1.5",
            }
        )

        self.assertEqual(settings.lease_seconds, 180)
        self.assertEqual(settings.retry_base_seconds, 45)
        self.assertEqual(settings.poll_seconds, 1.5)

        with self.assertRaisesRegex(SettingsError, "between 10 and 3600"):
            ReminderWorkerSettings.from_mapping(
                {"REMINDER_WORKER_LEASE_SECONDS": "9"}
            )

    def test_invalid_base_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "HTTP"):
            TaskLlmSettings.from_mapping(
                {
                    "TASK_LLM_API_KEY": "test-key",
                    "TASK_LLM_BASE_URL": "not-a-url",
                    "TASK_LLM_MODEL": "qwen-test",
                }
            )


if __name__ == "__main__":
    unittest.main()
