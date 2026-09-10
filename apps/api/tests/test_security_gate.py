from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tarfile
from datetime import date
from pathlib import Path

import pytest

from scripts.check_security_gate import (
    SEMGREP_IMAGE_REF,
    SEMGREP_RULES_COMMIT,
    SEMGREP_RULES_TREE,
    SEMGREP_VERSION,
    TRIVY_INPUT_ARTIFACT_NAME,
    Finding,
    Waiver,
    evaluate,
    load_waivers,
    main,
    semgrep_findings,
    trivy_findings,
    validate_run_provenance,
    validate_sbom,
    validate_scan_subject,
    validate_semgrep_identity_and_coverage,
)

IMAGE_REF = "zhongshu-security:test"
CONFIG_BYTES = b"{}"
IMAGE_ID = "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest()
CONFIG_NAME = IMAGE_ID.removeprefix("sha256:") + ".json"
SEMGREP_OCCURRENCE = "L164:C33-L164:C40"
TRIVY_OCCURRENCE = "Python|lang-pkgs|python-pkg|49.0.0"
MAIN_SHA = "a" * 40
POLICY_SHA = "b" * 40


def _semgrep(*results: dict, scanned: list[str] | None = None) -> dict:
    return {
        "version": SEMGREP_VERSION,
        "results": list(results),
        "errors": [],
        "paths": {"scanned": scanned or ["apps/example.py"]},
        "skipped_rules": [],
    }


def _semgrep_error(
    *,
    check_id: str = "python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args",
    path: str = "scripts/verify_production.py",
    start_line: int = 164,
    start_col: int = 33,
    end_line: int = 164,
    end_col: int = 40,
) -> dict:
    return {
        "check_id": check_id,
        "path": path,
        "start": {"line": start_line, "col": start_col},
        "end": {"line": end_line, "col": end_col},
        "extra": {"severity": "ERROR", "message": "test finding"},
    }


def _trivy(*vulnerabilities: dict) -> dict:
    return {
        "SchemaVersion": 2,
        "ArtifactName": TRIVY_INPUT_ARTIFACT_NAME,
        "ArtifactType": "container_image",
        "Metadata": {
            "ImageID": IMAGE_ID,
            "RepoTags": [IMAGE_REF],
        },
        "Results": [
            {
                "Target": "bookworm (debian 12)",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [
                    {
                        "Name": "base-files",
                        "Version": "12.4",
                        "Identifier": {"PURL": "pkg:deb/debian/base-files@12.4"},
                    },
                    {
                        "Name": "libc6",
                        "Version": "2.36",
                        "Identifier": {"PURL": "pkg:deb/debian/libc6@2.36"},
                    },
                ],
                "Vulnerabilities": [],
            },
            {
                "Target": "Python",
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Packages": [
                    {
                        "Name": "cryptography",
                        "Version": "49.0.0",
                        "Identifier": {"PURL": "pkg:pypi/cryptography@49.0.0"},
                    },
                    {
                        "Name": "fastapi",
                        "Version": "0.141.1",
                        "Identifier": {"PURL": "pkg:pypi/fastapi@0.141.1"},
                    },
                ],
                "Vulnerabilities": list(vulnerabilities),
            },
        ],
    }


def _waiver(**overrides) -> Waiver:
    values = {
        "scanner": "trivy",
        "finding_id": "CVE-2099-0100",
        "scope": "openssl",
        "occurrence": "Python|lang-pkgs|python-pkg|1.0.0",
        "reason": "accepted temporarily",
        "owner": "security-owner",
        "created_on": date(2026, 8, 9),
        "expires_on": date(2026, 9, 8),
        "first_waived_on": date(2026, 8, 9),
    }
    values.update(overrides)
    return Waiver(**values)


def _sbom() -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "components": [
            {
                "bom-ref": "os-ref",
                "type": "operating-system",
                "name": "debian",
                "version": "12",
            },
            {
                "bom-ref": "base-files-ref",
                "type": "library",
                "name": "base-files",
                "version": "12.4",
                "purl": "pkg:deb/debian/base-files@12.4",
            },
            {
                "bom-ref": "libc6-ref",
                "type": "library",
                "name": "libc6",
                "version": "2.36",
                "purl": "pkg:deb/debian/libc6@2.36",
            },
            {
                "bom-ref": "cryptography-ref",
                "type": "library",
                "name": "cryptography",
                "version": "49.0.0",
                "purl": "pkg:pypi/cryptography@49.0.0",
            },
            {
                "bom-ref": "fastapi-ref",
                "type": "library",
                "name": "fastapi",
                "version": "0.141.1",
                "purl": "pkg:pypi/fastapi@0.141.1",
            },
        ],
        "dependencies": [
            {
                "ref": "root-ref",
                "dependsOn": [
                    "os-ref",
                    "base-files-ref",
                    "libc6-ref",
                    "cryptography-ref",
                    "fastapi-ref",
                ],
            },
            {"ref": "os-ref", "dependsOn": []},
            {"ref": "base-files-ref", "dependsOn": []},
            {"ref": "libc6-ref", "dependsOn": []},
            {"ref": "cryptography-ref", "dependsOn": []},
            {"ref": "fastapi-ref", "dependsOn": []},
        ],
        "metadata": {
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "group": "aquasecurity",
                        "name": "trivy",
                        "version": "0.70.0",
                    }
                ]
            },
            "component": {
                "bom-ref": "root-ref",
                "type": "container",
                "name": TRIVY_INPUT_ARTIFACT_NAME,
                "properties": [
                    {"name": "aquasecurity:trivy:Reference", "value": IMAGE_REF},
                    {"name": "aquasecurity:trivy:RepoTag", "value": IMAGE_REF},
                    {"name": "aquasecurity:trivy:ImageID", "value": IMAGE_ID},
                ],
            },
        },
    }


