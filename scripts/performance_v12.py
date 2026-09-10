from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import create_engine, text


SCHEMA_VERSION = 1
DEFAULT_PROFILES = (100, 300, 500)
PNG_FIXTURE = b"\x89PNG\r\n\x1a\nH04 synthetic performance evidence\n"
CLAIM_APPROVAL_REFERENCE_PATTERN = re.compile(
    r"https://github\.com/peihr666-max/zhongshu-lead-platform/(?:issues|pull)/[1-9]\d*"
)


@dataclass(frozen=True)
class Scenario:
    name: str
    role: str
    method: str
    path: str
    category: str
    repetitions: int = 2
    json_body: dict[str, Any] | None = None
    evidence_return_ids: tuple[str, ...] = ()


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def scenario_metrics(latencies_ms: list[float], failures: int, duration_seconds: float) -> dict[str, Any]:
    requests = len(latencies_ms)
    successes = requests - failures
    return {
        "requests": requests,
        "successes": successes,
        "failures": failures,
        "error_rate": round(failures / requests, 6) if requests else 1.0,
        "duration_seconds": round(duration_seconds, 6),
        "throughput_rps": round(successes / duration_seconds, 3) if duration_seconds > 0 else 0.0,
        "p50_ms": round(percentile(latencies_ms, 0.50), 3),
        "p95_ms": round(percentile(latencies_ms, 0.95), 3),
        "p99_ms": round(percentile(latencies_ms, 0.99), 3),
        "mean_ms": round(statistics.fmean(latencies_ms), 3) if latencies_ms else 0.0,
    }


def validate_claim_baseline(claim_baseline: Any) -> None:
    if claim_baseline is None:
        return
    if not isinstance(claim_baseline, dict) or claim_baseline.get("approved") is not True:
        raise ValueError("dataset claim_baseline must be absent or explicitly approved")
    limit = claim_baseline.get("p95_limit_ms")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(float(limit))
        or limit < 500
    ):
        raise ValueError("dataset claim_baseline.p95_limit_ms must be at least 500")
    reference = claim_baseline.get("approval_reference")
    if not isinstance(reference, str) or not CLAIM_APPROVAL_REFERENCE_PATTERN.fullmatch(reference):
        raise ValueError("dataset claim_baseline requires this repository's numeric Issue/PR approval_reference")


def validate_dataset(document: dict[str, Any], *, profiles: tuple[int, ...] = DEFAULT_PROFILES, runtime: bool = False) -> None:
    required_strings = (
        "dataset_id",
        "environment",
        "base_url_origin",
        "assignment_detail_id",
        "dispatch_company_id",
    )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"dataset schema_version must be {SCHEMA_VERSION}")
    if document.get("synthetic_data") is not True:
        raise ValueError("dataset must declare synthetic_data=true")
    validate_claim_baseline(document.get("claim_baseline"))
    for key in required_strings:
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise ValueError(f"dataset field {key!r} must be a non-empty string")
    dispatch_cases = document.get("dispatch_cases")
    if not isinstance(dispatch_cases, dict):
        raise ValueError("dataset dispatch_cases must be an object keyed by concurrency profile")
    dispatch_lead_ids: set[str] = set()
    dispatch_idempotency_keys: set[str] = set()
    for profile in profiles:
        case = dispatch_cases.get(str(profile))
        if not isinstance(case, dict):
            raise ValueError(f"dataset dispatch_cases must contain profile {profile}")
        for key in ("lead_id", "idempotency_key"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"dataset dispatch_cases.{profile}.{key} must be a non-empty string")
        if case["lead_id"] in dispatch_lead_ids or case["idempotency_key"] in dispatch_idempotency_keys:
            raise ValueError("dataset dispatch_cases must use distinct leads and idempotency keys for every profile")
        dispatch_lead_ids.add(case["lead_id"])
        dispatch_idempotency_keys.add(case["idempotency_key"])
    claim_cases = document.get("claim_cases")
    if not isinstance(claim_cases, dict):
        raise ValueError("dataset claim_cases must be an object keyed by concurrency profile")
    used_claim_ids: set[str] = set()
    for profile in profiles:
        assignment_id = claim_cases.get(str(profile))
        if not isinstance(assignment_id, str) or not assignment_id.strip():
            raise ValueError(f"dataset claim_cases must contain a non-empty assignment id for profile {profile}")
        if assignment_id in used_claim_ids:
            raise ValueError("dataset claim_cases must use distinct assignments for every profile")
        used_claim_ids.add(assignment_id)
    evidence_cases = document.get("evidence_cases")
    if not isinstance(evidence_cases, dict) or set(evidence_cases) != {str(profile) for profile in profiles}:
        raise ValueError("dataset evidence_cases must contain exactly the requested concurrency profiles")
    used_evidence_ids: set[str] = set()
    for profile in profiles:
        evidence_ids = evidence_cases[str(profile)]
        if not isinstance(evidence_ids, list) or not evidence_ids or not all(
            isinstance(item, str) and item.strip() for item in evidence_ids
        ):
            raise ValueError(f"dataset evidence_cases.{profile} must be a non-empty string array")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError(f"dataset evidence_cases.{profile} must not contain duplicate return ids")
        overlap = used_evidence_ids.intersection(evidence_ids)
        if overlap:
            raise ValueError("dataset evidence_cases must use disjoint return ids for every profile")
        used_evidence_ids.update(evidence_ids)
        if runtime and len(evidence_ids) < profile:
            raise ValueError(f"runtime dataset evidence_cases.{profile} needs at least {profile} return ids")
    if runtime:
        if document["environment"].strip().lower() not in {"staging", "staging-equivalent"}:
            raise ValueError("runtime dataset environment must be staging or staging-equivalent")
        placeholders = [value for value in _walk_strings(document) if value.startswith("REPLACE_WITH_")]
        if placeholders:
            raise ValueError("runtime dataset still contains placeholder values")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def load_dataset(path: Path, *, profiles: tuple[int, ...], runtime: bool) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("dataset root must be an object")
    validate_dataset(document, profiles=profiles, runtime=runtime)
    return document


