from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    AuditLog,
    Company,
    CompanyAccountRequest,
    FollowUp,
    InviteToken,
    Lead,
    LeadDuplicateRelation,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnEvidence,
    ReturnRequest,
    StorageCleanupOutbox,
    SupplyTerminationRequest,
    User,
    WechatIdentity,
)
from apps.api.src.core.models_v12 import LeadDedupEvent, SupplierLeadReward
from apps.api.src.services.auth_service import bind_wechat_by_invite
from apps.api.src.core.security import encrypt_text
from apps.api.src.services.outbox_worker import process_outbox
from apps.api.src.services.storage import get_storage
from apps.api.src.services.storage_cleanup_worker import process_storage_cleanup


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.cookies.get('access_token')}"}


def _create_company(
    client,
    headers: dict[str, str],
    *,
    name: str,
    is_test: bool,
) -> str:
    response = client.post(
        "/api/v1/companies/simple",
        headers=headers,
        json={
            "name": name,
            "owner_name": "测试负责人",
            "primary_city_code": "310000",
            "district_codes": ["310104"],
            "serve_all_districts": False,
            "is_test": is_test,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _bind_company(client, factory, headers: dict[str, str], company_id: str, openid: str) -> str:
    response = client.post(
        f"/api/v1/auth/companies/{company_id}/invites",
        headers=headers,
        json={"expires_hours": 72},
    )
    assert response.status_code == 200, response.text
    raw_token = response.json()["data"]["token"]
    with factory() as db:
        user, _ = bind_wechat_by_invite(db, raw_token, openid, "微信负责人")
        db.commit()
        return user.id


def _disable_company(client, headers: dict[str, str], company_id: str) -> None:
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        headers=headers,
        json={"status": "DISABLED", "reason": "业务隔离"},
    )
    assert response.status_code == 200, response.text


def _purge_preview(client, headers: dict[str, str], company_id: str) -> dict:
    response = client.get(
        f"/api/v1/companies/{company_id}/purge-preview",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _mark_test_body(
    client,
    headers: dict[str, str],
    company_id: str,
    *,
    confirm_name: str,
    reason: str,
) -> dict[str, str]:
    preview = _purge_preview(client, headers, company_id)
    return {
        "confirm_name": confirm_name,
        "reason": reason,
        "confirm_phrase": "永久删除测试数据",
        "scope_token": preview["scope_token"],
    }


def test_company_disable_isolates_business_without_releasing_wechat(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="停用隔离测试加盟商",
        is_test=False,
    )
    user_id = _bind_company(client, factory, admin, company_id, "openid-disable-kept")

    with factory() as db:
        session_version = db.get(User, user_id).session_version

    _disable_company(client, admin, company_id)

    with factory() as db:
        company = db.get(Company, company_id)
        user = db.get(User, user_id)
        identity = db.scalar(
            select(WechatIdentity).where(WechatIdentity.openid == "openid-disable-kept")
        )
        assert company is not None
        assert company.status == "DISABLED"
        assert company.primary_user_id == user_id
        assert user is not None and user.session_version == session_version + 1
        assert identity is not None and identity.user_id == user_id


def test_company_disable_revokes_invites_and_cancels_stale_delivery(api_client) -> None:
    client, factory = api_client
    operation = _login(client, "operation", "Operation123!")
    company_id = _create_company(
        client,
        operation,
        name="停用邀请隔离测试",
        is_test=True,
    )
    invite_response = client.post(
        f"/api/v1/auth/companies/{company_id}/invites",
        headers=operation,
        json={"expires_hours": 72},
    )
    assert invite_response.status_code == 200, invite_response.text
    invite_id = invite_response.json()["data"]["invite_id"]

    _disable_company(client, operation, company_id)

    with factory() as db:
        invite = db.get(InviteToken, invite_id)
        outbox = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.aggregate_type == "invite",
                NotificationOutbox.aggregate_id == invite_id,
            )
        )
        assert invite is not None and invite.revoked_at is not None
        assert outbox is not None and outbox.status == "CANCELLED"

        # 模拟 worker 在停用交易前已读到的旧队列状态；发送边界仍必须二次校验。
        outbox.status = "PENDING"
        db.commit()
        result = process_outbox(db)
        db.commit()
        assert result["cancelled"] == 1
        assert outbox.status == "CANCELLED"


