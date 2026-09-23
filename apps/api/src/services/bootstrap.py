from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.auth import Principal
from ..core.enums import LeadStatus
from ..core.models import (
    Company,
    CompanyCapability,
    CompanyServiceRegion,
    DictionaryItem,
    Lead,
    LeadPriceRule,
    PointsPackage,
    Region,
    User,
    VerificationTask,
    VerificationTemplate,
    WechatIdentity,
)
from ..core.security import encrypt_text, hash_phone
from .auth_service import create_internal_user
from .claim_service import claim_assignment
from .company_service import create_company
from .dispatch_service import dispatch_lead
from .points_service import change_points
from .rbac import assign_role, seed_rbac
from ..schemas.company import CompanyCreateBody


DEMO_PASSWORDS = {
    "admin": "Admin123!",
    "operation": "Operation123!",
    "telesales": "Telesales123!",
    "franchise_demo": "Franchise123!",
    "franchise_employee_demo": "Employee123!",
}


def _principal(user: User) -> Principal:
    role_codes = frozenset(role.code for role in user.roles)
    permission_codes = frozenset(permission.code for role in user.roles for permission in role.permissions)
    return Principal(
        user_id=user.id,
        display_name=user.display_name,
        company_id=user.company_id,
        role_codes=role_codes,
        permission_codes=permission_codes,
        session_version=user.session_version,
    )


def seed_reference_data(db: Session) -> None:
    regions = [
        ("310000", "上海市", "CITY", None, ["上海", "上海市"]),
        ("310115", "浦东新区", "DISTRICT", "310000", ["浦东", "浦东新区"]),
        ("310104", "徐汇区", "DISTRICT", "310000", ["徐汇", "徐汇区"]),
        ("320500", "苏州市", "CITY", None, ["苏州", "苏州市"]),
        ("330100", "杭州市", "CITY", None, ["杭州", "杭州市"]),
    ]
    for code, name, level, parent, aliases in regions:
        item = db.get(Region, code)
        if not item:
            db.add(Region(code=code, name=name, level=level, parent_code=parent, aliases=aliases, active=True))

    dictionaries: dict[str, list[tuple[str, str]]] = {
        "lead_category": [
            ("OLD_RENOVATION", "旧房改造"),
            ("SELF_BUILD", "农村自建房"),
            ("INTERIOR", "室内装修"),
        ],
        "brand": [("ZHONGSHU", "合家美宅"), ("PARTNER", "合作品牌")],
        "return_reason": [
            ("EMPTY_NUMBER", "空号/停机/无法接通"),
            ("DUPLICATE", "重复客户"),
            ("REGION_WRONG", "地区错误"),
            ("NON_TARGET", "非目标客户"),
            ("INFO_ERROR", "关键信息错误"),
        ],
        "source_channel": [
            ("DOUYIN", "抖音/信息流"),
            ("WECHAT_VIDEO", "视频号"),
            ("XIAOHONGSHU", "小红书"),
            # 2026-09-23 反馈 F1(b)：补齐 直播/广告/其他。
            ("LIVE", "直播"),
            ("AD", "广告"),
            ("MANUAL", "人工录入"),
            ("OTHER", "其他"),
        ],
    }
    for domain, values in dictionaries.items():
        for index, (code, label) in enumerate(values):
            existing = db.scalar(
                select(DictionaryItem).where(DictionaryItem.domain == domain, DictionaryItem.code == code, DictionaryItem.version == 1)
            )
            if not existing:
                db.add(
                    DictionaryItem(
                        domain=domain,
                        code=code,
                        label=label,
                        version=1,
                        sort_order=index,
                        metadata_json={},
                        active=True,
                    )
                )


