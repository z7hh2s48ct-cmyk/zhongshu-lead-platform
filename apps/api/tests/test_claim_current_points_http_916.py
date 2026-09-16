from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, select

from apps.api.src.core.models import Assignment, Company, Lead, PointsAccount, PointsLedger, User
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _save_points(client, headers: dict[str, str], points: int, version: int) -> None:
    response = client.put(
        "/api/v1/v1.2/admin/lead-points-settings",
        headers=headers,
        json={
            "operation_claim_points": points,
            "supplier_provision_points": 30,
            "expected_version": version,
        },
    )
    assert response.status_code == 200, response.text


def _pending_assignment(
    db,
    *,
    company: Company,
    assigned_by: User,
    source_kind: str,
    phone: str,
) -> Assignment:
    now = datetime.now(timezone.utc)
    lead = Lead(
        source_type=source_kind,
        source_kind=source_kind,
        submitter_user_id=assigned_by.id,
        customer_name=f"实时领取价-{source_kind}",
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        phone_fingerprint=fingerprint_phone(phone),
        consent_confirmed=True,
        city="上海市",
        region_code="310000",
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        need_summary="验证后台改价后立即生效",
        status=LeadV12Status.DISPATCHED.value,
        review_status="APPROVED",
        duplicate_status="CLEAR",
        imported_at=now,
        submitted_at=now,
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        receiver_company_id=company.id,
        status="PENDING_CLAIM",
        points_price=100,
        claim_points=100,
        lead_snapshot={"phone_masked": "139****9160"},
        assigned_by=assigned_by.id,
        assigned_at=now,
        expires_at=now + timedelta(hours=24),
        idempotency_key=f"current-price-{source_kind}-{phone}",
    )
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    return assignment


def _ensure_lead_receiver_capability(db, company_id: str) -> None:
    existing = db.scalar(
        select(CompanyLeadCapability).where(
            CompanyLeadCapability.company_id == company_id,
            CompanyLeadCapability.capability_code == "LEAD_RECEIVER",
        )
    )
    if existing is None:
        db.add(
            CompanyLeadCapability(
                company_id=company_id,
                capability_code="LEAD_RECEIVER",
                active=True,
                review_status="APPROVED",
            )
        )
    service_area = db.scalar(
        select(CompanyServiceAreaV12).where(
            CompanyServiceAreaV12.company_id == company_id,
            CompanyServiceAreaV12.region_code == "310000",
        )
    )
    if service_area is None:
        db.add(
            CompanyServiceAreaV12(
                company_id=company_id,
                region_code="310000",
                region_level="CITY",
                is_primary_city=True,
                active=True,
                review_status="APPROVED",
            )
        )


