from __future__ import annotations

import pytest
from sqlalchemy import func, select

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import AuditLog, Company, Lead, LeadImportIssue, Region, User
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.v12_enums import (
    CustomerSource,
    DuplicateDecision,
    LeadSourceKind,
    LeadV12Status,
)
from apps.api.src.integrations.feishu import FeishuRecord
from apps.api.src.services.dispatch_v12 import has_receiver_coverage
from apps.api.src.services.lead_supply_v12 import (
    create_draft,
    lead_supply_list_to_dict,
    lead_supply_to_dict,
    submit_draft,
    update_draft,
)
from apps.api.src.services.public_pool_v12 import (
    PublicPoolTarget,
    create_public_pool_lead,
    import_feishu_customer_view,
    list_public_pool_leads,
    transfer_public_pool_lead,
    update_public_pool_lead,
)


def _principal(user_id: str, *permissions: str) -> Principal:
    return Principal(
        user_id=user_id,
        display_name="公海池测试运营",
        company_id=None,
        role_codes=frozenset({"OPERATION"}),
        permission_codes=frozenset(permissions),
        session_version=1,
    )


def _operation(db) -> tuple[User, Principal]:
    user = User(display_name="公海池测试运营", status="ACTIVE")
    db.add(user)
    db.flush()
    return user, _principal(user.id, "lead.manual.manage")


def _supplier(db) -> tuple[Company, User, Principal]:
    company = Company(code="SUP-PUBLIC", name="客资提供加盟商", status="ACTIVE")
    db.add(company)
    db.flush()
    user = User(
        display_name="供客负责人",
        status="ACTIVE",
        company_id=company.id,
    )
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
    return company, user, principal


def _receiver(db, *, region_code: str = "420100") -> Company:
    company = Company(code="REC-PUBLIC", name="当地接收加盟商", status="ACTIVE")
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
    db.flush()


def _complete_values(phone: str = "13800138000") -> dict:
    return {
        "customer_name": "张先生",
        "phone": phone,
        "province": "湖北省",
        "city": "武汉市",
        "region_code": "420100",
        "source_channel": "OTHER",
        "source_detail": "飞书客户视图",
        "consent_confirmed": True,
    }


class FakeCustomerViewClient:
    def __init__(self, records: list[FeishuRecord]) -> None:
        self.records = records
        self.resolved_view_names: list[str] = []
        self.iterated_view_ids: list[str] = []
        self.writeback_calls = 0

    def resolve_view_id(self, view_name: str) -> str:
        self.resolved_view_names.append(view_name)
        return "view-customer"

    def iter_records(self, *, view_id: str, page_size: int, max_pages: int):
        self.iterated_view_ids.append(view_id)
        yield from self.records

    def write_back(self, record_id: str, fields: dict) -> None:
        self.writeback_calls += 1
        raise AssertionError("一期导入不得回写飞书")


def test_manual_and_inline_entries_share_public_pool_and_transfer_revalidates(db) -> None:
    _seed_region(db)
    _, principal = _operation(db)
    lead = create_public_pool_lead(
        db,
        principal=principal,
        values={"customer_name": "待补资料客户", "phone": "138 0013 8000"},
    )

    assert lead.status == LeadV12Status.DRAFT.value
    assert lead.source_kind == LeadSourceKind.PLATFORM_MANUAL.value
    assert lead.pending_reason == "PUBLIC_POOL_INCOMPLETE"
    assert set(lead.raw_payload["public_pool_validation_errors"]) >= {
        "region_code",
        "source_channel",
        "consent_confirmed",
    }

    incomplete, incomplete_total = list_public_pool_leads(
        db,
        completeness="INCOMPLETE",
    )
    assert incomplete_total == 1
    assert [item.id for item in incomplete] == [lead.id]

    blocked = transfer_public_pool_lead(db, lead=lead, principal=principal)
    assert blocked.transferred is False
    assert set(blocked.validation_errors) >= {
        "region_code",
        "source_channel",
        "consent_confirmed",
    }
    assert lead.status == LeadV12Status.DRAFT.value

    lead.region_code = "420100"
    lead.city = "武汉市"
    lead.source_channel = "OTHER"
    lead.source_detail = "线下活动"
    lead.consent_confirmed = True
    transferred = transfer_public_pool_lead(db, lead=lead, principal=principal)

    assert transferred.transferred is True
    assert transferred.validation_errors == {}
    assert lead.status == LeadV12Status.READY_DISPATCH.value


