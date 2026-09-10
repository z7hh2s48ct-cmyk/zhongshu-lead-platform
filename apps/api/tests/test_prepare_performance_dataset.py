from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePosixPath

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from apps.api.src.core.enums import AssignmentStatus, PointsLedgerType
from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, PointsLedger, ReturnRequest, User
from apps.api.src.core.security import verify_password
from apps.api.src.core.v12_enums import LeadV12Status, ReturnV12Status
from apps.api.src.services.dispatch_v12 import claim_assignment, dispatch_manually, evaluate_candidate
from apps.api.src.services.reconciliation_v12 import reconcile_v12
from apps.api.src.services.rbac import seed_rbac
from scripts.performance_v12 import validate_dataset
from scripts.prepare_performance_dataset import (
    DatasetCredentials,
    credentials_for,
    persist_dataset,
    prepare_dataset,
    validate_credentials_storage,
    validate_database_target,
    validate_runtime_secrets,
    write_outputs,
)


PROFILES = (2, 3, 4)


def _credentials() -> DatasetCredentials:
    return DatasetCredentials(
        operator_username="h04_test_operation",
        operator_password="H04!operator-test-password",
        receiver_username="h04_test_receiver",
        receiver_password="H04!receiver-test-password",
        owner_username="h04_test_owner",
        owner_password="H04!owner-test-password",
    )


def test_prepare_dataset_creates_an_isolated_reconciled_tenant(db) -> None:
    credentials = _credentials()
    dataset = prepare_dataset(
        db,
        dataset_id="capacity-test",
        base_url_origin="http://127.0.0.1:18080",
        environment="staging-equivalent",
        profiles=PROFILES,
        credentials=credentials,
        initial_points=10_000,
    )
    db.commit()
    validate_dataset(dataset, profiles=PROFILES, runtime=True)

    company = db.scalar(select(Company).where(Company.id == dataset["dispatch_company_id"]))
    assert company is not None and company.code == "H04-CAPACITY-TEST"
    users = {
        user.username: user
        for user in db.scalars(
            select(User).where(
                User.username.in_(
                    {
                        credentials.operator_username,
                        credentials.receiver_username,
                        credentials.owner_username,
                    }
                )
            )
        ).all()
    }
    assert users[credentials.receiver_username].company_id == company.id
    assert verify_password(credentials.operator_password, users[credentials.operator_username].password_hash)
    assert verify_password(credentials.receiver_password, users[credentials.receiver_username].password_hash)
    assert verify_password(credentials.owner_password, users[credentials.owner_username].password_hash)

    evidence_count = sum(PROFILES)
    assert db.scalar(select(func.count(Lead.id)).where(Lead.source_app_token == "h04:capacity-test")) == evidence_count + 6
    assert db.scalar(select(func.count(Assignment.id)).where(Assignment.company_id == company.id)) == evidence_count + 3
    assert db.scalar(select(func.count(ReturnRequest.id)).where(ReturnRequest.company_id == company.id)) == evidence_count
    assert db.scalar(
        select(func.count(ReturnRequest.id)).where(
            ReturnRequest.company_id == company.id,
            ReturnRequest.status == ReturnV12Status.DRAFT.value,
        )
    ) == evidence_count
    assert db.scalar(
        select(func.count(Assignment.id)).where(
            Assignment.company_id == company.id,
            Assignment.status == AssignmentStatus.PENDING_CLAIM.value,
        )
    ) == 3
    assert db.scalar(
        select(func.count(Lead.id)).where(
            Lead.source_app_token == "h04:capacity-test",
            Lead.status == LeadV12Status.READY_DISPATCH.value,
        )
    ) == 3

    account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company.id))
    assert account is not None
    ledger_total = db.scalar(
        select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(PointsLedger.account_id == account.id)
    )
    assert account.balance == 10_000 - evidence_count * 100
    assert ledger_total == account.balance
    assert db.scalar(
        select(func.count(PointsLedger.id)).where(
            PointsLedger.account_id == account.id,
            PointsLedger.ledger_type == PointsLedgerType.CLAIM.value,
        )
    ) == evidence_count
    assert len({item["lead_id"] for item in dataset["dispatch_cases"].values()}) == len(PROFILES)
    assert len(set(dataset["claim_cases"].values())) == len(PROFILES)
    assert len({item for values in dataset["evidence_cases"].values() for item in values}) == evidence_count

    dispatch_lead = db.get(Lead, dataset["dispatch_cases"]["2"]["lead_id"])
    candidate = evaluate_candidate(db, lead=dispatch_lead, company=company)
    assert candidate.eligible is True
    balance_before_claim = int(account.balance)
    dispatched = dispatch_manually(
        db,
        lead_id=dispatch_lead.id,
        company_id=company.id,
        assigned_by=users[credentials.operator_username].id,
        idempotency_key=dataset["dispatch_cases"]["2"]["idempotency_key"],
    )
    claimed = claim_assignment(
        db,
        assignment_id=dataset["claim_cases"]["2"],
        company_id=company.id,
        claimed_by=users[credentials.receiver_username].id,
    )
    db.commit()
    assert dispatched.status == AssignmentStatus.PENDING_CLAIM.value
    assert claimed.idempotent is False
    assert db.get(PointsAccount, account.id).balance == balance_before_claim - 100
    reconciliation = reconcile_v12(db, require_completed_backfill=False)
    assert reconciliation.valid, reconciliation.to_dict()


