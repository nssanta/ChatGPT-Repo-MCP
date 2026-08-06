from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_command_tools import make_settings

from chatrepo_mcp import fs_tools


def test_search_text_streams_and_stops_ripgrep_after_global_limit(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")

    class _Stdout:
        def __init__(self) -> None:
            self._lines = iter(
                [
                    "a.txt:1:first\n",
                    "a.txt:2:second\n",
                    "a.txt:3:must-not-be-read\n",
                ]
            )

        def __iter__(self):
            return self

        def __next__(self) -> str:
            return next(self._lines)

        def close(self) -> None:
            return None

    class _Process:
        def __init__(self, cmd: list[str], **_: object) -> None:
            assert "--max-columns" in cmd
            assert cmd[cmd.index("--max-columns") + 1] == "4096"
            self.stdout = _Stdout()
            self.returncode: int | None = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    created: list[_Process] = []

    def fake_popen(cmd: list[str], **kwargs: object) -> _Process:
        process = _Process(cmd, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(fs_tools.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        fs_tools.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("search_text must not buffer ripgrep with subprocess.run")
        ),
    )

    result = fs_tools.search_text("needle", settings, path=".", limit=2)

    assert result["count"] == 2
    assert result["complete"] is False
    assert result["reason"] == "result_limit"
    assert created[0].terminated is True
    audit = [
        json.loads(line)
        for line in settings.command_audit_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event"] for record in audit] == ["heavy_started", "heavy_finished"]
    assert all(record["tool"] == "search_text" for record in audit)
    assert audit[1]["status"] == "completed"


def test_exhaustive_search_reuses_durable_background_job(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "source file.txt").write_text("needle\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_start(command: str, received_settings, **kwargs: object) -> dict[str, object]:
        captured.update(command=command, settings=received_settings, **kwargs)
        return {
            "ok": True,
            "job_id": "search-job",
            "status": "running",
            "artifact": {"artifact_id": "search-job"},
        }

    monkeypatch.setitem(
        __import__("sys").modules,
        "chatrepo_mcp.command_tools",
        SimpleNamespace(start_command_job=fake_start),
    )

    result = fs_tools.search_text(
        "needle with spaces", settings, paths=["source file.txt"], mode="exhaustive",
    )

    assert result["job_id"] == "search-job"
    assert result["mode"] == "exhaustive"
    assert captured["policy_exempt"] is True
    assert "'needle with spaces'" in str(captured["command"])
    assert "'source file.txt'" in str(captured["command"])


def test_quick_search_audits_pipe_start_failure(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(
        fs_tools.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("pipe")),
    )
    with pytest.raises(OSError, match="pipe"):
        fs_tools.search_text("needle", settings)
    audit = [json.loads(line) for line in settings.command_audit_log_path.read_text().splitlines()]
    assert [record["event"] for record in audit] == ["heavy_started", "heavy_finished"]
    assert audit[-1]["status"] == "failed"
    assert audit[-1]["tool"] == "search_text"