def safe_origin(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base URL must be an HTTP(S) origin without embedded credentials")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("non-local performance targets must use HTTPS")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


async def login(base_url: str, role: str) -> tuple[str, float]:
    prefix = f"V12_PERF_{role.upper()}"
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, follow_redirects=False) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": _required_env(f"{prefix}_USERNAME"), "password": _required_env(f"{prefix}_PASSWORD")},
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        token = response.cookies.get("access_token")
        if not token:
            raise RuntimeError(f"{role} login did not return the access_token cookie")
        return token, elapsed_ms


def build_scenarios(dataset: dict[str, Any], profile: int) -> list[Scenario]:
    dispatch_case = dataset["dispatch_cases"][str(profile)]
    lead_id = dispatch_case["lead_id"]
    assignment_id = dataset["assignment_detail_id"]
    return [
        Scenario("lead_list", "operator", "GET", "/api/v1/v1.2/dispatch-pool?page=1&page_size=20", "list"),
        Scenario("lead_candidates", "operator", "GET", f"/api/v1/v1.2/dispatch-pool/{lead_id}/candidates", "list"),
        Scenario(
            "manual_dispatch",
            "operator",
            "POST",
            f"/api/v1/v1.2/dispatch-pool/{lead_id}/dispatch",
            "write",
            repetitions=1,
            json_body={
                "company_id": dataset["dispatch_company_id"],
                "idempotency_key": dispatch_case["idempotency_key"],
                "note": "H04 synthetic idempotency load",
            },
        ),
        Scenario("assignment_list", "receiver", "GET", "/api/v1/v1.2/assignments?page=1&page_size=20", "list"),
        Scenario("assignment_detail", "receiver", "GET", f"/api/v1/v1.2/assignments/{assignment_id}", "detail"),
        Scenario(
            "claim",
            "receiver",
            "POST",
            f"/api/v1/v1.2/assignments/{dataset['claim_cases'][str(profile)]}/claim",
            "claim",
            repetitions=1,
        ),
        Scenario("points_ledger", "receiver", "GET", "/api/v1/points/ledgers?page=1&page_size=20", "list"),
        Scenario("dashboard_report", "owner", "GET", "/api/v1/dashboard/performance?days=30", "list"),
        Scenario(
            "evidence_upload",
            "receiver",
            "POST",
            "/api/v1/v1.2/returns/{return_id}/evidence",
            "write",
            repetitions=1,
            evidence_return_ids=tuple(dataset["evidence_cases"][str(profile)][:profile]),
        ),
    ]