def seed_users_and_company(db: Session) -> dict[str, User | Company]:
    user_specs = [
        ("admin", "平台超级管理员", "SUPER_ADMIN"),
        ("operation", "运营管理员", "OPERATION"),
        ("telesales", "电销人员", "TELESALES"),
    ]
    users: dict[str, User] = {}
    for username, display_name, role_code in user_specs:
        user = db.scalar(select(User).options(selectinload(User.roles)).where(User.username == username))
        if not user:
            user = create_internal_user(
                db,
                username=username,
                password=DEMO_PASSWORDS[username],
                display_name=display_name,
                role_code=role_code,
            )
        users[username] = user

    company = db.scalar(
        select(Company)
        .options(selectinload(Company.service_regions), selectinload(Company.capabilities), selectinload(Company.points_account))
        .where(Company.code == "SH-DEMO")
    )
    if not company:
        company = create_company(
            db,
            CompanyCreateBody(
                code="SH-DEMO",
                name="上海合家美宅加盟服务中心",
                owner_name="张老板",
                contact_phone="13800138000",
                level_code="V1",
                region_codes=["310000", "310104", "310115"],
                capabilities=[{"category_code": "OLD_RENOVATION", "brand_code": "ZHONGSHU"}],
                notes="演示加盟商，仅用于本地测试。",
            ),
        )
    else:
        for region_code in ("310000", "310104", "310115"):
            # 2026-09-19 S7：演示客资落到区县级，服务区域同步补齐区级行。
            if not any(x.region_code == region_code for x in company.service_regions):
                db.add(CompanyServiceRegion(company_id=company.id, region_code=region_code, active=True))
        if not any(x.category_code == "OLD_RENOVATION" and x.brand_code == "ZHONGSHU" for x in company.capabilities):
            db.add(CompanyCapability(company_id=company.id, category_code="OLD_RENOVATION", brand_code="ZHONGSHU", active=True))

    franchise = db.scalar(select(User).options(selectinload(User.roles)).where(User.username == "franchise_demo"))
    if not franchise:
        franchise = create_internal_user(
            db,
            username="franchise_demo",
            password=DEMO_PASSWORDS["franchise_demo"],
            display_name="张老板",
            role_code="FRANCHISE_OWNER",
            company_id=company.id,
        )
    else:
        franchise.company_id = company.id
        if not any(role.code == "FRANCHISE_OWNER" for role in franchise.roles):
            assign_role(db, franchise, "FRANCHISE_OWNER")
    employee = db.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(User.username == "franchise_employee_demo")
    )
    if not employee:
        employee = create_internal_user(
            db,
            username="franchise_employee_demo",
            password=DEMO_PASSWORDS["franchise_employee_demo"],
            display_name="加盟商员工演示账号",
            role_code="FRANCHISE_EMPLOYEE",
            company_id=company.id,
        )
    else:
        employee.company_id = company.id
        if not any(role.code == "FRANCHISE_EMPLOYEE" for role in employee.roles):
            assign_role(db, employee, "FRANCHISE_EMPLOYEE")
    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == franchise.id))
    if not identity:
        db.add(WechatIdentity(openid="demo-openid-franchise", nickname="张老板", subscribed=True, user_id=franchise.id))
    company.primary_user_id = franchise.id
    users["franchise_demo"] = franchise
    users["franchise_employee_demo"] = employee
    return {**users, "company": company}


def seed_rules(db: Session) -> None:
    if not db.scalar(select(VerificationTemplate).where(VerificationTemplate.code == "LEAD_VERIFY", VerificationTemplate.version == 1)):
        db.add(
            VerificationTemplate(
                code="LEAD_VERIFY",
                name="客资电话核验模板",
                version=1,
                status="PUBLISHED",
                effective_at=datetime.now(timezone.utc),
                schema_json={
                    "fields": [
                        {"key": "identity_confirmed", "label": "是否本人咨询", "type": "boolean", "required": True},
                        {"key": "demand_confirmed", "label": "需求是否明确", "type": "boolean", "required": True},
                        {"key": "contact_result", "label": "联系结果", "type": "select", "options": ["接通", "无人接听", "空号"]},
                    ]
                },
            )
        )
    if not db.scalar(
        select(VerificationTemplate).where(
            VerificationTemplate.code == "PRE_DISPATCH",
            VerificationTemplate.status == "PUBLISHED",
        )
    ):
        latest = db.scalar(
            select(VerificationTemplate)
            .where(VerificationTemplate.code == "PRE_DISPATCH")
            .order_by(VerificationTemplate.version.desc())
        )
        db.add(
            VerificationTemplate(
                code="PRE_DISPATCH",
                name="前置电销核验模板",
                version=(latest.version if latest else 0) + 1,
                status="PUBLISHED",
                effective_at=datetime.now(timezone.utc),
                schema_json={"fields": []},
            )
        )
    packages = [
        ("V1_20000", "V1 两万元档", 2_000_000, 20_000, 0, "V1"),
        ("V3_100000", "V3 十万元档", 10_000_000, 100_000, 50_000, "V3"),
    ]
    for code, name, cash, base, bonus, level in packages:
        if not db.scalar(select(PointsPackage).where(PointsPackage.code == code, PointsPackage.version == 1)):
            db.add(
                PointsPackage(
                    code=code,
                    name=name,
                    cash_amount_cents=cash,
                    base_points=base,
                    bonus_points=bonus,
                    level_code=level,
                    entitlements_json={"note": "演示档位，正式商务参数须在上线前冻结"},
                    version=1,
                    status="PUBLISHED",
                    effective_at=datetime.now(timezone.utc),
                )
            )
    if not db.scalar(select(LeadPriceRule).where(LeadPriceRule.version == 1, LeadPriceRule.priority == 999)):
        db.add(
            LeadPriceRule(
                region_code=None,
                category_code=None,
                brand_code=None,
                level_code=None,
                points_cost=100,
                version=1,
                status="PUBLISHED",
                priority=999,
                effective_at=datetime.now(timezone.utc),
            )
        )