def _evaluate(*, semgrep: dict | None = None, trivy: dict | None = None, waivers=None):
    return evaluate(
        semgrep_payload=semgrep or _semgrep(),
        trivy_payload=trivy or _trivy(),
        waivers=list(waivers or []),
        expected_artifact_name=TRIVY_INPUT_ARTIFACT_NAME,
        expected_image_ref=IMAGE_REF,
        expected_image_id=IMAGE_ID,
    )


def _add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _write_image_archive(
    path: Path,
    *,
    image_ref: str = IMAGE_REF,
    config_name: str = CONFIG_NAME,
) -> str:
    manifest = [{"Config": config_name, "RepoTags": [image_ref], "Layers": []}]
    with tarfile.open(path, "w") as archive:
        _add_tar_bytes(archive, "manifest.json", json.dumps(manifest).encode("utf-8"))
        _add_tar_bytes(archive, config_name, CONFIG_BYTES)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _write_oci_image_archive(
    path: Path,
    *,
    link_manifest_config: bool = True,
    duplicate_tagged_manifest: bool = False,
    conflicting_tagged_config: bool = False,
    index_depth: int = 1,
    root_size_delta: int = 0,
    root_media_type: str | None = None,
    tamper_root_blob: bool = False,
) -> tuple[str, str]:
    config = CONFIG_BYTES
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    unlinked_config = b'{"unlinked":true}'
    unlinked_config_digest = "sha256:" + hashlib.sha256(unlinked_config).hexdigest()
    manifest_config = config if link_manifest_config else unlinked_config
    manifest_config_digest = "sha256:" + hashlib.sha256(manifest_config).hexdigest()

    image_manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": [],
        }
    )
    child_payload = image_manifest
    child_digest = "sha256:" + hashlib.sha256(child_payload).hexdigest()
    child_media_type = "application/vnd.oci.image.manifest.v1+json"
    blobs = {
        "blobs/sha256/" + config_digest.removeprefix("sha256:"): config,
        "blobs/sha256/" + manifest_config_digest.removeprefix("sha256:"): manifest_config,
        "blobs/sha256/" + child_digest.removeprefix("sha256:"): child_payload,
    }
    if conflicting_tagged_config:
        blobs["blobs/sha256/" + unlinked_config_digest.removeprefix("sha256:")] = unlinked_config
    for _ in range(index_depth):
        child_payload = _json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": child_media_type,
                        "digest": child_digest,
                        "size": len(child_payload),
                    }
                ],
            }
        )
        child_digest = "sha256:" + hashlib.sha256(child_payload).hexdigest()
        child_media_type = "application/vnd.oci.image.index.v1+json"
        blobs["blobs/sha256/" + child_digest.removeprefix("sha256:")] = child_payload
    image_id = child_digest
    root_blob = blobs["blobs/sha256/" + image_id.removeprefix("sha256:")]
    if tamper_root_blob:
        blobs["blobs/sha256/" + image_id.removeprefix("sha256:")] = root_blob + b" "
    index = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": root_media_type or child_media_type,
                    "digest": image_id,
                    "size": len(root_blob) + root_size_delta,
                }
            ],
        }
    )
    config_name = "blobs/sha256/" + manifest_config_digest.removeprefix("sha256:")
    manifest = [{"Config": config_name, "RepoTags": [IMAGE_REF], "Layers": []}]
    if duplicate_tagged_manifest:
        manifest.append({"Config": config_name, "RepoTags": [IMAGE_REF], "Layers": []})
    if conflicting_tagged_config:
        manifest.append(
            {
                "Config": "blobs/sha256/" + unlinked_config_digest.removeprefix("sha256:"),
                "RepoTags": [IMAGE_REF],
                "Layers": [],
            }
        )
    with tarfile.open(path, "w") as archive:
        _add_tar_bytes(archive, "manifest.json", _json_bytes(manifest))
        _add_tar_bytes(archive, "index.json", index)
        for name, payload in blobs.items():
            _add_tar_bytes(archive, name, payload)
    return hashlib.sha256(path.read_bytes()).hexdigest(), image_id


def _write_semgrep_identity(root: Path) -> dict[str, Path]:
    paths = {
        "image_ref": root / "semgrep-image-ref.txt",
        "version": root / "semgrep-version.txt",
        "rules_commit": root / "semgrep-rules-commit.txt",
        "rules_tree": root / "semgrep-rules-tree.txt",
    }
    paths["image_ref"].write_text(SEMGREP_IMAGE_REF + "\n", encoding="utf-8")
    paths["version"].write_text(SEMGREP_VERSION + "\n", encoding="utf-8")
    paths["rules_commit"].write_text(SEMGREP_RULES_COMMIT + "\n", encoding="utf-8")
    paths["rules_tree"].write_text(SEMGREP_RULES_TREE + "\n", encoding="utf-8")
    return paths


def test_clean_security_gate_passes_with_required_inventories() -> None:
    report, inventory = _evaluate()
    assert report["blocking_count"] == 0
    assert report["waived_count"] == 0
    assert inventory == {"debian_package_count": 2, "python_package_count": 2}


def test_semgrep_error_is_blocking_but_warning_is_not() -> None:
    report, _ = _evaluate(
        semgrep=_semgrep(
            _semgrep_error(),
            {
                "check_id": "python.style.warning",
                "path": "apps/example.py",
                "extra": {"severity": "WARNING", "message": "non-blocking review item"},
            },
        )
    )
    assert report["blocking_count"] == 1
    finding = report["blocking_findings"][0]
    assert finding["id"] == "dangerous-subprocess-use-tainted-env-args"
    assert finding["occurrence"] == SEMGREP_OCCURRENCE