def test_unbind_releases_wechat_and_allows_new_company_binding(api_client) -> None:
    client, factory = api_client
    operation = _login(client, "operation", "Operation123!")
    old_company_id = _create_company(
        client,
        operation,
        name="原测试加盟商",
        is_test=True,
    )
    new_company_id = _create_company(
        client,
        operation,
        name="新测试加盟商",
        is_test=True,
    )
    old_user_id = _bind_company(
        client,
        factory,
        operation,
        old_company_id,
        "openid-move-owner",
    )

    active_response = client.post(
        f"/api/v1/companies/{old_company_id}/wechat-binding/unbind",
        headers=operation,
        json={"confirm_name": "原测试加盟商", "reason": "负责人改签新公司"},
    )
    assert active_response.status_code == 409, active_response.text
    assert active_response.json()["code"] == "COMPANY_MUST_BE_DISABLED"

    _disable_company(client, operation, old_company_id)
    response = client.post(
        f"/api/v1/companies/{old_company_id}/wechat-binding/unbind",
        headers=operation,
        json={"confirm_name": "原测试加盟商", "reason": "负责人改签新公司"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["unbound_user_id"] == old_user_id

    new_user_id = _bind_company(
        client,
        factory,
        operation,
        new_company_id,
        "openid-move-owner",
    )
    assert new_user_id != old_user_id

    with factory() as db:
        old_company = db.get(Company, old_company_id)
        new_company = db.get(Company, new_company_id)
        old_user = db.get(User, old_user_id)
        identity = db.scalar(
            select(WechatIdentity).where(WechatIdentity.openid == "openid-move-owner")
        )
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "COMPANY_WECHAT_UNBIND",
                AuditLog.resource_id == old_company_id,
            )
        )
        assert old_company is not None and old_company.primary_user_id is None
        assert new_company is not None and new_company.primary_user_id == new_user_id
        assert old_user is not None and old_user.status == "DISABLED"
        assert identity is not None and identity.user_id == new_user_id
        assert audit is not None
        assert audit.metadata_json["reason"] == "负责人改签新公司"


def test_delete_removes_disabled_test_company_and_binding(api_client) -> None:
    client, factory = api_client
    operation = _login(client, "operation", "Operation123!")
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        operation,
        name="可删除测试主体",
        is_test=True,
    )
    owner_user_id = _bind_company(
        client,
        factory,
        operation,
        company_id,
        "openid-delete-test",
    )
    with factory() as db:
        db.add(SupplyTerminationRequest(
            company_id=company_id,
            status="CANCELLED",
            reason="测试结算记录",
            requested_by=owner_user_id,
            payee_name_encrypted=encrypt_text("测试收款人"),
            payee_account_encrypted=encrypt_text("6222000012345678"),
            payment_method="BANK_TRANSFER",
        ))
        db.commit()
    _disable_company(client, operation, company_id)
    assert _purge_preview(client, admin, company_id)["counts"]["supply_terminations"] == 1
    with factory() as db:
        unrelated_notification = Notification(
            user_id=owner_user_id,
            company_id=None,
            scene="PLATFORM_NOTICE",
            title="无关平台通知",
            body="这条消息与测试加盟商删除无关",
            deep_link="/h5/#/notifications/platform-notice",
            status="CREATED",
        )
        db.add(unrelated_notification)
        db.commit()
        unrelated_notification_id = unrelated_notification.id

    forbidden = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=operation,
        json={"confirm_name": "可删除测试主体", "reason": "清理历史联测数据"},
    )
    assert forbidden.status_code == 403, forbidden.text

    response = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"confirm_name": "可删除测试主体", "reason": "清理历史联测数据"},
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        assert db.get(Company, company_id) is None
        assert db.scalar(
            select(WechatIdentity).where(WechatIdentity.openid == "openid-delete-test")
        ) is None
        owner = db.get(User, owner_user_id)
        assert owner is not None
        assert owner.status == "DISABLED"
        assert owner.company_id is None
        assert db.get(Notification, unrelated_notification_id) is not None
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "COMPANY_TEST_DELETE",
                AuditLog.resource_id == company_id,
            )
        )
        assert audit is not None
        assert audit.before_json["name"] == "可删除测试主体"
        assert audit.metadata_json["reason"] == "清理历史联测数据"


def test_existing_zero_business_company_requires_superadmin_to_mark_as_test(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    operation = _login(client, "operation", "Operation123!")
    company_id = _create_company(
        client,
        admin,
        name="历史联测主体",
        is_test=False,
    )
    _disable_company(client, admin, company_id)

    forbidden = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=operation,
        json={
            "confirm_name": "历史联测主体",
            "reason": "清理历史联测数据",
            "confirm_phrase": "永久删除测试数据",
            "scope_token": "0" * 64,
        },
    )
    assert forbidden.status_code == 403, forbidden.text

    preview = _purge_preview(client, admin, company_id)
    assert preview["counts"]["assignments"] == 0
    response = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=admin,
        json=_mark_test_body(
            client,
            admin,
            company_id,
            confirm_name="历史联测主体",
            reason="清理历史联测数据",
        ),
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        company = db.get(Company, company_id)
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "COMPANY_TEST_MARK",
                AuditLog.resource_id == company_id,
            )
        )
        assert company is not None and company.is_test is True
        assert audit is not None
        assert audit.metadata_json["reason"] == "清理历史联测数据"


def test_superadmin_can_mark_and_delete_historical_test_company_with_points(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="历史有积分联测主体",
        is_test=False,
    )
    _disable_company(client, admin, company_id)
    with factory() as db:
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
        assert account is not None
        account.balance = 50
        db.add(
            PointsLedger(
                account_id=account.id,
                company_id=company_id,
                ledger_type="RECHARGE",
                delta=50,
                balance_after=50,
                business_type="RECHARGE",
                business_id="historical-test-recharge",
                idempotency_key="historical-test-recharge",
            )
        )
        db.commit()

    preview = _purge_preview(client, admin, company_id)
    assert preview["counts"]["points_ledgers"] == 1
    marked = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=admin,
        json=_mark_test_body(
            client,
            admin,
            company_id,
            confirm_name="历史有积分联测主体",
            reason="确认为历史联测账号",
        ),
    )
    assert marked.status_code == 200, marked.text

    deleted = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"confirm_name": "历史有积分联测主体", "reason": "清理历史联测数据"},
    )
    assert deleted.status_code == 200, deleted.text
    with factory() as db:
        assert db.get(Company, company_id) is None
        assert db.scalar(select(PointsLedger).where(PointsLedger.company_id == company_id)) is None


