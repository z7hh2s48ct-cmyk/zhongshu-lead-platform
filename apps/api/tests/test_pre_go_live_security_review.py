from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from sqlalchemy import func, select

from apps.api.src.core.enums import AssignmentStatus, EvidenceType, PointsLedgerType
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    Company,
    Lead,
    PointsAccount,
    PointsLedger,
    ReturnEvidence,
    ReturnRequest,
    User,
    WechatIdentity,
)
from apps.api.src.core.security import encrypt_text, fingerprint_phone, hash_phone
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status, ReturnV12Status
from apps.api.src.services.audit import write_audit
from apps.api.src.services.auth_service import bind_wechat_by_invite, create_company_invite, create_internal_user
from apps.api.src.services.storage import create_file_access_token, get_storage


def _login_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _seed_same_company_evidence(factory) -> dict[str, str]:
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        target = db.scalar(select(User).where(User.username == "franchise_demo"))
        operation = db.scalar(select(User).where(User.username == "operation"))
        assert company is not None and target is not None and operation is not None
        peer = create_internal_user(
            db,
            username="security_same_company_peer",
            password="SecurityPeer123!",
            display_name="同公司第二用户",
            role_code="FRANCHISE_OWNER",
            company_id=company.id,
        )
        now = datetime.now(timezone.utc)
        phone = "13900139992"
        lead = Lead(
            source_type=LeadSourceKind.SUPPLIER_H5.value,
            source_kind=LeadSourceKind.SUPPLIER_H5.value,
            submitter_user_id=target.id,
            supplier_company_id=company.id,
            customer_name="文件令牌用户绑定测试",
            phone_encrypted=encrypt_text(phone),
            phone_hash=hash_phone(phone),
            phone_fingerprint=fingerprint_phone(phone),
            consent_confirmed=True,
            city="上海市",
            district="浦东新区",
            region_code="310115",
            category_code="OLD_RENOVATION",
            brand_code="ZHONGSHU",
            need_summary="same-company file token binding test",
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
            status=AssignmentStatus.CLAIMED.value,
            points_price=100,
            claim_points=100,
            lead_snapshot={"phone_masked": "139****9992"},
            assigned_by=operation.id,
            assigned_at=now - timedelta(hours=2),
            claimed_at=now - timedelta(hours=1),
            appeal_deadline_at=now + timedelta(days=3),
            idempotency_key="security-same-company-token-assignment",
        )
        db.add(assignment)
        db.flush()
        request = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=lead.id,
            company_id=company.id,
            reason_code="INFO_ERROR",
            description="同公司不同用户文件令牌测试",
            status=ReturnV12Status.DRAFT.value,
            submitted_by=target.id,
            due_at=now + timedelta(days=3),
            appeal_deadline_at=now + timedelta(days=3),
        )
        db.add(request)
        db.flush()
        stored = get_storage().save(
            b"\x89PNG\r\n\x1a\npeer-binding-proof",
            prefix=f"security/peer/{request.id}",
            filename="peer-proof.png",
            mime_type="image/png",
        )
        evidence = ReturnEvidence(
            return_request_id=request.id,
            evidence_type=EvidenceType.CHAT_SCREENSHOT.value,
            object_key=stored.object_key,
            original_name="peer-proof.png",
            mime_type="image/png",
            file_size=stored.size,
            sha256=stored.sha256,
            uploaded_by=target.id,
        )
        db.add(evidence)
        db.commit()
        return {
            "company_id": company.id,
            "target_user_id": target.id,
            "peer_user_id": peer.id,
            "evidence_id": evidence.id,
        }


