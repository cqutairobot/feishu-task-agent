"""Phase 1A smoke tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import DatabaseSettings, FeishuSettings, LifecycleSettings
from app.main import main, runtime_summary


class RuntimeSmokeTest(unittest.TestCase):
    def test_runtime_summary_does_not_require_secrets(self) -> None:
        summary = runtime_summary()

        self.assertIn("local runtime is ready", summary)
        self.assertIn("Python", summary)

    def test_main_exits_successfully(self) -> None:
        self.assertEqual(main(["check"]), 0)

    def test_listener_stops_cleanly(self) -> None:
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            ingestion=object(),
            repository=MagicMock(),
            aliases=object(),
            tasks=object(),
        )
        with (
            patch(
                "app.main.load_settings",
                return_value=FeishuSettings.from_mapping(
                    {
                        "FEISHU_APP_ID": "cli_test",
                        "FEISHU_APP_SECRET": "secret",
                    }
                ),
            ),
            patch("app.main.load_database_settings"),
            patch(
                "app.main.load_lifecycle_settings",
                return_value=LifecycleSettings(),
            ),
            patch("app.main.open_database_runtime", return_value=runtime_context),
            patch(
                "app.feishu.receiver.start_listener",
                side_effect=KeyboardInterrupt,
            ) as start_listener,
        ):
            self.assertEqual(main(["listen"]), 0)
            start_listener.assert_called_once()

    def test_database_status_initializes_empty_store(self) -> None:
        from contextlib import redirect_stdout
        import io
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'status.db'}"
            output = io.StringIO()
            with (
                patch(
                    "app.main.load_database_settings",
                    return_value=DatabaseSettings(url=database_url, echo=False),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(main(["db-status"]), 0)

        self.assertIn("SQLite message store is ready", output.getvalue())
        self.assertIn("messages: 0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