def test_v12_pending_price_refreshes_for_every_source_and_claim_locks_actual_price(
    api_client,
) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    _save_points(client, admin, 150, 0)

    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == company.id)
        )
        assert company is not None and operation is not None and account is not None
        _ensure_lead_receiver_capability(db, company.id)
        account.balance = 1_000
        assignments = [
            _pending_assignment(
                db,
                company=company,
                assigned_by=operation,
                source_kind=source,
                phone=f"1390013916{index}",
            )
            for index, source in enumerate(
                (
                    LeadSourceKind.PLATFORM_MANUAL.value,
                    LeadSourceKind.FEISHU_IMPORT.value,
                    LeadSourceKind.SUPPLIER_H5.value,
                )
            )
        ]
        assignment_ids = [item.id for item in assignments]
        company_id = company.id
        db.commit()

    franchise = _login(client, "franchise_demo", "Franchise123!")
    settings_queries: list[str] = []

    def record_settings_query(*args) -> None:
        statement = args[2]
        if statement.lstrip().upper().startswith("SELECT") and "system_configs" in statement:
            settings_queries.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_settings_query)
    try:
        listing = client.get(
            "/api/v1/v1.2/assignments?status=PENDING_CLAIM",
            headers=franchise,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_settings_query)
    assert listing.status_code == 200, listing.text
    assert len(settings_queries) == 1
    by_id = {item["id"]: item for item in listing.json()["data"]["items"]}
    for assignment_id in assignment_ids:
        assert by_id[assignment_id]["points_price"] == 150
        assert by_id[assignment_id]["claim_points"] == 150
        detail = client.get(
            f"/api/v1/v1.2/assignments/{assignment_id}",
            headers=franchise,
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["points_price"] == 150
        assert detail.json()["data"]["claim_points"] == 150

    operation_headers = _login(client, "operation", "Operation123!")
    company_assignments = client.get(
        f"/api/v1/v1.2/companies/{company_id}/assignments?assignment_status=PENDING_CLAIM",
        headers=operation_headers,
    )
    assert company_assignments.status_code == 200, company_assignments.text
    company_items = {
        item["id"]: item for item in company_assignments.json()["data"]["items"]
    }
    for assignment_id in assignment_ids:
        assert company_items[assignment_id]["points_price"] == 150
        assert company_items[assignment_id]["claim_points"] == 150
    trace = client.get(
        f"/api/v1/v1.2/trace/{assignment_ids[0]}",
        headers=admin,
    )
    assert trace.status_code == 200, trace.text
    traced_assignment = next(
        item
        for item in trace.json()["data"]["assignments"]
        if item["id"] == assignment_ids[0]
    )
    assert traced_assignment["points_price"] == 150
    assert traced_assignment["claim_points"] == 150

    target_id = assignment_ids[2]
    stale = client.post(
        f"/api/v1/v1.2/assignments/{target_id}/claim",
        headers=franchise,
        json={"expected_points": 100},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "CLAIM_PRICE_CHANGED"
    assert stale.json()["details"] == {
        "expected_points": 100,
        "current_points": 150,
    }
    with factory() as db:
        assert db.get(Assignment, target_id).status == "PENDING_CLAIM"
        assert db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.business_id == target_id
            )
        ) == 0
        assert db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company_id)
        ) == 1_000

    claimed = client.post(
        f"/api/v1/v1.2/assignments/{target_id}/claim",
        headers=franchise,
        json={"expected_points": 150},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["ledger"]["delta"] == -150
    assert claimed.json()["data"]["assignment"]["points_price"] == 150
    assert claimed.json()["data"]["assignment"]["claim_points"] == 150

    _save_points(client, admin, 120, 1)
    historical = client.get(
        f"/api/v1/v1.2/assignments/{target_id}",
        headers=franchise,
    )
    assert historical.status_code == 200, historical.text
    assert historical.json()["data"]["points_price"] == 150
    assert historical.json()["data"]["claim_points"] == 150
    replay = client.post(
        f"/api/v1/v1.2/assignments/{target_id}/claim",
        headers=franchise,
        json={"expected_points": 150},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["ledger"]["delta"] == -150
    assert replay.json()["data"]["idempotent"] is True
    with factory() as db:
        assert db.scalar(
            select(PointsAccount.balance).where(PointsAccount.company_id == company_id)
        ) == 850
        assert db.scalar(
            select(func.count(PointsLedger.id)).where(
                PointsLedger.business_id == target_id
            )
        ) == 1


def test_claim_expected_points_validation_and_legacy_contract(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    _save_points(client, admin, 150, 0)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        _ensure_lead_receiver_capability(db, company.id)
        assignment = _pending_assignment(
            db,
            company=company,
            assigned_by=operation,
            source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
            phone="13900139169",
        )
        assignment_id = assignment.id
        db.commit()

    franchise = _login(client, "franchise_demo", "Franchise123!")
    for invalid in (0, -1, True, 1.5, "150"):
        invalid_response = client.post(
            f"/api/v1/v1.2/assignments/{assignment_id}/claim",
            headers=franchise,
            json={"expected_points": invalid},
        )
        assert invalid_response.status_code == 422, invalid_response.text

    legacy_detail = client.get(
        f"/api/v1/claims/assignments/{assignment_id}",
        headers=franchise,
    )
    assert legacy_detail.status_code == 200, legacy_detail.text
    assert legacy_detail.json()["data"]["points_price"] == 150
    legacy_dispatch_list = client.get(
        "/api/v1/dispatch/assignments?status=PENDING_CLAIM",
        headers=franchise,
    )
    assert legacy_dispatch_list.status_code == 200, legacy_dispatch_list.text
    listed = next(
        item
        for item in legacy_dispatch_list.json()["data"]["items"]
        if item["id"] == assignment_id
    )
    assert listed["points_price"] == 150
    legacy_dispatch_detail = client.get(
        f"/api/v1/dispatch/assignments/{assignment_id}",
        headers=franchise,
    )
    assert legacy_dispatch_detail.status_code == 200, legacy_dispatch_detail.text
    assert legacy_dispatch_detail.json()["data"]["points_price"] == 150
    stale = client.post(
        f"/api/v1/claims/assignments/{assignment_id}",
        headers=franchise,
        json={"idempotency_key": "legacy-current-price", "expected_points": 100},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "CLAIM_PRICE_CHANGED"
    claimed = client.post(
        f"/api/v1/claims/assignments/{assignment_id}",
        headers=franchise,
        json={"idempotency_key": "legacy-current-price", "expected_points": 150},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["ledger"]["delta"] == -150


def test_historical_nullable_claim_snapshot_is_not_rewritten_by_display(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    _save_points(client, admin, 150, 0)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assignment = _pending_assignment(
            db,
            company=company,
            assigned_by=operation,
            source_kind=LeadSourceKind.PLATFORM_MANUAL.value,
            phone="13900139157",
        )
        assignment.status = "CLAIMED"
        assignment.claimed_at = datetime.now(timezone.utc)
        assignment.claim_points = None
        assignment_id, company_id = assignment.id, company.id
        db.commit()

    franchise = _login(client, "franchise_demo", "Franchise123!")
    detail = client.get(f"/api/v1/v1.2/assignments/{assignment_id}", headers=franchise)
    assert detail.status_code == 200, detail.text
    listing = client.get("/api/v1/v1.2/assignments?status=CLAIMED", headers=franchise)
    assert listing.status_code == 200, listing.text
    company_listing = client.get(
        f"/api/v1/v1.2/companies/{company_id}/assignments", headers=admin
    )
    assert company_listing.status_code == 200, company_listing.text
    trace = client.get(f"/api/v1/v1.2/trace/{assignment_id}", headers=admin)
    assert trace.status_code == 200, trace.text
    rows = [detail.json()["data"]]
    for payload in (listing, company_listing):
        rows.append(next(row for row in payload.json()["data"]["items"] if row["id"] == assignment_id))
    rows.append(next(row for row in trace.json()["data"]["assignments"] if row["id"] == assignment_id))
    for row in rows:
        assert row["points_price"] == 100
        assert row["claim_points"] is None


def test_v12_claim_without_body_remains_compatible_and_permissions_stay_scoped(
    api_client,
) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    _save_points(client, admin, 150, 0)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and operation is not None
        _ensure_lead_receiver_capability(db, company.id)
        assignment = _pending_assignment(
            db,
            company=company,
            assigned_by=operation,
            source_kind=LeadSourceKind.FEISHU_IMPORT.value,
            phone="13900139168",
        )
        assignment_id = assignment.id
        db.commit()

    operation_headers = _login(client, "operation", "Operation123!")
    forbidden = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/claim",
        headers=operation_headers,
        json={"expected_points": 150},
    )
    assert forbidden.status_code == 403

    franchise = _login(client, "franchise_demo", "Franchise123!")
    compatible = client.post(
        f"/api/v1/v1.2/assignments/{assignment_id}/claim",
        headers=franchise,
    )
    assert compatible.status_code == 200, compatible.text
    assert compatible.json()["data"]["ledger"]["delta"] == -150
