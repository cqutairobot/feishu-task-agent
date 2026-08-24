"""Safe explicit CLI entry point tests for the detection Worker."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.agent.worker import WorkerOutcome, WorkerOutcomeStatus
from app.config import (
    DatabaseSettings,
    DetectionWorkerSettings,
    TaskSettings,
    TaskLlmSettings,
)
from app.main import _effective_worker_lease_seconds, main


class DetectionWorkerCliTest(unittest.TestCase):
    def test_worker_requires_explicit_once_mode(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["worker"])

    def test_forever_mode_rejects_targeted_job_id(self) -> None:
        error = io.StringIO()

        with redirect_stderr(error):
            exit_code = main(["worker", "--forever", "--job-id", "42"])

        self.assertEqual(exit_code, 2)
        self.assertIn("only be used with --once", error.getvalue())

    def test_targeted_once_mode_passes_exact_job_id(self) -> None:
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            repository=MagicMock(),
            aliases=MagicMock(),
            detection_queue=MagicMock(),
            tasks=MagicMock(),
        )
        detector_context = MagicMock()
        detector_context.__enter__.return_value = MagicMock()
        outcome = WorkerOutcome(
            status=WorkerOutcomeStatus.COMPLETED,
            job_id=42,
            run_id=8,
            attempt=1,
            candidate_count=2,
            created_task_count=2,
            reused_task_count=0,
            task_ids=(11, 12),
            error_code=None,
            retry_at=None,
        )
        worker_instance = MagicMock()
        worker_instance.run_once.return_value = outcome
        output = io.StringIO()

        with (
            patch(
                "app.main.load_database_settings",
                return_value=DatabaseSettings(
                    url="sqlite:///unused-test.db", echo=False
                ),
            ),
            patch(
                "app.main.load_task_llm_settings",
                return_value=TaskLlmSettings(
                    api_key="test-key",
                    base_url="https://llm.example.test/v1",
                    model="qwen-test",
                    timeout_seconds=60,
                    max_retries=2,
                ),
            ),
            patch(
                "app.main.load_detection_worker_settings",
                return_value=DetectionWorkerSettings(),
            ),
            patch(
                "app.main.load_task_settings",
                return_value=TaskSettings(),
            ),
            patch(
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            patch(
                "app.agent.provider.OpenAICompatibleTaskDetector",
                return_value=detector_context,
            ),
            patch(
                "app.agent.worker.DetectionWorker",
                return_value=worker_instance,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(["worker", "--once", "--job-id", "42"])

        self.assertEqual(exit_code, 0)
        worker_instance.run_once.assert_called_once()
        self.assertEqual(
            worker_instance.run_once.call_args.kwargs["job_id"], 42
        )
        self.assertEqual(json.loads(output.getvalue())["job_id"], 42)
        self.assertEqual(json.loads(output.getvalue())["task_ids"], [11, 12])

    def test_lease_expands_to_cover_provider_retry_budget(self) -> None:
        lease = _effective_worker_lease_seconds(
            300,
            timeout_seconds=300,
            max_retries=5,
        )

        self.assertGreaterEqual(lease, 1_845)
        self.assertLessEqual(lease, 3_600)


if __name__ == "__main__":
    unittest.main()
