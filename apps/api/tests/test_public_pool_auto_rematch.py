"""问题一回归：历史公海客资自动重匹配（2026-09-24 资金修复方案·公海部分）。

覆盖验收点：
- 先无接收方进入公海、后启用区域 -> 自动任务将符合条件者移至待派发；
  不生成派发单、不扣积分、不通知接收方（转池只改状态，无 Assignment）。
- 来源公司即使具备接收能力也不算自己的接收方，不能自动转池。
- 仍无接收方 / 未审核 / 能力停用时保持公海并保留明确阻断原因。
- 任务重复运行不重复转池、不产生重复审计；每批有上限。
"""
from __future__ import annotations

from sqlalchemy import func, select

from apps.api.src.core.models import Assignment, AuditLog, Company, Lead, Region, User
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.lead_supply_v12 import create_draft, submit_draft
from apps.api.src.services.public_pool_v12 import (
    AUTO_REMATCH_AUDIT_ACTION,
    rematch_no_receiver_public_pool_leads,
)
from apps.api.src.core.auth import Principal


def _supplier(db) -> tuple[Company, Principal]:
    company = Company(code="SUP-REMATCH", name="客资提供加盟商", status="ACTIVE")
    db.add(company)
    db.flush()
    user = User(display_name="供客负责人", status="ACTIVE", company_id=company.id)
    db.add(user)
    db.add(
        CompanyLeadCapability(
            company_id=company.id,
            capability_code="LEAD_SUPPLIER",
            active=True,
            review_status="APPROVED",
        )
    )
    db.flush()
    principal = Principal(
        user_id=user.id,
        display_name=user.display_name,
        company_id=company.id,
        role_codes=frozenset({"FRANCHISE_OWNER"}),
        permission_codes=frozenset({"supplier.lead.manage"}),
        session_version=1,
    )
    return company, principal


def _receiver(db, *, code: str = "REC-REMATCH", region_code: str = "420102") -> Company:
    company = Company(code=code, name="当地接收加盟商", status="ACTIVE")
    db.add(company)
    db.flush()
    db.add_all(
        [
            CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_RECEIVER",
                active=True,
                review_status="APPROVED",
            ),
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code=region_code,
                region_level="CITY",
                is_primary_city=True,
                active=True,
                review_status="APPROVED",
            ),
        ]
    )
    db.flush()
    return company


def _seed_region(db) -> None:
    db.add(Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True))
    db.add(
        Region(
            code="420102",
            name="江岸区",
            level="DISTRICT",
            parent_code="420100",
            aliases=[],
            active=True,
        )
    )
    db.flush()


def _values(phone: str) -> dict:
    return {
        "customer_name": "张先生",
        "phone": phone,
        "province": "湖北省",
        "city": "武汉市",
        "region_code": "420102",
        "source_channel": "OTHER",
        "source_detail": "加盟商小程序提交",
        "consent_confirmed": True,
    }


def _submit_supplier_lead(db, supplier: Principal, phone: str) -> Lead:
    lead = create_draft(
        db,
        principal=supplier,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        values=_values(phone),
    )
    submit_draft(db, lead=lead, principal=supplier)
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    return lead


def _rematch_audit_count(db) -> int:
    return int(
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == AUTO_REMATCH_AUDIT_ACTION
            )
        )
        or 0
    )


def _counts(result: dict) -> dict:
    return {key: result[key] for key in ("scanned", "transferred", "blocked", "errors")}


def test_auto_rematch_transfers_after_receiver_enabled_and_is_idempotent(db) -> None:
    _seed_region(db)
    _, supplier = _supplier(db)
    lead = _submit_supplier_lead(db, supplier, "13800138101")

    # 加盟商后续启用合格接收方（非来源公司）。
    receiver = _receiver(db)
    assert receiver.id is not None

    first = rematch_no_receiver_public_pool_leads(db)
    assert _counts(first) == {"scanned": 1, "transferred": 1, "blocked": 0, "errors": 0}
    assert lead.status == LeadV12Status.READY_DISPATCH.value
    assert lead.pending_reason is None
    assert lead.review_status == "APPROVED"
    # 转池不生成派发单、不扣积分、不通知：不存在任何 Assignment。
    assert int(db.scalar(select(func.count(Assignment.id))) or 0) == 0
    assert _rematch_audit_count(db) == 1
    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == AUTO_REMATCH_AUDIT_ACTION)
    )
    assert audit.actor_user_id is None  # 系统审计
    assert audit.resource_id == lead.id

    # 幂等：已转池的客资不再满足筛选条件，重复运行不重复转池/审计。
    second = rematch_no_receiver_public_pool_leads(db)
    assert _counts(second) == {"scanned": 0, "transferred": 0, "blocked": 0, "errors": 0}
    assert _rematch_audit_count(db) == 1


