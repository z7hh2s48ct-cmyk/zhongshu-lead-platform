from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

import apps.api.src.core.legacy_guard as legacy_guard
from apps.api.src.core import models_v12 as _models_v12  # noqa: F401
from apps.api.src.core.enums import AssignmentStatus, EvidenceType, VerificationTaskStatus
from apps.api.src.core.models import (
    Assignment,
    AssignmentEvent,
    AuditLog,
    Company,
    Lead,
    Notification,
    ReturnEvidence,
    ReturnRequest,
    User,
    VerificationTask,
)
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import (
    LeadSourceKind,
    LeadV12Status,
    ReturnV12Status,
    VerificationTaskType,
)
from apps.api.src.services import return_v12 as return_v12_service
from apps.api.src.services.auth_service import create_internal_user
from apps.api.src.services.storage import get_storage


CALL_APP = Path("apps/call-h5/public/app.js")
CALL_INDEX = Path("apps/call-h5/public/index.html")
TASKS_ENDPOINT = "/v1.2/return-verifications/tasks"
PRE_DISPATCH_TASKS_ENDPOINT = "/v1.2/pre-dispatch-verifications/tasks"


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _data(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "OK"
    return payload["data"]


def _seed_return_verification(factory) -> tuple[str, str, str, str, str]:
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        franchise = db.scalar(select(User).where(User.username == "franchise_demo"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        telesales = db.scalar(select(User).where(User.username == "telesales"))
        assert company is not None
        assert franchise is not None
        assert operation is not None
        assert telesales is not None

        now = datetime.now(timezone.utc)
        phone = "13900139211"
        lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            submitter_user_id=franchise.id,
            supplier_company_id=company.id,
            customer_name="电销 V1.2 契约客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=True,
            city="上海市",
            district="浦东新区",
            region_code="310115",
            category_code="OLD_RENOVATION",
            brand_code="ZHONGSHU",
            need_summary="核验退回申请中的号码事实",
            status=LeadV12Status.CLAIMED.value,
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
            supplier_company_id=company.id,
            status=AssignmentStatus.RETURN_PENDING.value,
            points_price=100,
            claim_points=100,
            lead_snapshot={"phone_masked": "139****9211"},
            assigned_by=operation.id,
            assigned_at=now - timedelta(hours=3),
            claimed_at=now - timedelta(hours=2),
            appeal_deadline_at=now + timedelta(days=3),
            idempotency_key="call-h5-v12-contract",
        )
        db.add(assignment)
        db.flush()

        return_request = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=lead.id,
            company_id=company.id,
            reason_code="EMPTY_NUMBER",
            description="加盟商反馈号码疑似空号，请电销复核事实。",
            status=ReturnV12Status.VERIFYING.value,
            submitted_by=franchise.id,
            submitted_at=now - timedelta(hours=1),
            due_at=now + timedelta(days=3),
            appeal_deadline_at=now + timedelta(days=3),
        )
        db.add(return_request)
        db.flush()

        task = VerificationTask(
            lead_id=lead.id,
            task_type=VerificationTaskType.RETURN_VERIFY.value,
            return_request_id=return_request.id,
            assignment_id=assignment.id,
            status=VerificationTaskStatus.PENDING.value,
            due_at=now + timedelta(hours=8),
        )
        db.add(task)
        db.flush()
        return_request.verification_task_id = task.id
        stored = get_storage().save(
            b"\xff\xd8\xff\xe0franchise-evidence",
            prefix=f"evidence/v1.2/{return_request.id}",
            filename="franchise-proof.jpg",
            mime_type="image/jpeg",
        )
        franchise_evidence = ReturnEvidence(
            return_request_id=return_request.id,
            evidence_type=EvidenceType.CHAT_SCREENSHOT.value,
            object_key=stored.object_key,
            original_name="franchise-proof.jpg",
            mime_type=stored.mime_type,
            file_size=stored.size,
            sha256=stored.sha256,
            uploaded_by=franchise.id,
        )
        db.add(franchise_evidence)
        db.flush()
        db.commit()
        return task.id, return_request.id, phone, telesales.id, franchise_evidence.id


def test_call_h5_uses_only_v12_assigned_verification_contracts() -> None:
    source = CALL_APP.read_text(encoding="utf-8")
    index = CALL_INDEX.read_text(encoding="utf-8")

    assert TASKS_ENDPOINT in source
    assert PRE_DISPATCH_TASKS_ENDPOINT in source
    assert "verification_evidence" in source
    assert "evidence_ids" in source
    assert "verification-evidence-files" in source
    assert "new FormData()" in source
    assert "taskPath(kind, id, 'evidence')" in source
    assert "/verification/tasks" not in source
    assert "taskPath(kind, taskId, action = '')" in source
    for action in ("start", "dial", "submit"):
        assert f"taskPath(kind, id, '{action}')" in source
    for field in ("contact_result", "conclusion", "note"):
        assert field in source
    for legacy_field in ("invalid_reason", "answers:", "corrections:"):
        assert legacy_field not in source
    for removed_editor in ('id="region"', 'id="category"', 'id="summary"'):
        assert removed_editor not in source
    assert "return_request" in source
    assert "PRE_DISPATCH" in source
    assert "运营派发" in source
    assert "自主领取" in source
    assert "go('home');route()" not in source
    assert "/admin/index.html" not in source
    assert "app.js?v=20260916-feedback-915" in index


def test_call_h5_home_first_screen_has_personal_task_summary_without_team_finance() -> None:
    source = CALL_APP.read_text(encoding="utf-8")

    assert "TELESALES_HOME_CONTRACT" in source
    for label in ("待开始", "核验中", "已提交", "开始核验", "继续核验", "最近任务"):
        assert label in source
    for copy in ("团队排行", "公司充值", "平台收入", "加盟商积分"):
        assert copy not in source


def test_call_h5_task_detail_first_screen_exposes_dial_rules_and_result_entry() -> None:
    source = CALL_APP.read_text(encoding="utf-8")

    for label in ("一键拨号", "复制号码", "核验说明", "填写结果", "拨号", "提交核验结果"):
        assert label in source
    for forbidden in ("自动录音", "云外呼", "在线支付"):
        assert forbidden not in source
    assert source.count("['处理参考时间', fmt(data.due_at)]") >= 2
    assert "任务已超时" not in source
    assert "不能继续处理" not in source
    assert "async function copyPhone" in source
    assert "navigator.clipboard.writeText" in source
    assert "document.execCommand('copy')" in source


def test_call_h5_route_awaits_async_views_so_failures_reach_the_error_state() -> None:
    source = CALL_APP.read_text(encoding="utf-8")

    for view in ("home()", "verify()", "records()", "task(parts[1], parts[2])", "profile()"):
        assert f"await {view}" in source


def test_v12_call_flow_works_with_legacy_writes_disabled(api_client, monkeypatch) -> None:
    client, factory = api_client
    monkeypatch.setattr(legacy_guard.settings, "legacy_write_enabled", False)
    task_id, return_id, phone, telesales_id, franchise_evidence_id = _seed_return_verification(factory)
    with factory() as db:
        other_telesales_user = create_internal_user(
            db,
            username="return-history-other",
            password="simple88",
            display_name="其他历史电销",
            role_code="TELESALES",
        )
        db.commit()
        other_telesales_id = other_telesales_user.id
    decrypt_calls: list[str] = []
    real_decrypt = return_v12_service.decrypt_text

    def tracked_decrypt(value: str) -> str:
        decrypt_calls.append(value)
        return real_decrypt(value)

    monkeypatch.setattr(return_v12_service, "decrypt_text", tracked_decrypt)
    operation = _login(client, "operation", "Operation123!")
    missing_reason = client.post(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}/assign",
        headers=operation,
        json={"assignee_user_id": telesales_id},
    )
    assert missing_reason.status_code == 422
    assigned = _data(
        client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/assign",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "运营确认需要电话核验退回事实",
            },
        )
    )
    assert assigned["status"] == VerificationTaskStatus.ASSIGNED.value
    assert assigned["assignee_user_id"] == telesales_id
    assert assigned["due_at"] is not None
    assert decrypt_calls == []
    with factory() as db:
        notification = db.scalar(
            select(Notification).where(
                Notification.user_id == telesales_id,
                Notification.scene == "V12_RETURN_VERIFY_ASSIGNED",
            )
        )
        assert notification is not None
        assert notification.deep_link == "/h5/call/#/verify"
        audit = db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "V12_RETURN_VERIFY_ASSIGN",
                AuditLog.resource_id == task_id,
            )
            .order_by(AuditLog.created_at.desc())
        )
        assert audit is not None
        assert audit.before_json["status"] == VerificationTaskStatus.PENDING.value
        assert audit.after_json["assignee_user_id"] == telesales_id
        assert audit.after_json["due_at"] is not None

    legacy_claim = client.post(
        f"/api/v1/verification/tasks/{task_id}/claim",
        headers=operation,
    )
    assert legacy_claim.status_code == 410
    assert legacy_claim.json()["code"] == "LEGACY_WRITE_DISABLED"

    telesales = _login(client, "telesales", "Telesales123!")
    other_telesales = _login(client, "return-history-other", "simple88")

    with factory() as db:
        task = db.get(VerificationTask, task_id)
        assert task is not None
        task.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    listed = _data(
        client.get(
            f"/api/v1{TASKS_ENDPOINT}?mine=true&page=1&page_size=20",
            headers=telesales,
        )
    )
    item = next(item for item in listed["items"] if item["id"] == task_id)
    assert item["lead"]["phone"] is None
    assert item["lead"]["phone_masked"] == "139****9211"
    assert item["return_request"]["id"] == return_id
    assert decrypt_calls == []

    evidence_file = ("verification.jpg", b"\xff\xd8\xff\xe0verification", "image/jpeg")
    assigned_upload = client.post(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}/evidence",
        headers=telesales,
        data={"evidence_type": "CHAT_SCREENSHOT"},
        files={"file": evidence_file},
    )
    assert assigned_upload.status_code == 409

    detail = _data(client.get(f"/api/v1{TASKS_ENDPOINT}/{task_id}", headers=telesales))
    assert detail["lead"]["phone"] is None
    assert detail["is_overdue"] is True
    franchise_evidence = next(
        item
        for item in detail["return_request"]["available_evidences"]
        if item["id"] == franchise_evidence_id
    )
    assert franchise_evidence["uploaded_by_name"] == "张老板"
    assigned_download = client.get(
        f"/api/v1/v1.2/return-evidences/{franchise_evidence_id}/download",
        headers=telesales,
        params={"token": franchise_evidence["access_token"]},
    )
    assert assigned_download.status_code == 200
    assert assigned_download.content == b"\xff\xd8\xff\xe0franchise-evidence"
    with factory() as db:
        evidence_read_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_RETURN_EVIDENCE_READ",
                AuditLog.resource_id == franchise_evidence_id,
                AuditLog.actor_user_id == telesales_id,
            )
        )
        assert evidence_read_audit is not None
        evidence = db.get(ReturnEvidence, franchise_evidence_id)
        assert evidence is not None
        get_storage().delete(evidence.object_key)
    missing_file = client.get(
        f"/api/v1/v1.2/return-evidences/{franchise_evidence_id}/download",
        headers=telesales,
        params={"token": franchise_evidence["access_token"]},
    )
    assert missing_file.status_code == 404
    assert missing_file.json()["code"] == "EVIDENCE_FILE_MISSING"
    assert missing_file.json()["message"] == "证据文件已丢失，请联系管理员"

    reassigned = _data(
        client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/assign",
            headers=operation,
            json={
                "assignee_user_id": other_telesales_id,
                "reason": "运营调整退回核验人员",
            },
        )
    )
    assert reassigned["assignee_user_id"] == other_telesales_id
    old_assignee_detail = client.get(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}", headers=telesales
    )
    assert old_assignee_detail.status_code == 403
    old_assignee_download = client.get(
        f"/api/v1/v1.2/return-evidences/{franchise_evidence_id}/download",
        headers=telesales,
        params={"token": franchise_evidence["access_token"]},
    )
    assert old_assignee_download.status_code == 403

    _data(
        client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/assign",
            headers=operation,
            json={
                "assignee_user_id": telesales_id,
                "reason": "运营重新分配给原核验人员",
            },
        )
    )
    with factory() as db:
        task = db.get(VerificationTask, task_id)
        assert task is not None
        task.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert decrypt_calls == []

    claimed = _data(
        client.post(f"/api/v1{TASKS_ENDPOINT}/{task_id}/start", headers=telesales)
    )
    assert claimed["status"] == VerificationTaskStatus.IN_PROGRESS.value
    assert claimed["is_overdue"] is True
    assert claimed["lead"]["phone"] == phone
    assert len(decrypt_calls) == 1

    other_upload = client.post(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}/evidence",
        headers=other_telesales,
        data={"evidence_type": "CHAT_SCREENSHOT"},
        files={"file": evidence_file},
    )
    assert other_upload.status_code == 409
    uploaded_evidence = _data(
        client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/evidence",
            headers=telesales,
            data={"evidence_type": "CHAT_SCREENSHOT"},
            files={"file": evidence_file},
        )
    )
    assert uploaded_evidence["access_token"]
    assert uploaded_evidence["uploaded_by"] == telesales_id
    assert uploaded_evidence["uploaded_by_name"] == "电销人员"

    dial = _data(
        client.post(f"/api/v1{TASKS_ENDPOINT}/{task_id}/dial", headers=telesales)
    )
    assert dial == {"phone": phone, "tel_url": f"tel:{phone}"}
    assert len(decrypt_calls) == 2

    old_payload = client.post(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}/submit",
        headers=telesales,
        json={
            "contact_result": "CONNECTED",
            "conclusion": "INCONCLUSIVE",
            "result": "INVALID",
            "invalid_reason": "EMPTY_NUMBER",
            "answers": {"called": True},
            "corrections": {},
            "note": "旧版字段必须被拒绝。",
        },
    )
    assert old_payload.status_code == 422

    submitted = _data(
        client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/submit",
            headers=telesales,
            json={
                "contact_result": "EMPTY_NUMBER",
                "conclusion": "SUPPORT_RETURN",
                "note": "连续拨打后确认号码为空号，支持本次退回。",
                "evidence_ids": [uploaded_evidence["id"]],
            },
        )
    )
    assert submitted["status"] == VerificationTaskStatus.SUBMITTED.value
    assert submitted["contact_result"] == "EMPTY_NUMBER"
    assert submitted["conclusion"] == "SUPPORT_RETURN"
    completed_detail = _data(
        client.get(f"/api/v1{TASKS_ENDPOINT}/{task_id}", headers=telesales)
    )
    assert completed_detail["lead"]["phone"] == phone
    assert completed_detail["verification_info"]["note"] == (
        "连续拨打后确认号码为空号，支持本次退回。"
    )
    verification_evidence = completed_detail["verification_info"]["evidences"][0]
    assert verification_evidence["id"] == uploaded_evidence["id"]
    assert verification_evidence["uploaded_by"] == telesales_id
    assert verification_evidence["uploaded_by_name"] == "电销人员"
    assert verification_evidence["access_token"]
    downloaded = client.get(
        f"/api/v1/v1.2/return-evidences/{verification_evidence['id']}/download",
        headers=telesales,
        params={"token": verification_evidence["access_token"]},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == evidence_file[1]
    operation_return_detail = _data(
        client.get(f"/api/v1/v1.2/returns/{return_id}", headers=operation)
    )
    operation_verification_evidence = operation_return_detail["verification"]["evidences"][0]
    assert operation_verification_evidence["id"] == uploaded_evidence["id"]
    assert operation_verification_evidence["access_token"]

    repeated = _data(
        client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/submit",
            headers=telesales,
            json={
                "contact_result": "CONNECTED",
                "conclusion": "DOES_NOT_SUPPORT_RETURN",
                "note": "重复提交不得覆盖第一次事实结论。",
            },
        )
    )
    assert repeated["contact_result"] == "EMPTY_NUMBER"
    assert repeated["conclusion"] == "SUPPORT_RETURN"

    for action in ("start", "dial", "submit"):
        response = client.post(
            f"/api/v1{TASKS_ENDPOINT}/{task_id}/{action}",
            headers=operation,
            json={
                "contact_result": "CONNECTED",
                "conclusion": "INCONCLUSIVE",
                "note": "非电销角色不得操作任务。",
            }
            if action == "submit"
            else None,
        )
        assert response.status_code == 403

    with factory() as db:
        request = db.get(ReturnRequest, return_id)
        events = db.scalar(
            select(func.count(AssignmentEvent.id)).where(
                AssignmentEvent.assignment_id == request.assignment_id,
                AssignmentEvent.event_type == "V12_RETURN_VERIFY_SUBMITTED",
            )
        )
        assert request.status == ReturnV12Status.REVIEWING.value
        assert request.review_note == "连续拨打后确认号码为空号，支持本次退回。"
        assert events == 1
        task = db.get(VerificationTask, task_id)
        assert task is not None
        task.status = VerificationTaskStatus.RELEASED.value
        db.commit()

    submitted_history = _data(
        client.get(
            f"/api/v1{TASKS_ENDPOINT}?mine=true&submitted_history=true&page=1&page_size=20",
            headers=telesales,
        )
    )
    history_item = next(item for item in submitted_history["items"] if item["id"] == task_id)
    assert history_item["status"] == VerificationTaskStatus.RELEASED.value
    assert history_item["submitted_at"]
    assert history_item["lead"]["phone"] == phone
    other_history = _data(
        client.get(
            f"/api/v1{TASKS_ENDPOINT}?mine=true&submitted_history=true&page=1&page_size=20",
            headers=other_telesales,
        )
    )
    assert all(item["id"] != task_id for item in other_history["items"])
    forbidden_detail = client.get(
        f"/api/v1{TASKS_ENDPOINT}/{task_id}",
        headers=other_telesales,
    )
    assert forbidden_detail.status_code == 403
    released_detail = _data(
        client.get(f"/api/v1{TASKS_ENDPOINT}/{task_id}", headers=telesales)
    )
    assert released_detail["verification_info"]["note"] == (
        "连续拨打后确认号码为空号，支持本次退回。"
    )
    assert released_detail["lead"]["phone"] == phone
    assert len(decrypt_calls) == 6
