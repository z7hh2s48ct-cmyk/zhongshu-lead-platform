from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REQUIRED_PROFILES = (100, 300, 500)
REQUIRED_SCENARIOS = {
    "login",
    "lead_list",
    "lead_candidates",
    "manual_dispatch",
    "assignment_list",
    "assignment_detail",
    "claim",
    "points_ledger",
    "dashboard_report",
    "evidence_upload",
}
P95_LIMITS_MS = {"list": 1500.0, "detail": 1000.0, "claim": 500.0, "login": 1500.0, "write": 1500.0}
CONSISTENCY_KEYS = {
    "duplicate_claim_ledgers",
    "points_balance_mismatches",
    "duplicate_active_assignments",
}
TARGET_METRIC_KEYS = {
    "source",
    "environment",
    "base_url_origin",
    "window_started_at",
    "window_ended_at",
    "exported_at",
    "export_reference",
    "export_sha256",
    "cpu_percent_max",
    "memory_percent_max",
    "io_read_bytes",
    "io_write_bytes",
}
SENSITIVE_KEY_PARTS = ("password", "cookie", "authorization", "access_token", "refresh_token", "phone")
CLAIM_APPROVAL_REFERENCE_PATTERN = re.compile(
    r"https://github\.com/peihr666-max/zhongshu-lead-platform/(?:issues|pull)/[1-9]\d*"
)


class GateError(ValueError):
    pass


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < minimum
    ):
        raise GateError(f"{field} must be a number >= {minimum}")
    return float(value)


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise GateError(f"{field} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise GateError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GateError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GateError(f"{field} must include a timezone")
    return parsed


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _immutable_measurement_view(report: dict[str, Any]) -> dict[str, Any]:
    view = copy.deepcopy(report)
    view.pop("signoff", None)
    view.pop("preview_evidence", None)
    profiles = view.get("profiles")
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict):
                profile["target_infrastructure"] = None
    return view


