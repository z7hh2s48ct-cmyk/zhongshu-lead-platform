from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

from apps.api.src.routers.v12_returns import upload_return_evidence
from scripts.check_performance_gate import GateError, evaluate_report, render_markdown as render_gate_markdown
from scripts.finalize_performance_report import finalize_report
from scripts.performance_v12 import build_scenarios, percentile, safe_origin, validate_dataset


SCENARIOS = {
    "login": "login",
    "lead_list": "list",
    "lead_candidates": "list",
    "manual_dispatch": "write",
    "assignment_list": "list",
    "assignment_detail": "detail",
    "claim": "claim",
    "points_ledger": "list",
    "dashboard_report": "list",
    "evidence_upload": "write",
}
EXPORT_SHA256 = "a" * 64
PREVIEW_SHA256 = "b" * 64
SOURCE_COMMIT_SHA = "c" * 40


def test_evidence_upload_runs_blocking_work_off_event_loop() -> None:
    assert not inspect.iscoroutinefunction(upload_return_evidence)


def _preview_report(report: dict) -> dict:
    preview = copy.deepcopy(report)
    preview.pop("preview_evidence", None)
    preview["signoff"] = {
        "status": "PENDING",
        "approved_by": None,
        "approved_at": None,
        "approval_reference": None,
    }
    for profile in preview["profiles"].values():
        profile["target_infrastructure"] = None
    return preview


def _evaluate(report: dict, **kwargs) -> dict:
    return evaluate_report(
        report,
        metrics_export_sha256=EXPORT_SHA256,
        preview_report_sha256=PREVIEW_SHA256,
        preview_report=_preview_report(report),
        expected_source_commit=SOURCE_COMMIT_SHA,
        **kwargs,
    )


def _metrics(category: str, profile: int) -> dict:
    requests = 3 if category == "login" else profile
    return {
        "requests": requests,
        "successes": requests,
        "failures": 0,
        "error_rate": 0.0,
        "duration_seconds": 1.0,
        "throughput_rps": float(requests),
        "p50_ms": 100.0,
        "p95_ms": 200.0,
        "p99_ms": 300.0,
        "mean_ms": 150.0,
        "category": category,
        "concurrency": 1 if category == "login" else profile,
    }


def _report() -> dict:
    profiles = {}
    for profile in (100, 300, 500):
        profiles[str(profile)] = {
            "concurrency": profile,
            "started_at": "2026-08-11T04:00:00+00:00",
            "completed_at": "2026-08-11T04:10:00+00:00",
            "scenarios": {name: _metrics(category, profile) for name, category in SCENARIOS.items()},
            "database": {
                "samples": 2,
                "max_connections": 600,
                "peak_total_connections": profile + 10,
                "peak_active_connections": profile,
                "peak_idle_connections": 10,
                "peak_waiting_connections": 0,
                "peak_lock_waiting_connections": 0,
                "peak_blocked_queries": 0,
                "peak_slow_queries": 0,
                "longest_query_seconds": 0.25,
                "deadlocks_delta": 0,
                "temp_files_delta": 0,
                "temp_bytes_delta": 0,
                "block_read_ms_delta": 1.0,
                "block_write_ms_delta": 1.0,
            },
            "target_infrastructure": {
                "source": "staging-monitor-window-20260811",
                "environment": "staging",
                "base_url_origin": "https://staging.invalid",
                "window_started_at": "2026-08-11T03:59:00+00:00",
                "window_ended_at": "2026-08-11T04:11:00+00:00",
                "exported_at": "2026-08-11T04:12:00+00:00",
                "export_reference": "https://monitoring.example.com/exports/h04-20260811",
                "export_sha256": EXPORT_SHA256,
                "cpu_percent_max": 60.0,
                "memory_percent_max": 50.0,
                "io_read_bytes": 1024,
                "io_write_bytes": 2048,
            },
            "load_generator": {"platform": "Linux", "python": "3.12.0"},
            "consistency": {
                "duplicate_claim_ledgers": 0,
                "points_balance_mismatches": 0,
                "duplicate_active_assignments": 0,
            },
        }
    return {
        "schema_version": 1,
        "generated_at": "2026-08-11T03:58:00+00:00",
        "source_commit_sha": SOURCE_COMMIT_SHA,
        "mode": "staging",
        "environment": "staging",
        "dataset_id": "h04-synthetic-20260811",
        "synthetic_data": True,
        "base_url_origin": "https://staging.invalid",
        "claim_baseline": None,
        "profiles": profiles,
        "preview_evidence": {
            "github_run_id": "31456001934",
            "artifact_name": "staging-performance-31456001934",
            "report_sha256": PREVIEW_SHA256,
            "source_commit_sha": SOURCE_COMMIT_SHA,
        },
        "signoff": {
            "status": "APPROVED",
            "approved_by": "release-owner",
            "approved_at": "2026-08-11T04:13:00+00:00",
            "approval_reference": "https://github.com/peihr666-max/zhongshu-lead-platform/issues/53",
        },
    }


