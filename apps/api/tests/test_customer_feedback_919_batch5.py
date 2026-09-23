"""2026-09-19 第五批服务端验收：第二轮客户确认的 S10 统计口径细化与 S8 更正范围收窄。

S10：统计主数字为核验条数（每核验 1 次计 1 条，不论接通与否）；记录页支持日期范围与
有效/无效筛选；首页显示本日核验条数。
S8：更正仅可修改客户姓名、客户需求与所在地址；其余字段只读，后端强制拒绝改动。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import User
from apps.api.src.services.auth_service import create_internal_user
from apps.api.src.services.lead_supply_v12 import correct_platform_lead
from apps.api.tests.test_customer_feedback_829 import _lead as make_lead
from apps.api.tests.test_v12_return_workflow import _principal

CALL_APP = Path("apps/call-h5/public/app.js")
ADMIN_WORKBENCH = Path("apps/admin/public/v12-operations.js")


def _seed_draft_lead(db):
    operation = create_internal_user(
        db,
        username="correction-scope-operation",
        password="CorrScope#2026",
        display_name="更正范围运营",
        role_code="OPERATION",
    )
    lead = make_lead(operation_id=operation.id, phone="13900139819", name="更正范围基线")
    lead.status = "DRAFT"
    lead.review_status = "DRAFT"
    lead.current_assignment_id = None
    db.add(lead)
    db.commit()
    return lead, operation


def test_s8_correction_allows_name_need_and_address(db):
    lead, operation = _seed_draft_lead(db)
    result = correct_platform_lead(
        db,
        lead_id=lead.id,
        principal=_principal(operation, "lead.manual.manage"),
        values={
            "customer_name": "更正后姓名",
            "need_summary": "电销售后回访确认的实际装修需求",
            "district": "徐汇区",
            "region_code": "310104",
        },
        reason=None,
        expected_snapshot_version=None,
    )
    assert set(result.changed_fields) == {
        "customer_name",
        "need_summary",
        "district",
        "region_code",
    }


def test_s8_correction_rejects_phone_change(db):
    lead, operation = _seed_draft_lead(db)
    with pytest.raises(AppError) as error:
        correct_platform_lead(
            db,
            lead_id=lead.id,
            principal=_principal(operation, "lead.manual.manage"),
            values={"phone": "13900139820"},
            reason=None,
            expected_snapshot_version=None,
        )
    assert error.value.code == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    assert error.value.details["fields"] == ["phone"]


def test_s8_correction_rejects_read_only_fact_fields(db):
    lead, operation = _seed_draft_lead(db)
    with pytest.raises(AppError) as error:
        correct_platform_lead(
            db,
            lead_id=lead.id,
            principal=_principal(operation, "lead.manual.manage"),
            values={
                "category_code": "INTERIOR",
                "source_channel": "DOUYIN",
                "budget_min": 100,
            },
            reason=None,
            expected_snapshot_version=None,
        )
    assert error.value.code == "LEAD_CORRECTION_FIELD_NOT_ALLOWED"
    # 2026-09-23 口径：consent_confirmed 改为运营可代改，不再位于只读清单
    assert set(error.value.details["fields"]) == {
        "budget_min",
        "category_code",
        "source_channel",
    }


def test_s8_correction_tolerates_unchanged_read_only_fields(db):
    lead, operation = _seed_draft_lead(db)
    result = correct_platform_lead(
        db,
        lead_id=lead.id,
        principal=_principal(operation, "lead.manual.manage"),
        values={
            "customer_name": "只改姓名",
            "phone": "13900139819",
            "category_code": "OLD_RENOVATION",
        },
        reason=None,
        expected_snapshot_version=None,
    )
    assert result.changed_fields == ("customer_name",)


def test_s10_call_h5_contract_markers():
    app_js = CALL_APP.read_text(encoding="utf-8")
    # 统计卡主数字＝核验条数（submitted），拨打次数降为辅助口径。
    assert "${esc(label)}核验" in app_js
    assert "${esc(label)}拨打" not in app_js
    assert "<b>${Number(value?.submitted || 0)}</b><small>拨打" in app_js
    # 记录页日期范围＋有效/无效筛选。
    for marker in (
        "verificationOutcome",
        "filterSubmittedHistory",
        "records-filter-start",
        "records-filter-end",
        "data-records-outcome",
    ):
        assert marker in app_js
    # 首页展示本日核验条数。
    assert "今日已核验" in app_js


def test_s8_admin_workbench_correction_lock_markers():
    admin_js = ADMIN_WORKBENCH.read_text(encoding="utf-8")
    # 更正模式锁字段：只读/禁用属性与口径提示。
    assert "lockAttrs" in admin_js
    assert "lockSelectAttrs" in admin_js
    # 2026-09-23 口径：授权勾选在更正中可代改，提示文案随之更新。
    assert "本次更正可修改" in admin_js
    # 更正提交时锁定字段回填原值，避免 disabled 控件副作用触发后端拒绝。
    assert "phone:item?.phone||null" in admin_js
    # 授权勾选不再被原值覆盖，跟随勾选状态提交。
    assert "consent_confirmed:Boolean(item?.consent_confirmed)" not in admin_js
