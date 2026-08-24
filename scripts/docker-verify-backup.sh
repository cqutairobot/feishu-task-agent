#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"

usage() {
  echo "Usage: $0 /absolute/or/relative/path/to/backup.db" >&2
}

if [[ "$#" -ne 1 ]]; then
  usage
  exit 2
fi

cd "${repo_root}"

if ! docker compose version >/dev/null 2>&1; then
  echo "Restore verification failed: Docker Compose is unavailable." >&2
  exit 1
fi

backup_input="$1"
if [[ ! -f "${backup_input}" || ! -r "${backup_input}" ]]; then
  echo "Restore verification failed: backup is not a readable file." >&2
  exit 1
fi

backup_dir="$(cd -- "$(dirname -- "${backup_input}")" && pwd -P)"
backup_name="$(basename -- "${backup_input}")"
backup_path="${backup_dir}/${backup_name}"

if [[ "${backup_name}" == */* || "${backup_name}" == "." || "${backup_name}" == ".." ]]; then
  echo "Restore verification failed: backup filename is invalid." >&2
  exit 1
fi

backend_image="$(docker compose images --quiet management-api | head -n 1)"
if [[ -z "${backend_image}" ]]; then
  echo "Restore verification failed: management-api image is unavailable." >&2
  exit 1
fi

timestamp="$(date '+%Y%m%d-%H%M%S')"
restore_volume="feishu-task-agent-restore-check-${timestamp}-$$"
volume_created=false

if docker volume inspect "${restore_volume}" >/dev/null 2>&1; then
  echo "Restore verification failed: temporary volume already exists." >&2
  exit 1
fi

cleanup() {
  if [[ "${volume_created}" == "true" ]]; then
    case "${restore_volume}" in
      feishu-task-agent-restore-check-*)
        docker volume rm "${restore_volume}" >/dev/null 2>&1 || true
        ;;
    esac
  fi
}
trap cleanup EXIT

docker volume create "${restore_volume}" >/dev/null
volume_created=true

source_sha256="$(sha256sum "${backup_path}" | awk '{print $1}')"

docker run --rm \
  --user 0:0 \
  --entrypoint python \
  -e "BACKUP_FILE_NAME=${backup_name}" \
  -v "${backup_dir}:/backup-source:ro" \
  -v "${restore_volume}:/restore-data" \
  "${backend_image}" - <<'PY'
import os
import shutil
import sqlite3
from pathlib import Path

name = os.environ["BACKUP_FILE_NAME"]
if Path(name).name != name or name in {".", ".."}:
    raise SystemExit("invalid backup filename")

source_path = Path("/backup-source") / name
restore_root = Path("/restore-data")
restore_path = restore_root / "feishu_task_agent.db"

source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
try:
    result = source.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit(f"source integrity check failed: {result!r}")
finally:
    source.close()

shutil.copyfile(source_path, restore_path)
restore_root.chmod(0o700)
os.chown(restore_root, 10001, 10001)
restore_path.chmod(0o600)
os.chown(restore_path, 10001, 10001)
print("source_integrity: ok")
PY

restored_sha256="$(
  docker run --rm \
    --entrypoint python \
    -v "${restore_volume}:/restore-data:ro" \
    "${backend_image}" - <<'PY'
import hashlib
from pathlib import Path

path = Path("/restore-data/feishu_task_agent.db")
digest = hashlib.sha256()
with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"

if [[ "${source_sha256}" != "${restored_sha256}" ]]; then
  echo "Restore verification failed: restored checksum does not match backup." >&2
  exit 1
fi

echo "restored_sha256: ${restored_sha256}"
echo "restored_database_status:"
docker run --rm \
  -e "DATABASE_URL=sqlite:////app/data/feishu_task_agent.db" \
  -v "${restore_volume}:/app/data" \
  "${backend_image}" db-status

echo "restore_verification: ok"
echo "live_volume_untouched: feishu-task-agent_task-data"