def _dataset() -> dict:
    return {
        "schema_version": 1,
        "dataset_id": "h04-synthetic",
        "environment": "staging",
        "base_url_origin": "https://staging.example.com",
        "synthetic_data": True,
        "assignment_detail_id": "assignment-1",
        "dispatch_company_id": "company-1",
        "dispatch_cases": {
            "100": {"lead_id": "lead-100", "idempotency_key": "h04-fixed-key-100"},
            "300": {"lead_id": "lead-300", "idempotency_key": "h04-fixed-key-300"},
            "500": {"lead_id": "lead-500", "idempotency_key": "h04-fixed-key-500"},
        },
        "claim_cases": {
            "100": "claim-assignment-100",
            "300": "claim-assignment-300",
            "500": "claim-assignment-500",
        },
        "evidence_cases": {
            "100": [f"return-100-{index}" for index in range(100)],
            "300": [f"return-300-{index}" for index in range(300)],
            "500": [f"return-500-{index}" for index in range(500)],
        },
    }


def test_percentile_interpolates_and_handles_empty_values():
    assert percentile([], 0.95) == 0.0
    assert percentile([10.0], 0.95) == 10.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0


def test_dataset_contract_requires_synthetic_runtime_resources():
    validate_dataset(_dataset(), runtime=True)
    document = _dataset()
    document["synthetic_data"] = False
    with pytest.raises(ValueError, match="synthetic_data"):
        validate_dataset(document, runtime=True)

    document = _dataset()
    document["evidence_cases"]["500"] = ["return-1"]
    with pytest.raises(ValueError, match="evidence_cases.500 needs at least 500"):
        validate_dataset(document, runtime=True)

    document = _dataset()
    document["evidence_cases"]["300"][0] = document["evidence_cases"]["100"][0]
    with pytest.raises(ValueError, match="disjoint"):
        validate_dataset(document, runtime=True)


def test_dataset_contract_requires_an_explicit_github_approved_claim_baseline():
    document = _dataset()
    document["claim_baseline"] = {
        "approved": True,
        "p95_limit_ms": 5000.0,
        "approval_reference": "https://github.com/peihr666-max/zhongshu-lead-platform/issues/71",
    }
    validate_dataset(document, runtime=True)

    for invalid in (
        {"approved": False, "p95_limit_ms": 5000.0, "approval_reference": "https://github.com/example/1"},
        {"approved": True, "p95_limit_ms": 499.0, "approval_reference": "https://github.com/example/1"},
        {"approved": True, "p95_limit_ms": float("nan"), "approval_reference": "https://github.com/example/1"},
        {"approved": True, "p95_limit_ms": 5000.0, "approval_reference": "https://example.com/issues/1"},
        {"approved": True, "p95_limit_ms": 5000.0, "approval_reference": "https://github.com/other/repo/issues/71"},
        {"approved": True, "p95_limit_ms": 5000.0, "approval_reference": "https://github.com/phlong026/zhongshu-lead-platform/issues/71"},
        {"approved": True, "p95_limit_ms": 5000.0, "approval_reference": "https://github.com/peihr666-max/zhongshu-lead-platform/issues/not-a-number"},
    ):
        document = _dataset()
        document["claim_baseline"] = invalid
        with pytest.raises(ValueError, match="claim_baseline"):
            validate_dataset(document, runtime=True)


def test_dataset_runtime_rejects_placeholders_but_contract_validation_allows_them():
    document = _dataset()
    document["dataset_id"] = "REPLACE_WITH_DATASET"
    validate_dataset(document, runtime=False)
    with pytest.raises(ValueError, match="placeholder"):
        validate_dataset(document, runtime=True)