def test_historical_company_with_platform_assignment_can_be_marked_and_deleted(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="与平台客资关联的主体",
        is_test=False,
    )
    with factory() as db:
        operator = db.scalar(select(User).where(User.username == "admin"))
        assert operator is not None
        lead = Lead(
            customer_name="平台客资",
            phone_encrypted="test-encrypted-phone",
            phone_hash="platform-cross-business-phone",
            status="READY_DISPATCH",
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company_id,
            status="PENDING_CLAIM",
            points_price=100,
            lead_snapshot={},
            assigned_by=operator.id,
        )
        db.add(assignment)
        db.flush()
        lead.current_assignment_id = assignment.id
        lead.status = "DISPATCHED"
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id

    _disable_company(client, admin, company_id)
    preview = _purge_preview(client, admin, company_id)
    assert preview["counts"]["assignments"] == 1
    assert preview["cross_company_impact"]["companies"] == 0
    response = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=admin,
        json=_mark_test_body(
            client,
            admin,
            company_id,
            confirm_name="与平台客资关联的主体",
            reason="尝试标记为测试",
        ),
    )
    assert response.status_code == 200, response.text

    deleted = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"confirm_name": "与平台客资关联的主体", "reason": "清理历史测试派发"},
    )
    assert deleted.status_code == 200, deleted.text
    with factory() as db:
        assert db.get(Company, company_id) is None
        preserved_lead = db.get(Lead, lead_id)
        assert preserved_lead is not None
        assert preserved_lead.current_assignment_id is None
        assert preserved_lead.status == "READY_DISPATCH"
        assert db.get(Assignment, assignment_id) is None


