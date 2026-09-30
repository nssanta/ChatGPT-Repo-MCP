from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import time

from chatrepo_mcp import server
from chatrepo_mcp.maintenance import run_maintenance_once


def _age(path: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_runtime_maintenance_prunes_only_expired_durable_files(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    artifacts = jobs / "artifacts"
    locks = jobs / "locks"
    artifacts.mkdir(parents=True)
    locks.mkdir(parents=True)
    audit = tmp_path / "state" / "commands.log"
    audit.parent.mkdir(parents=True)

    active_audit = audit
    old_rotated = audit.with_name("commands.log.1")
    recent_rotated = audit.with_name("commands.log.2")
    old_lock = locks / "old.json"
    recent_lock = locks / "recent.json"
    old_tmp = artifacts / "abandoned.tmp"
    recent_tmp = artifacts / "fresh.tmp"
    orphan_artifact = artifacts / "orphan.out"

    for path in (
        active_audit, old_rotated, recent_rotated, old_lock, recent_lock,
        old_tmp, recent_tmp, orphan_artifact,
    ):
        path.write_text("x", encoding="utf-8")

    _age(old_rotated, 7200)
    _age(old_lock, 7200)
    _age(old_tmp, 7200)
    _age(orphan_artifact, 7200)

    settings = replace(
        server.settings,
        command_jobs_dir=jobs,
        command_audit_log_path=audit,
        artifact_ttl_seconds=3600,
        audit_log_ttl_seconds=3600,
        maintenance_enabled=True,
        maintenance_interval_seconds=3600,
    )
    result = run_maintenance_once(settings)

    assert result.artifacts_checked is True
    assert result.audit_files_removed == 1
    assert result.stale_runtime_files_removed == 1
    assert active_audit.exists()
    assert not old_rotated.exists()
    assert recent_rotated.exists()
    assert old_lock.exists()
    assert recent_lock.exists()
    assert not old_tmp.exists()
    assert recent_tmp.exists()
    assert not orphan_artifact.exists()