def test_each_profile_uses_a_distinct_pending_claim_fixture():
    dataset = _dataset()
    claim_paths = {
        next(scenario.path for scenario in build_scenarios(dataset, profile) if scenario.name == "claim")
        for profile in (100, 300, 500)
    }
    assert len(claim_paths) == 3


def test_dataset_contract_rejects_reused_dispatch_or_claim_fixtures():
    document = _dataset()
    document["dispatch_cases"]["300"] = dict(document["dispatch_cases"]["100"])
    with pytest.raises(ValueError, match="distinct leads"):
        validate_dataset(document, runtime=True)

    document = _dataset()
    document["claim_cases"]["300"] = document["claim_cases"]["100"]
    with pytest.raises(ValueError, match="distinct assignments"):
        validate_dataset(document, runtime=True)


def test_each_profile_uses_disjoint_evidence_upload_fixtures():
    dataset = _dataset()
    evidence_sets = [
        set(next(scenario.evidence_return_ids for scenario in build_scenarios(dataset, profile)
                 if scenario.name == "evidence_upload"))
        for profile in (100, 300, 500)
    ]
    assert [len(values) for values in evidence_sets] == [100, 300, 500]
    assert not evidence_sets[0] & evidence_sets[1]
    assert not evidence_sets[0] & evidence_sets[2]
    assert not evidence_sets[1] & evidence_sets[2]


def test_safe_origin_removes_paths_and_rejects_embedded_credentials():
    assert safe_origin("https://staging.example.com:8443/a/path") == "https://staging.example.com:8443"
    assert safe_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="without embedded credentials"):
        safe_origin("https://user:secret@staging.example.com")
    with pytest.raises(ValueError, match="must use HTTPS"):
        safe_origin("http://staging.example.com")


def test_complete_signed_report_passes_all_profiles():
    result = _evaluate(_report())
    assert result["status"] == "PASS"
    assert result["profiles"]["300"]["conclusion"] == "PASS"
    assert result["profiles"]["500"]["conclusion"] == "PASS"
    assert "release-owner" not in render_gate_markdown(result)


def test_500_latency_limit_has_explicit_capacity_conclusion():
    report = _report()
    report["profiles"]["500"]["scenarios"]["lead_list"]["p95_ms"] = 1600.0
    report["profiles"]["500"]["scenarios"]["lead_list"]["p99_ms"] = 1700.0
    result = _evaluate(report)
    assert result["status"] == "CAPACITY_LIMIT_REACHED"
    assert result["profiles"]["500"]["conclusion"] == "CAPACITY_LIMIT_REACHED"


def test_300_latency_failure_blocks_gate():
    report = _report()
    report["profiles"]["300"]["scenarios"]["assignment_detail"]["p95_ms"] = 1100.0
    report["profiles"]["300"]["scenarios"]["assignment_detail"]["p99_ms"] = 1200.0
    with pytest.raises(GateError, match="profile 300 failed latency gates"):
        _evaluate(report)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["profiles"].pop("300"), "exactly the 100, 300 and 500"),
        (lambda report: report.update(environment="production"), "staging or staging-equivalent"),
        (lambda report: report["profiles"]["100"]["scenarios"].pop("claim"), "exact required scenario"),
        (lambda report: report["profiles"]["100"]["consistency"].update(points_balance_mismatches=1), "consistency check"),
        (lambda report: report["profiles"]["100"]["database"].update(deadlocks_delta=1), "deadlocks"),
        (lambda report: report["profiles"]["100"].update(target_infrastructure=None), "CPU, memory and I/O"),
        (
            lambda report: report["profiles"]["100"]["scenarios"]["claim"].update(
                successes=99, failures=1, error_rate=0.01
            ),
            "request errors",
        ),
    ],
)
def test_gate_fails_closed_on_incomplete_or_unsafe_evidence(mutation, message):
    report = _report()
    mutation(report)
    with pytest.raises(GateError, match=message):
        _evaluate(report)


