#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"
backup_dir="${BACKUP_DIR:-${HOME}/feishu-task-agent-backups}"

cd "${repo_root}"

if [[ ! -f .env ]]; then
  echo "Backup failed: ${repo_root}/.env does not exist." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Backup failed: Docker Compose is unavailable." >&2
  exit 1
fi

management_container="$(docker compose ps --quiet --status running management-api)"
if [[ -z "${management_container}" ]]; then
  echo "Backup failed: management-api is not running." >&2
  exit 1
fi

mkdir -p -- "${backup_dir}"
chmod 700 -- "${backup_dir}"
backup_dir="$(cd -- "${backup_dir}" && pwd -P)"

timestamp="$(date '+%Y%m%d-%H%M%S')"
backup_path="${backup_dir}/feishu-task-agent-${timestamp}.db"
staging_path="${backup_path}.partial"
container_path="/tmp/feishu-task-agent-backup-${timestamp}-$$.db"

if [[ -e "${backup_path}" || -e "${staging_path}" ]]; then
  echo "Backup failed: destination already exists for timestamp ${timestamp}." >&2
  exit 1
fi

cleanup() {
  docker compose exec -T \
    -e "BACKUP_TEMP_PATH=${container_path}" \
    management-api python - <<'PY' >/dev/null 2>&1 || true
import os
from pathlib import Path

path = Path(os.environ["BACKUP_TEMP_PATH"])
if path.parent == Path("/tmp") and path.name.startswith("feishu-task-agent-backup-"):
    path.unlink(missing_ok=True)
PY

  case "${staging_path}" in
    "${backup_dir}"/feishu-task-agent-*.db.partial)
      rm -f -- "${staging_path}"
      ;;
  esac
}
trap cleanup EXIT

docker compose exec -T \
  -e "BACKUP_TEMP_PATH=${container_path}" \
  management-api python - <<'PY'
import os
import sqlite3
from pathlib import Path

source_path = Path("/app/data/feishu_task_agent.db")
backup_path = Path(os.environ["BACKUP_TEMP_PATH"])
if backup_path.parent != Path("/tmp") or not backup_path.name.startswith(
    "feishu-task-agent-backup-"
):
    raise SystemExit("invalid container backup path")

backup_path.unlink(missing_ok=True)
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
backup = sqlite3.connect(backup_path, timeout=30)
try:
    source.backup(backup, sleep=0.1)
    result = backup.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit(f"backup integrity check failed: {result!r}")
finally:
    backup.close()
    source.close()

print("backup_integrity: ok")
PY

container_sha256="$(
  docker compose exec -T \
    -e "BACKUP_TEMP_PATH=${container_path}" \
    management-api python - <<'PY'
import hashlib
import os
from pathlib import Path

path = Path(os.environ["BACKUP_TEMP_PATH"])
digest = hashlib.sha256()
with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"

docker compose cp "management-api:${container_path}" "${staging_path}"
host_sha256="$(sha256sum "${staging_path}" | awk '{print $1}')"

if [[ "${container_sha256}" != "${host_sha256}" ]]; then
  echo "Backup failed: copied file checksum does not match container snapshot." >&2
  exit 1
fi

mv -- "${staging_path}" "${backup_path}"
chmod 600 -- "${backup_path}"

printf 'backup_path: %s\n' "${backup_path}"
printf 'backup_sha256: %s\n' "${host_sha256}"
