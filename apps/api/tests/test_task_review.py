from __future__ import annotations

import sys

import pytest

from scripts.task_review import run


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Python 编译", ["python", "-m", "compileall", "-q", "apps/api/src", "scripts"]),
        ("后端测试", ["python", "-m", "pytest", "apps/api/tests", "-q"]),
        ("前端 JavaScript 语法", ["python", "scripts/check_js.py"]),
        ("敏感信息扫描", ["python", "scripts/secret_scan.py"]),
    ],
)
def test_review_checks_use_only_fixed_argv(
    label: str,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command: list[str], **options: object) -> Result:
        seen["command"] = command
        seen["options"] = options
        return Result()

    monkeypatch.setattr("scripts.task_review.subprocess.run", fake_run)

    ok, output = run(label)

    assert ok is True
    assert output == "ok"
    assert seen["command"] == expected
    assert seen["options"] == {
        "executable": sys.executable,
        "cwd": run.__globals__["ROOT"],
        "text": True,
        "capture_output": True,
    }


def test_review_rejects_unknown_check() -> None:
    with pytest.raises(ValueError, match="未知评审检查"):
        run("任意命令")
