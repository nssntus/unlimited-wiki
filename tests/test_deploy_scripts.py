from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "offline-backup.sh"
ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_backup_script(tmp_path: Path, *, active: bool, start_fails: bool = False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _write_executable(bin_dir / "install", "exit 0\n")
    _write_executable(bin_dir / "runuser", f'echo "runuser $*" >> "{log}"\n')
    _write_executable(
        bin_dir / "systemctl",
        f'''echo "systemctl $*" >> "{log}"
if [ "$1" = "is-active" ]; then
  {'exit 0' if active else 'exit 3'}
fi
if [ "$1" = "start" ] && [ "{1 if start_fails else 0}" = "1" ]; then
  exit 1
fi
''',
    )
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WIKI_PROJECT_ROOT": str(tmp_path / "project"),
        "WIKI_BACKUP_ROOT": str(tmp_path / "backups"),
        "WIKI_SERVICE_USER": "wiki-test",
    }
    result = subprocess.run(
        ["sh", str(SCRIPT)], text=True, capture_output=True, env=environment, check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_offline_backup_does_not_start_an_inactive_service(tmp_path: Path):
    result, calls = _run_backup_script(tmp_path, active=False)
    assert result.returncode == 0
    assert "systemctl stop unlimited-wiki.service" not in calls
    assert "systemctl start unlimited-wiki.service" not in calls
    assert any(call.startswith("runuser ") for call in calls)


def test_offline_backup_reports_restart_failure(tmp_path: Path):
    result, calls = _run_backup_script(tmp_path, active=True, start_fails=True)
    assert result.returncode != 0
    assert "systemctl stop unlimited-wiki.service" in calls
    assert "systemctl start unlimited-wiki.service" in calls
    assert "failed to restart" in result.stderr


def test_supported_environment_enables_remote_workers_by_default():
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "systemd" / "unlimited-wiki.service").read_text(encoding="utf-8")
    backend = (ROOT / "serve.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/unlimited-wiki.env" in service
    assert "WIKI_DISABLE_REMOTE_WORKER=" not in environment
    assert "WIKI_REMOTE_TASK_KINDS=generate,supplement,governance" in environment
    assert "remote_tasks_enabled=remote_worker_enabled" in backend
    assert "Remote worker: disabled; new remote tasks will be rejected" in backend
    assert "只用于恢复后的隔离冒烟或明确的维护窗口" in readme
    assert "正常服务不得设置 `WIKI_DISABLE_REMOTE_WORKER`" in readme
    assert "重新启用会立即开始消费所有 Workspace" in readme
