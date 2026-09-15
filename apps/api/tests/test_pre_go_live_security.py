from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import jwt
from sqlalchemy import func, select

from apps.api.src.core import reward_models_v12 as _reward_models_v12  # noqa: F401
from apps.api.src.core.config import get_settings
from apps.api.src.core.enums import AssignmentStatus, EvidenceType, PointsLedgerType
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    Lead,
    Notification,
    PointsAccount,
    PointsLedger,
    ReturnEvidence,
    ReturnRequest,
    User,
)
from apps.api.src.core.models_v12 import SupplierLeadReward
from apps.api.src.core.security import (
    create_access_token,
    create_signed_state,
    decode_signed_state,
    encrypt_text,
    fingerprint_phone,
    hash_phone,
)
from apps.api.src.core.v12_enums import (
    LeadSourceKind,
    LeadV12Status,
    ReturnV12Status,
    RewardStatus,
)
from apps.api.src.services.audit import write_audit
from apps.api.src.services.auth_service import (
    create_company_invite,
    create_internal_user,
)
from apps.api.src.services.storage import (
    LocalObjectStorage,
    create_file_access_token,
    get_storage,
)

settings = get_settings()


def _login_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    assert "token" not in response.json()["data"]
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _seed_cross_tenant_graph(factory) -> dict[str, str | int]:
    with factory() as db:
        target_company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        target_user = db.scalar(select(User).where(User.username == "franchise_demo"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert target_company is not None and target_user is not None and operation is not None

        attacker_company = Company(code="SEC-ATTACK", name="安全负例攻击租户", status="ACTIVE")
        db.add(attacker_company)
        db.flush()
        attacker_account = PointsAccount(company_id=attacker_company.id, balance=100, version=1)
        db.add(attacker_account)
        attacker_user = create_internal_user(
            db,
            username="security_attacker",
            password="SecurityAttack123!",
            display_name="安全负例攻击账号",
            role_code="FRANCHISE_OWNER",
            company_id=attacker_company.id,
        )
        # 不设置 primary_user_id：攻击方公司保持可发邀请状态，
        # 供跨公司绑定负例使用（已绑定主账号的公司拒绝创建邀请）。

        now = datetime.now(timezone.utc)
        phone = "13900139991"
        lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            submitter_user_id=target_user.id,
            supplier_company_id=target_company.id,
            customer_name="跨租户安全测试客户",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=True,
            city="上海市",
            district="浦东新区",
            region_code="310115",
            category_code="OLD_RENOVATION",
            brand_code="ZHONGSHU",
            need_summary="仅用于上线前跨租户安全负例",
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
            company_id=target_company.id,
            receiver_company_id=target_company.id,
            supplier_company_id=target_company.id,
            status=AssignmentStatus.CLAIMED.value,
            points_price=100,
            claim_points=100,
            lead_snapshot={"phone_masked": "139****9991"},
            assigned_by=operation.id,
            assigned_at=now - timedelta(hours=2),
            claimed_at=now - timedelta(hours=1),
            appeal_deadline_at=now + timedelta(days=3),
            reward_due_at=now - timedelta(minutes=1),
            idempotency_key="security-cross-tenant-assignment",
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id

        return_request = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=lead.id,
            company_id=target_company.id,
            reason_code="INFO_ERROR",
            description="跨租户安全测试退回申请",
            status=ReturnV12Status.DRAFT.value,
            submitted_by=target_user.id,
            due_at=now + timedelta(days=3),
            appeal_deadline_at=now + timedelta(days=3),
        )
        db.add(return_request)
        db.flush()

        storage = get_storage()
        assert isinstance(storage, LocalObjectStorage)
        first = storage.save(
            b"\x89PNG\r\n\x1a\nsecurity-evidence-a",
            prefix=f"security/{return_request.id}",
            filename="proof-a.png",
            mime_type="image/png",
        )
        second = storage.save(
            b"\x89PNG\r\n\x1a\nsecurity-evidence-b",
            prefix=f"security/{return_request.id}",
            filename="proof-b.png",
            mime_type="image/png",
        )
        evidence_a = ReturnEvidence(
            return_request_id=return_request.id,
            evidence_type=EvidenceType.CHAT_SCREENSHOT.value,
            object_key=first.object_key,
            original_name="proof-a.png",
            mime_type="image/png",
            file_size=first.size,
            sha256=first.sha256,
            uploaded_by=target_user.id,
        )
        evidence_b = ReturnEvidence(
            return_request_id=return_request.id,
            evidence_type=EvidenceType.CHAT_SCREENSHOT.value,
            object_key=second.object_key,
            original_name="proof-b.png",
            mime_type="image/png",
            file_size=second.size,
            sha256=second.sha256,
            uploaded_by=target_user.id,
        )
        db.add_all([evidence_a, evidence_b])

        target_notification = Notification(
            user_id=target_user.id,
            company_id=target_company.id,
            scene="SECURITY_TEST",
            title="目标租户私有消息",
            body="该消息不得被其他加盟商读取",
            status="CREATED",
        )
        db.add(target_notification)

        reward = SupplierLeadReward(
            lead_id=lead.id,
            assignment_id=assignment.id,
            supplier_company_id=target_company.id,
            receiver_company_id=target_company.id,
            status=RewardStatus.OBSERVING.value,
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=30,
            rule_version=1,
            observed_at=now - timedelta(days=1),
            appeal_deadline_at=now - timedelta(minutes=1),
            reward_due_at=now - timedelta(minutes=1),
        )
        db.add(reward)

        target_account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == target_company.id)
        )
        assert target_account is not None
        target_account.balance = max(int(target_account.balance), 1000)
        db.commit()

        return {
            "target_company_id": target_company.id,
            "target_user_id": target_user.id,
            "attacker_company_id": attacker_company.id,
            "attacker_user_id": attacker_user.id,
            "attacker_balance": int(attacker_account.balance),
            "lead_id": lead.id,
            "assignment_id": assignment.id,
            "return_id": return_request.id,
            "evidence_a": evidence_a.id,
            "evidence_b": evidence_b.id,
            "notification_id": target_notification.id,
            "reward_id": reward.id,
        }