def test_customer_name_is_optional_when_other_dispatch_fields_are_complete(db) -> None:
    _seed_region(db)
    _, principal = _operation(db)
    lead = create_public_pool_lead(
        db,
        principal=principal,
        values={
            "phone": "13800138009",
            "region_code": "420100",
            "city": "武汉市",
            "source_channel": "OTHER",
            "source_detail": "线下活动",
            "consent_confirmed": True,
        },
    )

    assert lead.customer_name == "未填写"
    assert lead.pending_reason is None
    assert "customer_name" not in lead.raw_payload.get(
        "public_pool_validation_errors",
        {},
    )

    transferred = transfer_public_pool_lead(db, lead=lead, principal=principal)

    assert transferred.transferred is True
    assert transferred.validation_errors == {}
    assert lead.status == LeadV12Status.READY_DISPATCH.value


@pytest.mark.parametrize(
    "source_kind",
    [LeadSourceKind.PLATFORM_MANUAL.value, LeadSourceKind.FEISHU_IMPORT.value],
)
def test_rework_reason_stays_until_successful_dispatch_transfer(db, source_kind) -> None:
    _seed_region(db)
    _, principal = _operation(db)
    lead = create_public_pool_lead(
        db,
        principal=principal,
        values={"customer_name": "待补充客户", "phone": "13800138008"},
    )
    lead.source_kind = source_kind
    lead.source_type = source_kind
    lead.pending_reason = "PRE_DISPATCH_REWORK_REQUIRED"

    update_public_pool_lead(
        db,
        lead=lead,
        principal=principal,
        values={"source_channel": "OTHER", "source_detail": "补充来源"},
    )

    assert lead.pending_reason == "PRE_DISPATCH_REWORK_REQUIRED"
    assert set(lead.raw_payload["public_pool_validation_errors"]) >= {
        "region_code",
        "consent_confirmed",
    }

    update_public_pool_lead(
        db,
        lead=lead,
        principal=principal,
        values={
            "region_code": "420100",
            "city": "武汉市",
            "consent_confirmed": True,
        },
    )

    assert lead.pending_reason == "PRE_DISPATCH_REWORK_REQUIRED"
    assert "public_pool_validation_errors" not in lead.raw_payload

    transferred = transfer_public_pool_lead(db, lead=lead, principal=principal)

    assert transferred.transferred is True
    assert lead.status == LeadV12Status.READY_DISPATCH.value
    assert lead.pending_reason is None


def test_editing_public_pool_lead_refreshes_completeness_immediately(db) -> None:
    _seed_region(db)
    _, principal = _operation(db)
    lead = create_public_pool_lead(
        db,
        principal=principal,
        values={"customer_name": "待补资料客户", "phone": "13800138010"},
    )

    update_public_pool_lead(
        db,
        lead=lead,
        principal=principal,
        values={
            "region_code": "420100",
            "city": "武汉市",
            "source_channel": "OTHER",
            "source_detail": "线下活动",
            "consent_confirmed": True,
        },
    )

    assert lead.pending_reason is None
    assert "public_pool_validation_errors" not in lead.raw_payload
    complete, complete_total = list_public_pool_leads(db, completeness="COMPLETE")
    assert complete_total == 1
    assert [item.id for item in complete] == [lead.id]


def test_public_pool_completeness_filter_recognizes_preexisting_manual_drafts(db) -> None:
    _, principal = _operation(db)
    legacy_draft = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values={"customer_name": "历史未补资料客户"},
    )
    assert legacy_draft.pending_reason is None

    incomplete, incomplete_total = list_public_pool_leads(
        db,
        completeness="INCOMPLETE",
    )

    assert incomplete_total == 1
    assert [item.id for item in incomplete] == [legacy_draft.id]


