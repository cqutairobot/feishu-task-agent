"""End-to-end tests for the Phase 2E-A alias CLI."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import DatabaseSettings
from app.database.runtime import open_database_runtime
from app.main import main
from tests.test_messages import TEXT_EVENT


class AliasCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "alias-cli.db"
        self.settings = DatabaseSettings(
            url=f"sqlite:///{database_path}", echo=False
        )
        with open_database_runtime(self.settings) as runtime:
            runtime.ingestion.process_payload(
                TEXT_EVENT,
                received_at=datetime(
                    2026, 8, 22, 18, 32, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
            )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_sets_alias_from_message_and_resolves_it(self) -> None:
        set_output = io.StringIO()
        resolve_output = io.StringIO()
        with patch("app.main.load_database_settings", return_value=self.settings):
            with redirect_stdout(set_output):
                set_status = main(
                    [
                        "alias",
                        "set",
                        "--message-id",
                        "om_test",
                        "--name",
                        "王政",
                    ]
                )
            with redirect_stdout(resolve_output):
                resolve_status = main(
                    [
                        "alias",
                        "resolve",
                        "--chat-id",
                        "oc_test",
                        "--name",
                        "王政",
                    ]
                )

        self.assertEqual(set_status, 0)
        self.assertEqual(resolve_status, 0)
        self.assertIn("Alias saved", set_output.getvalue())
        self.assertIn("open_id: ou_test", resolve_output.getvalue())

    def test_rejects_mixed_message_and_member_arguments(self) -> None:
        error_output = io.StringIO()
        with (
            patch("app.main.load_database_settings", return_value=self.settings),
            redirect_stderr(error_output),
        ):
            status = main(
                [
                    "alias",
                    "set",
                    "--message-id",
                    "om_test",
                    "--chat-id",
                    "oc_test",
                    "--name",
                    "王政",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("use either --message-id", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