def test_semgrep_identity_and_complete_source_coverage(tmp_path: Path) -> None:
    for directory in ("apps", "scripts", "migrations"):
        (tmp_path / directory).mkdir()
    (tmp_path / "apps" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "scripts" / "b.js").write_text("console.log('b');\n", encoding="utf-8")
    (tmp_path / "migrations" / "c.py").write_text("x = 1\n", encoding="utf-8")
    identity = _write_semgrep_identity(tmp_path)
    payload = _semgrep(
        scanned=["apps/a.py", "scripts/b.js", "migrations/c.py"]
    )
    summary = validate_semgrep_identity_and_coverage(
        payload,
        source_root=tmp_path,
        image_ref_path=identity["image_ref"],
        version_path=identity["version"],
        rules_commit_path=identity["rules_commit"],
        rules_tree_path=identity["rules_tree"],
    )
    assert summary["expected_source_count"] == 3
    assert summary["scanned_path_count"] == 3

    payload["paths"]["scanned"].remove("scripts/b.js")
    with pytest.raises(RuntimeError, match="scan coverage missing 1"):
        validate_semgrep_identity_and_coverage(
            payload,
            source_root=tmp_path,
            image_ref_path=identity["image_ref"],
            version_path=identity["version"],
            rules_commit_path=identity["rules_commit"],
            rules_tree_path=identity["rules_tree"],
        )


def test_semgrep_empty_scan_or_wrong_identity_fails_closed(tmp_path: Path) -> None:
    for directory in ("apps", "scripts", "migrations"):
        (tmp_path / directory).mkdir()
    (tmp_path / "apps" / "a.py").write_text("x = 1\n", encoding="utf-8")
    identity = _write_semgrep_identity(tmp_path)
    payload = _semgrep(scanned=[])
    payload["paths"]["scanned"] = []
    with pytest.raises(RuntimeError, match="paths.scanned must be a non-empty array"):
        validate_semgrep_identity_and_coverage(
            payload,
            source_root=tmp_path,
            image_ref_path=identity["image_ref"],
            version_path=identity["version"],
            rules_commit_path=identity["rules_commit"],
            rules_tree_path=identity["rules_tree"],
        )

    identity["rules_tree"].write_text("0" * 40 + "\n", encoding="utf-8")
    payload["paths"]["scanned"] = ["apps/a.py"]
    with pytest.raises(RuntimeError, match="rules tree mismatch"):
        validate_semgrep_identity_and_coverage(
            payload,
            source_root=tmp_path,
            image_ref_path=identity["image_ref"],
            version_path=identity["version"],
            rules_commit_path=identity["rules_commit"],
            rules_tree_path=identity["rules_tree"],
        )


def test_trivy_high_and_critical_are_blocking_and_occurrence_bound() -> None:
    report, _ = _evaluate(
        trivy=_trivy(
            {
                "VulnerabilityID": "CVE-2099-0001",
                "Severity": "HIGH",
                "PkgName": "cryptography",
                "InstalledVersion": "49.0.0",
                "FixedVersion": "50.0.0",
                "Title": "high test vulnerability",
            },
            {
                "VulnerabilityID": "CVE-2099-0002",
                "Severity": "CRITICAL",
                "PkgName": "fastapi",
                "InstalledVersion": "0.141.1",
                "FixedVersion": "0.142.0",
                "Title": "critical test vulnerability",
            },
        )
    )
    assert report["blocking_count"] == 2
    first = report["blocking_findings"][0]
    assert first["occurrence"] == TRIVY_OCCURRENCE


def test_trivy_empty_or_missing_expected_inventory_fails_closed() -> None:
    kwargs = {
        "expected_artifact_name": TRIVY_INPUT_ARTIFACT_NAME,
        "expected_image_ref": IMAGE_REF,
        "expected_image_id": IMAGE_ID,
    }
    payload = _trivy()
    payload["Results"] = []
    with pytest.raises(RuntimeError, match="Results must be a non-empty array"):
        trivy_findings(payload, **kwargs)

    payload = _trivy()
    payload["Results"] = [payload["Results"][1]]
    with pytest.raises(RuntimeError, match="missing Debian OS package inventory"):
        trivy_findings(payload, **kwargs)

    payload = _trivy()
    payload["Results"] = [payload["Results"][0]]
    with pytest.raises(RuntimeError, match="missing Python package inventory"):
        trivy_findings(payload, **kwargs)

    payload = _trivy()
    payload["Results"][0]["Packages"] = []
    with pytest.raises(RuntimeError, match="Debian OS Packages must be a non-empty array"):
        trivy_findings(payload, **kwargs)


def test_waiver_requires_exact_occurrence_not_only_id_and_scope() -> None:
    finding = Finding(
        scanner="trivy",
        finding_id="CVE-2099-0100",
        raw_id="CVE-2099-0100",
        severity="HIGH",
        scope="openssl",
        occurrence="Python|lang-pkgs|python-pkg|1.0.0",
        message="test",
    )
    exact = _waiver()
    wrong_scope = _waiver(scope="libc")
    wrong_occurrence = _waiver(occurrence="Python|lang-pkgs|python-pkg|1.0.1")
    assert exact.matches(finding)
    assert not wrong_scope.matches(finding)
    assert not wrong_occurrence.matches(finding)