def test_auto_rematch_keeps_lead_when_still_no_receiver(db) -> None:
    _seed_region(db)
    _, supplier = _supplier(db)
    lead = _submit_supplier_lead(db, supplier, "13800138102")

    result = rematch_no_receiver_public_pool_leads(db)

    assert _counts(result) == {"scanned": 1, "transferred": 0, "blocked": 1, "errors": 0}
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    # 未转池不写自动重匹配审计。
    assert _rematch_audit_count(db) == 0


def test_source_supplier_never_counts_as_its_own_receiver_for_auto_rematch(db) -> None:
    _seed_region(db)
    supplier_company, supplier = _supplier(db)
    lead = _submit_supplier_lead(db, supplier, "13800138103")

    # 来源公司自身启用接收能力与服务区域，仍不得接收自己提供的客资。
    db.add_all(
        [
            CompanyLeadCapability(
                company_id=supplier_company.id,
                capability_code="LEAD_RECEIVER",
                active=True,
                review_status="APPROVED",
            ),
            CompanyServiceAreaV12(
                company_id=supplier_company.id,
                region_code="420100",
                region_level="CITY",
                is_primary_city=True,
                active=True,
                review_status="APPROVED",
            ),
        ]
    )
    db.flush()

    result = rematch_no_receiver_public_pool_leads(db)

    assert _counts(result) == {"scanned": 1, "transferred": 0, "blocked": 1, "errors": 0}
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    assert _rematch_audit_count(db) == 0


def test_inactive_or_unapproved_receiver_does_not_trigger_auto_rematch(db) -> None:
    _seed_region(db)
    _, supplier = _supplier(db)
    lead = _submit_supplier_lead(db, supplier, "13800138104")

    # 接收能力停用 / 未审核：不构成合格接收方。
    company = Company(code="REC-INACTIVE", name="能力停用加盟商", status="ACTIVE")
    db.add(company)
    db.flush()
    db.add_all(
        [
            CompanyLeadCapability(
                company_id=company.id,
                capability_code="LEAD_RECEIVER",
                active=False,
                review_status="APPROVED",
            ),
            CompanyServiceAreaV12(
                company_id=company.id,
                region_code="420102",
                region_level="CITY",
                is_primary_city=True,
                active=True,
                review_status="PENDING",
            ),
        ]
    )
    db.flush()

    result = rematch_no_receiver_public_pool_leads(db)

    assert _counts(result) == {"scanned": 1, "transferred": 0, "blocked": 1, "errors": 0}
    assert lead.status == LeadV12Status.PUBLIC_POOL.value


def test_auto_rematch_respects_batch_size_and_continues_next_round(db) -> None:
    _seed_region(db)
    _, supplier = _supplier(db)
    leads = [
        _submit_supplier_lead(db, supplier, f"138001381{i:02d}") for i in range(5, 8)
    ]
    _receiver(db)

    first = rematch_no_receiver_public_pool_leads(db, batch_size=2)
    assert _counts(first) == {"scanned": 2, "transferred": 2, "blocked": 0, "errors": 0}
    transferred_statuses = [lead.status for lead in leads]
    assert transferred_statuses.count(LeadV12Status.READY_DISPATCH.value) == 2
    assert transferred_statuses.count(LeadV12Status.PUBLIC_POOL.value) == 1

    # 下一轮继续处理剩余记录，直到全部转池。
    second = rematch_no_receiver_public_pool_leads(db, batch_size=2)
    assert _counts(second) == {"scanned": 1, "transferred": 1, "blocked": 0, "errors": 0}
    assert all(
        lead.status == LeadV12Status.READY_DISPATCH.value for lead in leads
    )
    assert _rematch_audit_count(db) == 3


def test_blocked_old_lead_does_not_starve_newer_eligible_lead(db) -> None:
    _seed_region(db)
    db.add_all(
        [
            Region(code="420200", name="黄石市", level="CITY", aliases=[], active=True),
            Region(
                code="420202", name="黄石港区", level="DISTRICT",
                parent_code="420200", aliases=[], active=True,
            ),
        ]
    )
    db.flush()
    _, supplier = _supplier(db)
    blocked = _submit_supplier_lead(db, supplier, "13800138111")
    eligible = _submit_supplier_lead(db, supplier, "13800138112")
    blocked.created_at = eligible.created_at.replace(year=eligible.created_at.year - 1)
    db.flush()
    _receiver(db)
    # 第一条仍无覆盖；第二条已有覆盖。模拟历史阻断前缀长期占满批次。
    blocked.region_code = "420202"
    db.flush()

    first = rematch_no_receiver_public_pool_leads(db, batch_size=1)
    second = rematch_no_receiver_public_pool_leads(
        db, batch_size=1, after=first["next_cursor"]
    )

    assert first["blocked"] == 1
    assert blocked.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    assert second["transferred"] == 1
    assert blocked.status == LeadV12Status.PUBLIC_POOL.value
    assert eligible.status == LeadV12Status.READY_DISPATCH.value
    third = rematch_no_receiver_public_pool_leads(
        db, batch_size=1, after=second["next_cursor"]
    )
    assert third["blocked"] == 1  # 游标到尾后从头继续扫描
