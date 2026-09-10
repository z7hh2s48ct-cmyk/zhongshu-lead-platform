from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_security_promotion_source import validate_promotion_source


RUN_ID = 33875211984
MAIN_SHA = "a" * 40
REPOSITORY = "phlong026/zhongshu-lead-platform"
WORKFLOW_ID = 330043382


def _run(**overrides) -> dict:
    value = {
        "id": RUN_ID,
        "event": "push",
        "head_branch": "main",
        "head_sha": MAIN_SHA,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/security-analysis.yml",
        "workflow_id": WORKFLOW_ID,
        "run_attempt": 1,
        "repository": {"full_name": REPOSITORY},
    }
    value.update(overrides)
    return value


def _workflow(**overrides) -> dict:
    value = {
        "id": WORKFLOW_ID,
        "name": "Security Analysis",
        "path": ".github/workflows/security-analysis.yml",
        "state": "active",
    }
    value.update(overrides)
    return value


def _artifact(name: str, artifact_id: int, **overrides) -> dict:
    value = {
        "id": artifact_id,
        "name": name,
        "expired": False,
        "workflow_run": {
            "id": RUN_ID,
            "head_branch": "main",
            "head_sha": MAIN_SHA,
        },
    }
    value.update(overrides)
    return value


def _artifacts(*items: dict) -> dict:
    return {"total_count": len(items), "artifacts": list(items)}


def _owner_dispatch() -> dict:
    owner = {"id": 123, "login": "phlong026", "type": "User"}
    return _run(
        event="workflow_dispatch",
        repository={"id": 456, "full_name": REPOSITORY, "owner": owner},
        head_repository={"id": 456, "full_name": REPOSITORY},
        actor=dict(owner),
        triggering_actor=dict(owner),
    )


def _validate_owner_dispatch(run: dict, *, enabled: bool = True) -> dict:
    return validate_promotion_source(
        run=run,
        workflow=_workflow(),
        artifacts=_artifacts(
            _artifact(f"security-candidate-image-{RUN_ID}", 101),
            _artifact(f"security-analysis-{RUN_ID}", 102),
        ),
        expected_repository=REPOSITORY,
        expected_sha=MAIN_SHA,
        allow_owner_dispatch=enabled,
    )


def test_owner_dispatch_requires_explicit_opt_in_and_records_real_event() -> None:
    with pytest.raises(RuntimeError, match="main push"):
        _validate_owner_dispatch(_owner_dispatch(), enabled=False)
    report = _validate_owner_dispatch(_owner_dispatch())
    assert report["valid"] is True
    assert report["source_event"] == "workflow_dispatch"
    assert report["owner_actor_id"] == 123
    assert report["main_commit_sha"] == MAIN_SHA


@pytest.mark.parametrize(
    "change",
    [
        {"actor": {"id": 999, "login": "phlong026", "type": "User"}},
        {"triggering_actor": {"id": 999, "login": "other", "type": "User"}},
        {"triggering_actor": None},
        {"actor": {"id": 123, "login": "other", "type": "User"}},
        {"head_repository": {"id": 999, "full_name": REPOSITORY}},
        {"head_repository": {"id": 456, "full_name": "other/fork"}},
        {"repository": {"id": 456, "full_name": REPOSITORY,
                        "owner": {"id": 123, "login": "phlong026", "type": "Organization"}}},
        {"run_attempt": 2},
        {"head_branch": "feature"},
        {"head_sha": "b" * 40},
        {"conclusion": "failure"},
        {"workflow_id": 999},
        {"event": "pull_request_target"},
    ],
)
def test_owner_dispatch_rejects_untrusted_identity_or_build(change: dict) -> None:
    run = _owner_dispatch()
    run.update(change)
    with pytest.raises(RuntimeError):
        _validate_owner_dispatch(run)


def test_owner_dispatch_does_not_relax_artifact_checks() -> None:
    with pytest.raises(RuntimeError, match="expired"):
        validate_promotion_source(
            run=_owner_dispatch(),
            workflow=_workflow(),
            artifacts=_artifacts(
                _artifact(f"security-candidate-image-{RUN_ID}", 101, expired=True),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            ),
            expected_repository=REPOSITORY,
            expected_sha=MAIN_SHA,
            allow_owner_dispatch=True,
        )


