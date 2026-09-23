from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_v12_e2e


def _completed_process(command, returncode=0, stdout=None, stderr=None):
    """等价 CompletedProcess 的假返回值。

    不经 subprocess 命名空间构造：dangerous-subprocess-use-audit 豁免已于
    2026-09-22 到期且 30 天总寿命耗尽，按门禁要求改为消除告警本身。
    """
    return SimpleNamespace(args=command, returncode=returncode, stdout=stdout, stderr=stderr)


def test_rejects_database_url_without_isolated_name() -> None:
    with pytest.raises(ValueError, match="e2e/test/ci"):
        run_v12_e2e._validate_database_url(
            "postgresql+psycopg://zhongshu:secret@127.0.0.1:5432/zhongshu_prod"
        )


def test_rejects_non_postgresql_database_url() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        run_v12_e2e._validate_database_url("sqlite:////tmp/zhongshu_e2e.db")


def test_explicit_database_url_runs_lifecycle_pytest_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setenv("WECHAT_SECRET", "must-not-reach-e2e-child")

    def fake_run(command, *, env=None, **kwargs):
        calls.append((list(command), dict(env or {})))
        return _completed_process(command, 0)

    monkeypatch.setattr(run_v12_e2e.subprocess, "run", fake_run)

    exit_code = run_v12_e2e.main(
        [
            "--database-url",
            "postgresql+psycopg://zhongshu:secret@127.0.0.1:5432/zhongshu_e2e",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    command, env = calls[0]
    assert "apps/api/tests/test_v12_production_lifecycle_e2e.py" in command
    assert "apps/api/tests/test_quick_dispatch_postgres_concurrency_e2e.py" in command
    assert "apps/api/tests/test_internal_user_postgres_concurrency_e2e.py" in command
    assert env["V12_E2E_DATABASE_URL"].endswith("/zhongshu_e2e")
    assert env["APP_ENV"] == "test"
    assert "WECHAT_SECRET" not in env
    assert "--junitxml" in command


def test_runner_removes_stale_evidence_before_pytest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.json"
    junit = tmp_path / "junit.xml"
    evidence.write_text("stale", encoding="utf-8")
    junit.write_text("stale", encoding="utf-8")

    def fake_run(command, *, env=None, **kwargs):
        return _completed_process(command, 1)

    monkeypatch.setattr(run_v12_e2e.subprocess, "run", fake_run)

    exit_code = run_v12_e2e.main(
        [
            "--database-url",
            "postgresql+psycopg://zhongshu:secret@127.0.0.1:5432/zhongshu_e2e",
            "--evidence-path",
            str(evidence),
            "--junit-xml",
            str(junit),
        ]
    )

    assert exit_code == 1
    assert not evidence.exists()
    assert not junit.exists()


def test_missing_database_url_uses_disposable_docker_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command, *, capture_output=False, text=False, check=False, env=None, **kwargs
    ):
        commands.append(list(command))
        if command[:2] == ["docker", "port"]:
            return _completed_process(command, 0, stdout="127.0.0.1:45432\n")
        return _completed_process(command, 0, stdout="container-id\n")

    monkeypatch.delenv("V12_E2E_DATABASE_URL", raising=False)
    monkeypatch.setattr(run_v12_e2e.subprocess, "run", fake_run)
    monkeypatch.setattr(run_v12_e2e.time, "sleep", lambda _seconds: None)

    exit_code = run_v12_e2e.main([])

    assert exit_code == 0
    assert ["docker", "--version"] in commands
    assert any(command[:3] == ["docker", "run", "-d"] for command in commands)
    assert any(command[:2] == ["docker", "stop"] for command in commands)
    pytest_command = commands[-2]
    assert "apps/api/tests/test_v12_production_lifecycle_e2e.py" in pytest_command
    assert "apps/api/tests/test_quick_dispatch_postgres_concurrency_e2e.py" in pytest_command
    assert "apps/api/tests/test_internal_user_postgres_concurrency_e2e.py" in pytest_command


def test_docker_start_failure_still_stops_container(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command, *, capture_output=False, text=False, check=False, env=None, **kwargs
    ):
        commands.append(list(command))
        if command[:2] == ["docker", "port"]:
            raise subprocess.CalledProcessError(1, command)
        return _completed_process(command, 0, stdout="container-id\n")

    monkeypatch.delenv("V12_E2E_DATABASE_URL", raising=False)
    monkeypatch.setattr(run_v12_e2e.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="docker port"):
        run_v12_e2e.main([])

    assert any(command[:2] == ["docker", "stop"] for command in commands)


def test_runner_uses_fixed_subprocess_executables() -> None:
    source = Path(run_v12_e2e.__file__).read_text(encoding="utf-8")

    assert "subprocess.run(command" not in source
    assert 'subprocess.run(["docker", *args]' in source
    assert '["python", "-m", "pytest", "-q", *TARGET_TESTS' in source
    assert "executable=sys.executable" in source