def test_file_token_cannot_be_reused_by_second_user_in_same_company(api_client) -> None:
    client, factory = api_client
    graph = _seed_same_company_evidence(factory)
    peer_headers = _login_headers(
        client, "security_same_company_peer", "SecurityPeer123!"
    )
    target_token = create_file_access_token(
        graph["evidence_id"], graph["target_user_id"]
    )
    response = client.get(
        f"/api/v1/v1.2/return-evidences/{graph['evidence_id']}/download",
        params={"token": target_token},
        headers=peer_headers,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FILE_TOKEN_MISMATCH"


def test_parallel_invite_callbacks_still_consume_exactly_once(api_client) -> None:
    """I20：并行回调的「一次性消费」不变量（SQLite 串行化口径）。

    SQLite 上 with_for_update 是 no-op，本测试对行锁语义没有证据价值；
    锁语义（原子消费、创建/绑定交叉竞争、并发重复撤销）由
    test_invite_binding_postgres_concurrency_e2e.py 在一次性 PostgreSQL
    上经 run_v12_e2e 执行守护。这里锁定的是调度无关的不变量：无论两个
    回调以何种顺序交错，恰好一次成功绑定、一次明确拒绝、只落一个身份。
    """

    _, factory = api_client
    with factory() as db:
        # SH-DEMO 已绑定主账号，改用未绑定的独立公司验证邀请一次性消费。
        company = Company(code="SEC-INVITE", name="邀请并发安全公司", status="ACTIVE")
        db.add(company)
        db.flush()
        _, raw, _ = create_company_invite(db, company.id, None, 24)
        db.commit()

    barrier = Barrier(2)

    def bind(index: int) -> tuple[str, str | None]:
        with factory() as db:
            # 两连接全部就绪后再发起，排除「一个完全结束后另一个才开始」的假并发。
            barrier.wait(timeout=10)
            try:
                user, _ = bind_wechat_by_invite(
                    db,
                    raw,
                    f"security-concurrent-openid-{index}",
                    f"并发邀请用户{index}",
                )
                db.commit()
                return "OK", user.id
            except AppError as exc:
                db.rollback()
                return exc.code, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(bind, index) for index in range(2)]
        results = [future.result() for future in futures]

    assert [code for code, _ in results].count("OK") == 1
    assert [code for code, _ in results].count("AUTH_INVITE_INVALID") == 1
    with factory() as db:
        identities = db.scalar(
            select(func.count(WechatIdentity.openid)).where(
                WechatIdentity.openid.like("security-concurrent-openid-%")
            )
        )
        assert identities == 1


def test_points_same_idempotency_key_is_atomic_under_concurrent_requests(api_client) -> None:
    client, factory = api_client
    admin = _login_headers(client, "admin", "Admin123!")
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == company.id)
        )
        assert account is not None
        company_id = company.id
        before = int(account.balance)

    payload = {
        "company_id": company_id,
        "point_kind": "CUSTOMER",
        "delta": 37,
        "reason": "并发幂等安全负例",
        "idempotency_key": "security-concurrent-points-idempotency",
    }

    def adjust(_: int):
        return client.post("/api/v1/points/adjust", headers=admin, json=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(adjust, range(2)))

    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    ledger_ids = {response.json()["data"]["id"] for response in responses}
    assert len(ledger_ids) == 1
    with factory() as db:
        account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == company_id)
        )
        ledgers = db.scalars(
            select(PointsLedger).where(
                PointsLedger.company_id == company_id,
                PointsLedger.idempotency_key
                == "security-concurrent-points-idempotency",
            )
        ).all()
        assert account is not None and int(account.balance) == before + 37
        assert len(ledgers) == 1
        assert ledgers[0].ledger_type == PointsLedgerType.ADJUST.value


def test_audit_free_text_redacts_phone_jwt_bearer_and_cookie(api_client) -> None:
    _, factory = api_client
    jwt_text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSJ9.c2lnbmF0dXJl"
    with factory() as db:
        write_audit(
            db,
            principal=None,
            action="SECURITY_FREE_TEXT_REDACTION",
            resource_type="security_test",
            metadata={
                "note": f"客户 13800138000 粘贴 {jwt_text}",
                "description": "Authorization was Bearer abc.def.ghi",
            },
            reason="cookie access_token=raw-cookie-token should not persist",
        )
        db.commit()
        item = db.scalar(
            select(apps_api_audit_log := __import__(
                "apps.api.src.core.models", fromlist=["AuditLog"]
            ).AuditLog).where(apps_api_audit_log.action == "SECURITY_FREE_TEXT_REDACTION")
        )
        assert item is not None
        serialized = str(item.metadata_json)
        for secret in (
            "13800138000",
            jwt_text,
            "abc.def.ghi",
            "raw-cookie-token",
        ):
            assert secret not in serialized
        assert "[REDACTED]" in serialized


def test_disabled_company_cookie_is_rejected_by_api_and_web_entry(api_client) -> None:
    client, factory = api_client
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "franchise_demo", "password": "Franchise123!"},
    )
    assert login.status_code == 200
    token = login.cookies.get("access_token")
    assert token
    with factory() as db:
        company = db.scalar(select(Company).where(Company.code == "SH-DEMO"))
        assert company is not None
        company.status = "DISABLED"
        db.commit()

    api = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert api.status_code == 401
    h5 = client.get("/h5/", follow_redirects=False)
    assert h5.status_code == 302
    assert h5.headers["location"] == "/h5/v12-workbench.html"

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "Admin123!"},
    )
    assert admin_login.status_code == 200
    admin = client.get("/admin/", follow_redirects=False)
    assert admin.status_code == 302
    assert admin.headers["location"] == "/admin/v12-operations.html"