def test_matching_waiver_only_suppresses_one_semgrep_occurrence() -> None:
    first = _semgrep_error(start_line=164, start_col=33, end_line=164, end_col=40)
    second = _semgrep_error(start_line=200, start_col=10, end_line=200, end_col=20)
    waiver = Waiver(
        scanner="semgrep",
        finding_id="dangerous-subprocess-use-tainted-env-args",
        scope="scripts/verify_production.py",
        occurrence=SEMGREP_OCCURRENCE,
        reason="exact reviewed occurrence",
        owner="security-owner",
        created_on=date(2026, 8, 9),
        expires_on=date(2026, 9, 8),
        first_waived_on=date(2026, 8, 9),
    )
    report, _ = _evaluate(
        semgrep=_semgrep(first, second, scanned=["scripts/verify_production.py"]),
        waivers=[waiver],
    )
    assert report["waived_count"] == 1
    assert report["blocking_count"] == 1
    assert report["waived_findings"][0]["first_waived_on"] == "2026-08-09"
    assert report["blocking_findings"][0]["occurrence"] == "L200:C10-L200:C20"


def test_security_run_provenance_accepts_only_main_ref() -> None:
    pull_request = validate_run_provenance(
        event_name="pull_request_target",
        git_ref="refs/heads/main",
        candidate_sha=MAIN_SHA,
        policy_sha=POLICY_SHA,
    )
    assert pull_request == {
        "event_name": "pull_request_target",
        "git_ref": "refs/heads/main",
        "candidate_sha": MAIN_SHA,
        "policy_sha": POLICY_SHA,
    }

    for event_name in ("push", "workflow_dispatch"):
        trusted_main = validate_run_provenance(
            event_name=event_name,
            git_ref="refs/heads/main",
            candidate_sha=MAIN_SHA,
            policy_sha=MAIN_SHA,
        )
        assert trusted_main["candidate_sha"] == trusted_main["policy_sha"]


@pytest.mark.parametrize(
    ("event_name", "git_ref", "candidate_sha", "policy_sha", "message"),
    [
        (
            "workflow_dispatch",
            "refs/heads/security/test",
            MAIN_SHA,
            MAIN_SHA,
            "must target refs/heads/main",
        ),
        ("push", "refs/heads/main", MAIN_SHA, POLICY_SHA, "must use the same candidate and policy"),
        ("pull_request", "refs/heads/main", MAIN_SHA, POLICY_SHA, "unsupported security event"),
        ("push", "refs/heads/main", "short", "short", "candidate_sha must be a full Git SHA"),
    ],
)
def test_security_run_provenance_fails_closed(
    event_name: str,
    git_ref: str,
    candidate_sha: str,
    policy_sha: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_run_provenance(
            event_name=event_name,
            git_ref=git_ref,
            candidate_sha=candidate_sha,
            policy_sha=policy_sha,
        )


def _write_waivers(path: Path, waivers: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "waivers": waivers}), encoding="utf-8")


def _waiver_json(**overrides) -> dict:
    value = {
        "scanner": "trivy",
        "id": "CVE-2099-0300",
        "scope": "openssl",
        "occurrence": "Python|lang-pkgs|python-pkg|1.0.0",
        "reason": "temporary only",
        "owner": "security-owner",
        "created_on": "2026-08-09",
        "expires_on": "2026-09-08",
        "first_waived_on": "2026-08-09",
    }
    if "created_on" in overrides and "first_waived_on" not in overrides:
        value["first_waived_on"] = overrides["created_on"]
    value.update(overrides)
    return value


def test_expired_waiver_registry_fails_even_if_finding_is_absent(tmp_path: Path) -> None:
    path = tmp_path / "waivers.json"
    _write_waivers(
        path,
        [_waiver_json(created_on="2026-08-01", expires_on="2026-08-08")],
    )
    with pytest.raises(RuntimeError, match="expired security waiver"):
        load_waivers(path, today=date(2026, 8, 9))


def test_wildcard_future_or_long_lived_waivers_fail_closed(tmp_path: Path) -> None:
    wildcard = tmp_path / "wildcard.json"
    _write_waivers(wildcard, [_waiver_json(scope="*")])
    with pytest.raises(RuntimeError, match="forbidden wildcard"):
        load_waivers(wildcard, today=date(2026, 8, 9))

    wildcard_occurrence = tmp_path / "wildcard-occurrence.json"
    _write_waivers(wildcard_occurrence, [_waiver_json(occurrence="*")])
    with pytest.raises(RuntimeError, match="forbidden wildcard"):
        load_waivers(wildcard_occurrence, today=date(2026, 8, 9))

    future = tmp_path / "future.json"
    _write_waivers(
        future,
        [_waiver_json(created_on="2026-08-10", expires_on="2026-08-20")],
    )
    with pytest.raises(RuntimeError, match="future created_on"):
        load_waivers(future, today=date(2026, 8, 9))

    long_lived = tmp_path / "long-lived.json"
    _write_waivers(
        long_lived,
        [_waiver_json(created_on="2026-08-01", expires_on="2026-09-15")],
    )
    with pytest.raises(RuntimeError, match="exceeds 30d maximum"):
        load_waivers(long_lived, today=date(2026, 8, 9))


def test_invalid_or_duplicate_waivers_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    _write_waivers(missing, [{"scanner": "trivy", "id": "CVE-X"}])
    with pytest.raises(RuntimeError, match="missing fields"):
        load_waivers(missing, today=date(2026, 8, 9))

    duplicate = tmp_path / "duplicate.json"
    item = _waiver_json()
    _write_waivers(duplicate, [item, item])
    with pytest.raises(RuntimeError, match="duplicate security waiver"):
        load_waivers(duplicate, today=date(2026, 8, 9))


def test_repository_waivers_keep_explicit_first_waived_dates() -> None:
    payload = json.loads(Path("security/waivers.json").read_text(encoding="utf-8"))
    assert payload["waivers"]
    assert all("first_waived_on" in waiver for waiver in payload["waivers"])
    assert any(
        waiver["first_waived_on"] < waiver["created_on"] for waiver in payload["waivers"]
    )
    load_waivers(Path("security/waivers.json"), today=date(2026, 9, 4))