def test_cross_tenant_ids_do_not_leak_business_objects(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    attacker = _login_headers(client, "security_attacker", "SecurityAttack123!")

    assignment = client.get(
        f"/api/v1/v1.2/assignments/{graph['assignment_id']}", headers=attacker
    )
    assert assignment.status_code == 404
    assert "13900139991" not in assignment.text
    assert "跨租户安全测试客户" not in assignment.text

    claim = client.post(
        f"/api/v1/v1.2/assignments/{graph['assignment_id']}/claim", headers=attacker
    )
    assert claim.status_code in {403, 404}
    assert "13900139991" not in claim.text

    supplier_lead = client.get(
        f"/api/v1/v1.2/supplier/leads/{graph['lead_id']}", headers=attacker
    )
    assert supplier_lead.status_code == 403
    assert "13900139991" not in supplier_lead.text

    returned = client.get(f"/api/v1/v1.2/returns/{graph['return_id']}", headers=attacker)
    assert returned.status_code == 403
    assert "跨租户安全测试退回申请" not in returned.text

    reward = client.get(
        f"/api/v1/v1.2/supplier-rewards/{graph['reward_id']}", headers=attacker
    )
    assert reward.status_code == 403

    account = client.get(
        f"/api/v1/points/accounts/{graph['target_company_id']}", headers=attacker
    )
    assert account.status_code == 403

    ledgers = client.get(
        f"/api/v1/points/ledgers?company_id={graph['target_company_id']}&page_size=200",
        headers=attacker,
    )
    assert ledgers.status_code == 200
    assert all(
        item["company_id"] == graph["attacker_company_id"]
        for item in ledgers.json()["data"]["items"]
    )

    messages = client.get("/api/v1/notifications?page_size=100", headers=attacker)
    assert messages.status_code == 200
    assert graph["notification_id"] not in {
        item["id"] for item in messages.json()["data"]["items"]
    }
    mark_read = client.post(
        f"/api/v1/notifications/{graph['notification_id']}/read", headers=attacker
    )
    assert mark_read.status_code == 404

    platform_report = client.get(
        f"/api/v1/v1.2/reports/overview?company_id={graph['target_company_id']}",
        headers=attacker,
    )
    assert platform_report.status_code == 403


def test_cross_tenant_evidence_upload_has_no_storage_or_database_side_effect(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    attacker = _login_headers(client, "security_attacker", "SecurityAttack123!")
    storage = get_storage()
    assert isinstance(storage, LocalObjectStorage)
    before_files = {path for path in storage.root.rglob("*") if path.is_file()}
    with factory() as db:
        before_rows = db.scalar(select(func.count(ReturnEvidence.id))) or 0

    response = client.post(
        f"/api/v1/v1.2/returns/{graph['return_id']}/evidence",
        headers=attacker,
        data={"evidence_type": EvidenceType.CHAT_SCREENSHOT.value},
        files={"file": ("attack.png", b"\x89PNG\r\n\x1a\nshould-not-save", "image/png")},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"

    after_files = {path for path in storage.root.rglob("*") if path.is_file()}
    with factory() as db:
        after_rows = db.scalar(select(func.count(ReturnEvidence.id))) or 0
    assert after_files == before_files
    assert after_rows == before_rows


def test_forged_evidence_upload_has_no_storage_or_database_side_effect(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    target = _login_headers(client, "franchise_demo", "Franchise123!")
    storage = get_storage()
    assert isinstance(storage, LocalObjectStorage)
    before_files = {path for path in storage.root.rglob("*") if path.is_file()}
    with factory() as db:
        before_rows = db.scalar(select(func.count(ReturnEvidence.id))) or 0

    response = client.post(
        f"/api/v1/v1.2/returns/{graph['return_id']}/evidence",
        headers=target,
        data={"evidence_type": EvidenceType.CHAT_SCREENSHOT.value},
        files={"file": ("proof.jpg", b"not-an-image", "image/jpeg")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "EVIDENCE_FILE_SIGNATURE_INVALID"

    after_files = {path for path in storage.root.rglob("*") if path.is_file()}
    with factory() as db:
        after_rows = db.scalar(select(func.count(ReturnEvidence.id))) or 0
    assert after_files == before_files
    assert after_rows == before_rows


def test_private_file_tokens_reject_swap_replay_forgery_expiry_and_cross_tenant_use(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    target = _login_headers(client, "franchise_demo", "Franchise123!")
    attacker = _login_headers(client, "security_attacker", "SecurityAttack123!")

    detail = client.get(f"/api/v1/v1.2/returns/{graph['return_id']}", headers=target)
    assert detail.status_code == 200
    evidence = {item["id"]: item for item in detail.json()["data"]["evidences"]}
    token_a = evidence[graph["evidence_a"]]["access_token"]

    valid = client.get(
        f"/api/v1/v1.2/return-evidences/{graph['evidence_a']}/download",
        params={"token": token_a},
        headers=target,
    )
    assert valid.status_code == 200

    swapped = client.get(
        f"/api/v1/v1.2/return-evidences/{graph['evidence_b']}/download",
        params={"token": token_a},
        headers=target,
    )
    assert swapped.status_code == 403
    assert swapped.json()["code"] == "FILE_TOKEN_MISMATCH"

    forged = client.get(
        f"/api/v1/v1.2/return-evidences/{graph['evidence_a']}/download",
        params={"token": token_a[:-1] + ("A" if token_a[-1] != "A" else "B")},
        headers=target,
    )
    assert forged.status_code == 403
    assert forged.json()["code"] == "FILE_TOKEN_INVALID"

    expired_token = create_file_access_token(
        str(graph["evidence_a"]), str(graph["target_user_id"]), expires_minutes=-1
    )
    expired = client.get(
        f"/api/v1/v1.2/return-evidences/{graph['evidence_a']}/download",
        params={"token": expired_token},
        headers=target,
    )
    assert expired.status_code == 403
    assert expired.json()["code"] == "FILE_TOKEN_INVALID"

    attacker_scoped_token = create_file_access_token(
        str(graph["evidence_a"]), str(graph["attacker_user_id"])
    )
    cross_tenant = client.get(
        f"/api/v1/v1.2/return-evidences/{graph['evidence_a']}/download",
        params={"token": attacker_scoped_token},
        headers=attacker,
    )
    assert cross_tenant.status_code == 403
    assert cross_tenant.json()["code"] == "FORBIDDEN"


def test_jwt_tamper_expiry_stale_session_and_disabled_company_are_rejected(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    attacker = _login_headers(client, "security_attacker", "SecurityAttack123!")
    token = attacker["Authorization"].split(" ", 1)[1]

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID"

    with factory() as db:
        attacker_user = db.get(User, graph["attacker_user_id"])
        assert attacker_user is not None
        stale = create_access_token(
            attacker_user.id,
            attacker_user.session_version + 1,
            ["FRANCHISE_OWNER"],
            attacker_user.company_id,
        )
        now = datetime.now(timezone.utc)
        expired = jwt.encode(
            {
                "sub": attacker_user.id,
                "sv": attacker_user.session_version,
                "roles": ["FRANCHISE_OWNER"],
                "company_id": attacker_user.company_id,
                "iat": int((now - timedelta(minutes=2)).timestamp()),
                "exp": int((now - timedelta(minutes=1)).timestamp()),
            },
            settings.jwt_secret,
            algorithm="HS256",
        )

    for bad_token in (stale, expired):
        invalid = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {bad_token}"}
        )
        assert invalid.status_code == 401
        assert invalid.json()["code"] == "AUTH_INVALID"

    with factory() as db:
        company = db.get(Company, graph["attacker_company_id"])
        assert company is not None
        company.status = "DISABLED"
        db.commit()
    disabled_company = client.get("/api/v1/auth/me", headers=attacker)
    assert disabled_company.status_code == 401
    assert disabled_company.json()["code"] == "AUTH_INVALID"


def test_oauth_state_redirect_invite_replay_and_cross_company_binding_are_rejected(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)

    for hostile_return in ("https://evil.example/steal", "//evil.example/steal"):
        start = client.get(
            "/api/v1/auth/wechat/start",
            params={"return_url": hostile_return},
            follow_redirects=False,
        )
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        payload = decode_signed_state(state, purpose="wechat-oauth")
        assert payload["return_url"] == "/h5/#/home"

    valid_state = create_signed_state(
        {"invite": None, "return_url": "/h5/#/home"}, purpose="wechat-oauth"
    )
    tampered_state = valid_state[:-1] + ("A" if valid_state[-1] != "A" else "B")
    tampered = client.get(
        "/api/v1/auth/wechat/callback",
        params={"code": "never-exchanged", "state": tampered_state},
        follow_redirects=False,
    )
    # P1-04：浏览器回调的绑定/授权失败统一 302 到 H5 状态页，不再返回裸 JSON。
    assert tampered.status_code == 302
    assert "/h5/auth-error.html?code=AUTH_OAUTH_STATE_INVALID" in tampered.headers["location"]

    expired_state = create_signed_state(
        {"invite": None, "return_url": "/h5/#/home"},
        purpose="wechat-oauth",
        expires_minutes=-1,
    )
    expired = client.get(
        "/api/v1/auth/wechat/callback",
        params={"code": "never-exchanged", "state": expired_state},
        follow_redirects=False,
    )
    assert expired.status_code == 302
    assert "/h5/auth-error.html?code=AUTH_OAUTH_STATE_INVALID" in expired.headers["location"]

    with factory() as db:
        # 本用例聚焦邀请链路：目标公司须处于未绑定主账号状态
        target_company = db.get(Company, graph["target_company_id"])
        assert target_company is not None
        target_company.primary_user_id = None
        _, target_raw, _ = create_company_invite(
            db, str(graph["target_company_id"]), None, 24
        )
        _, attacker_raw, _ = create_company_invite(
            db, str(graph["attacker_company_id"]), None, 24
        )
        db.commit()

    def _confirm_state(raw_invite: str) -> str:
        confirm = client.post(
            "/api/v1/auth/invites/confirm-start",
            json={"invite": raw_invite},
        )
        assert confirm.status_code == 200, confirm.text
        return parse_qs(
            urlparse(confirm.json()["data"]["authorization_url"]).query
        )["state"][0]

    first_state = _confirm_state(target_raw)
    first = client.post(
        "/api/v1/auth/wechat/mock-callback",
        json={
            "state": first_state,
            "openid": "security-openid-001",
            "nickname": "安全微信用户",
        },
    )
    assert first.status_code == 200, first.text

    # 已消费的邀请不能再发起绑定确认
    client.cookies.clear()
    replay_confirm = client.post(
        "/api/v1/auth/invites/confirm-start",
        json={"invite": target_raw},
    )
    assert replay_confirm.status_code == 400
    assert replay_confirm.json()["code"] == "AUTH_INVITE_INVALID"

    # 重放同一确认意图换第二个 openid：邀请已消费，拒绝
    replay = client.post(
        "/api/v1/auth/wechat/mock-callback",
        json={
            "state": first_state,
            "openid": "security-openid-002",
            "nickname": "重放攻击",
        },
    )
    assert replay.status_code == 400
    assert replay.json()["code"] == "AUTH_INVITE_INVALID"

    cross_company = client.post(
        "/api/v1/auth/wechat/mock-callback",
        json={
            "state": _confirm_state(attacker_raw),
            "openid": "security-openid-001",
            "nickname": "跨公司绑定攻击",
        },
    )
    assert cross_company.status_code == 409
    assert cross_company.json()["code"] == "AUTH_WECHAT_BOUND_OTHER_COMPANY"


def test_mock_callback_without_confirmation_intent_is_rejected(api_client) -> None:
    client, _ = api_client
    # 篡改签名的 state 不得通过校验
    valid_state = create_signed_state(
        {
            "invite_id": "forged-invite",
            "company_id": "forged-company",
            "binding_confirmed": True,
            "return_url": "/h5/#/home",
        },
        purpose="wechat-oauth-bind",
    )
    tampered_state = valid_state[:-1] + ("A" if valid_state[-1] != "A" else "B")
    tampered = client.post(
        "/api/v1/auth/wechat/mock-callback",
        json={
            "state": tampered_state,
            "openid": "security-forged-openid",
            "nickname": "伪造确认意图",
        },
    )
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "AUTH_OAUTH_STATE_INVALID"

    # legacy purpose 的 state 只允许已绑定用户登录，不允许触发首次绑定
    legacy_state = create_signed_state(
        {"invite": None, "return_url": "/h5/#/home"}, purpose="wechat-oauth"
    )
    legacy = client.post(
        "/api/v1/auth/wechat/mock-callback",
        json={
            "state": legacy_state,
            "openid": "security-unbound-openid",
            "nickname": "未绑定用户",
        },
    )
    assert legacy.status_code == 403
    assert legacy.json()["code"] == "AUTH_WECHAT_NOT_BOUND"


def test_points_idempotency_negative_balance_and_privilege_boundaries(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    attacker = _login_headers(client, "security_attacker", "SecurityAttack123!")

    forbidden_adjust = client.post(
        "/api/v1/points/adjust",
        headers=attacker,
        json={
            "company_id": graph["target_company_id"],
            "point_kind": "CUSTOMER",
            "delta": 10,
            "reason": "越权调整积分",
            "idempotency_key": "security-forbidden-adjust",
        },
    )
    assert forbidden_adjust.status_code == 403

    client.post("/api/v1/auth/logout", headers=attacker)
    admin = _login_headers(client, "admin", "Admin123!")
    key = "security-idempotent-adjust"
    first = client.post(
        "/api/v1/points/adjust",
        headers=admin,
        json={
            "company_id": graph["attacker_company_id"],
            "point_kind": "CUSTOMER",
            "delta": 25,
            "reason": "安全测试幂等加分",
            "idempotency_key": key,
        },
    )
    second = client.post(
        "/api/v1/points/adjust",
        headers=admin,
        json={
            "company_id": graph["attacker_company_id"],
            "point_kind": "CUSTOMER",
            "delta": 25,
            "reason": "安全测试幂等加分重复请求",
            "idempotency_key": key,
        },
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    with factory() as db:
        account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == graph["attacker_company_id"]
            )
        )
        assert account is not None
        assert account.balance == int(graph["attacker_balance"]) + 25
        before_negative = int(account.balance)

    negative = client.post(
        "/api/v1/points/adjust",
        headers=admin,
        json={
            "company_id": graph["attacker_company_id"],
            "point_kind": "CUSTOMER",
            "delta": -100000,
            "reason": "安全测试禁止负余额",
            "idempotency_key": "security-negative-balance",
        },
    )
    assert negative.status_code == 409
    assert negative.json()["code"] == "POINTS_INSUFFICIENT"
    with factory() as db:
        account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.company_id == graph["attacker_company_id"]
            )
        )
        assert account is not None and account.balance == before_negative


def test_return_reward_review_permissions_and_reward_settlement_are_idempotent(api_client) -> None:
    client, factory = api_client
    graph = _seed_cross_tenant_graph(factory)
    attacker = _login_headers(client, "security_attacker", "SecurityAttack123!")

    final_review = client.post(
        f"/api/v1/v1.2/returns/{graph['return_id']}/final-review",
        headers=attacker,
        json={"decision": "APPROVE", "note": "越权审核"},
    )
    assert final_review.status_code == 403
    settle = client.post(
        f"/api/v1/v1.2/admin/supplier-rewards/{graph['reward_id']}/settle",
        headers=attacker,
    )
    assert settle.status_code == 403
    reverse = client.post(
        f"/api/v1/v1.2/admin/supplier-rewards/{graph['reward_id']}/reverse",
        headers=attacker,
        json={"reason_code": "ADMIN_ERROR", "note": "越权冲正"},
    )
    assert reverse.status_code == 403

    client.post("/api/v1/auth/logout", headers=attacker)
    # The authorization fixture intentionally contains a self-supplied lead.
    # Settlement requires a genuine other-company supply and an elapsed window.
    with factory() as db:
        supplier = Company(
            code="SEC-REWARD-SUP", name="结算安全测试供资方", status="ACTIVE"
        )
        db.add(supplier)
        db.flush()
        reward = db.get(SupplierLeadReward, graph["reward_id"])
        assignment = db.get(Assignment, reward.assignment_id)
        lead = db.get(Lead, assignment.lead_id)
        reward.supplier_company_id = supplier.id
        assignment.supplier_company_id = supplier.id
        lead.supplier_company_id = supplier.id
        assignment.claimed_at = datetime.now(timezone.utc) - timedelta(hours=49)
        assignment.assigned_at = assignment.claimed_at - timedelta(hours=1)
        db.commit()
    admin = _login_headers(client, "admin", "Admin123!")
    first = client.post(
        f"/api/v1/v1.2/admin/supplier-rewards/{graph['reward_id']}/settle",
        headers=admin,
        json={"note": "安全测试奖励结算"},
    )
    second = client.post(
        f"/api/v1/v1.2/admin/supplier-rewards/{graph['reward_id']}/settle",
        headers=admin,
        json={"note": "安全测试重复结算"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    with factory() as db:
        reward = db.get(SupplierLeadReward, graph["reward_id"])
        assert reward is not None and reward.status == RewardStatus.SETTLED.value
        ledgers = db.scalars(
            select(PointsLedger).where(
                PointsLedger.business_type == "V12_SUPPLIER_REWARD",
                PointsLedger.business_id == graph["reward_id"],
                PointsLedger.ledger_type == PointsLedgerType.REWARD.value,
            )
        ).all()
        assert len(ledgers) == 1
        assert reward.ledger_id == ledgers[0].id


def test_audit_sanitizer_never_persists_credentials_tokens_or_plain_phone(api_client) -> None:
    _, factory = api_client
    with factory() as db:
        write_audit(
            db,
            principal=None,
            action="SECURITY_SANITIZE_PROBE",
            resource_type="security_test",
            before={
                "password": "PlainPassword123!",
                "phone": "13800138000",
                "nested": {"authorization": "Bearer secret-token"},
            },
            after={
                "access_token": "raw-jwt-value",
                "cookie": "access_token=raw-cookie-value",
                "phone_masked": "138****8000",
            },
            metadata={
                "client_secret": "wechat-secret",
                "phone_hash": "phone-hash-value",
            },
        )
        db.commit()
        item = db.scalar(
            select(AuditLog).where(AuditLog.action == "SECURITY_SANITIZE_PROBE")
        )
        assert item is not None
        serialized = str(
            {
                "before": item.before_json,
                "after": item.after_json,
                "metadata": item.metadata_json,
            }
        )
        for secret in (
            "PlainPassword123!",
            "13800138000",
            "secret-token",
            "raw-jwt-value",
            "raw-cookie-value",
            "wechat-secret",
            "phone-hash-value",
        ):
            assert secret not in serialized
        assert "138****8000" in serialized
        assert "[REDACTED]" in serialized