def test_prepare_dataset_embeds_only_an_explicit_approved_claim_baseline(db) -> None:
    baseline = {
        "approved": True,
        "p95_limit_ms": 5000.0,
        "approval_reference": "https://github.com/peihr666-max/zhongshu-lead-platform/issues/71",
    }
    dataset = prepare_dataset(
        db,
        dataset_id="capacity-baseline",
        base_url_origin="http://127.0.0.1:18080",
        environment="staging-equivalent",
        profiles=PROFILES,
        credentials=_credentials(),
        initial_points=10_000,
        claim_baseline=baseline,
    )

    assert dataset["claim_baseline"] == baseline
    validate_dataset(dataset, profiles=PROFILES, runtime=True)


def test_prepare_dataset_refuses_to_reuse_an_existing_dataset(db) -> None:
    kwargs = {
        "dataset_id": "capacity-test",
        "base_url_origin": "http://127.0.0.1:18080",
        "environment": "staging-equivalent",
        "profiles": PROFILES,
        "credentials": _credentials(),
        "initial_points": 10_000,
    }
    prepare_dataset(db, **kwargs)
    db.commit()
    before = db.scalar(select(func.count(Lead.id)))
    with pytest.raises(ValueError, match="already exists"):
        prepare_dataset(db, **kwargs)
    assert db.scalar(select(func.count(Lead.id))) == before


def test_prepare_dataset_validates_scope_and_available_points(db) -> None:
    common = {
        "db": db,
        "base_url_origin": "http://127.0.0.1:18080",
        "environment": "staging-equivalent",
        "profiles": PROFILES,
        "credentials": _credentials(),
        "initial_points": 10_000,
    }
    with pytest.raises(ValueError, match="dataset id"):
        prepare_dataset(dataset_id="INVALID", **common)
    with pytest.raises(ValueError, match="initial points"):
        prepare_dataset(dataset_id="too-few-points", **{**common, "initial_points": 1})


def test_write_outputs_keeps_credentials_out_of_the_dataset(tmp_path: Path) -> None:
    credentials = _credentials()
    dataset = {"schema_version": 1, "dataset_id": "capacity-test", "synthetic_data": True}
    dataset_path = tmp_path / "dataset.json"
    credentials_path = tmp_path / "credentials.env"
    write_outputs(
        dataset=dataset,
        credentials=credentials,
        dataset_path=dataset_path,
        credentials_path=credentials_path,
    )
    dataset_text = dataset_path.read_text(encoding="utf-8")
    credentials_text = credentials_path.read_text(encoding="utf-8")
    assert "password" not in dataset_text.lower()
    assert credentials.operator_password not in dataset_text
    assert f"V12_PERF_OPERATOR_USERNAME={credentials.operator_username}" in credentials_text
    assert f"V12_PERF_RECEIVER_PASSWORD={credentials.receiver_password}" in credentials_text


def test_database_target_must_be_explicit_and_non_production() -> None:
    validate_database_target(
        app_env="staging",
        current_database="zhongshu_staging",
        expected_database="zhongshu_staging",
        database_environment="staging-performance",
    )
    with pytest.raises(ValueError, match="APP_ENV=staging"):
        validate_database_target(
            app_env="production",
            current_database="zhongshu",
            expected_database="zhongshu",
            database_environment="staging-performance",
        )
    with pytest.raises(ValueError, match="APP_ENV=staging"):
        validate_database_target(
            app_env="development",
            current_database="zhongshu_staging",
            expected_database="zhongshu_staging",
            database_environment="staging-performance",
        )
    with pytest.raises(ValueError, match="does not match"):
        validate_database_target(
            app_env="staging",
            current_database="unexpected_database",
            expected_database="zhongshu_staging",
            database_environment="staging-performance",
        )
    with pytest.raises(ValueError, match="environment_classification"):
        validate_database_target(
            app_env="staging",
            current_database="zhongshu_staging",
            expected_database="zhongshu_staging",
            database_environment="",
        )