def test_semgrep_scan_errors_and_schema_damage_fail_closed() -> None:
    with pytest.raises(RuntimeError, match="Semgrep reported scan errors"):
        semgrep_findings({"results": [], "errors": [{"message": "registry unavailable"}]})
    with pytest.raises(RuntimeError, match="results array"):
        semgrep_findings({"errors": []})
    with pytest.raises(RuntimeError, match="errors array"):
        semgrep_findings({"results": []})
    with pytest.raises(RuntimeError, match="missing severity"):
        semgrep_findings(
            {
                "results": [
                    {
                        "check_id": "broken",
                        "path": "example.py",
                        "extra": {},
                    }
                ],
                "errors": [],
            }
        )
    with pytest.raises(RuntimeError, match="missing start/end location"):
        semgrep_findings(
            {
                "results": [
                    {
                        "check_id": "security.rule",
                        "path": "example.py",
                        "extra": {"severity": "ERROR"},
                    }
                ],
                "errors": [],
            }
        )


def test_trivy_schema_damage_or_subject_mismatch_fail_closed() -> None:
    kwargs = {
        "expected_artifact_name": TRIVY_INPUT_ARTIFACT_NAME,
        "expected_image_ref": IMAGE_REF,
        "expected_image_id": IMAGE_ID,
    }
    with pytest.raises(RuntimeError, match="SchemaVersion"):
        trivy_findings({}, **kwargs)
    with pytest.raises(RuntimeError, match="artifact mismatch"):
        payload = _trivy()
        payload["ArtifactName"] = "other.tar"
        trivy_findings(payload, **kwargs)
    with pytest.raises(RuntimeError, match="ImageID"):
        payload = _trivy()
        payload["Metadata"]["ImageID"] = "sha256:" + "2" * 64
        trivy_findings(payload, **kwargs)
    with pytest.raises(RuntimeError, match="RepoTags"):
        payload = _trivy()
        payload["Metadata"]["RepoTags"] = ["other:image"]
        trivy_findings(payload, **kwargs)
    with pytest.raises(RuntimeError, match="Vulnerabilities must be null or an array"):
        payload = _trivy()
        payload["Results"][0]["Vulnerabilities"] = {}
        trivy_findings(payload, **kwargs)


def test_scan_subject_binds_hash_manifest_tag_and_config(tmp_path: Path) -> None:
    archive = tmp_path / "app-image.tar"
    digest = _write_image_archive(archive)
    subject = validate_scan_subject(
        {"image_ref": IMAGE_REF, "image_id": IMAGE_ID, "archive_sha256": digest},
        archive_path=archive,
    )
    assert subject["archive_sha256"] == digest
    assert subject["trivy_image_id"] == IMAGE_ID
    assert subject["trivy_artifact_name"] == TRIVY_INPUT_ARTIFACT_NAME

    archive.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="archive SHA-256 mismatch"):
        validate_scan_subject(
            {"image_ref": IMAGE_REF, "image_id": IMAGE_ID, "archive_sha256": digest},
            archive_path=archive,
        )


def test_scan_subject_accepts_digest_linked_oci_archive(tmp_path: Path) -> None:
    archive = tmp_path / "app-image-oci.tar"
    digest, image_id = _write_oci_image_archive(archive)

    subject = validate_scan_subject(
        {"image_ref": IMAGE_REF, "image_id": image_id, "archive_sha256": digest},
        archive_path=archive,
    )

    assert subject["image_id"] == image_id
    assert subject["trivy_image_id"] == IMAGE_ID


def test_scan_subject_v2_binds_runtime_manifest_to_canonical_config(tmp_path: Path) -> None:
    archive = tmp_path / "app-image-oci-v2.tar"
    digest, manifest_digest = _write_oci_image_archive(archive)

    subject = validate_scan_subject(
        {
            "schema_version": 2,
            "image_ref": IMAGE_REF,
            "image_id": IMAGE_ID,
            "runtime_image_id": manifest_digest,
            "manifest_digest": manifest_digest,
            "archive_sha256": digest,
        },
        archive_path=archive,
    )

    assert subject["schema_version"] == 2
    assert subject["image_id"] == IMAGE_ID
    assert subject["runtime_image_id"] == manifest_digest
    assert subject["manifest_digest"] == manifest_digest
    assert subject["trivy_image_id"] == IMAGE_ID


def test_scan_subject_v2_allows_duplicate_tag_entries_for_same_config(tmp_path: Path) -> None:
    archive = tmp_path / "app-image-oci-v2-duplicate-tag.tar"
    digest, manifest_digest = _write_oci_image_archive(
        archive,
        duplicate_tagged_manifest=True,
    )

    subject = validate_scan_subject(
        {
            "schema_version": 2,
            "image_ref": IMAGE_REF,
            "image_id": IMAGE_ID,
            "runtime_image_id": manifest_digest,
            "manifest_digest": manifest_digest,
            "archive_sha256": digest,
        },
        archive_path=archive,
    )

    assert subject["trivy_image_id"] == IMAGE_ID


def test_scan_subject_v2_rejects_same_tag_with_conflicting_configs(tmp_path: Path) -> None:
    archive = tmp_path / "app-image-oci-v2-conflicting-tag.tar"
    digest, manifest_digest = _write_oci_image_archive(
        archive,
        conflicting_tagged_config=True,
    )

    with pytest.raises(RuntimeError, match="exactly one config identity"):
        validate_scan_subject(
            {
                "schema_version": 2,
                "image_ref": IMAGE_REF,
                "image_id": IMAGE_ID,
                "runtime_image_id": manifest_digest,
                "manifest_digest": manifest_digest,
                "archive_sha256": digest,
            },
            archive_path=archive,
        )