def test_final_gate_requires_signoff_but_engineering_preview_can_be_pending():
    report = _report()
    report["signoff"] = {"status": "PENDING", "approved_by": None, "approved_at": None, "approval_reference": None}
    with pytest.raises(GateError, match="requires signoff"):
        _evaluate(report)
    assert _evaluate(report, require_signoff=False)["status"] == "EVIDENCE_PENDING"


def test_final_signoff_rejects_another_repository_reference():
    report = _report()
    report["signoff"]["approval_reference"] = "https://github.com/other/repository/issues/53"
    with pytest.raises(GateError, match="this repository"):
        _evaluate(report)


def test_engineering_preview_labels_missing_monitoring_evidence_as_pending():
    report = _report()
    report["signoff"] = {"status": "PENDING", "approved_by": None, "approved_at": None, "approval_reference": None}
    for profile in report["profiles"].values():
        profile["target_infrastructure"] = None
    result = evaluate_report(
        report,
        require_signoff=False,
        allow_pending_infrastructure=True,
    )
    assert result["status"] == "EVIDENCE_PENDING"


def test_final_gate_binds_monitoring_window_and_archived_export_hash():
    report = _report()
    with pytest.raises(GateError, match="archived raw export"):
        evaluate_report(
            report,
            metrics_export_sha256="b" * 64,
            preview_report_sha256=PREVIEW_SHA256,
            preview_report=_preview_report(report),
            expected_source_commit=SOURCE_COMMIT_SHA,
        )
    report["profiles"]["300"]["target_infrastructure"]["window_started_at"] = "2026-08-11T04:01:00+00:00"
    with pytest.raises(GateError, match="does not cover"):
        _evaluate(report)


def test_final_gate_binds_signed_metrics_to_preview_artifact():
    report = _report()
    with pytest.raises(GateError, match="archived preview report"):
        evaluate_report(
            report,
            metrics_export_sha256=EXPORT_SHA256,
            preview_report_sha256="c" * 64,
            preview_report=_preview_report(report),
            expected_source_commit=SOURCE_COMMIT_SHA,
        )


def test_final_gate_rejects_signed_report_that_rewrites_preview_measurements():
    report = _report()
    preview = _preview_report(report)
    report["profiles"]["300"]["scenarios"]["lead_list"]["p50_ms"] = 90.0
    with pytest.raises(GateError, match="rewrites immutable"):
        evaluate_report(
            report,
            metrics_export_sha256=EXPORT_SHA256,
            preview_report_sha256=PREVIEW_SHA256,
            preview_report=preview,
            expected_source_commit=SOURCE_COMMIT_SHA,
        )


def test_final_gate_rejects_preview_from_a_different_main_commit():
    report = _report()
    with pytest.raises(GateError, match="source commit"):
        evaluate_report(
            report,
            metrics_export_sha256=EXPORT_SHA256,
            preview_report_sha256=PREVIEW_SHA256,
            preview_report=_preview_report(report),
            expected_source_commit="d" * 40,
        )


def test_finalizer_only_attaches_signed_evidence_to_preview_metrics(tmp_path):
    preview = _preview_report(_report())
    target_document = {
        "schema_version": 1,
        "source": "staging-monitor-window-20260811",
        "environment": "staging",
        "base_url_origin": "https://staging.invalid",
        "exported_at": "2026-08-11T04:12:00+00:00",
        "export_reference": "https://monitoring.example.com/exports/h04-20260811",
        "export_sha256": EXPORT_SHA256,
        "profiles": {
            str(profile): {
                "window_started_at": "2026-08-11T03:59:00+00:00",
                "window_ended_at": "2026-08-11T04:11:00+00:00",
                "cpu_percent_max": 60.0,
                "memory_percent_max": 50.0,
                "io_read_bytes": 1024,
                "io_write_bytes": 2048,
            }
            for profile in (100, 300, 500)
        },
    }
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target_document), encoding="utf-8")
    signoff = _report()["signoff"]
    finalized = finalize_report(
        preview,
        preview_report_sha256=PREVIEW_SHA256,
        target_metrics_path=target_path,
        signoff=signoff,
        github_run_id="31456001934",
        expected_source_commit=SOURCE_COMMIT_SHA,
    )
    assert finalized["profiles"]["300"]["scenarios"] == preview["profiles"]["300"]["scenarios"]
    assert finalized["preview_evidence"]["report_sha256"] == PREVIEW_SHA256
    assert finalized["preview_evidence"]["source_commit_sha"] == SOURCE_COMMIT_SHA
    assert finalized["profiles"]["300"]["target_infrastructure"]["export_sha256"] == EXPORT_SHA256