def test_customer_source_is_system_derived_and_cannot_be_overwritten(db) -> None:
    _seed_region(db)
    _, operation = _operation(db)
    platform = create_public_pool_lead(
        db,
        principal=operation,
        values={
            **_complete_values("13800138020"),
            "customer_source": "FRANCHISE_SUPPLIED",
        },
    )
    update_draft(
        db,
        lead=platform,
        principal=operation,
        values={"customer_source": "FRANCHISE_SUPPLIED"},
    )

    supplier_company, _, supplier = _supplier(db)
    supplied = create_draft(
        db,
        principal=supplier,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        values=_complete_values("13800138021"),
    )

    assert (
        lead_supply_to_dict(platform, operation)["customer_source"]
        == CustomerSource.OPERATION_ENTRY.value
    )
    assert (
        lead_supply_to_dict(supplied, supplier)["customer_source"]
        == CustomerSource.FRANCHISE_SUPPLIED.value
    )
    assert supplied.supplier_company_id == supplier_company.id


def test_supplier_without_local_receiver_waits_in_public_pool_then_rechecks(db) -> None:
    _seed_region(db)
    supplier_company, _, supplier = _supplier(db)
    _, operation = _operation(db)
    supplied = create_draft(
        db,
        principal=supplier,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        values=_complete_values("13800138022"),
    )

    submitted = submit_draft(db, lead=supplied, principal=supplier)

    assert submitted.decision is DuplicateDecision.CLEAR
    assert supplied.status == LeadV12Status.PUBLIC_POOL.value
    assert supplied.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    assert has_receiver_coverage(db, supplied) is False

    franchise_items, franchise_total = list_public_pool_leads(
        db,
        customer_source=CustomerSource.FRANCHISE_SUPPLIED.value,
    )
    assert franchise_total == 1
    assert [item.id for item in franchise_items] == [supplied.id]
    serialized = lead_supply_list_to_dict(db, franchise_items, operation)[0]
    assert serialized["customer_source"] == CustomerSource.FRANCHISE_SUPPLIED.value
    assert serialized["supplier_company_id"] == supplier_company.id
    assert serialized["supplier_company_name"] == supplier_company.name

    blocked = transfer_public_pool_lead(
        db,
        lead=supplied,
        principal=operation,
    )
    assert blocked.transferred is False
    assert blocked.validation_errors == {
        "receiver_coverage": "当地暂无可接收加盟商"
    }
    assert supplied.status == LeadV12Status.PUBLIC_POOL.value

    receiver = _receiver(db)
    assert receiver.id != supplier_company.id
    assert has_receiver_coverage(db, supplied) is True

    transferred = transfer_public_pool_lead(
        db,
        lead=supplied,
        principal=operation,
    )
    assert transferred.transferred is True
    assert transferred.validation_errors == {}
    assert supplied.status == LeadV12Status.READY_DISPATCH.value
    assert supplied.pending_reason is None


def test_source_supplier_never_counts_as_its_own_local_receiver(db) -> None:
    _seed_region(db)
    supplier_company, _, supplier = _supplier(db)
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
    supplied = create_draft(
        db,
        principal=supplier,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        values=_complete_values("13800138023"),
    )
    submit_draft(db, lead=supplied, principal=supplier)

    assert has_receiver_coverage(db, supplied) is False
    assert supplied.status == LeadV12Status.PUBLIC_POOL.value


def test_supplier_with_other_approved_local_receiver_enters_dispatch_pool(db) -> None:
    _seed_region(db)
    supplier_company, _, supplier = _supplier(db)
    receiver = _receiver(db)
    supplied = create_draft(
        db,
        principal=supplier,
        source_kind=LeadSourceKind.SUPPLIER_H5,
        values=_complete_values("13800138024"),
    )

    submit_draft(db, lead=supplied, principal=supplier)

    assert receiver.id != supplier_company.id
    assert has_receiver_coverage(db, supplied) is True
    assert supplied.status == LeadV12Status.READY_DISPATCH.value
    assert supplied.pending_reason is None
    _, public_pool_total = list_public_pool_leads(
        db,
        customer_source=CustomerSource.FRANCHISE_SUPPLIED.value,
    )
    assert public_pool_total == 0