def test_scan_subject_v2_rejects_manifest_not_linked_to_config(tmp_path: Path) -> None:
    archive = tmp_path / "app-image-oci-v2-wrong-manifest.tar"
    digest, manifest_digest = _write_oci_image_archive(archive)

    with pytest.raises(RuntimeError, match="manifest_digest"):
        validate_scan_subject(
            {
                "schema_version": 2,
                "image_ref": IMAGE_REF,
                "image_id": IMAGE_ID,
                "runtime_image_id": "sha256:" + "f" * 64,
                "manifest_digest": "sha256:" + "f" * 64,
                "archive_sha256": digest,
            },
            archive_path=archive,
        )

    with pytest.raises(RuntimeError, match="runtime_image_id"):
        validate_scan_subject(
            {
                "schema_version": 2,
                "image_ref": IMAGE_REF,
                "image_id": IMAGE_ID,
                "runtime_image_id": "sha256:" + "e" * 64,
                "manifest_digest": manifest_digest,
                "archive_sha256": digest,
            },
            archive_path=archive,
        )


def test_scan_subject_rejects_oci_config_not_linked_from_image_id(tmp_path: Path) -> None:
    archive = tmp_path / "app-image-oci-unlinked.tar"
    digest, image_id = _write_oci_image_archive(archive, link_manifest_config=False)

    with pytest.raises(RuntimeError, match="is not linked from ImageID"):
        validate_scan_subject(
            {"image_ref": IMAGE_REF, "image_id": image_id, "archive_sha256": digest},
            archive_path=archive,
        )


@pytest.mark.parametrize(
    ("archive_kwargs", "message"),
    [
        ({"tamper_root_blob": True}, "blob digest mismatch"),
        ({"root_size_delta": 1}, "blob size mismatch"),
        ({"root_media_type": "application/vnd.example.unsupported"}, "unsupported OCI mediaType"),
        ({"index_depth": 6}, "nesting exceeds four levels"),
    ],
)
def test_scan_subject_rejects_malformed_oci_descriptor_chains(
    tmp_path: Path,
    archive_kwargs: dict[str, object],
    message: str,
) -> None:
    archive = tmp_path / "app-image-oci-malformed.tar"
    digest, image_id = _write_oci_image_archive(archive, **archive_kwargs)

    with pytest.raises(RuntimeError, match=message):
        validate_scan_subject(
            {"image_ref": IMAGE_REF, "image_id": image_id, "archive_sha256": digest},
            archive_path=archive,
        )


def test_scan_subject_rejects_wrong_archive_tag_or_config(tmp_path: Path) -> None:
    wrong_tag = tmp_path / "wrong-tag.tar"
    wrong_tag_digest = _write_image_archive(wrong_tag, image_ref="other:image")
    with pytest.raises(RuntimeError, match="exactly one manifest entry"):
        validate_scan_subject(
            {"image_ref": IMAGE_REF, "image_id": IMAGE_ID, "archive_sha256": wrong_tag_digest},
            archive_path=wrong_tag,
        )

    wrong_config = tmp_path / "wrong-config.tar"
    wrong_config_digest = _write_image_archive(wrong_config, config_name="2" * 64 + ".json")
    with pytest.raises(RuntimeError, match="config mismatch"):
        validate_scan_subject(
            {"image_ref": IMAGE_REF, "image_id": IMAGE_ID, "archive_sha256": wrong_config_digest},
            archive_path=wrong_config,
        )


def test_scan_subject_requires_full_sha256_image_id(tmp_path: Path) -> None:
    archive = tmp_path / "app-image.tar"
    digest = _write_image_archive(archive)
    for bad_image_id in ("sha256:short", "sha256:" + "G" * 64, "not-a-sha256"):
        with pytest.raises(RuntimeError, match="image_id must be sha256"):
            validate_scan_subject(
                {"image_ref": IMAGE_REF, "image_id": bad_image_id, "archive_sha256": digest},
                archive_path=archive,
            )


def test_security_workflow_scans_files_ignored_by_semgrep_defaults() -> None:
    workflow = Path(__file__).parents[3] / ".github" / "workflows" / "security-analysis.yml"
    source = workflow.read_text(encoding="utf-8")

    assert source.count("--x-ignore-semgrepignore-files") == 1


def test_security_workflow_emits_v2_runtime_identity_subject() -> None:
    workflow = Path(__file__).parents[3] / ".github" / "workflows" / "security-analysis.yml"
    source = workflow.read_text(encoding="utf-8")

    assert '"schema_version": 2' in source
    assert '"runtime_image_id": runtime_image_id' in source
    assert '"manifest_digest": manifest_digest' in source
    assert "runtime_image_id not in {image_id, manifest_digest}" in source


def test_security_workflow_pins_third_party_actions_to_full_commits() -> None:
    workflow = Path(__file__).parents[3] / ".github" / "workflows" / "security-analysis.yml"
    source = workflow.read_text(encoding="utf-8")
    action_refs = re.findall(r"^\s+uses:\s+([^\s#]+)", source, flags=re.MULTILINE)

    assert action_refs == [
        "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
        "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
        "actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4",
        "actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4",
    ]


def test_security_workflow_executes_policy_from_protected_base() -> None:
    workflow = Path(__file__).parents[3] / ".github" / "workflows" / "security-analysis.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "pull_request_target:" in source
    assert re.search(r"^\s+pull_request:\s*$", source, flags=re.MULTILINE) is None
    assert "ref: ${{ github.event.pull_request.base.sha || github.sha }}" in source
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in source
    assert "persist-credentials: false" in source
    assert "python ../policy/scripts/check_security_gate.py" in source
    assert "--waivers ../policy/security/waivers.json" in source
    assert "if: github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main'" in source
    assert "--event-name \"${{ github.event_name }}\"" in source
    assert "--git-ref \"${{ github.ref }}\"" in source
    assert "--candidate-sha \"$CANDIDATE_SHA\"" in source
    assert "--policy-sha \"$POLICY_SHA\"" in source
    assert "waivers-policy.json" in source
    assert "success() && github.ref == 'refs/heads/main'" in source
    assert "github.event_name == 'push' ||" in source
    assert "github.event_name == 'workflow_dispatch' && github.run_attempt == '1'" in source
    assert "github.actor == github.repository_owner" in source
    assert "github.triggering_actor == github.repository_owner" in source
    assert "github.event.repository.owner.type == 'User'" in source
    assert "github.actor_id == format('{0}', github.event.repository.owner.id)" in source


