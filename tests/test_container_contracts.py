"""Static production-container and Compose contract tests."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerContractTest(unittest.TestCase):
    def test_backend_image_is_non_root_and_does_not_copy_environment(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.13-slim-bookworm", dockerfile)
        self.assertIn("USER app", dockerfile)
        self.assertIn("ENTRYPOINT", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+\.\s+")
        self.assertIn(".env", dockerignore)
        self.assertIn("data", dockerignore)

    def test_frontend_uses_non_root_standalone_runtime(self) -> None:
        dockerfile = (ROOT / "management-web" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        next_config = (ROOT / "management-web" / "next.config.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn('output: "standalone"', next_config)
        self.assertIn("/app/dist/standalone", dockerfile)
        self.assertIn("USER node", dockerfile)
        self.assertIn('["node", "server.js"]', dockerfile)

    def test_compose_separates_services_and_persists_sqlite(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        for service in (
            "migrate",
            "listener",
            "detection-worker",
            "reminder-worker",
            "notification-worker",
            "management-api",
            "management-web",
            "gateway",
        ):
            self.assertRegex(compose, rf"(?m)^  {re.escape(service)}:$")
        self.assertIn("sqlite:////app/data/feishu_task_agent.db", compose)
        self.assertIn("task-data:/app/data", compose)
        self.assertIn("condition: service_completed_successfully", compose)
        self.assertIn("condition: service_healthy", compose)
        self.assertGreaterEqual(compose.count("restart: unless-stopped"), 7)
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotIn("FEISHU_APP_SECRET:", compose)
        self.assertNotIn("TASK_LLM_API_KEY:", compose)

    def test_gateway_keeps_browser_traffic_same_origin(self) -> None:
        nginx = (ROOT / "gateway" / "nginx.conf").read_text(encoding="utf-8")

        self.assertIn("location ^~ /api/", nginx)
        self.assertIn("location ^~ /auth/", nginx)
        self.assertIn("location = /health", nginx)
        self.assertIn("resolver 127.0.0.11", nginx)
        self.assertEqual(nginx.count(" resolve;"), 2)
        self.assertIn("proxy_pass http://management_api", nginx)
        self.assertIn("proxy_pass http://management_web", nginx)

    def test_docker_backup_uses_sqlite_snapshot_and_verifies_copy(self) -> None:
        script_path = ROOT / "scripts" / "docker-backup.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(script_path.stat().st_mode & 0o100)
        self.assertIn("source.backup(backup", script)
        self.assertIn("PRAGMA integrity_check", script)
        self.assertIn("container_sha256", script)
        self.assertIn("host_sha256", script)
        self.assertIn("chmod 600", script)
        self.assertIn("management-api is not running", script)
        self.assertNotIn("down --volumes", script)

    def test_restore_verification_uses_disposable_volume_only(self) -> None:
        script_path = ROOT / "scripts" / "docker-verify-backup.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(script_path.stat().st_mode & 0o100)
        self.assertIn("feishu-task-agent-restore-check-", script)
        self.assertIn("source_integrity: ok", script)
        self.assertIn("restored checksum does not match backup", script)
        self.assertIn("restore_verification: ok", script)
        self.assertIn("live_volume_untouched", script)
        self.assertNotIn("-v feishu-task-agent_task-data:/restore-data", script)
        self.assertNotIn("down --volumes", script)


if __name__ == "__main__":
    unittest.main()
