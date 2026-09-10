from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

from apps.api.src.core import models_v12 as _models_v12  # noqa: F401
from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.legacy_guard import is_legacy_write
from apps.api.src.core.models import Assignment, AssignmentEvent, Company, Lead, Notification, User
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.assignment_timeout_v12 import run_assignment_timeouts_v12
from apps.api.src.services.followup_service import run_followup_overdue


def test_legacy_write_classifier_blocks_only_legacy_mutations() -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert is_legacy_write(method, "/api/v1/verification/tasks") is True
        assert is_legacy_write(method, "/api/v1/returns/return-1/submit") is True
        assert is_legacy_write(method, "/api/v1/claims/assignments/a-1") is True
        assert is_legacy_write(method, "/api/v1/dispatch/leads/lead-1") is True
        assert is_legacy_write(method, "/api/v1/leads/feishu/mock-sync") is True

    for method in ("GET", "HEAD", "OPTIONS"):
        assert is_legacy_write(method, "/api/v1/verification/tasks") is False
        assert is_legacy_write(method, "/api/v1/returns/return-1") is False

    assert is_legacy_write("POST", "/api/v1/v1.2/returns/assignments/a-1/draft") is False
    assert is_legacy_write("POST", "/api/v1/v1.2/assignments/a-1/claim") is False
    assert is_legacy_write("POST", "/api/v1/auth/login") is False
    assert is_legacy_write("POST", "/api/v1/followups/assignments/a-1") is False
    assert is_legacy_write("POST", "/api/v1/points/recharge") is False
    assert is_legacy_write("POST", "/api/v1/leads/staging-cleanup") is True
    assert is_legacy_write("PUT", "/api/v1/leads/staging-cleanup") is True
    assert is_legacy_write("DELETE", "/api/v1/leads/staging-cleanup") is True
    assert is_legacy_write("POST", "/api/v1/verification/tasks/task-1/assign") is True
    assert is_legacy_write("POST", "/api/v1/verification/tasks/task-1/reclaim") is True


def test_default_web_entries_use_only_the_formal_role_workbenches(api_client) -> None:
    client, _ = api_client

    admin = client.get("/admin/", follow_redirects=False)
    assert admin.status_code == 302
    assert admin.headers["location"] == "/admin/v12-operations.html"

    h5 = client.get("/h5/", follow_redirects=False)
    assert h5.status_code == 302
    assert h5.headers["location"] == "/h5/v12-workbench.html"

    client.cookies.set("access_token", "invalid-token")
    invalid = client.get("/admin/", follow_redirects=False)
    assert invalid.status_code == 302
    assert invalid.headers["location"] == "/admin/v12-operations.html"
    client.cookies.clear()

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "Admin123!"},
    )
    assert login.status_code == 200

    admin_authenticated = client.get("/admin/", follow_redirects=False)
    assert admin_authenticated.status_code == 302
    assert admin_authenticated.headers["location"] == "/admin/v12-operations.html"

    h5_authenticated = client.get("/h5/", follow_redirects=False)
    assert h5_authenticated.status_code == 302
    assert h5_authenticated.headers["location"] == "/h5/admin/"

    admin_legacy = client.get("/admin/legacy", follow_redirects=False)
    assert admin_legacy.status_code == 302
    assert admin_legacy.headers["location"] == "/admin/"

    h5_legacy = client.get("/h5/legacy", follow_redirects=False)
    assert h5_legacy.status_code == 302
    assert h5_legacy.headers["location"] == "/h5/"

    admin_index = client.get("/admin/index.html", follow_redirects=False)
    assert admin_index.status_code == 302
    assert admin_index.headers["location"] == "/admin/"

    h5_index = client.get("/h5/index.html", follow_redirects=False)
    assert h5_index.status_code == 302
    assert h5_index.headers["location"] == "/h5/"


def test_legacy_mutations_fail_closed_even_when_app_env_is_mislabelled(
    api_client,
    monkeypatch,
) -> None:
    client, _ = api_client
    import apps.api.src.core.legacy_guard as legacy_guard

    monkeypatch.setattr(legacy_guard.settings, "app_env", "development")
    monkeypatch.setattr(legacy_guard.settings, "legacy_write_enabled", False)

    legacy_posts = (
        "/api/v1/verification/tasks",
        "/api/v1/returns/missing/submit",
        "/api/v1/claims/assignments/missing",
        "/api/v1/dispatch/leads/missing",
        "/api/v1/leads/feishu/mock-sync",
    )
    for path in legacy_posts:
        response = client.post(path, json={})
        assert response.status_code == 410, path
        assert response.json()["code"] == "LEGACY_WRITE_DISABLED"

    legacy_read = client.get("/api/v1/verification/tasks")
    assert legacy_read.status_code != 410
    assert legacy_read.json()["code"] != "LEGACY_WRITE_DISABLED"

    v12_write = client.post(
        "/api/v1/v1.2/returns/assignments/missing/draft",
        json={"reason_code": "EMPTY_NUMBER", "description": "测试 V1.2 写路径未被 Legacy 门禁误伤"},
    )
    assert v12_write.status_code != 410
    assert v12_write.json()["code"] != "LEGACY_WRITE_DISABLED"


def _lead(phone: str, name: str, status: str) -> Lead:
    return Lead(
        customer_name=name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        status=status,
        raw_payload={},
    )