def test_browser_entrypoints_load_safe_html_boundary_first() -> None:
    root = Path(__file__).parents[3]
    entrypoints = {
        "apps/admin/public/v12-operations.html": "/h5/safe-html.js",
        "apps/admin/public/h5/index.html": "/h5/safe-html.js",
        "apps/call-h5/public/index.html": "/h5/safe-html.js",
        "apps/h5/public/v12-workbench.html": "./safe-html.js",
    }

    for relative, sanitizer in entrypoints.items():
        source = (root / relative).read_text(encoding="utf-8")
        assert source.count(sanitizer) == 1
        script_sources = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', source)
        # P2-3：引用可带 ?v= 缓存版本参数——比较剥去版本后缀，递增不挂本测试。
        assert script_sources[0].split("?")[0] == sanitizer


def _write_cli_evidence(root: Path) -> tuple[list[str], Path]:
    for directory in ("apps", "scripts", "migrations"):
        (root / directory).mkdir()
    (root / "apps" / "example.py").write_text("value = 1\n", encoding="utf-8")
    identity = _write_semgrep_identity(root)

    semgrep_path = root / "semgrep.json"
    semgrep_path.write_text(
        json.dumps(_semgrep(scanned=["apps/example.py"])),
        encoding="utf-8",
    )
    trivy_path = root / "trivy.json"
    trivy_path.write_text(json.dumps(_trivy()), encoding="utf-8")
    sbom_path = root / "sbom.json"
    sbom_path.write_text(json.dumps(_sbom()), encoding="utf-8")
    archive_path = root / "app-image.tar"
    archive_digest = _write_image_archive(archive_path)
    subject_path = root / "subject.json"
    subject_path.write_text(
        json.dumps(
            {
                "image_ref": IMAGE_REF,
                "image_id": IMAGE_ID,
                "archive_sha256": archive_digest,
            }
        ),
        encoding="utf-8",
    )
    waivers_path = root / "waivers.json"
    _write_waivers(waivers_path, [])
    semgrep_exit = root / "semgrep-exit.txt"
    semgrep_exit.write_text("0\n", encoding="utf-8")
    trivy_exit = root / "trivy-exit.txt"
    trivy_exit.write_text("0\n", encoding="utf-8")
    output = root / "gate.json"
    args = [
        "check_security_gate.py",
        "--semgrep",
        str(semgrep_path),
        "--semgrep-exit",
        str(semgrep_exit),
        "--semgrep-image-ref",
        str(identity["image_ref"]),
        "--semgrep-version",
        str(identity["version"]),
        "--semgrep-rules-commit",
        str(identity["rules_commit"]),
        "--semgrep-rules-tree",
        str(identity["rules_tree"]),
        "--source-root",
        str(root),
        "--trivy",
        str(trivy_path),
        "--trivy-exit",
        str(trivy_exit),
        "--sbom",
        str(sbom_path),
        "--subject",
        str(subject_path),
        "--image-archive",
        str(archive_path),
        "--waivers",
        str(waivers_path),
        "--event-name",
        "push",
        "--git-ref",
        "refs/heads/main",
        "--candidate-sha",
        MAIN_SHA,
        "--policy-sha",
        MAIN_SHA,
        "--output",
        str(output),
        "--today",
        "2026-08-10",
    ]
    return args, output


def test_security_gate_cli_accepts_complete_valid_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, output = _write_cli_evidence(tmp_path)
    monkeypatch.setattr(sys, "argv", args)

    assert main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["blocking_count"] == 0
    assert report["semgrep"]["expected_source_count"] == 1
    assert report["provenance"] == {
        "event_name": "push",
        "git_ref": "refs/heads/main",
        "candidate_sha": MAIN_SHA,
        "policy_sha": MAIN_SHA,
        "waiver_registry_sha256": hashlib.sha256(
            (tmp_path / "waivers.json").read_bytes()
        ).hexdigest(),
    }


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("--semgrep-exit", "3\n", "Semgrep scanner failed with exit code 3"),
        ("--trivy-exit", "125\n", "Trivy scanner/SBOM failed with exit code 125"),
        ("--semgrep-exit", "not-an-exit-code\n", "invalid scanner exit code"),
    ],
)
def test_security_gate_cli_fails_closed_on_scanner_exit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
    value: str,
    message: str,
) -> None:
    semgrep_exit = tmp_path / "semgrep-exit.txt"
    trivy_exit = tmp_path / "trivy-exit.txt"
    semgrep_exit.write_text("0\n", encoding="utf-8")
    trivy_exit.write_text("0\n", encoding="utf-8")
    target = semgrep_exit if argument == "--semgrep-exit" else trivy_exit
    target.write_text(value, encoding="utf-8")
    waivers = tmp_path / "waivers.json"
    _write_waivers(waivers, [])
    output = tmp_path / "gate.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_security_gate.py",
            "--semgrep-exit",
            str(semgrep_exit),
                "--trivy-exit",
                str(trivy_exit),
                "--waivers",
                str(waivers),
                "--event-name",
                "push",
                "--git-ref",
                "refs/heads/main",
                "--candidate-sha",
                MAIN_SHA,
                "--policy-sha",
                MAIN_SHA,
                "--output",
            str(output),
            "--today",
            "2026-08-10",
        ],
    )

    assert main() == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert message in report["error"]
    assert report["provenance_valid"] is True
    assert report["provenance"]["candidate_sha"] == MAIN_SHA
    assert report["provenance"]["waiver_registry_sha256"] == hashlib.sha256(
        waivers.read_bytes()
    ).hexdigest()