def test_promotion_source_binds_trusted_run_workflow_and_artifacts() -> None:
    report = validate_promotion_source(
        run=_run(),
        workflow=_workflow(),
        artifacts=_artifacts(
            _artifact(f"security-candidate-image-{RUN_ID}", 101),
            _artifact(f"security-analysis-{RUN_ID}", 102),
        ),
        expected_repository=REPOSITORY,
        expected_sha=MAIN_SHA,
    )

    assert report == {
        "valid": True,
        "repository": REPOSITORY,
        "main_commit_sha": MAIN_SHA,
        "github_run_id": RUN_ID,
        "run_attempt": 1,
        "workflow_id": WORKFLOW_ID,
        "workflow_path": ".github/workflows/security-analysis.yml",
        "candidate_artifact_id": 101,
        "evidence_artifact_id": 102,
        "source_event": "push",
        "run_url": f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}",
    }


@pytest.mark.parametrize(
    ("run", "workflow", "artifacts", "message"),
    [
        (_run(event="workflow_dispatch"), _workflow(), _artifacts(), "main push"),
        (_run(head_branch="candidate"), _workflow(), _artifacts(), "main push"),
        (_run(head_sha="b" * 40), _workflow(), _artifacts(), "expected main commit"),
        (_run(conclusion="failure"), _workflow(), _artifacts(), "completed successfully"),
        (_run(path=".github/workflows/fake.yml"), _workflow(), _artifacts(), "workflow path"),
        (_run(workflow_id=999), _workflow(), _artifacts(), "workflow id"),
    ],
)
def test_promotion_source_rejects_untrusted_run_metadata(
    run: dict,
    workflow: dict,
    artifacts: dict,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_promotion_source(
            run=run,
            workflow=workflow,
            artifacts=artifacts,
            expected_repository=REPOSITORY,
            expected_sha=MAIN_SHA,
        )


def test_promotion_source_rejects_missing_expired_or_cross_run_artifacts() -> None:
    candidate = _artifact(f"security-candidate-image-{RUN_ID}", 101)
    evidence = _artifact(f"security-analysis-{RUN_ID}", 102)

    with pytest.raises(RuntimeError, match="exactly one security-analysis"):
        validate_promotion_source(
            run=_run(),
            workflow=_workflow(),
            artifacts=_artifacts(candidate),
            expected_repository=REPOSITORY,
            expected_sha=MAIN_SHA,
        )

    expired = _artifact(f"security-candidate-image-{RUN_ID}", 101, expired=True)
    with pytest.raises(RuntimeError, match="expired"):
        validate_promotion_source(
            run=_run(),
            workflow=_workflow(),
            artifacts=_artifacts(expired, evidence),
            expected_repository=REPOSITORY,
            expected_sha=MAIN_SHA,
        )

    cross_run = _artifact(
        f"security-analysis-{RUN_ID}",
        102,
        workflow_run={"id": RUN_ID + 1, "head_branch": "main", "head_sha": MAIN_SHA},
    )
    with pytest.raises(RuntimeError, match="does not belong to run"):
        validate_promotion_source(
            run=_run(),
            workflow=_workflow(),
            artifacts=_artifacts(candidate, cross_run),
            expected_repository=REPOSITORY,
            expected_sha=MAIN_SHA,
        )


@pytest.mark.parametrize(
    ("run", "workflow", "artifacts", "message"),
    [
        (
            _run(repository={"full_name": "attacker/fork"}),
            _workflow(),
            _artifacts(
                _artifact(f"security-candidate-image-{RUN_ID}", 101),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            ),
            "repository",
        ),
        (
            _run(),
            _workflow(state="disabled_manually"),
            _artifacts(
                _artifact(f"security-candidate-image-{RUN_ID}", 101),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            ),
            "must be active",
        ),
        (
            _run(),
            _workflow(path=".github/workflows/fake.yml"),
            _artifacts(
                _artifact(f"security-candidate-image-{RUN_ID}", 101),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            ),
            "workflow path",
        ),
        (
            _run(),
            _workflow(),
            {
                "total_count": 3,
                "artifacts": [
                    _artifact(f"security-candidate-image-{RUN_ID}", 101),
                    _artifact(f"security-analysis-{RUN_ID}", 102),
                ],
            },
            "listing is incomplete",
        ),
        (
            _run(),
            _workflow(),
            _artifacts(
                _artifact(f"security-candidate-image-{RUN_ID}", 101),
                _artifact(f"security-candidate-image-{RUN_ID}", 103),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            ),
            "exactly one security-candidate-image",
        ),
        (
            _run(),
            _workflow(),
            _artifacts(
                _artifact(
                    f"security-candidate-image-{RUN_ID}",
                    101,
                    workflow_run={
                        "id": RUN_ID,
                        "head_branch": "candidate",
                        "head_sha": MAIN_SHA,
                    },
                ),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            ),
            "source does not match trusted main commit",
        ),
    ],
)
def test_promotion_source_rejects_repository_workflow_and_listing_drift(
    run: dict,
    workflow: dict,
    artifacts: dict,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_promotion_source(
            run=run,
            workflow=workflow,
            artifacts=artifacts,
            expected_repository=REPOSITORY,
            expected_sha=MAIN_SHA,
        )


def test_promotion_source_cli_writes_auditable_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path = tmp_path / "run.json"
    workflow_path = tmp_path / "workflow.json"
    artifacts_path = tmp_path / "artifacts.json"
    output_path = tmp_path / "source.json"
    run_path.write_text(json.dumps(_run()), encoding="utf-8")
    workflow_path.write_text(json.dumps(_workflow()), encoding="utf-8")
    artifacts_path.write_text(
        json.dumps(
            _artifacts(
                _artifact(f"security-candidate-image-{RUN_ID}", 101),
                _artifact(f"security-analysis-{RUN_ID}", 102),
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_security_promotion_source.py",
            "--run",
            str(run_path),
            "--workflow",
            str(workflow_path),
            "--artifacts",
            str(artifacts_path),
            "--expected-repository",
            REPOSITORY,
            "--expected-sha",
            MAIN_SHA,
            "--output",
            str(output_path),
        ],
    )

    from scripts.check_security_promotion_source import main

    assert main() == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["github_run_id"] == RUN_ID


def test_promotion_runbook_uses_github_api_metadata_before_download() -> None:
    source = Path("docs/runbooks/IMAGE_PROMOTION_V1.2.md").read_text(encoding="utf-8")
    fail_closed = source.index("set -euo pipefail")
    main_checkout = source.index("git fetch origin main")
    api_check = source.index('gh api "repos/$GITHUB_REPOSITORY/actions/runs/$SECURITY_RUN_ID"')
    validator = source.index(
        'python3 -I "$REPO_ROOT/scripts/check_security_promotion_source.py"'
    )
    download = source.index(
        'gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$CANDIDATE_ARTIFACT_ID/zip"'
    )

    assert fail_closed < main_checkout < api_check < validator < download
    assert 'actions/workflows/security-analysis.yml' in source
    assert 'actions/runs/$SECURITY_RUN_ID/artifacts?per_page=100' in source
    assert 'actions/artifacts/$EVIDENCE_ARTIFACT_ID/zip' in source
    assert "gh run download" not in source
    assert "security-promotion-source.json" in source
    assert 'REPO_ROOT="$(git rev-parse --show-toplevel)"' in source
    assert "git status --porcelain --untracked-files=all -- scripts/check_security_promotion_source.py" in source
    assert 'git hash-object "$REPO_ROOT/scripts/check_security_promotion_source.py"' in source
    assert 'git rev-parse HEAD:scripts/check_security_promotion_source.py' in source
    assert 'python3 -I "$REPO_ROOT/scripts/check_security_promotion_source.py"' in source
    assert source.count("python3 -I -c") == 2
    assert "python -c" not in source
    assert 'mktemp -d "$REPO_ROOT/dist/security-promotion/${SECURITY_RUN_ID}.XXXXXX"' in source
    assert '--env-file "$REPO_ROOT/.env"' in source
    assert '--scan-subject "$PROMOTION_DIR/candidate/scan-subject.json"' in source
