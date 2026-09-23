"""2026-09-22/23 客户反馈第二批之二（A1/A2，对应反馈第 5、6 条）：

撤销"必须到县级地区才能派发"的硬门槛，改为运营自主决定（缺县可直派、
软提醒、留缺县派发标记）；"缺县转电销补县"降级为系统推荐动作。

覆盖范围：
- 状态机补边（池内 ↔ 电销核验）；
- approved_lead_pool_target / route_approved_lead_to_pool 不再因缺县转电销；
- 运营处置确认合格一律入池（不再打回电销）；
- 已关闭客资重新启用不再因缺县转电销；
- 客资列表 DTO 暴露 missing_district_region 软提醒标记；
- 存量解卡脚本 unblock_district_pending_leads.py。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import AuditLog, VerificationTask
from apps.api.src.core.state_machine_v12 import LEAD_TRANSITIONS
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.lead_supply_v12 import lead_supply_list_to_dict
from apps.api.src.services.pre_dispatch_v12 import (
    decide_pre_dispatch_disposition,
    reopen_closed_lead,
)
from apps.api.src.services.dispatch_v12 import (
    approved_lead_pool_target,
    route_approved_lead_to_pool,
)
from apps.api.src.services.rbac import assign_role
from apps.api.tests.test_v12_return_workflow import (
    _principal,
    _workflow_setup,
)

SCRIPT_PATH = Path("scripts/unblock_district_pending_leads.py")


def _load_unblock_script():
    spec = importlib.util.spec_from_file_location(
        "unblock_district_pending_leads", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 状态机与路由
# ---------------------------------------------------------------------------


def test_state_machine_allows_pool_to_telesales_round_trip() -> None:
    ready = LEAD_TRANSITIONS[LeadV12Status.READY_DISPATCH]
    pool = LEAD_TRANSITIONS[LeadV12Status.PUBLIC_POOL]
    verifying = LEAD_TRANSITIONS[LeadV12Status.PENDING_TELESALES_VERIFY]
    # 运营可显式把池内客资转回电销核验（推荐动作）。
    assert LeadV12Status.PENDING_TELESALES_VERIFY in ready
    assert LeadV12Status.PENDING_TELESALES_VERIFY in pool
    # 电销/解卡路径可直接入池，无需兜底改写。
    assert LeadV12Status.READY_DISPATCH in verifying
    assert LeadV12Status.PUBLIC_POOL in verifying


def test_pool_target_prefers_dispatch_pool_even_when_district_missing(db) -> None:
    setup = _workflow_setup(db, suffix="FB922F")
    lead = setup["lead"]
    # 平台客资缺县 -> 直接 READY_DISPATCH（不再转电销）。
    lead.source_kind = "PLATFORM_MANUAL"
    lead.region_code = "420900"
    assert approved_lead_pool_target(db, lead) is LeadV12Status.READY_DISPATCH


def test_route_approved_keeps_missing_county_lead_dispatchable(db) -> None:
    setup = _workflow_setup(db, suffix="FB922G")
    lead = setup["lead"]
    lead.status = LeadV12Status.PENDING_REVIEW.value
    lead.source_kind = "PLATFORM_MANUAL"
    lead.region_code = "420900"
    target = route_approved_lead_to_pool(db, lead)
    db.commit()
    assert target is LeadV12Status.READY_DISPATCH
    assert lead.pending_reason is None


# ---------------------------------------------------------------------------
# 运营处置与重新启用
# ---------------------------------------------------------------------------


def _lead_with_submitted_task(db, suffix: str):
    setup = _workflow_setup(
        db,
        lead_status=LeadV12Status.PENDING_OPERATION_DISPOSITION.value,
        suffix=suffix,
    )
    lead = setup["lead"]
    lead.region_code = "420900"
    now = datetime.now(timezone.utc)
    db.add(
        VerificationTask(
            lead_id=lead.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="SUBMITTED",
            assignee_user_id=setup["telesales"].id,
            assigned_by=setup["operator"].id,
            assigned_at=now,
            submitted_at=now,
            verification_conclusion="QUALIFIED",
        )
    )
    db.commit()
    return setup


def test_disposition_approve_routes_missing_county_lead_to_pool(db) -> None:
    setup = _lead_with_submitted_task(db, suffix="FB922H")
    lead = setup["lead"]
    principal = _principal(setup["operator"], "verification.read")

    decide_pre_dispatch_disposition(
        db,
        lead_id=lead.id,
        principal=principal,
        decision="APPROVE_POOL",
        note="电销结论合格，同意进入派发池",
    )
    db.commit()

    # 加盟商客资且当地无覆盖 -> 公海池；关键是不再打回电销。
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    assert lead.pending_reason != "DISTRICT_PENDING_VERIFY"


def test_reopen_closed_lead_goes_back_to_dispatch_pool_when_district_missing(db) -> None:
    setup = _workflow_setup(
        db, lead_status=LeadV12Status.CLOSED.value, suffix="FB922I"
    )
    assign_role(db, setup["operator"], "OPERATION")
    lead = setup["lead"]
    lead.region_code = "420900"
    principal = _principal(setup["operator"], "lead.manual.manage")

    reopen_closed_lead(
        db,
        lead_id=lead.id,
        principal=principal,
        reason="客户主动恢复合作，重新启用派发",
    )
    db.commit()

    assert lead.status == LeadV12Status.READY_DISPATCH.value
    assert lead.pending_reason == "REOPENED_FOR_REDISPATCH"


# ---------------------------------------------------------------------------
# 软提醒标记与解卡脚本
# ---------------------------------------------------------------------------


def test_lead_dto_exposes_missing_district_region_flag(db) -> None:
    setup = _workflow_setup(db, suffix="FB922J")
    lead = setup["lead"]
    lead.region_code = "420900"
    db.commit()

    dicts = lead_supply_list_to_dict(db, [lead])
    assert dicts[0]["missing_district_region"] is True

    lead.region_code = "420106"
    db.commit()
    dicts = lead_supply_list_to_dict(db, [lead])
    assert dicts[0]["missing_district_region"] is False


def test_unblock_script_recovers_district_pending_stuck_leads(db) -> None:
    module = _load_unblock_script()
    setup = _workflow_setup(db, suffix="FB922K")
    lead = setup["lead"]
    lead.status = LeadV12Status.PENDING_TELESALES_VERIFY.value
    lead.pending_reason = "DISTRICT_PENDING_VERIFY"
    lead.region_code = "420900"
    # 解卡只针对未派发客资：释放夹具带出的进行中派发单。
    lead.current_assignment_id = None
    setup["assignment"].status = AssignmentStatus.RELEASED.value
    # 夹具可能带出历史核验轮次；解卡目标要求没有进行中的核验任务。
    db.execute(
        delete(VerificationTask).where(
            VerificationTask.lead_id == lead.id,
            VerificationTask.task_type == "PRE_DISPATCH_VERIFY",
        )
    )
    db.commit()

    targets = module.collect_targets(db)
    assert [item.id for item in targets] == [lead.id]

    summary = module.apply_unblock(db, targets)
    db.commit()

    assert summary["unblocked"] == 1
    assert lead.status == LeadV12Status.PUBLIC_POOL.value
    assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "SYSTEM_DISTRICT_RULE_RETIRED_UNBLOCK",
            AuditLog.resource_id == lead.id,
        )
    )
    assert audit is not None
    # 幂等：解卡后不再命中筛选条件。
    assert module.collect_targets(db) == []