def test_mark_test_requires_current_scope_preview_and_strong_confirmation(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="强确认历史联测主体",
        is_test=False,
    )
    _disable_company(client, admin, company_id)

    missing_confirmation = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=admin,
        json={"confirm_name": "强确认历史联测主体", "reason": "确认联测数据"},
    )
    assert missing_confirmation.status_code == 422, missing_confirmation.text

    stale_preview = _purge_preview(client, admin, company_id)
    with factory() as db:
        db.add(
            Lead(
                customer_name="预览后新增测试客资",
                phone_encrypted="test-encrypted-phone",
                phone_hash="stale-preview-test-phone",
                status="READY_DISPATCH",
                source_kind="SUPPLIER_H5",
                supplier_company_id=company_id,
            )
        )
        db.commit()

    stale = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=admin,
        json={
            "confirm_name": "强确认历史联测主体",
            "reason": "确认联测数据",
            "confirm_phrase": "永久删除测试数据",
            "scope_token": stale_preview["scope_token"],
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "COMPANY_PURGE_PREVIEW_STALE"

    current_preview = _purge_preview(client, admin, company_id)
    assert current_preview["counts"]["leads"] == 1
    marked = client.post(
        f"/api/v1/companies/{company_id}/mark-test",
        headers=admin,
        json={
            "confirm_name": "强确认历史联测主体",
            "reason": "确认联测数据",
            "confirm_phrase": "永久删除测试数据",
            "scope_token": current_preview["scope_token"],
        },
    )
    assert marked.status_code == 200, marked.text


def test_test_supplier_and_dispatched_lead_can_be_deleted_without_deleting_receiver(
    api_client,
) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    supplier_id = _create_company(
        client,
        admin,
        name="跨主体供资测试方",
        is_test=True,
    )
    receiver_id = _create_company(
        client,
        admin,
        name="真实接收方",
        is_test=False,
    )
    with factory() as db:
        operator = db.scalar(select(User).where(User.username == "admin"))
        assert operator is not None
        lead = Lead(
            customer_name="已派给其他主体的客资",
            phone_encrypted="test-encrypted-phone",
            phone_hash="cross-company-supplier-lead",
            status="CLAIMED",
            source_kind="SUPPLIER_H5",
            supplier_company_id=supplier_id,
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=receiver_id,
            supplier_company_id=None,
            receiver_company_id=receiver_id,
            status="CLAIMED",
            points_price=100,
            claim_points=100,
            lead_snapshot={},
            assigned_by=operator.id,
        )
        db.add(assignment)
        db.flush()
        receiver_account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == receiver_id)
        )
        assert receiver_account is not None
        receiver_account.balance = 50
        recharge_ledger = PointsLedger(
            account_id=receiver_account.id,
            company_id=receiver_id,
            ledger_type="RECHARGE",
            delta=100,
            balance_after=100,
            business_type="POINTS_PACKAGE",
            business_id="cross-company-test-recharge",
            idempotency_key="cross-company-test-recharge",
        )
        claim_ledger = PointsLedger(
            account_id=receiver_account.id,
            company_id=receiver_id,
            ledger_type="CLAIM",
            delta=-100,
            balance_after=0,
            business_type="V12_ASSIGNMENT_CLAIM",
            business_id=assignment.id,
            idempotency_key="cross-company-test-lead-claim",
        )
        later_ledger = PointsLedger(
            account_id=receiver_account.id,
            company_id=receiver_id,
            ledger_type="ADJUST",
            delta=50,
            balance_after=50,
            business_type="MANUAL_ADJUSTMENT",
            business_id="cross-company-test-later-adjustment",
            idempotency_key="cross-company-test-later-adjustment",
        )
        receiver_notification = Notification(
            company_id=receiver_id,
            scene="CLAIM_SUCCESS",
            title="测试客资已领取",
            body="该消息应随测试派发一并清理",
            deep_link=f"/h5/#/leads/{assignment.id}",
            status="CREATED",
        )
        db.add_all([recharge_ledger, claim_ledger, later_ledger, receiver_notification])
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id
        receiver_account_id = receiver_account.id
        claim_ledger_id = claim_ledger.id
        recharge_ledger_id = recharge_ledger.id
        later_ledger_id = later_ledger.id
        receiver_notification_id = receiver_notification.id

    _disable_company(client, admin, supplier_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{supplier_id}",
        headers=admin,
        json={"confirm_name": "跨主体供资测试方", "reason": "清理已派发测试客资"},
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(Company, supplier_id) is None
        assert db.get(Lead, lead_id) is None
        assert db.get(Assignment, assignment_id) is None
        assert db.get(Company, receiver_id) is not None
        assert db.get(PointsLedger, claim_ledger_id) is None
        assert db.get(Notification, receiver_notification_id) is None
        receiver_account = db.get(PointsAccount, receiver_account_id)
        assert receiver_account is not None
        assert receiver_account.balance == 150
        recharge_ledger = db.get(PointsLedger, recharge_ledger_id)
        later_ledger = db.get(PointsLedger, later_ledger_id)
        assert recharge_ledger is not None and recharge_ledger.balance_after == 100
        assert later_ledger is not None and later_ledger.balance_after == 150


def test_delete_rechecks_formal_duplicate_after_test_source_is_removed(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    test_company_id = _create_company(
        client,
        admin,
        name="去重源测试主体",
        is_test=True,
    )
    now = datetime.now(timezone.utc)
    with factory() as db:
        test_lead = Lead(
            customer_name="将被删除的测试客资",
            phone_encrypted="test-encrypted-phone",
            phone_hash="dedup-after-purge",
            phone_fingerprint="dedup-after-purge-fingerprint",
            status="READY_DISPATCH",
            source_kind="SUPPLIER_H5",
            supplier_company_id=test_company_id,
            imported_at=now - timedelta(days=1),
        )
        formal_lead = Lead(
            customer_name="应恢复的正式客资",
            phone_encrypted="formal-encrypted-phone",
            phone_hash="dedup-after-purge",
            phone_fingerprint="dedup-after-purge-fingerprint",
            status="DUPLICATE",
            pending_reason="HARD_DUPLICATE",
            duplicate_status="HARD_DUPLICATE",
            review_status="APPROVED",
            source_kind="PLATFORM_MANUAL",
            imported_at=now,
        )
        db.add_all([test_lead, formal_lead])
        db.flush()
        db.add_all(
            [
                LeadDedupEvent(
                    lead_id=formal_lead.id,
                    phone_fingerprint=formal_lead.phone_fingerprint,
                    checkpoint="SUBMIT",
                    decision="HARD_DUPLICATE",
                    matched_lead_id=test_lead.id,
                    window_days=30,
                    details_json={"age_days": 1},
                ),
                LeadDuplicateRelation(
                    lead_id=formal_lead.id,
                    duplicate_lead_id=test_lead.id,
                    reason="V12_HARD_DUPLICATE",
                ),
            ]
        )
        db.commit()
        formal_lead_id = formal_lead.id

    _disable_company(client, admin, test_company_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{test_company_id}",
        headers=admin,
        json={"confirm_name": "去重源测试主体", "reason": "清理会阻断正式客资的测试源"},
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        formal_lead = db.get(Lead, formal_lead_id)
        assert formal_lead is not None
        assert formal_lead.status == "READY_DISPATCH"
        assert formal_lead.duplicate_status == "CLEAR"
        assert formal_lead.pending_reason is None
        latest = db.scalar(
            select(LeadDedupEvent)
            .where(LeadDedupEvent.lead_id == formal_lead_id)
            .order_by(LeadDedupEvent.created_at.desc(), LeadDedupEvent.id.desc())
        )
        assert latest is not None
        assert latest.decision == "CLEAR"
        assert latest.matched_lead_id is None


def test_delete_keeps_formal_duplicate_when_another_formal_match_remains(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    test_company_id = _create_company(
        client,
        admin,
        name="多源去重测试主体",
        is_test=True,
    )
    now = datetime.now(timezone.utc)
    with factory() as db:
        test_lead = Lead(
            customer_name="将被删除的重复源",
            phone_encrypted="test-encrypted-phone",
            phone_hash="dedup-formal-remains",
            phone_fingerprint="dedup-formal-remains-fingerprint",
            status="READY_DISPATCH",
            source_kind="SUPPLIER_H5",
            supplier_company_id=test_company_id,
            imported_at=now - timedelta(days=1),
        )
        surviving_match = Lead(
            customer_name="保留的正式重复源",
            phone_encrypted="surviving-encrypted-phone",
            phone_hash="dedup-formal-remains",
            phone_fingerprint="dedup-formal-remains-fingerprint",
            status="READY_DISPATCH",
            source_kind="PLATFORM_MANUAL",
            imported_at=now - timedelta(hours=12),
        )
        formal_lead = Lead(
            customer_name="仍应阻断的正式客资",
            phone_encrypted="formal-encrypted-phone",
            phone_hash="dedup-formal-remains",
            phone_fingerprint="dedup-formal-remains-fingerprint",
            status="DUPLICATE",
            pending_reason="HARD_DUPLICATE",
            duplicate_status="HARD_DUPLICATE",
            review_status="APPROVED",
            source_kind="PLATFORM_MANUAL",
            imported_at=now,
        )
        db.add_all([test_lead, surviving_match, formal_lead])
        db.flush()
        db.add(
            LeadDedupEvent(
                lead_id=formal_lead.id,
                phone_fingerprint=formal_lead.phone_fingerprint,
                checkpoint="SUBMIT",
                decision="HARD_DUPLICATE",
                matched_lead_id=test_lead.id,
                window_days=30,
                details_json={"age_days": 1},
            )
        )
        db.commit()
        formal_lead_id = formal_lead.id
        surviving_match_id = surviving_match.id

    _disable_company(client, admin, test_company_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{test_company_id}",
        headers=admin,
        json={"confirm_name": "多源去重测试主体", "reason": "清理但保留真实重复判定"},
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        formal_lead = db.get(Lead, formal_lead_id)
        assert formal_lead is not None
        assert formal_lead.status == "DUPLICATE"
        assert formal_lead.duplicate_status == "HARD_DUPLICATE"
        assert formal_lead.pending_reason == "HARD_DUPLICATE"
        latest = db.scalar(
            select(LeadDedupEvent)
            .where(LeadDedupEvent.lead_id == formal_lead_id)
            .order_by(LeadDedupEvent.created_at.desc(), LeadDedupEvent.id.desc())
        )
        assert latest is not None
        assert latest.decision == "HARD_DUPLICATE"
        assert latest.matched_lead_id == surviving_match_id


def test_delete_reblocks_overridden_formal_lead_when_another_match_remains(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    test_company_id = _create_company(
        client,
        admin,
        name="覆盖去重测试主体",
        is_test=True,
    )
    now = datetime.now(timezone.utc)
    with factory() as db:
        test_lead = Lead(
            customer_name="将被删除的覆盖源",
            phone_encrypted="test-encrypted-phone",
            phone_hash="dedup-override-remains",
            phone_fingerprint="dedup-override-remains-fingerprint",
            status="READY_DISPATCH",
            source_kind="SUPPLIER_H5",
            supplier_company_id=test_company_id,
            imported_at=now - timedelta(days=1),
        )
        surviving_match = Lead(
            customer_name="仍存在的正式重复源",
            phone_encrypted="surviving-encrypted-phone",
            phone_hash="dedup-override-remains",
            phone_fingerprint="dedup-override-remains-fingerprint",
            status="READY_DISPATCH",
            source_kind="PLATFORM_MANUAL",
            imported_at=now - timedelta(hours=12),
        )
        overridden_lead = Lead(
            customer_name="必须重新阻断的正式客资",
            phone_encrypted="formal-encrypted-phone",
            phone_hash="dedup-override-remains",
            phone_fingerprint="dedup-override-remains-fingerprint",
            status="READY_DISPATCH",
            pending_reason=None,
            duplicate_status="OVERRIDDEN",
            review_status="APPROVED",
            source_kind="PLATFORM_MANUAL",
            imported_at=now,
        )
        db.add_all([test_lead, surviving_match, overridden_lead])
        db.flush()
        db.add(
            LeadDedupEvent(
                lead_id=overridden_lead.id,
                phone_fingerprint=overridden_lead.phone_fingerprint,
                checkpoint="OVERRIDE",
                decision="OVERRIDDEN",
                matched_lead_id=test_lead.id,
                window_days=30,
                details_json={"override": True},
            )
        )
        db.commit()
        overridden_lead_id = overridden_lead.id
        surviving_match_id = surviving_match.id

    _disable_company(client, admin, test_company_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{test_company_id}",
        headers=admin,
        json={"confirm_name": "覆盖去重测试主体", "reason": "清理覆盖源并恢复真实阻断"},
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        lead = db.get(Lead, overridden_lead_id)
        assert lead is not None
        assert lead.status == "DUPLICATE"
        assert lead.duplicate_status == "HARD_DUPLICATE"
        assert lead.pending_reason == "HARD_DUPLICATE"
        latest = db.scalar(
            select(LeadDedupEvent)
            .where(LeadDedupEvent.lead_id == overridden_lead_id)
            .order_by(LeadDedupEvent.created_at.desc(), LeadDedupEvent.id.desc())
        )
        assert latest is not None
        assert latest.matched_lead_id == surviving_match_id


def test_test_company_with_return_or_followup_on_external_assignment_can_be_deleted(
    api_client,
) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    test_company_id = _create_company(
        client,
        admin,
        name="跨主体退回测试方",
        is_test=True,
    )
    receiver_id = _create_company(
        client,
        admin,
        name="跨主体退回接收方",
        is_test=False,
    )
    with factory() as db:
        operator = db.scalar(select(User).where(User.username == "admin"))
        assert operator is not None
        lead = Lead(
            customer_name="平台真实客资",
            phone_encrypted="test-encrypted-phone",
            phone_hash="cross-company-return-lead",
            status="CLAIMED",
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=receiver_id,
            receiver_company_id=receiver_id,
            status="CLAIMED",
            points_price=100,
            claim_points=100,
            lead_snapshot={},
            assigned_by=operator.id,
        )
        db.add(assignment)
        db.flush()
        db.add_all(
            [
                FollowUp(
                    assignment_id=assignment.id,
                    company_id=test_company_id,
                    status="FOLLOWING",
                    note="测试主体产生的跟进",
                    created_by=operator.id,
                ),
                ReturnRequest(
                    assignment_id=assignment.id,
                    lead_id=lead.id,
                    company_id=test_company_id,
                    reason_code="TEST_RETURN",
                    reason_version=1,
                    description="测试主体产生的退回",
                    status="DRAFT",
                    submitted_by=operator.id,
                ),
            ]
        )
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id

    _disable_company(client, admin, test_company_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{test_company_id}",
        headers=admin,
        json={"confirm_name": "跨主体退回测试方", "reason": "清理跨主体测试记录"},
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(Company, test_company_id) is None
        assert db.get(Company, receiver_id) is not None
        assert db.get(Lead, lead_id) is not None
        assert db.get(Assignment, assignment_id) is not None
        assert db.scalar(select(FollowUp).where(FollowUp.company_id == test_company_id)) is None
        assert db.scalar(
            select(ReturnRequest).where(ReturnRequest.company_id == test_company_id)
        ) is None


def test_test_receiver_can_be_deleted_after_supplier_reward_settlement(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    test_receiver_id = _create_company(
        client,
        admin,
        name="奖励已结算测试接收方",
        is_test=True,
    )
    supplier_id = _create_company(
        client,
        admin,
        name="保留的供资方",
        is_test=False,
    )
    with factory() as db:
        operator = db.scalar(select(User).where(User.username == "admin"))
        supplier_account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == supplier_id)
        )
        assert operator is not None
        assert supplier_account is not None
        lead = Lead(
            customer_name="外部供资客户",
            phone_encrypted="test-encrypted-phone",
            phone_hash="test-receiver-settled-reward",
            status="CLAIMED",
            source_kind="SUPPLIER_H5",
            supplier_company_id=supplier_id,
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=test_receiver_id,
            supplier_company_id=supplier_id,
            receiver_company_id=test_receiver_id,
            status="CLAIMED",
            points_price=100,
            claim_points=100,
            lead_snapshot={},
            assigned_by=operator.id,
        )
        db.add(assignment)
        db.flush()
        reward = SupplierLeadReward(
            lead_id=lead.id,
            assignment_id=assignment.id,
            supplier_company_id=supplier_id,
            receiver_company_id=test_receiver_id,
            status="SETTLED",
            claim_points=100,
            reward_ratio_bps=3000,
            reward_points=30,
            rule_version=1,
            rule_snapshot_json={"version": 1, "ratio_bps": 3000},
        )
        db.add(reward)
        db.flush()
        supplier_account.supply_balance = 30
        reward_ledger = PointsLedger(
            account_id=supplier_account.id,
            company_id=supplier_id,
            ledger_type="REWARD",
            point_kind="SUPPLY",
            delta=30,
            balance_after=30,
            business_type="V12_SUPPLIER_REWARD",
            business_id=reward.id,
            idempotency_key="test-receiver-settled-reward",
        )
        db.add(reward_ledger)
        db.flush()
        reward.ledger_id = reward_ledger.id
        db.commit()
        lead_id = lead.id
        assignment_id = assignment.id
        reward_id = reward.id
        reward_ledger_id = reward_ledger.id
        supplier_account_id = supplier_account.id

    _disable_company(client, admin, test_receiver_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{test_receiver_id}",
        headers=admin,
        json={"confirm_name": "奖励已结算测试接收方", "reason": "清理已结算测试业务"},
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(Company, test_receiver_id) is None
        assert db.get(Company, supplier_id) is not None
        assert db.get(Lead, lead_id) is not None
        assert db.get(Assignment, assignment_id) is None
        assert db.get(SupplierLeadReward, reward_id) is None
        assert db.get(PointsLedger, reward_ledger_id) is None
        supplier_account = db.get(PointsAccount, supplier_account_id)
        assert supplier_account is not None
        assert supplier_account.supply_balance == 0


def test_test_marker_cannot_be_changed_through_general_company_update(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="正常主体不可改标记",
        is_test=False,
    )

    response = client.patch(
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"is_test": True},
    )
    assert response.status_code == 422, response.text
    with factory() as db:
        company = db.get(Company, company_id)
        assert company is not None and company.is_test is False


def test_company_status_change_requires_an_auditable_reason(api_client) -> None:
    client, _ = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="启停理由校验主体",
        is_test=True,
    )

    response = client.patch(
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"status": "DISABLED"},
    )
    assert response.status_code == 422, response.text

    disabled = client.patch(
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"status": "DISABLED", "reason": "暂停联测主体"},
    )
    assert disabled.status_code == 200, disabled.text

    enabled_without_reason = client.patch(
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"status": "ACTIVE"},
    )
    assert enabled_without_reason.status_code == 422, enabled_without_reason.text


def test_delete_rejects_normal_company_but_purges_test_company_points_history(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    active_test_id = _create_company(
        client,
        admin,
        name="未停用测试主体",
        is_test=True,
    )
    active_response = client.request(
        "DELETE",
        f"/api/v1/companies/{active_test_id}",
        headers=admin,
        json={"confirm_name": "未停用测试主体", "reason": "尝试跳过停用"},
    )
    assert active_response.status_code == 409, active_response.text
    assert active_response.json()["code"] == "COMPANY_MUST_BE_DISABLED"

    normal_id = _create_company(
        client,
        admin,
        name="正常加盟商不可删",
        is_test=False,
    )
    _disable_company(client, admin, normal_id)
    normal_response = client.request(
        "DELETE",
        f"/api/v1/companies/{normal_id}",
        headers=admin,
        json={"confirm_name": "正常加盟商不可删", "reason": "尝试清理正常主体"},
    )
    assert normal_response.status_code == 409, normal_response.text
    assert normal_response.json()["code"] == "COMPANY_DELETE_TEST_ONLY"

    test_id = _create_company(
        client,
        admin,
        name="有积分流水测试主体",
        is_test=True,
    )
    _disable_company(client, admin, test_id)
    with factory() as db:
        account = db.scalar(
            select(PointsAccount).where(PointsAccount.company_id == test_id)
        )
        assert account is not None
        account.balance = 100
        db.add(
            PointsLedger(
                account_id=account.id,
                company_id=test_id,
                ledger_type="RECHARGE",
                delta=100,
                balance_after=100,
                business_type="RECHARGE",
                business_id="test-recharge",
                idempotency_key="test-company-delete-blocker",
            )
        )
        db.commit()

    deleted_response = client.request(
        "DELETE",
        f"/api/v1/companies/{test_id}",
        headers=admin,
        json={"confirm_name": "有积分流水测试主体", "reason": "尝试清理有业务数据"},
    )
    assert deleted_response.status_code == 200, deleted_response.text

    with factory() as db:
        assert db.get(Company, test_id) is None
        assert db.scalar(select(PointsAccount).where(PointsAccount.company_id == test_id)) is None
        assert db.scalar(select(PointsLedger).where(PointsLedger.company_id == test_id)) is None


def test_delete_purges_self_contained_test_company_business_history(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="全量清理测试主体",
        is_test=True,
    )
    owner_user_id = _bind_company(
        client,
        factory,
        admin,
        company_id,
        "openid-full-test-purge",
    )

    platform_notification_id = ""
    with factory() as db:
        operator = db.scalar(select(User).where(User.username == "admin"))
        account = db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id))
        assert operator is not None and account is not None

        lead = Lead(
            customer_name="测试客户",
            phone_encrypted="test-encrypted-phone",
            phone_hash="full-test-purge-phone",
            region_code="310104",
            category_code="OLD_RENOVATION",
            status="CLAIMED",
            source_kind="SUPPLIER_H5",
            submitter_user_id=owner_user_id,
            supplier_company_id=company_id,
        )
        db.add(lead)
        db.flush()
        assignment = Assignment(
            lead_id=lead.id,
            company_id=company_id,
            supplier_company_id=company_id,
            receiver_company_id=company_id,
            status="CLAIMED",
            points_price=100,
            claim_points=100,
            lead_snapshot={},
            assigned_by=operator.id,
        )
        db.add(assignment)
        db.flush()
        db.add(
            FollowUp(
                assignment_id=assignment.id,
                company_id=company_id,
                status="FOLLOWING",
                note="测试跟进",
                created_by=owner_user_id,
            )
        )
        return_request = ReturnRequest(
            assignment_id=assignment.id,
            lead_id=lead.id,
            company_id=company_id,
            reason_code="TEST_RETURN",
            reason_version=1,
            description="测试退回",
            status="DRAFT",
            submitted_by=owner_user_id,
        )
        db.add(return_request)
        db.flush()
        stored_evidence = get_storage().save(
            b"test-company-return-evidence",
            prefix=f"returns/{return_request.id}",
            filename="evidence.txt",
            mime_type="text/plain",
        )
        evidence = ReturnEvidence(
            return_request_id=return_request.id,
            evidence_type="SCREENSHOT",
            object_key=stored_evidence.object_key,
            original_name="evidence.txt",
            mime_type=stored_evidence.mime_type,
            file_size=stored_evidence.size,
            sha256=stored_evidence.sha256,
            uploaded_by=owner_user_id,
        )
        db.add(evidence)
        db.add(
            SupplierLeadReward(
                lead_id=lead.id,
                assignment_id=assignment.id,
                supplier_company_id=company_id,
                receiver_company_id=company_id,
                status="OBSERVING",
                claim_points=100,
                reward_ratio_bps=3000,
                reward_points=30,
                rule_version=1,
                rule_snapshot_json={"version": 1, "ratio_bps": 3000},
            )
        )
        account.balance = 70
        db.add(
            PointsLedger(
                account_id=account.id,
                company_id=company_id,
                ledger_type="RECHARGE",
                delta=100,
                balance_after=100,
                business_type="RECHARGE",
                business_id="full-test-purge-recharge",
                idempotency_key="full-test-purge-recharge",
            )
        )
        db.add(
            PointsLedger(
                account_id=account.id,
                company_id=company_id,
                ledger_type="CLAIM",
                delta=-30,
                balance_after=70,
                business_type="ASSIGNMENT",
                business_id=assignment.id,
                idempotency_key="full-test-purge-claim",
            )
        )
        platform_notification = Notification(
            user_id=operator.id,
            company_id=None,
            scene="COMPANY_TEST_MESSAGE",
            title="测试主体消息",
            body="该消息应随测试主体清理",
            deep_link=f"/admin/v12-operations.html?view=companies&id={company_id}",
            status="CREATED",
        )
        db.add(platform_notification)
        db.flush()
        platform_notification_id = platform_notification.id
        db.add(
            NotificationOutbox(
                event_key=f"company:{company_id}:test-message",
                event_type="COMPANY_TEST_MESSAGE",
                aggregate_type="profile",
                aggregate_id=company_id,
                payload={"notification_id": platform_notification.id},
                status="PENDING",
            )
        )
        db.commit()
        evidence_id = evidence.id
        evidence_object_key = evidence.object_key

    _disable_company(client, admin, company_id)
    response = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"confirm_name": "全量清理测试主体", "reason": "清理完整联测数据"},
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        assert db.get(Company, company_id) is None
        assert db.scalar(select(Lead).where(Lead.supplier_company_id == company_id)) is None
        assert db.scalar(select(Assignment).where(Assignment.company_id == company_id)) is None
        assert db.scalar(select(FollowUp).where(FollowUp.company_id == company_id)) is None
        assert db.scalar(select(ReturnRequest).where(ReturnRequest.company_id == company_id)) is None
        assert db.scalar(
            select(SupplierLeadReward).where(
                SupplierLeadReward.supplier_company_id == company_id
            )
        ) is None
        assert db.scalar(select(PointsLedger).where(PointsLedger.company_id == company_id)) is None
        assert db.scalar(select(PointsAccount).where(PointsAccount.company_id == company_id)) is None
        assert db.get(Notification, platform_notification_id) is None
        assert db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.aggregate_id == company_id
            )
        ) is None
        cleanup = db.scalar(
            select(StorageCleanupOutbox).where(
                StorageCleanupOutbox.source_type == "return_evidence",
                StorageCleanupOutbox.source_id == evidence_id,
            )
        )
        assert cleanup is not None and cleanup.status == "PENDING"
        result = process_storage_cleanup(db)
        db.commit()
        assert result["deleted"] == 1
        assert cleanup.status == "DELETED"

    with pytest.raises(AppError) as exc_info:
        get_storage().read(evidence_object_key)
    assert exc_info.value.code == "FILE_NOT_FOUND"