def test_approved_claim_baseline_requires_reference_and_changes_only_claim_limit():
    report = _report()
    report["claim_baseline"] = {
        "approved": True,
        "p95_limit_ms": 750.0,
        "approval_reference": "https://github.com/peihr666-max/zhongshu-lead-platform/issues/99",
    }
    report["profiles"]["300"]["scenarios"]["claim"]["p95_ms"] = 700.0
    report["profiles"]["300"]["scenarios"]["claim"]["p99_ms"] = 800.0
    result = _evaluate(report)
    assert result["claim_p95_limit_ms"] == 750.0


def test_claim_baseline_gate_rejects_another_repository_reference():
    report = _report()
    report["claim_baseline"] = {
        "approved": True,
        "p95_limit_ms": 5000.0,
        "approval_reference": "https://github.com/other/repository/issues/71",
    }
    with pytest.raises(GateError, match="this repository"):
        _evaluate(report)


def test_sensitive_fields_are_rejected_from_evidence():
    report = copy.deepcopy(_report())
    report["profiles"]["100"]["access_token"] = "secret"
    with pytest.raises(GateError, match="sensitive field"):
        _evaluate(report)


def test_non_finite_performance_numbers_are_rejected():
    report = _report()
    report["profiles"]["100"]["scenarios"]["lead_list"]["p95_ms"] = float("nan")
    with pytest.raises(GateError, match="must be a number"):
        _evaluate(report)


def test_main_workflow_keeps_real_staging_load_on_protected_manual_surface():
    workflow = Path(".github/workflows/main-release.yml").read_text(encoding="utf-8")
    assert "Validate performance and capacity contract" in workflow
    assert "performance/v12-staging-dataset.example.json --validate-config" in workflow
    assert (
        "if: github.event_name == 'workflow_dispatch' && inputs.performance_action == 'run' "
        "&& github.ref == 'refs/heads/main'"
    ) in workflow
    assert "environment: staging-performance" in workflow
    assert "V12_PERF_STAGING_BASE_URL: ${{ secrets.V12_PERF_BASE_URL }}" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "--profiles 100,300,500" in workflow
    assert '--source-commit "$GITHUB_SHA"' in workflow
    assert "--allow-pending-signoff" in workflow
    assert "--allow-pending-infrastructure" in workflow
    assert "inputs.performance_action == 'finalize'" in workflow
    assert 'gh run download "$V12_PERF_PREVIEW_RUN_ID"' in workflow
    assert 'gh run view "$V12_PERF_PREVIEW_RUN_ID"' in workflow
    assert "--json conclusion,event,headBranch,headSha" in workflow
    assert "staging-performance-$V12_PERF_PREVIEW_RUN_ID" in workflow
    assert "scripts/finalize_performance_report.py" in workflow
    assert "scripts/finalize_performance_report.py --help" in workflow
    assert "--metrics-export dist/performance/v12-target-monitoring-export.bin" in workflow
    assert "--preview-report dist/performance/preview/v12-performance-report.json" in workflow
    assert '--expected-source-commit "$GITHUB_SHA"' in workflow
    assert "V12_PERF_SIGNOFF_JSON" in workflow
    assert "V12_PERF_TARGET_METRICS_JSON" in workflow
    assert "V12_PERF_METRICS_EXPORT_BASE64" in workflow
    staging_job = workflow.split("  staging-performance:", maxsplit=1)[1].split(
        "  staging-performance-finalize:", maxsplit=1
    )[0]
    protected_jobs = workflow.split("  staging-performance:", maxsplit=1)[1]
    assert "continue-on-error" not in staging_job
    assert "pull_request" not in staging_job
    assert "inputs.staging_base_url" not in staging_job
    assert "V12_PERF_TARGET_METRICS_JSON" not in staging_job
    assert "cancel-in-progress: true" not in workflow
    assert "actions: read" in workflow
    assert "actions/checkout@v4" not in protected_jobs
    assert "actions/setup-python@v5" not in protected_jobs
    assert "actions/upload-artifact@v4" not in protected_jobs