def test_sbom_must_be_substantive_cyclonedx_with_valid_dependency_graph() -> None:
    sbom = _sbom()
    kwargs = {
        "expected_artifact_name": TRIVY_INPUT_ARTIFACT_NAME,
        "expected_image_ref": IMAGE_REF,
        "expected_image_id": IMAGE_ID,
        "expected_package_purls": {
            "pkg:deb/debian/base-files@12.4",
            "pkg:deb/debian/libc6@2.36",
            "pkg:pypi/cryptography@49.0.0",
            "pkg:pypi/fastapi@0.141.1",
        },
    }
    summary = validate_sbom(sbom, **kwargs)
    assert summary["component_count"] == 5
    assert summary["dependency_count"] == 6
    assert summary["generator"] == "trivy/0.70.0"

    for bad_component in (None, {}):
        damaged = json.loads(json.dumps(sbom))
        damaged["components"] = [bad_component]
        with pytest.raises(RuntimeError, match="SBOM component #1"):
            validate_sbom(damaged, **kwargs)

    duplicate = json.loads(json.dumps(sbom))
    duplicate["components"][1]["bom-ref"] = duplicate["components"][0]["bom-ref"]
    with pytest.raises(RuntimeError, match="duplicate component bom-ref"):
        validate_sbom(duplicate, **kwargs)

    no_os = json.loads(json.dumps(sbom))
    no_os["components"] = no_os["components"][1:]
    no_os["dependencies"][0]["dependsOn"].remove("os-ref")
    no_os["dependencies"] = [item for item in no_os["dependencies"] if item["ref"] != "os-ref"]
    with pytest.raises(RuntimeError, match="missing operating-system"):
        validate_sbom(no_os, **kwargs)

    truncated = json.loads(json.dumps(sbom))
    truncated["components"] = [
        item for item in truncated["components"] if item["bom-ref"] != "cryptography-ref"
    ]
    truncated["dependencies"][0]["dependsOn"].remove("cryptography-ref")
    truncated["dependencies"] = [
        item for item in truncated["dependencies"] if item["ref"] != "cryptography-ref"
    ]
    with pytest.raises(RuntimeError, match="SBOM package inventory mismatch"):
        validate_sbom(truncated, **kwargs)

    unknown_dependency = json.loads(json.dumps(sbom))
    unknown_dependency["dependencies"][0]["dependsOn"].append("unknown-ref")
    with pytest.raises(RuntimeError, match="unknown refs"):
        validate_sbom(unknown_dependency, **kwargs)


def test_sbom_identity_and_generator_damage_fail_closed() -> None:
    sbom = _sbom()
    kwargs = {
        "expected_artifact_name": TRIVY_INPUT_ARTIFACT_NAME,
        "expected_image_ref": IMAGE_REF,
        "expected_image_id": IMAGE_ID,
        "expected_package_purls": {
            "pkg:deb/debian/base-files@12.4",
            "pkg:deb/debian/libc6@2.36",
            "pkg:pypi/cryptography@49.0.0",
            "pkg:pypi/fastapi@0.141.1",
        },
    }
    wrong_tool_version = json.loads(json.dumps(sbom))
    wrong_tool_version["metadata"]["tools"]["components"][0]["version"] = "0.69.0"
    with pytest.raises(RuntimeError, match="Trivy version mismatch"):
        validate_sbom(wrong_tool_version, **kwargs)

    duplicate_reference = json.loads(json.dumps(sbom))
    duplicate_reference["metadata"]["component"]["properties"].append(
        {"name": "aquasecurity:trivy:Reference", "value": IMAGE_REF}
    )
    with pytest.raises(RuntimeError, match="must contain exactly"):
        validate_sbom(duplicate_reference, **kwargs)

def test_waiver_total_span_hard_cap_blocks_perpetual_renewal(tmp_path: Path) -> None:
    """waivers 门禁：续期不得重置累计起点——first_waived_on 与 expires_on 的
    总跨度超过 90 天硬上限时 fail-closed；缺失首次日期也必须拒绝。"""

    renewed = tmp_path / "renewed.json"
    _write_waivers(
        renewed,
        [
            _waiver_json(
                created_on="2026-09-01",
                expires_on="2026-09-30",
                first_waived_on="2026-06-01",
            )
        ],
    )
    with pytest.raises(RuntimeError, match="hard cap"):
        load_waivers(renewed, today=date(2026, 9, 2))

    missing_first = tmp_path / "missing-first.json"
    waiver_without_first = _waiver_json()
    waiver_without_first.pop("first_waived_on")
    _write_waivers(missing_first, [waiver_without_first])
    with pytest.raises(RuntimeError, match="missing fields: first_waived_on"):
        load_waivers(missing_first, today=date(2026, 8, 9))

    late_first = tmp_path / "late-first.json"
    _write_waivers(late_first, [_waiver_json(first_waived_on="2026-08-12")])
    with pytest.raises(RuntimeError, match="first_waived_on .* later than created_on"):
        load_waivers(late_first, today=date(2026, 8, 9))

    malformed = tmp_path / "malformed-first.json"
    _write_waivers(malformed, [_waiver_json(first_waived_on="not-a-date")])
    with pytest.raises(RuntimeError, match="first_waived_on must be YYYY-MM-DD"):
        load_waivers(malformed, today=date(2026, 8, 9))
