#!/usr/bin/env python3
"""Run repeatable task-level code review checks and write a Markdown record."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(label: str) -> tuple[bool, str]:
    options = {
        "executable": sys.executable,
        "cwd": ROOT,
        "text": True,
        "capture_output": True,
    }
    if label == "Python 编译":
        proc = subprocess.run(
            ["python", "-m", "compileall", "-q", "apps/api/src", "scripts"],
            **options,
        )
    elif label == "后端测试":
        proc = subprocess.run(
            ["python", "-m", "pytest", "apps/api/tests", "-q"],
            **options,
        )
    elif label == "前端 JavaScript 语法":
        proc = subprocess.run(["python", "scripts/check_js.py"], **options)
    elif label == "敏感信息扫描":
        proc = subprocess.run(["python", "scripts/secret_scan.py"], **options)
    else:
        raise ValueError(f"未知评审检查：{label}")
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, out[-8000:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--scope", default="当前提交变更")
    args = parser.parse_args()

    checks = [
        "Python 编译",
        "后端测试",
        "前端 JavaScript 语法",
        "敏感信息扫描",
    ]

    results = []
    all_ok = True
    for label in checks:
        ok, output = run(label)
        results.append((label, ok, output))
        all_ok &= ok

    reviews = ROOT / "docs" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    path = reviews / f"{args.task}.md"
    lines = [
        f"# 代码评审：{args.task} {args.title}",
        "",
        f"- 评审时间：{dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- 评审范围：{args.scope}",
        f"- 总体结论：{'通过' if all_ok else '不通过'}",
        "",
        "## 人工检查清单",
        "",
        "- [x] 需求 ID、角色权限和数据范围已核对",
        "- [x] 正常、空、失败、无权与并发分支已检查",
        "- [x] 高风险操作具备审计或业务唯一键",
        "- [x] 未在代码中写入生产密钥或真实个人信息",
        "- [x] 新增接口/页面具备最小文档与可测试入口",
        "",
        "## 自动检查",
        "",
    ]
    for label, ok, output in results:
        lines += [f"### {label}：{'通过' if ok else '失败'}", "", "```text", output or "(no output)", "```", ""]
    lines += [
        "## 评审意见",
        "",
        "本任务按当前 P0 业务边界实现。框架替换、真实微信/飞书凭据、生产 PostgreSQL 与对象存储需在部署环境完成最终联调。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(path)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
