#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


EXPECTED_WORKFLOW_PATH = ".github/workflows/security-analysis.yml"
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"required GitHub API evidence missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid GitHub API evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"GitHub API evidence root must be an object: {path}")
    return payload


def _positive_integer(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"{field} must be a positive integer")
    return value


def validate_promotion_source(
    *,
    run: dict[str, Any],
    workflow: dict[str, Any],
    artifacts: dict[str, Any],
    expected_repository: str,
    expected_sha: str,
    allow_owner_dispatch: bool = False,
) -> dict[str, Any]:
    expected_repository = expected_repository.strip()
    expected_sha = expected_sha.strip()
    if not expected_repository or expected_repository.count("/") != 1:
        raise RuntimeError("expected repository must use owner/name format")
    if not _GIT_SHA_RE.fullmatch(expected_sha):
        raise RuntimeError("expected main commit must be a lowercase 40-character Git SHA")

    run_id = _positive_integer(run.get("id"), field="run id")
    run_attempt = _positive_integer(run.get("run_attempt"), field="run attempt")
    owner_dispatch = allow_owner_dispatch is True and run.get("event") == "workflow_dispatch"
    if (run.get("event") != "push" and not owner_dispatch) or run.get("head_branch") != "main":
        raise RuntimeError("security candidate source must be a main push")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise RuntimeError("security candidate source must have completed successfully")
    if run.get("head_sha") != expected_sha:
        raise RuntimeError("security candidate source does not match expected main commit")

    repository = run.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != expected_repository:
        raise RuntimeError(
            "security candidate source repository does not match expected repository"
        )

    if owner_dispatch:
        # A first owner dispatch has no artifacts left over from a previous attempt.
        if run_attempt != 1:
            raise RuntimeError("owner dispatch must be a first run attempt")
        owner = repository.get("owner")
        owner_login = expected_repository.split("/", 1)[0]
        if (
            not isinstance(owner, dict)
            or owner.get("type") != "User"
            or owner.get("login") != owner_login
        ):
            raise RuntimeError("owner dispatch requires an identified personal repository owner")
        owner_id = _positive_integer(owner.get("id"), field="repository owner id")
        for actor_field in ("actor", "triggering_actor"):
            actor = run.get(actor_field)
            if (
                not isinstance(actor, dict)
                or actor.get("login") != owner_login
                or actor.get("type") != "User"
                or _positive_integer(actor.get("id"), field=actor_field) != owner_id
            ):
                raise RuntimeError(f"owner dispatch {actor_field} must be the repository owner")
        repository_id = _positive_integer(repository.get("id"), field="repository id")
        head_repository = run.get("head_repository")
        if (
            not isinstance(head_repository, dict)
            or head_repository.get("full_name") != expected_repository
            or _positive_integer(head_repository.get("id"), field="head repository id")
            != repository_id
        ):
            raise RuntimeError("owner dispatch head repository must match the trusted repository")

    workflow_id = _positive_integer(workflow.get("id"), field="workflow id")
    if (
        workflow.get("path") != EXPECTED_WORKFLOW_PATH
        or run.get("path") != EXPECTED_WORKFLOW_PATH
    ):
        raise RuntimeError("security candidate source workflow path is not trusted")
    if workflow.get("state") != "active":
        raise RuntimeError("trusted Security Analysis workflow must be active")
    if run.get("workflow_id") != workflow_id:
        raise RuntimeError("security candidate source workflow id is not trusted")

    raw_artifacts = artifacts.get("artifacts")
    total_count = artifacts.get("total_count")
    if not isinstance(raw_artifacts, list) or not isinstance(total_count, int):
        raise RuntimeError("GitHub artifact listing is malformed")
    if total_count != len(raw_artifacts):
        raise RuntimeError(
            "GitHub artifact listing is incomplete; fetch all pages before validation"
        )

    expected_names = {
        "candidate": f"security-candidate-image-{run_id}",
        "evidence": f"security-analysis-{run_id}",
    }
    verified: dict[str, int] = {}
    for kind, expected_name in expected_names.items():
        matches = [
            item
            for item in raw_artifacts
            if isinstance(item, dict) and item.get("name") == expected_name
        ]
        if len(matches) != 1:
            raise RuntimeError(f"expected exactly one {expected_name} artifact")
        artifact = matches[0]
        artifact_id = _positive_integer(artifact.get("id"), field=f"{kind} artifact id")
        if artifact.get("expired") is not False:
            raise RuntimeError(f"{expected_name} artifact is expired")
        artifact_run = artifact.get("workflow_run")
        if not isinstance(artifact_run, dict) or artifact_run.get("id") != run_id:
            raise RuntimeError(f"{expected_name} artifact does not belong to run {run_id}")
        if (
            artifact_run.get("head_branch") != "main"
            or artifact_run.get("head_sha") != expected_sha
        ):
            raise RuntimeError(
                f"{expected_name} artifact source does not match trusted main commit"
            )
        verified[kind] = artifact_id

    report = {
        "valid": True,
        "repository": expected_repository,
        "main_commit_sha": expected_sha,
        "github_run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_id": workflow_id,
        "workflow_path": EXPECTED_WORKFLOW_PATH,
        "candidate_artifact_id": verified["candidate"],
        "evidence_artifact_id": verified["evidence"],
        "source_event": run["event"],
        "run_url": f"https://github.com/{expected_repository}/actions/runs/{run_id}",
    }
    if owner_dispatch:
        report.update(owner_actor_id=owner_id, owner_actor_login=owner_login)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind a production image promotion to trusted GitHub Actions run metadata"
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--allow-owner-dispatch",
        action="store_true",
        help="Also accept a first main workflow_dispatch by the personal repository owner",
    )
    parser.add_argument("--output", default="security-promotion-source.json")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = validate_promotion_source(
            run=_read_json_object(Path(args.run)),
            workflow=_read_json_object(Path(args.workflow)),
            artifacts=_read_json_object(Path(args.artifacts)),
            expected_repository=args.expected_repository,
            expected_sha=args.expected_sha,
            allow_owner_dispatch=args.allow_owner_dispatch,
        )
    except RuntimeError as exc:
        report = {"valid": False, "error": str(exc)}

    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("valid") else 2


if __name__ == "__main__":
    raise SystemExit(main())