def test_feishu_dispatch_target_retains_incomplete_rows_and_is_idempotent(db, monkeypatch) -> None:
    import apps.api.src.services.public_pool_v12 as module

    _seed_region(db)
    _, principal = _operation(db)
    monkeypatch.setattr(module.settings, "feishu_app_token", "base-token")
    monkeypatch.setattr(module.settings, "feishu_table_id", "table-customer")
    monkeypatch.setattr(module.settings, "feishu_view_id", "")
    monkeypatch.setattr(module.settings, "feishu_view_name", "客户视图")
    monkeypatch.setattr(module.settings, "feishu_sync_page_size", 200)
    monkeypatch.setattr(module.settings, "feishu_sync_max_pages", 20)
    client = FakeCustomerViewClient(
        [
            FeishuRecord(
                record_id="rec-complete",
                fields={
                    "客户姓名": "完整客户",
                    "手机号": "+86 138-0013-8001",
                    "市": "武汉市",
                    "地区编码": "420100",
                    "来源渠道": "OTHER",
                    "具体来源": "飞书转介绍",
                    "已获客户授权": True,
                },
            ),
            FeishuRecord(
                record_id="rec-incomplete",
                fields={"客户姓名": "缺地区客户", "手机号": "13800138002"},
            ),
        ]
    )

    first = import_feishu_customer_view(
        db,
        principal=principal,
        target=PublicPoolTarget.DISPATCH_POOL,
        client=client,
    )
    db.commit()

    assert first.total_count == 2
    assert first.created_count == 2
    assert first.dispatch_pool_count == 1
    assert first.public_pool_count == 1
    assert first.skipped_count == 0
    assert client.resolved_view_names == ["客户视图"]
    assert client.iterated_view_ids == ["view-customer"]
    assert client.writeback_calls == 0

    complete = db.scalar(select(Lead).where(Lead.source_record_id == "rec-complete"))
    incomplete = db.scalar(select(Lead).where(Lead.source_record_id == "rec-incomplete"))
    assert complete is not None and complete.status == LeadV12Status.READY_DISPATCH.value
    assert incomplete is not None and incomplete.status == LeadV12Status.DRAFT.value
    assert incomplete.pending_reason == "PUBLIC_POOL_INCOMPLETE"
    assert complete.raw_payload["feishu_imported_field_names"]
    assert "13800138001" not in str(complete.raw_payload)
    assert "完整客户" not in str(complete.raw_payload)

    second = import_feishu_customer_view(
        db,
        principal=principal,
        target=PublicPoolTarget.DISPATCH_POOL,
        client=client,
    )
    db.commit()

    assert second.created_count == 0
    assert second.skipped_count == 2
    assert db.scalar(select(func.count(Lead.id)).where(Lead.source_kind == LeadSourceKind.FEISHU_IMPORT.value)) == 2


def test_public_pool_import_normalizes_and_rejects_duplicate_phone(db, monkeypatch) -> None:
    import apps.api.src.services.public_pool_v12 as module

    _seed_region(db)
    _, principal = _operation(db)
    existing = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values=_complete_values("13800138003"),
    )
    submit_draft(db, lead=existing, principal=principal)
    db.commit()

    monkeypatch.setattr(module.settings, "feishu_app_token", "base-token")
    monkeypatch.setattr(module.settings, "feishu_table_id", "table-customer")
    monkeypatch.setattr(module.settings, "feishu_view_id", "view-customer")
    monkeypatch.setattr(module.settings, "feishu_view_name", "客户视图")
    client = FakeCustomerViewClient(
        [
            FeishuRecord(
                record_id="rec-duplicate-phone",
                fields={
                    "客户姓名": "重复手机号客户",
                    "手机号": "+86 138 0013 8003",
                    "市": "武汉市",
                    "地区编码": "420100",
                    "来源渠道": "OTHER",
                    "具体来源": "飞书客户视图",
                    "已获客户授权": True,
                },
            )
        ]
    )

    result = import_feishu_customer_view(
        db,
        principal=principal,
        target=PublicPoolTarget.PUBLIC_POOL,
        client=client,
    )
    db.commit()

    imported = db.scalar(select(Lead).where(Lead.source_record_id == "rec-duplicate-phone"))
    assert result.duplicate_count == 0
    assert result.public_pool_count == 0
    assert result.error_count == 1
    assert imported is None
    issue = db.scalar(
        select(LeadImportIssue).where(LeadImportIssue.sync_batch_id == result.batch.id)
    )
    assert issue is not None
    assert "LEAD_PHONE_DUPLICATE" in issue.message