def test_v12_timeout_returns_expired_lead_to_ready_dispatch_without_touching_legacy(api_client) -> None:
    _, factory = api_client
    current = datetime.now(timezone.utc)

    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert admin is not None and company is not None

        v12_lead = _lead("13800138011", "V1.2 超时客资", LeadV12Status.DISPATCHED.value)
        legacy_lead = _lead("13800138012", "Legacy 待领取客资", "QUALIFIED")
        db.add_all([v12_lead, legacy_lead])
        db.flush()

        v12_assignment = Assignment(
            lead_id=v12_lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=admin.id,
            assigned_at=current - timedelta(hours=49),
            expires_at=current - timedelta(hours=1),
        )
        legacy_assignment = Assignment(
            lead_id=legacy_lead.id,
            company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=admin.id,
            assigned_at=current - timedelta(hours=49),
            expires_at=current - timedelta(hours=1),
        )
        db.add_all([v12_assignment, legacy_assignment])
        db.flush()
        v12_lead.current_assignment_id = v12_assignment.id
        legacy_lead.current_assignment_id = legacy_assignment.id
        db.flush()

        result = run_assignment_timeouts_v12(db, now=current)
        db.flush()

        assert result == {"reminded": 0, "expired": 1}
        assert v12_assignment.status == AssignmentStatus.EXPIRED.value
        assert v12_assignment.release_reason == "UNCLAIMED_TIMEOUT"
        assert v12_lead.status == LeadV12Status.READY_DISPATCH.value
        assert v12_lead.current_assignment_id is None
        assert legacy_assignment.status == AssignmentStatus.PENDING_CLAIM.value
        assert legacy_lead.status == "QUALIFIED"
        assert legacy_lead.current_assignment_id == legacy_assignment.id
        event = db.scalar(
            select(AssignmentEvent).where(
                AssignmentEvent.assignment_id == v12_assignment.id,
                AssignmentEvent.event_type == "V12_ASSIGNMENT_EXPIRED",
            )
        )
        assert event is not None


def test_v12_timeout_refreshes_locked_row_before_sending_reminder(api_client) -> None:
    _, factory = api_client
    current = datetime.now(timezone.utc)

    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert admin is not None and company is not None
        lead = _lead("13800138013", "V1.2 并发提醒", LeadV12Status.DISPATCHED.value)
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.PENDING_CLAIM.value,
            points_price=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=admin.id,
            assigned_at=current - timedelta(hours=25),
            expires_at=current + timedelta(hours=23),
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        assignment_id = assignment.id
        company_id = company.id
        db.commit()

        stale = db.get(Assignment, assignment_id)
        assert stale is not None and stale.reminder_sent_at is None
        db.execute(
            update(Assignment)
            .where(Assignment.id == assignment_id)
            .values(reminder_sent_at=current - timedelta(minutes=1))
            .execution_options(synchronize_session=False)
        )
        db.commit()
        assert stale.reminder_sent_at is None

        result = run_assignment_timeouts_v12(db, now=current)
        assert result == {"reminded": 0, "expired": 0}
        # Due-only discovery now excludes the already-reminded row entirely.
        db.refresh(stale)
        assert stale.reminder_sent_at is not None
        reminder = db.scalar(
            select(Notification.id).where(
                Notification.company_id == company_id,
                Notification.scene == "V12_CLAIM_REMINDER",
                Notification.deep_link == f"/h5/v12-workbench.html?view=assignments&id={assignment_id}",
            )
        )
        assert reminder is None


def test_v12_overdue_followup_notification_uses_workbench_deep_link(api_client) -> None:
    _, factory = api_client
    current = datetime.now(timezone.utc)

    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert admin is not None and company is not None
        lead = _lead("13800138014", "V1.2 跟进逾期", LeadV12Status.CLAIMED.value)
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company.id,
            receiver_company_id=company.id,
            status=AssignmentStatus.CLAIMED.value,
            points_price=100,
            price_version=1,
            lead_snapshot={},
            assigned_by=admin.id,
            assigned_at=current - timedelta(hours=48),
            claimed_at=current - timedelta(hours=30),
            first_followup_due_at=current - timedelta(hours=1),
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        assignment_id = assignment.id
        company_id = company.id

        result = run_followup_overdue(db, now=current)
        assert result["overdue"] >= 1
        notification = db.scalar(
            select(Notification).where(
                Notification.company_id == company_id,
                Notification.scene == "FOLLOWUP_OVERDUE",
                Notification.deep_link == f"/h5/v12-workbench.html?view=assignments&id={assignment_id}",
            )
        )
        assert notification is not None


def test_all_timeout_entrypoints_use_shared_active_version_router() -> None:
    scheduler = Path("scripts/scheduler.py").read_text(encoding="utf-8")
    manual_jobs = Path("scripts/run_jobs.py").read_text(encoding="utf-8")
    for source in (scheduler, manual_jobs):
        assert "drain_assignment_timeouts_active" in source
        assert "run_assignment_timeouts_v12" not in source
        assert "from apps.api.src.services.claim_service import run_assignment_timeouts" not in source
    assert 'output["assignment_timeouts"] = drain_assignment_timeouts_active(' in manual_jobs
    assert '"timeouts": drain_assignment_timeouts_active(db)' in scheduler
