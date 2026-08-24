"""CLI tests for explicit Phase 4A run materialization."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.config import DatabaseSettings, TaskSettings
from app.main import main
from app.tasks.repository import (
    MaterializationResult,
    TaskMaterializationError,
)


class TaskMaterializeCliTest(unittest.TestCase):
    def test_materializes_only_the_requested_run(self) -> None:
        repository = MagicMock()
        timestamp = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
        repository.materialize_run.return_value = MaterializationResult(
            detection_run_id=7,
            already_materialized=False,
            candidate_count=2,
            created_task_count=2,
            reused_task_count=0,
            task_ids=(11, 12),
            materialized_at=timestamp,
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            tasks=repository
        )
        output = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.load_task_settings",
                return_value=TaskSettings(),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ) as open_runtime,
            redirect_stdout(output),
        ):
            exit_code = main(["task-materialize", "--run-id", "7"])

        self.assertEqual(exit_code, 0)
        repository.materialize_run.assert_called_once()
        self.assertEqual(repository.materialize_run.call_args.args, (7,))
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["task_ids"], [11, 12])
        open_runtime.assert_called_once()
        self.assertEqual(
            open_runtime.call_args.kwargs["task_settings"], TaskSettings()
        )

    def test_reports_materialization_validation_failure(self) -> None:
        repository = MagicMock()
        repository.materialize_run.side_effect = TaskMaterializationError(
            "only succeeded detection runs can be materialized"
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            tasks=repository
        )
        error = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.load_task_settings",
                return_value=TaskSettings(),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            redirect_stderr(error),
        ):
            exit_code = main(["task-materialize", "--run-id", "7"])

        self.assertEqual(exit_code, 2)
        self.assertIn("only succeeded", error.getvalue())


if __name__ == "__main__":
    unittest.main()