def test_feishu_import_rejects_a_configured_view_id_that_is_not_customer_view(db, monkeypatch) -> None:
    import apps.api.src.services.public_pool_v12 as module

    _, principal = _operation(db)
    monkeypatch.setattr(module.settings, "feishu_app_token", "base-token")
    monkeypatch.setattr(module.settings, "feishu_table_id", "table-customer")
    monkeypatch.setattr(module.settings, "feishu_view_id", "view-wrong")
    monkeypatch.setattr(module.settings, "feishu_view_name", "客户视图")
    client = FakeCustomerViewClient([])

    with pytest.raises(AppError) as error:
        import_feishu_customer_view(
            db,
            principal=principal,
            target=PublicPoolTarget.PUBLIC_POOL,
            client=client,
        )

    assert error.value.code == "FEISHU_VIEW_CONFIG_MISMATCH"
    assert client.resolved_view_names == ["客户视图"]
    assert client.iterated_view_ids == []


def test_public_pool_http_is_shared_by_admin_and_operation_but_forbidden_to_franchise(
    api_client,
    monkeypatch,
) -> None:
    client, factory = api_client

    def login(username: str, password: str) -> dict[str, str]:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert response.status_code == 200, response.text
        token = response.cookies.get("access_token")
        assert token
        return {"Authorization": f"Bearer {token}"}

    admin = login("admin", "Admin123!")
    operation = login("operation", "Operation123!")
    franchise = login("franchise_demo", "Franchise123!")

    created = client.post(
        "/api/v1/v1.2/public-pool/leads",
        headers=operation,
        json={
            "customer_name": "权限验收客户",
            "phone": "13800138011",
            "region_code": "310000",
            "city": "上海市",
            "source_channel": "OTHER",
            "source_detail": "权限验收",
            "consent_confirmed": True,
        },
    )
    assert created.status_code == 200, created.text

    admin_page = client.get("/api/v1/v1.2/public-pool/leads", headers=admin)
    operation_page = client.get("/api/v1/v1.2/public-pool/leads", headers=operation)
    forbidden_page = client.get("/api/v1/v1.2/public-pool/leads", headers=franchise)

    assert admin_page.status_code == 200
    assert operation_page.status_code == 200
    assert admin_page.json()["data"]["total"] == 1
    assert operation_page.json()["data"]["total"] == 1
    assert forbidden_page.status_code == 403

    lead_id = created.json()["data"]["id"]
    transferred = client.post(
        f"/api/v1/v1.2/public-pool/leads/{lead_id}/transfer-to-dispatch",
        headers=operation,
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["data"]["transferred"] is True

    with factory() as session:
        transfer_audit = session.scalar(
            select(AuditLog)
            .where(
                AuditLog.resource_id == lead_id,
                AuditLog.action == "V12_PUBLIC_POOL_TRANSFER",
            )
        )
        actions = list(
            session.scalars(
                select(AuditLog.action)
                .where(AuditLog.resource_id == lead_id)
                .order_by(AuditLog.created_at)
            ).all()
        )
    assert actions == ["V12_PUBLIC_POOL_LEAD_CREATE", "V12_PUBLIC_POOL_TRANSFER"]
    assert transfer_audit is not None
    assert transfer_audit.actor_user_id is not None
    assert transfer_audit.metadata_json == {
        "result": "TRANSFERRED",
        "customer_source": CustomerSource.OPERATION_ENTRY.value,
        "region_code": "310000",
        "supplier_company_id": None,
        "pending_reason": None,
        "validation_errors": {},
        "dedup_decision": DuplicateDecision.CLEAR.value,
    }

    import apps.api.src.services.public_pool_v12 as module

    feishu = FakeCustomerViewClient(
        [
            FeishuRecord(
                record_id="rec-http-audit",
                fields={"客户姓名": "导入审计客户", "手机号": "13800138012"},
            )
        ]
    )
    monkeypatch.setattr(module.settings, "feishu_app_token", "base-http-audit")
    monkeypatch.setattr(module.settings, "feishu_table_id", "table-http-audit")
    monkeypatch.setattr(module.settings, "feishu_view_id", "")
    monkeypatch.setattr(module.settings, "feishu_view_name", "客户视图")
    monkeypatch.setattr(module, "FeishuClient", lambda: feishu)

    imported = client.post(
        "/api/v1/v1.2/public-pool/feishu/import",
        headers=operation,
        json={"target_pool": "PUBLIC_POOL"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["data"]["created_count"] == 1
    assert feishu.writeback_calls == 0

    with factory() as session:
        import_audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "V12_PUBLIC_POOL_FEISHU_IMPORT")
        )
    assert import_audit is not None
