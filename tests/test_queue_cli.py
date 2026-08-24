"""CLI tests for auditable detection queue cancellation."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.agent.queue import CancellationResult, DetectionJobStatus
from app.config import DatabaseSettings
from app.main import main


class DetectionQueueCliTest(unittest.TestCase):
    def test_cancel_passes_all_exact_job_ids_in_one_atomic_call(self) -> None:
        queue = MagicMock()
        timestamp = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)
        queue.cancel_jobs.return_value = (
            CancellationResult(
                job_id=1,
                changed=True,
                status=DetectionJobStatus.CANCELLED,
                cancelled_at=timestamp,
                reason="acceptance cleanup",
            ),
            CancellationResult(
                job_id=2,
                changed=True,
                status=DetectionJobStatus.CANCELLED,
                cancelled_at=timestamp,
                reason="acceptance cleanup",
            ),
        )
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = SimpleNamespace(
            detection_queue=queue
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
                "app.main.open_database_runtime",
                return_value=runtime_context,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "queue",
                    "cancel",
                    "--job-id",
                    "1",
                    "--job-id",
                    "2",
                    "--reason",
                    "acceptance cleanup",
                ]
            )

        self.assertEqual(exit_code, 0)
        queue.cancel_jobs.assert_called_once()
        self.assertEqual(queue.cancel_jobs.call_args.args, ((1, 2),))
        payload = json.loads(output.getvalue())
        self.assertEqual(
            [item["status"] for item in payload["cancellations"]],
            ["cancelled", "cancelled"],
        )


if __name__ == "__main__":
    unittest.main()