def _assert_no_sensitive_data(value: Any, path: str = "report") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                raise GateError(f"sensitive field is forbidden in evidence: {path}.{key}")
            _assert_no_sensitive_data(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sensitive_data(item, f"{path}[{index}]")
    elif isinstance(value, str) and ("bearer " in value.lower() or "set-cookie" in value.lower()):
        raise GateError(f"credential-like value is forbidden in evidence: {path}")


def _claim_limit(report: dict[str, Any]) -> tuple[float, str | None]:
    baseline = report.get("claim_baseline")
    if baseline is None:
        return P95_LIMITS_MS["claim"], None
    if not isinstance(baseline, dict) or baseline.get("approved") is not True:
        raise GateError("claim_baseline must be absent or explicitly approved")
    reference = baseline.get("approval_reference")
    if not isinstance(reference, str) or not CLAIM_APPROVAL_REFERENCE_PATTERN.fullmatch(reference):
        raise GateError("approved claim_baseline needs this repository's numeric Issue/PR approval_reference")
    maximum = _number(baseline.get("p95_limit_ms"), "claim_baseline.p95_limit_ms", minimum=500.0)
    return maximum, reference


def evaluate_report(
    report: dict[str, Any],
    *,
    require_signoff: bool = True,
    allow_pending_infrastructure: bool = False,
    metrics_export_sha256: str | None = None,
    preview_report_sha256: str | None = None,
    preview_report: dict[str, Any] | None = None,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    _assert_no_sensitive_data(report)
    if report.get("schema_version") != 1 or report.get("mode") != "staging":
        raise GateError("report schema_version/mode is not the V1.2 staging contract")
    if report.get("synthetic_data") is not True:
        raise GateError("performance evidence must use synthetic_data=true")
    if report.get("environment") not in {"staging", "staging-equivalent"}:
        raise GateError("performance evidence environment must be staging or staging-equivalent")
    origin = report.get("base_url_origin")
    if not isinstance(origin, str):
        raise GateError("base_url_origin is required")
    parsed_origin = urlparse(origin)
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname or parsed_origin.username or parsed_origin.password:
        raise GateError("base_url_origin is invalid")
    if parsed_origin.scheme == "http" and parsed_origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise GateError("non-local performance evidence must use HTTPS")
    if not isinstance(report.get("dataset_id"), str) or not report["dataset_id"]:
        raise GateError("dataset_id is required")
    source_commit = report.get("source_commit_sha")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise GateError("source_commit_sha must be a lowercase 40-character Git commit SHA")
    report_generated_at = _timestamp(report.get("generated_at"), "report.generated_at")

    signoff = report.get("signoff")
    if not isinstance(signoff, dict):
        raise GateError("signoff object is required")
    signoff_approved_at: datetime | None = None
    if require_signoff:
        if signoff.get("status") != "APPROVED":
            raise GateError("final staging evidence requires signoff.status=APPROVED")
        for field in ("approved_by", "approved_at", "approval_reference"):
            if not isinstance(signoff.get(field), str) or not signoff[field]:
                raise GateError(f"final staging evidence requires signoff.{field}")
        if not CLAIM_APPROVAL_REFERENCE_PATTERN.fullmatch(signoff["approval_reference"]):
            raise GateError("final staging signoff requires this repository's numeric Issue/PR approval_reference")
        signoff_approved_at = _timestamp(signoff["approved_at"], "signoff.approved_at")
        if signoff_approved_at < report_generated_at:
            raise GateError("final staging signoff must occur after report generation")
        preview_evidence = report.get("preview_evidence")
        if not isinstance(preview_evidence, dict) or set(preview_evidence) != {
            "github_run_id",
            "artifact_name",
            "report_sha256",
            "source_commit_sha",
        }:
            raise GateError("final staging evidence requires exact preview artifact provenance")
        run_id = preview_evidence["github_run_id"]
        if not isinstance(run_id, str) or not run_id.isdigit() or int(run_id) <= 0:
            raise GateError("preview github_run_id must be a positive integer string")
        if preview_evidence["artifact_name"] != f"staging-performance-{run_id}":
            raise GateError("preview artifact_name does not match github_run_id")
        if not isinstance(expected_source_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}", expected_source_commit
        ):
            raise GateError("final staging evidence requires the trusted source commit")
        if source_commit != expected_source_commit or preview_evidence["source_commit_sha"] != source_commit:
            raise GateError("performance evidence source commit does not match the trusted finalize commit")
        if not re.fullmatch(r"[0-9a-f]{64}", str(preview_evidence["report_sha256"])):
            raise GateError("preview report_sha256 must be lowercase SHA-256")
        if preview_report_sha256 is None or preview_evidence["report_sha256"] != preview_report_sha256:
            raise GateError("signed report is not bound to the archived preview report")
        if not isinstance(preview_report, dict):
            raise GateError("archived preview report content is required")
        if preview_report.get("signoff", {}).get("status") != "PENDING":
            raise GateError("archived preview report must retain PENDING signoff")
        if _immutable_measurement_view(report) != _immutable_measurement_view(preview_report):
            raise GateError("signed report rewrites immutable preview measurements")

    claim_limit, baseline_reference = _claim_limit(report)
    profiles = report.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != {str(value) for value in REQUIRED_PROFILES}:
        raise GateError("report must contain exactly the 100, 300 and 500 concurrency profiles")

    evaluations: dict[str, Any] = {}
    for profile in REQUIRED_PROFILES:
        result = profiles[str(profile)]
        if not isinstance(result, dict) or result.get("concurrency") != profile:
            raise GateError(f"profile {profile} has an invalid concurrency marker")
        profile_started_at = _timestamp(result.get("started_at"), f"{profile}.started_at")
        profile_completed_at = _timestamp(result.get("completed_at"), f"{profile}.completed_at")
        if profile_completed_at < profile_started_at:
            raise GateError(f"profile {profile} completed before it started")
        scenarios = result.get("scenarios")
        if not isinstance(scenarios, dict) or set(scenarios) != REQUIRED_SCENARIOS:
            raise GateError(f"profile {profile} does not contain the exact required scenario set")

        latency_failures: list[str] = []
        for name, metrics in scenarios.items():
            if not isinstance(metrics, dict):
                raise GateError(f"profile {profile} scenario {name} must be an object")
            requests = _integer(metrics.get("requests"), f"{profile}.{name}.requests", minimum=1)
            successes = _integer(metrics.get("successes"), f"{profile}.{name}.successes")
            failures = _integer(metrics.get("failures"), f"{profile}.{name}.failures")
            error_rate = _number(metrics.get("error_rate"), f"{profile}.{name}.error_rate")
            p50 = _number(metrics.get("p50_ms"), f"{profile}.{name}.p50_ms")
            p95 = _number(metrics.get("p95_ms"), f"{profile}.{name}.p95_ms")
            p99 = _number(metrics.get("p99_ms"), f"{profile}.{name}.p99_ms")
            duration = _number(metrics.get("duration_seconds"), f"{profile}.{name}.duration_seconds")
            throughput = _number(metrics.get("throughput_rps"), f"{profile}.{name}.throughput_rps")
            if successes + failures != requests:
                raise GateError(f"profile {profile} scenario {name} request totals are inconsistent")
            if failures != 0 or error_rate != 0:
                raise GateError(f"profile {profile} scenario {name} has request errors")
            if duration <= 0 or throughput <= 0:
                raise GateError(f"profile {profile} scenario {name} has no measurable throughput")
            if not p50 <= p95 <= p99:
                raise GateError(f"profile {profile} scenario {name} percentiles are not monotonic")
            if name != "login" and requests < profile:
                raise GateError(f"profile {profile} scenario {name} did not execute at least {profile} requests")
            expected_concurrency = 1 if name == "login" else profile
            if metrics.get("concurrency") != expected_concurrency:
                raise GateError(f"profile {profile} scenario {name} has an invalid concurrency marker")
            category = metrics.get("category")
            if category not in P95_LIMITS_MS:
                raise GateError(f"profile {profile} scenario {name} has an unknown category")
            limit = claim_limit if category == "claim" else P95_LIMITS_MS[category]
            if p95 > limit:
                latency_failures.append(f"{name}: {p95:.3f}ms > {limit:.3f}ms")

        database = result.get("database")
        if not isinstance(database, dict):
            raise GateError(f"profile {profile} database evidence is required")
        for field in (
            "samples",
            "max_connections",
            "peak_total_connections",
            "peak_active_connections",
            "peak_idle_connections",
            "peak_waiting_connections",
            "peak_lock_waiting_connections",
            "peak_blocked_queries",
            "peak_slow_queries",
            "longest_query_seconds",
            "deadlocks_delta",
            "temp_files_delta",
            "temp_bytes_delta",
            "block_read_ms_delta",
            "block_write_ms_delta",
        ):
            if field in {
                "samples",
                "max_connections",
                "peak_total_connections",
                "peak_active_connections",
                "peak_idle_connections",
                "peak_waiting_connections",
                "peak_lock_waiting_connections",
                "peak_blocked_queries",
                "peak_slow_queries",
                "deadlocks_delta",
                "temp_files_delta",
                "temp_bytes_delta",
            }:
                _integer(database.get(field), f"{profile}.database.{field}")
            else:
                _number(database.get(field), f"{profile}.database.{field}")
        if database["samples"] < 2:
            raise GateError(f"profile {profile} needs at least two PostgreSQL samples")
        if database["deadlocks_delta"] != 0:
            raise GateError(f"profile {profile} recorded PostgreSQL deadlocks")

        consistency = result.get("consistency")
        if not isinstance(consistency, dict) or set(consistency) != CONSISTENCY_KEYS:
            raise GateError(f"profile {profile} consistency evidence is incomplete")
        for key in CONSISTENCY_KEYS:
            if _number(consistency[key], f"{profile}.consistency.{key}") != 0:
                raise GateError(f"profile {profile} failed consistency check {key}")

        target = result.get("target_infrastructure")
        if target is None and allow_pending_infrastructure:
            pass
        else:
            if not isinstance(target, dict) or set(target) != TARGET_METRIC_KEYS:
                raise GateError(f"profile {profile} requires target CPU, memory and I/O evidence")
            for key in ("source", "environment", "base_url_origin", "export_reference", "export_sha256"):
                if not isinstance(target[key], str) or not target[key].strip():
                    raise GateError(f"profile {profile} target infrastructure {key} is required")
            if target["environment"] != report["environment"] or target["base_url_origin"] != origin:
                raise GateError(f"profile {profile} target infrastructure identity does not match the run")
            window_started_at = _timestamp(target["window_started_at"], f"{profile}.target.window_started_at")
            window_ended_at = _timestamp(target["window_ended_at"], f"{profile}.target.window_ended_at")
            exported_at = _timestamp(target["exported_at"], f"{profile}.target.exported_at")
            if window_started_at > profile_started_at or window_ended_at < profile_completed_at:
                raise GateError(f"profile {profile} target monitoring window does not cover the measured run")
            if window_ended_at < window_started_at or exported_at < window_ended_at:
                raise GateError(f"profile {profile} target monitoring timestamps are inconsistent")
            if signoff_approved_at is not None and signoff_approved_at < exported_at:
                raise GateError(f"profile {profile} signoff predates the monitoring export")
            export_reference = urlparse(target["export_reference"])
            if export_reference.scheme != "https" or not export_reference.hostname:
                raise GateError(f"profile {profile} target export_reference must be an HTTPS URL")
            if not re.fullmatch(r"[0-9a-f]{64}", target["export_sha256"]):
                raise GateError(f"profile {profile} target export_sha256 must be lowercase SHA-256")
            if metrics_export_sha256 is None or target["export_sha256"] != metrics_export_sha256:
                raise GateError(f"profile {profile} target metrics are not bound to the archived raw export")
            for key in ("cpu_percent_max", "memory_percent_max", "io_read_bytes", "io_write_bytes"):
                _number(target[key], f"{profile}.target_infrastructure.{key}")
            if target["cpu_percent_max"] <= 0 or target["memory_percent_max"] <= 0:
                raise GateError(f"profile {profile} target CPU and memory samples must be greater than zero")

        if profile in (100, 300) and latency_failures:
            raise GateError(f"profile {profile} failed latency gates: {'; '.join(latency_failures)}")
        conclusion = "CAPACITY_LIMIT_REACHED" if profile == 500 and latency_failures else "PASS"
        evaluations[str(profile)] = {"conclusion": conclusion, "latency_failures": latency_failures}

    evidence_pending = signoff.get("status") != "APPROVED" or any(
        profiles[str(profile)].get("target_infrastructure") is None for profile in REQUIRED_PROFILES
    )
    if evidence_pending:
        status = "EVIDENCE_PENDING"
    elif any(item["conclusion"] == "CAPACITY_LIMIT_REACHED" for item in evaluations.values()):
        status = "CAPACITY_LIMIT_REACHED"
    else:
        status = "PASS"
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "dataset_id": report["dataset_id"],
        "claim_p95_limit_ms": claim_limit,
        "claim_baseline_reference": baseline_reference,
        "profiles": evaluations,
        "signoff_status": signoff.get("status"),
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# V1.2 Performance Gate",
        "",
        f"- Status: **{result['status']}**",
        f"- Dataset: `{result['dataset_id']}`",
        f"- Claim P95 limit: `{result['claim_p95_limit_ms']:.3f} ms`",
        f"- Sign-off: `{result['signoff_status']}`",
        "",
        "| Concurrency | Conclusion | Latency observations |",
        "|---:|---|---|",
    ]
    for profile, evaluation in result["profiles"].items():
        observations = "; ".join(evaluation["latency_failures"]) or "All latency gates passed"
        lines.append(f"| {profile} | {evaluation['conclusion']} | {observations} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce the V1.2 staging performance and capacity gate")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/performance/v12-performance-gate.json"))
    parser.add_argument("--allow-pending-signoff", action="store_true")
    parser.add_argument("--allow-pending-infrastructure", action="store_true")
    parser.add_argument("--metrics-export", type=Path)
    parser.add_argument("--preview-report", type=Path)
    parser.add_argument("--expected-source-commit")
    args = parser.parse_args()

    if not args.allow_pending_infrastructure and args.metrics_export is None:
        parser.error("--metrics-export is required for the final infrastructure evidence gate")
    if not args.allow_pending_signoff and args.preview_report is None:
        parser.error("--preview-report is required for the signed final evidence gate")
    if not args.allow_pending_signoff and args.expected_source_commit is None:
        parser.error("--expected-source-commit is required for the signed final evidence gate")
    if args.metrics_export is not None and args.metrics_export.stat().st_size == 0:
        raise GateError("archived raw metrics export must not be empty")

    report = json.loads(args.report.read_text(encoding="utf-8-sig"))
    if not isinstance(report, dict):
        raise GateError("report root must be an object")
    export_sha256 = file_sha256(args.metrics_export) if args.metrics_export else None
    preview_sha256 = file_sha256(args.preview_report) if args.preview_report else None
    preview_document = (
        json.loads(args.preview_report.read_text(encoding="utf-8-sig")) if args.preview_report else None
    )
    if preview_document is not None and not isinstance(preview_document, dict):
        raise GateError("preview report root must be an object")
    result = evaluate_report(
        report,
        require_signoff=not args.allow_pending_signoff,
        allow_pending_infrastructure=args.allow_pending_infrastructure,
        metrics_export_sha256=export_sha256,
        preview_report_sha256=preview_sha256,
        preview_report=preview_document,
        expected_source_commit=args.expected_source_commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(render_markdown(result), encoding="utf-8")
    print(f"performance gate completed with {result['status']}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