def test_runtime_secrets_must_be_explicit_strong_and_distinct() -> None:
    validate_runtime_secrets(
        field_encryption_key="field-encryption-key-0123456789abcdef",
        phone_hash_secret="phone-hash-secret-0123456789abcdefghi",
        phone_fingerprint_secret="phone-fingerprint-secret-0123456789abc",
    )
    with pytest.raises(ValueError, match="FIELD_ENCRYPTION_KEY"):
        validate_runtime_secrets(
            field_encryption_key="dev-only-key-change-in-production",
            phone_hash_secret="phone-hash-secret-0123456789abcdefghi",
            phone_fingerprint_secret="phone-fingerprint-secret-0123456789abc",
        )
    with pytest.raises(ValueError, match="PHONE_FINGERPRINT_SECRET"):
        validate_runtime_secrets(
            field_encryption_key="field-encryption-key-0123456789abcdef",
            phone_hash_secret="phone-hash-secret-0123456789abcdefghi",
            phone_fingerprint_secret="",
        )
    with pytest.raises(ValueError, match="distinct"):
        validate_runtime_secrets(
            field_encryption_key="same-secret-0123456789abcdefghijkl",
            phone_hash_secret="same-secret-0123456789abcdefghijkl",
            phone_fingerprint_secret="different-secret-0123456789abcdefgh",
        )


def test_credentials_output_requires_posix_permissions() -> None:
    validate_credentials_storage(
        platform_name="posix",
        credentials_path=PurePosixPath("/tmp/h04-credentials.env"),
        running_in_container=True,
    )
    with pytest.raises(ValueError, match="POSIX container"):
        validate_credentials_storage(
            platform_name="nt",
            credentials_path=PurePosixPath("/tmp/h04-credentials.env"),
            running_in_container=True,
        )
    with pytest.raises(ValueError, match="POSIX container"):
        validate_credentials_storage(
            platform_name="posix",
            credentials_path=PurePosixPath("/tmp/h04-credentials.env"),
            running_in_container=False,
        )
    for unsafe_path in (
        PurePosixPath("credentials.env"),
        PurePosixPath("/var/tmp/credentials.env"),
        PurePosixPath("/tmp/../credentials.env"),
    ):
        with pytest.raises(ValueError, match="below /tmp"):
            validate_credentials_storage(
                platform_name="posix",
                credentials_path=unsafe_path,
                running_in_container=True,
            )


class _RecordingSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")

    def rollback(self) -> None:
        self.rollbacks += 1


def test_persist_dataset_rejects_existing_outputs(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.json"
    credentials_path = tmp_path / "credentials.env"
    dataset_path.write_text("existing", encoding="utf-8")
    db = _RecordingSession()
    with pytest.raises(ValueError, match="distinct new files"):
        persist_dataset(
            db,  # type: ignore[arg-type]
            dataset={"schema_version": 1},
            credentials=_credentials(),
            dataset_path=dataset_path,
            credentials_path=credentials_path,
        )
    assert db.commits == 0 and db.rollbacks == 0


def test_persist_dataset_cleans_outputs_and_rolls_back_on_commit_failure(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.json"
    credentials_path = tmp_path / "credentials.env"
    db = _RecordingSession(fail_commit=True)
    with pytest.raises(RuntimeError, match="commit failed"):
        persist_dataset(
            db,  # type: ignore[arg-type]
            dataset={"schema_version": 1},
            credentials=_credentials(),
            dataset_path=dataset_path,
            credentials_path=credentials_path,
        )
    assert db.commits == 1 and db.rollbacks == 1
    assert not dataset_path.exists() and not credentials_path.exists()


@pytest.mark.skipif(
    not os.environ.get("H04_POSTGRES_DATASET_TEST_URL"),
    reason="set H04_POSTGRES_DATASET_TEST_URL to run the PostgreSQL dataset integration test",
)
def test_prepare_dataset_flush_order_on_postgresql() -> None:
    engine = create_engine(os.environ["H04_POSTGRES_DATASET_TEST_URL"], pool_pre_ping=True)
    dataset_id = f"h04-pg-{uuid.uuid4().hex[:12]}"
    try:
        assert engine.dialect.name == "postgresql"
        with Session(engine) as db:
            seed_rbac(db)
            dataset = prepare_dataset(
                db,
                dataset_id=dataset_id,
                base_url_origin="http://127.0.0.1:18080",
                environment="staging-equivalent",
                profiles=PROFILES,
                credentials=credentials_for(dataset_id),
                initial_points=10_000,
            )
            assert db.scalar(
                select(func.count(Assignment.id)).where(
                    Assignment.company_id == dataset["dispatch_company_id"]
                )
            ) == sum(PROFILES) + 3
            db.rollback()
    finally:
        engine.dispose()
