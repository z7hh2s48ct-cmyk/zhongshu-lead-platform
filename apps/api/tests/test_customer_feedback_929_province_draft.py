import pytest
from pathlib import Path

from apps.api.src.core.errors import AppError
from apps.api.src.core.v12_enums import LeadSourceKind, LeadV12Status
from apps.api.src.services.lead_supply_v12 import create_draft, submit_draft, update_draft
from apps.api.tests.test_v12_lead_supply import _principal, _seed_identity


def test_province_only_lead_stays_draft_until_city_is_completed(db) -> None:
    _, user = _seed_identity(db, company_code="PROVINCE-ONLY")
    principal = _principal(user.id, None, "lead.manual.manage")
    lead = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values={
            "customer_name": "待补地区客户",
            "phone": "13800139291",
            "province": "四川省",
            "consent_confirmed": True,
        },
    )
    assert lead.status == LeadV12Status.DRAFT.value
    assert lead.pending_reason == "LOCATION_PROVINCE_ONLY"

    with pytest.raises(AppError) as error:
        submit_draft(db, lead=lead, principal=principal)
    assert error.value.code == "LEAD_SUBMISSION_INVALID"
    assert lead.status == LeadV12Status.DRAFT.value

    update_draft(
        db,
        lead=lead,
        principal=principal,
        values={"province": "四川省", "city": "成都市", "region_code": "510100"},
    )
    assert lead.pending_reason is None


def test_changing_to_province_only_clears_stale_city_and_district(db) -> None:
    _, user = _seed_identity(db, company_code="PROVINCE-RESET")
    principal = _principal(user.id, None, "lead.manual.manage")
    lead = create_draft(
        db,
        principal=principal,
        source_kind=LeadSourceKind.PLATFORM_MANUAL,
        values={
            "phone": "13800139292",
            "province": "湖北省",
            "city": "武汉市",
            "district": "武昌区",
            "region_code": "420106",
        },
    )
    update_draft(db, lead=lead, principal=principal, values={"province": "四川省"})

    assert lead.province == "四川省"
    assert lead.city is None
    assert lead.district is None
    assert lead.region_code is None
    assert lead.pending_reason == "LOCATION_PROVINCE_ONLY"


def test_supplier_and_operation_forms_can_save_province_only() -> None:
    root = Path(__file__).resolve().parents[3]
    supplier = (root / "apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    operation = (root / "apps/admin/public/v12-operations.js").read_text(
        encoding="utf-8"
    )
    assert 'id="supply-province"' in supplier
    assert "province:province?.name||''" in supplier
    assert 'id="platform-lead-province"' in operation
    assert "province:province?.name||null" in operation