def test_delete_purges_test_company_account_application_history(api_client) -> None:
    client, factory = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="有账号申请的测试主体",
        is_test=True,
    )
    _disable_company(client, admin, company_id)

    with factory() as db:
        admin_user = db.scalar(select(User).where(User.username == "admin"))
        assert admin_user is not None
        db.add(
            CompanyAccountRequest(
                company_id=company_id,
                request_type="CREATE_EMPLOYEE",
                status="REJECTED",
                requested_by=admin_user.id,
                requested_username="historical_test_employee",
                requested_display_name="历史测试员工",
                reason="联测账号申请",
            )
        )
        db.commit()

    response = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=admin,
        json={"confirm_name": "有账号申请的测试主体", "reason": "尝试清理申请历史"},
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(Company, company_id) is None
        assert db.scalar(
            select(CompanyAccountRequest).where(
                CompanyAccountRequest.company_id == company_id
            )
        ) is None


def test_company_lifecycle_mutations_require_platform_permission(api_client) -> None:
    client, _ = api_client
    admin = _login(client, "admin", "Admin123!")
    company_id = _create_company(
        client,
        admin,
        name="权限测试主体",
        is_test=True,
    )
    _disable_company(client, admin, company_id)
    franchise = _login(client, "franchise_demo", "Franchise123!")

    response = client.post(
        f"/api/v1/companies/{company_id}/wechat-binding/unbind",
        headers=franchise,
        json={"confirm_name": "权限测试主体", "reason": "无权越权操作"},
    )
    assert response.status_code == 403, response.text

    delete_response = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=franchise,
        json={"confirm_name": "权限测试主体", "reason": "无权越权删除"},
    )
    assert delete_response.status_code == 403, delete_response.text

    operation = _login(client, "operation", "Operation123!")
    operation_delete = client.request(
        "DELETE",
        f"/api/v1/companies/{company_id}",
        headers=operation,
        json={"confirm_name": "权限测试主体", "reason": "运营不得永久删除"},
    )
    assert operation_delete.status_code == 403, operation_delete.text


def test_company_test_flag_migration_is_reversible() -> None:
    migration = Path("migrations/versions/0012_company_test_flag.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0012_company_test_flag"' in migration
    assert 'down_revision = "0011_company_account_req"' in migration
    assert "op.add_column(" in migration
    assert '"companies"' in migration
    assert 'op.drop_column("companies", "is_test")' in migration


def test_storage_cleanup_outbox_migration_is_reversible() -> None:
    migration = Path("migrations/versions/0014_storage_cleanup_outbox.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0014_storage_cleanup"' in migration
    assert 'down_revision = "0013_internal_user_test"' in migration
    assert '"storage_cleanup_outbox"' in migration
    assert 'op.drop_table("storage_cleanup_outbox")' in migration