async def _request_once(client: httpx.AsyncClient, scenario: Scenario, index: int) -> tuple[float, bool]:
    path = scenario.path
    kwargs: dict[str, Any] = {}
    if scenario.json_body is not None:
        kwargs["json"] = scenario.json_body
    if scenario.evidence_return_ids:
        path = path.format(return_id=scenario.evidence_return_ids[index % len(scenario.evidence_return_ids)])
        kwargs["data"] = {"evidence_type": "CHAT_SCREENSHOT"}
        kwargs["files"] = {"file": (f"h04-{index}.png", PNG_FIXTURE, "image/png")}
    started = time.perf_counter()
    try:
        response = await client.request(scenario.method, path, **kwargs)
        ok = 200 <= response.status_code < 300
    except httpx.HTTPError:
        ok = False
    return (time.perf_counter() - started) * 1000, ok


async def run_scenario(client: httpx.AsyncClient, scenario: Scenario, concurrency: int) -> dict[str, Any]:
    request_count = concurrency * scenario.repetitions
    gate = asyncio.Semaphore(concurrency)

    async def bounded(index: int) -> tuple[float, bool]:
        async with gate:
            return await _request_once(client, scenario, index)

    started = time.perf_counter()
    results = await asyncio.gather(*(bounded(index) for index in range(request_count)))
    duration = time.perf_counter() - started
    latencies = [latency for latency, _ in results]
    failures = sum(1 for _, ok in results if not ok)
    metrics = scenario_metrics(latencies, failures, duration)
    metrics.update({"category": scenario.category, "concurrency": concurrency})
    return metrics