def _ensure_demo_lead(db: Session, *, record_id: str, name: str, phone: str, status: str, district: str, summary: str) -> Lead:
    lead = db.scalar(select(Lead).where(Lead.source_app_token == "demo-app", Lead.source_table_id == "demo-table", Lead.source_record_id == record_id))
    if lead:
        return lead
    lead = Lead(
        source_type="DEMO",
        source_app_token="demo-app",
        source_table_id="demo-table",
        source_record_id=record_id,
        source_channel="视频号",
        customer_name=name,
        phone_encrypted=encrypt_text(phone),
        phone_hash=hash_phone(phone),
        province="上海市",
        city="上海市",
        district=district,
        # 演示客资同样满足县级派发门槛（2026-09-19 S7）。
        region_code={"徐汇区": "310104", "浦东新区": "310115"}.get(district, "310104"),
        category_code="OLD_RENOVATION",
        brand_code="ZHONGSHU",
        need_summary=summary,
        budget_min=300_000,
        budget_max=500_000,
        acquisition_cost_cents=0,
        status=status,
        pending_reason=None,
        raw_payload={"demo": True},
    )
    db.add(lead)
    db.flush()
    return lead


def seed_demo_workflow(db: Session, resources: dict[str, User | Company]) -> dict[str, Any]:
    operation = resources["operation"]
    telesales = resources["telesales"]
    franchise = resources["franchise_demo"]
    company = resources["company"]
    assert isinstance(operation, User) and isinstance(telesales, User) and isinstance(franchise, User) and isinstance(company, Company)

    # Provide enough demo points for manual dispatch and claim flows.
    change_points(
        db,
        company_id=company.id,
        delta=10_000,
        ledger_type="ADJUST",
        business_type="DEMO_BOOTSTRAP",
        business_id="demo-initial-points",
        idempotency_key="demo:initial-points",
        created_by=resources["admin"].id if isinstance(resources["admin"], User) else None,
        metadata={"note": "本地演示初始化积分"},
    )

    imported = _ensure_demo_lead(
        db,
        record_id="demo-imported",
        name="陈女士",
        phone="13800000001",
        status=LeadStatus.IMPORTED,
        district="徐汇区",
        summary="咨询旧房局部翻新，待电话核验。",
    )
    verifying = _ensure_demo_lead(
        db,
        record_id="demo-verifying",
        name="刘先生",
        phone="13800000002",
        status=LeadStatus.VERIFYING,
        district="浦东新区",
        summary="计划今年装修，希望了解施工周期。",
    )
    if not db.scalar(select(VerificationTask).where(VerificationTask.lead_id == verifying.id)):
        template = db.scalar(select(VerificationTemplate).where(VerificationTemplate.code == "LEAD_VERIFY", VerificationTemplate.status == "PUBLISHED"))
        db.add(
            VerificationTask(
                lead_id=verifying.id,
                template_id=template.id if template else None,
                template_version=template.version if template else 1,
                status="ASSIGNED",
                assignee_user_id=telesales.id,
                assigned_by=operation.id,
                assigned_at=datetime.now(timezone.utc),
            )
        )

    qualified = _ensure_demo_lead(
        db,
        record_id="demo-qualified",
        name="王女士",
        phone="13800000003",
        status=LeadStatus.QUALIFIED,
        district="浦东新区",
        summary="确认本人咨询旧改，预算约40万元。",
    )
    pending = _ensure_demo_lead(
        db,
        record_id="demo-pending-claim",
        name="张先生",
        phone="13800000004",
        status=LeadStatus.QUALIFIED,
        district="浦东新区",
        summary="两室一厅旧改，近期可量房。",
    )
    claimed = _ensure_demo_lead(
        db,
        record_id="demo-claimed",
        name="李女士",
        phone="13800000005",
        status=LeadStatus.QUALIFIED,
        district="徐汇区",
        summary="厨房和卫生间翻新，预算30万元。",
    )

    op_principal = _principal(operation)
    franchise_principal = _principal(franchise)
    pending_assignment = dispatch_lead(
        db,
        lead_id=pending.id,
        company_id=company.id,
        principal=op_principal,
        idempotency_key="demo:dispatch:pending",
        reason="演示待领取客资",
    )
    claimed_assignment = dispatch_lead(
        db,
        lead_id=claimed.id,
        company_id=company.id,
        principal=op_principal,
        idempotency_key="demo:dispatch:claimed",
        reason="演示已领取客资",
    )
    claim_assignment(db, claimed_assignment.id, franchise_principal, "demo-claim")
    if not db.scalar(select(VerificationTask).where(VerificationTask.lead_id == imported.id)):
        # imported remains in staging by design; no task is generated yet.
        pass
    return {
        "company_id": company.id,
        "qualified_lead_id": qualified.id,
        "pending_assignment_id": pending_assignment.id,
        "claimed_assignment_id": claimed_assignment.id,
    }


def seed_demo(db: Session) -> dict[str, Any]:
    seed_rbac(db, source="seed_demo")
    seed_reference_data(db)
    db.flush()
    resources = seed_users_and_company(db)
    db.flush()
    seed_rules(db)
    db.flush()
    workflow = seed_demo_workflow(db, resources)
    db.flush()
    return {
        "accounts": {key: password for key, password in DEMO_PASSWORDS.items()},
        **workflow,
    }
