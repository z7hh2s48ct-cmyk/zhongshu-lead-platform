"""2026-09-22/23 客户反馈第二批之一（B1/B2）：运营代改客户信息授权。

口径（2026-09-23 定稿）：
- 保留「客户信息授权」功能，H5 上传端仍强制勾选；
- 运营端任何状态可代改 consent_confirmed，不强制填写原因，系统自动留审计。
"""

from __future__ import annotations

import pytest

from apps.api.src.core.errors import AppError
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.lead_supply_v12 import (
    CORRECTION_READ_ONLY_FIELDS,
    _validate_submission,
    correct_platform_lead,
)
from apps.api.tests.test_v12_return_workflow import _principal, _workflow_setup

ADMIN_JS_PATH = "apps/admin/public/v12-operations.js"


def test_consent_is_not_read_only_in_correction() -> None:
    assert "consent_confirmed" not in CORRECTION_READ_ONLY_FIELDS


def test_operations_can_correct_consent_on_stuck_lead(db) -> None:
    setup = _workflow_setup(
        db,
        lead_status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
        suffix="FB922C",
    )
    lead = setup["lead"]
    lead.consent_confirmed = False
    db.commit()
    operator = setup["operator"]
    principal = _principal(operator, "lead.manual.manage")

    result = correct_platform_lead(
        db,
        lead_id=lead.id,
        principal=principal,
        values={"consent_confirmed": True},
        reason=None,
        expected_snapshot_version=lead.snapshot_version,
    )

    assert result.lead.consent_confirmed is True
    assert "consent_confirmed" in result.changed_fields
    assert result.before["consent_confirmed"] is False
    assert result.after["consent_confirmed"] is True


def test_corrected_lead_with_consent_passes_submission_validation(db) -> None:
    setup = _workflow_setup(
        db,
        lead_status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
        suffix="FB922D",
    )
    lead = setup["lead"]
    lead.consent_confirmed = False
    db.commit()
    operator = setup["operator"]
    principal = _principal(operator, "lead.manual.manage")
    correct_platform_lead(
        db,
        lead_id=lead.id,
        principal=principal,
        values={"consent_confirmed": True},
        reason=None,
        expected_snapshot_version=lead.snapshot_version,
    )

    phone = _validate_submission(db, lead)

    assert len(phone) == 11


def test_unconsented_lead_still_rejected_on_submit(db) -> None:
    setup = _workflow_setup(
        db,
        lead_status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
        suffix="FB922E",
    )
    lead = setup["lead"]
    lead.consent_confirmed = False
    db.commit()

    with pytest.raises(AppError) as error:
        _validate_submission(db, lead)
    assert error.value.code == "LEAD_SUBMISSION_INVALID"


def test_admin_consent_checkbox_editable_in_correction_mode() -> None:
    source = open(ADMIN_JS_PATH, encoding="utf-8").read()
    # 勾选框不再带更正禁用属性
    assert '<input id="platform-lead-consent" type="checkbox" ${item?.consent_confirmed?' in source
    # 更正模式不再用原值覆盖授权勾选
    assert "consent_confirmed:Boolean(item?.consent_confirmed)" not in source