class DatabaseSampler:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)
        if self.engine.dialect.name != "postgresql":
            raise ValueError("capacity evidence requires a PostgreSQL DATABASE_URL")
        self.samples: list[dict[str, float | int]] = []
        self._stop = asyncio.Event()
        self._deadlocks_start = 0

    def _snapshot(self) -> dict[str, float | int]:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT
                      count(*) AS total_connections,
                      count(*) FILTER (WHERE state = 'active') AS active_connections,
                      count(*) FILTER (WHERE state = 'idle') AS idle_connections,
                      count(*) FILTER (WHERE state = 'active' AND wait_event_type IS NOT NULL) AS waiting_connections,
                      count(*) FILTER (WHERE state = 'active' AND wait_event_type = 'Lock') AS lock_waiting_connections,
                      count(*) FILTER (WHERE cardinality(pg_blocking_pids(pid)) > 0) AS blocked_queries,
                      count(*) FILTER (
                        WHERE state = 'active'
                          AND query_start IS NOT NULL
                          AND clock_timestamp() - query_start >= interval '1 second'
                      ) AS slow_queries,
                      coalesce(max(extract(epoch FROM (clock_timestamp() - query_start)))
                        FILTER (WHERE state <> 'idle' AND query_start IS NOT NULL), 0) AS longest_query_seconds
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    """
                )
            ).mappings().one()
            max_connections = connection.execute(
                text("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
            ).scalar_one()
            database = connection.execute(
                text(
                    "SELECT deadlocks, temp_files, temp_bytes, blk_read_time, blk_write_time "
                    "FROM pg_stat_database WHERE datname = current_database()"
                )
            ).mappings().one()
        return {
            "total_connections": int(row["total_connections"]),
            "active_connections": int(row["active_connections"]),
            "idle_connections": int(row["idle_connections"]),
            "waiting_connections": int(row["waiting_connections"]),
            "lock_waiting_connections": int(row["lock_waiting_connections"]),
            "blocked_queries": int(row["blocked_queries"]),
            "slow_queries": int(row["slow_queries"]),
            "longest_query_seconds": round(float(row["longest_query_seconds"]), 6),
            "max_connections": int(max_connections),
            "deadlocks_total": int(database["deadlocks"]),
            "temp_files_total": int(database["temp_files"]),
            "temp_bytes_total": int(database["temp_bytes"]),
            "block_read_ms_total": round(float(database["blk_read_time"]), 3),
            "block_write_ms_total": round(float(database["blk_write_time"]), 3),
        }

    async def sample(self) -> None:
        initial = await asyncio.to_thread(self._snapshot)
        self._deadlocks_start = int(initial["deadlocks_total"])
        self.samples.append(initial)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                self.samples.append(await asyncio.to_thread(self._snapshot))

    async def finish(self) -> dict[str, Any]:
        self._stop.set()
        final = await asyncio.to_thread(self._snapshot)
        self.samples.append(final)
        self.engine.dispose()
        return {
            "samples": len(self.samples),
            "max_connections": max(int(item["max_connections"]) for item in self.samples),
            "peak_total_connections": max(int(item["total_connections"]) for item in self.samples),
            "peak_active_connections": max(int(item["active_connections"]) for item in self.samples),
            "peak_idle_connections": max(int(item["idle_connections"]) for item in self.samples),
            "peak_waiting_connections": max(int(item["waiting_connections"]) for item in self.samples),
            "peak_lock_waiting_connections": max(int(item["lock_waiting_connections"]) for item in self.samples),
            "peak_blocked_queries": max(int(item["blocked_queries"]) for item in self.samples),
            "peak_slow_queries": max(int(item["slow_queries"]) for item in self.samples),
            "longest_query_seconds": max(float(item["longest_query_seconds"]) for item in self.samples),
            "deadlocks_delta": int(final["deadlocks_total"]) - self._deadlocks_start,
            "temp_files_delta": int(final["temp_files_total"]) - int(self.samples[0]["temp_files_total"]),
            "temp_bytes_delta": int(final["temp_bytes_total"]) - int(self.samples[0]["temp_bytes_total"]),
            "block_read_ms_delta": round(float(final["block_read_ms_total"]) - float(self.samples[0]["block_read_ms_total"]), 3),
            "block_write_ms_delta": round(float(final["block_write_ms_total"]) - float(self.samples[0]["block_write_ms_total"]), 3),
        }


def consistency_snapshot(database_url: str) -> dict[str, int]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            duplicate_claim_ledgers = connection.execute(
                text(
                    """
                    SELECT count(*) FROM (
                      SELECT business_id
                      FROM points_ledgers
                      WHERE ledger_type = 'CLAIM'
                      GROUP BY business_id
                      HAVING count(*) > 1
                    ) duplicates
                    """
                )
            ).scalar_one()
            balance_mismatches = connection.execute(
                text(
                    """
                    SELECT count(*) FROM (
                      SELECT pa.id
                      FROM points_accounts pa
                      LEFT JOIN points_ledgers pl ON pl.account_id = pa.id
                      GROUP BY pa.id, pa.balance
                      HAVING pa.balance <> coalesce(sum(pl.delta), 0)
                    ) mismatches
                    """
                )
            ).scalar_one()
            duplicate_active_assignments = connection.execute(
                text(
                    """
                    SELECT count(*) FROM (
                      SELECT lead_id
                      FROM assignments
                      WHERE status IN ('PENDING_CLAIM', 'CLAIMED', 'FOLLOWING', 'RETURN_PENDING')
                      GROUP BY lead_id
                      HAVING count(*) > 1
                    ) duplicates
                    """
                )
            ).scalar_one()
        return {
            "duplicate_claim_ledgers": int(duplicate_claim_ledgers),
            "points_balance_mismatches": int(balance_mismatches),
            "duplicate_active_assignments": int(duplicate_active_assignments),
        }
    finally:
        engine.dispose()


def load_target_metrics(path: Path | None, profile: int) -> dict[str, Any] | None:
    if path is None:
        return None
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    required_strings = (
        "source",
        "environment",
        "base_url_origin",
        "exported_at",
        "export_reference",
        "export_sha256",
    )
    for key in required_strings:
        value = document.get(key)
        if not isinstance(value, str) or not value.strip() or value.startswith("REPLACE_WITH_"):
            raise ValueError(f"target metrics require a non-placeholder {key}")
    metrics = document.get("profiles", {}).get(str(profile))
    if not isinstance(metrics, dict):
        raise ValueError(f"target metrics do not contain profile {profile}")
    required = (
        "window_started_at",
        "window_ended_at",
        "cpu_percent_max",
        "memory_percent_max",
        "io_read_bytes",
        "io_write_bytes",
    )
    for key in required:
        if key.startswith("window_"):
            if not isinstance(metrics.get(key), str) or not metrics[key]:
                raise ValueError(f"target metrics field {key!r} for profile {profile} is invalid")
        elif not isinstance(metrics.get(key), (int, float)) or metrics[key] < 0:
            raise ValueError(f"target metrics field {key!r} for profile {profile} is invalid")
    return {
        **{key: document[key].strip() for key in required_strings},
        **{key: metrics[key] for key in required},
    }


async def run_profile(
    *,
    base_url: str,
    dataset: dict[str, Any],
    profile: int,
    database_url: str,
    target_metrics_path: Path | None,
) -> dict[str, Any]:
    profile_started_at = datetime.now(timezone.utc)
    login_latencies: list[float] = []
    tokens: dict[str, str] = {}
    for role in ("operator", "receiver", "owner"):
        token, latency = await login(base_url, role)
        tokens[role] = token
        login_latencies.append(latency)

    limits = httpx.Limits(max_connections=profile, max_keepalive_connections=profile)
    clients = {
        role: httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            limits=limits,
            follow_redirects=False,
        )
        for role, token in tokens.items()
    }
    sampler = DatabaseSampler(database_url)
    sampler_task = asyncio.create_task(sampler.sample())
    scenarios: dict[str, Any] = {
        "login": {
            **scenario_metrics(login_latencies, 0, sum(login_latencies) / 1000),
            "category": "login",
            "concurrency": 1,
            "note": "one setup login per required role; credentials are never retained",
        }
    }
    try:
        for scenario in build_scenarios(dataset, profile):
            scenarios[scenario.name] = await run_scenario(clients[scenario.role], scenario, profile)
    finally:
        for client in clients.values():
            await client.aclose()
        database = await sampler.finish()
        await sampler_task

    consistency = await asyncio.to_thread(consistency_snapshot, database_url)
    return {
        "concurrency": profile,
        "started_at": profile_started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": scenarios,
        "database": database,
        "target_infrastructure": load_target_metrics(target_metrics_path, profile),
        "load_generator": {"platform": platform.system(), "python": platform.python_version()},
        "consistency": consistency,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# V1.2 Performance and Capacity Report",
        "",
        f"- Dataset: `{report['dataset_id']}`",
        f"- Environment: `{report['environment']}`",
        f"- Source commit: `{report['source_commit_sha']}`",
        f"- Synthetic data only: `{str(report['synthetic_data']).lower()}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Sign-off: `{report['signoff']['status']}`",
        "",
    ]
    for profile, result in report["profiles"].items():
        lines.extend(
            [
                f"## Concurrency {profile}",
                "",
                "| Scenario | Requests | Error rate | P50 ms | P95 ms | P99 ms | RPS |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for name, metrics in result["scenarios"].items():
            lines.append(
                f"| {name} | {metrics['requests']} | {metrics['error_rate']:.2%} | "
                f"{metrics['p50_ms']:.3f} | {metrics['p95_ms']:.3f} | {metrics['p99_ms']:.3f} | "
                f"{metrics['throughput_rps']:.3f} |"
            )
        db = result["database"]
        consistency = result["consistency"]
        lines.extend(
            [
                "",
                f"Database: peak active `{db['peak_active_connections']}`, peak waiting `{db['peak_waiting_connections']}`, "
                f"peak lock waiting `{db['peak_lock_waiting_connections']}`, peak blocked `{db['peak_blocked_queries']}`, "
                f"peak slow queries `{db['peak_slow_queries']}`, deadlocks delta `{db['deadlocks_delta']}`.",
                "",
                "Consistency: " + ", ".join(f"{key}={value}" for key, value in consistency.items()) + ".",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    profiles = tuple(args.profiles)
    dataset = load_dataset(args.dataset, profiles=profiles, runtime=True)
    base_url = safe_origin(args.base_url)
    expected_base_url = safe_origin(dataset["base_url_origin"])
    if base_url != expected_base_url:
        raise ValueError("base URL does not match the protected synthetic dataset origin")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit_sha": args.source_commit,
        "mode": "staging",
        "environment": dataset["environment"],
        "dataset_id": dataset["dataset_id"],
        "synthetic_data": True,
        "base_url_origin": base_url,
        "claim_baseline": dataset.get("claim_baseline"),
        "profiles": {},
        "signoff": {"status": "PENDING", "approved_by": None, "approved_at": None, "approval_reference": None},
    }
    for profile in profiles:
        report["profiles"][str(profile)] = await run_profile(
            base_url=base_url,
            dataset=dataset,
            profile=profile,
            database_url=args.database_url,
            target_metrics_path=args.target_metrics,
        )
    return report


def parse_profiles(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("profiles must be comma-separated positive integers") from exc
    if not values or any(value <= 0 or value > 1000 for value in values) or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("profiles must be unique integers between 1 and 1000")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the V1.2 synthetic staging performance and capacity suite")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--profiles", type=parse_profiles, default=DEFAULT_PROFILES)
    parser.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--target-metrics", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/performance/v12-performance-report.json"))
    args = parser.parse_args()

    profiles = tuple(args.profiles)
    if args.validate_config:
        load_dataset(args.dataset, profiles=profiles, runtime=False)
        print(f"performance dataset contract valid: {args.dataset}")
        return 0
    if not args.base_url or not args.database_url:
        parser.error("--base-url and --database-url/DATABASE_URL are required for a staging run")
    if not isinstance(args.source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        parser.error("--source-commit/GITHUB_SHA must be a lowercase 40-character Git commit SHA")

    report = asyncio.run(execute(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"performance evidence written: {args.output} and {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
