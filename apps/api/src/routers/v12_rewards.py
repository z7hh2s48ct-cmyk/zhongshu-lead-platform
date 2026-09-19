from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import reward_models_v12 as _reward_models_v12  # noqa: F401
from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import ConfigStatus
from ..core.errors import AppError
from ..core.models import Company, Lead, SystemConfig
from ..core.models_v12 import SupplierLeadReward
from ..core.responses import ok, page
from ..schemas.v12_rewards import (
    SupplierRewardReversalBody,
    SupplierRewardRuleBody,
    SupplierRewardRulePublishBody,
    SupplierRewardSettleBody,
    SupplierRewardSettleOneBody,
)
from ..services.audit import write_audit
from ..services.reward_rule_v12 import (
    REWARD_RULE_DOMAIN,
    REWARD_RULE_KEY,
    create_supplier_reward_rule,
    publish_supplier_reward_rule,
    resolve_supplier_reward_rule,
    reward_rule_config_to_dict,
)
from ..services.supplier_reward_v12 import (
    get_reward,
    reverse_supplier_reward,
    reward_to_dict,
    run_due_supplier_reward_settlement,
    settle_supplier_reward,
    supplier_reward_summary,
)

router = APIRouter(prefix="/v1.2", tags=["v1.2-supplier-rewards"])


def _lead_code(lead_id: str) -> str:
    return f"KZ-{lead_id.replace('-', '')[-8:].upper()}"


def _reward_dicts(
    db: Session,
    rewards: list[SupplierLeadReward],
    *,
    viewer: CurrentPrincipal | None = None,
) -> list[dict]:
    if not rewards:
        return []
    lead_ids = {reward.lead_id for reward in rewards}
    company_ids = {
        company_id
        for reward in rewards
        for company_id in (reward.supplier_company_id, reward.receiver_company_id)
        if company_id
    }
    leads = {
        lead.id: lead
        for lead in db.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all()
    }
    companies = dict(
        db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids))).all()
    )
    # 2026-09-17 S9：供资方不显示接收方。平台权限（reward.read/*）保留完整追溯。
    hide_receiver = viewer is not None and not (
        viewer.can("reward.read") or viewer.can("*")
    )
    result = []
    for reward in rewards:
        lead = leads.get(reward.lead_id)
        item = reward_to_dict(reward)
        item.update(
            {
                "lead_code": _lead_code(reward.lead_id),
                "customer_name": lead.customer_name if lead else None,
                "supplier_company_name": companies.get(reward.supplier_company_id),
                "receiver_company_name": companies.get(reward.receiver_company_id),
            }
        )
        if hide_receiver:
            item["receiver_company_id"] = None
            item["receiver_company_name"] = None
        result.append(item)
    return result


def _reward_dict(
    db: Session,
    reward: SupplierLeadReward,
    *,
    viewer: CurrentPrincipal | None = None,
) -> dict:
    return _reward_dicts(db, [reward], viewer=viewer)[0]


def _can_read_own_rewards(principal: CurrentPrincipal) -> bool:
    return bool(
        principal.company_id
        and (
            principal.can("supplier.reward.own.read")
            or principal.can("*")
        )
    )


@router.get("/supplier-rewards")
def list_supplier_rewards(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    supplier_company_id: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    filters = []
    if principal.can("reward.read") or principal.can("*"):
        if supplier_company_id:
            filters.append(SupplierLeadReward.supplier_company_id == supplier_company_id)
    elif _can_read_own_rewards(principal):
        filters.append(SupplierLeadReward.supplier_company_id == principal.company_id)
    else:
        raise AppError("FORBIDDEN", "无权查看供应商奖励", 403)
    if status:
        filters.append(SupplierLeadReward.status == status.strip().upper())
    total = db.scalar(select(func.count(SupplierLeadReward.id)).where(*filters)) or 0
    items = db.scalars(
        select(SupplierLeadReward)
        .where(*filters)
        .order_by(SupplierLeadReward.created_at.desc(), SupplierLeadReward.id.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    data = page(
        _reward_dicts(db, list(items), viewer=principal),
        int(total),
        page_no,
        page_size,
    )
    if _can_read_own_rewards(principal) and not supplier_company_id:
        data["summary"] = supplier_reward_summary(db, principal.company_id or "")
    return ok(request, data)


@router.get("/supplier-rewards/{reward_id}")
def supplier_reward_detail(
    reward_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    reward = get_reward(db, reward_id)
    if not (
        principal.can("reward.read")
        or principal.can("*")
        or (
            _can_read_own_rewards(principal)
            and reward.supplier_company_id == principal.company_id
        )
    ):
        raise AppError("FORBIDDEN", "无权查看该供应商奖励", 403)
    return ok(request, _reward_dict(db, reward, viewer=principal))


@router.post("/admin/supplier-rewards/settle-due")
def settle_due_rewards(
    body: SupplierRewardSettleBody,
    request: Request,
    principal=Depends(require_permissions("reward.manage")),
    db: Session = Depends(get_db),
):
    result = run_due_supplier_reward_settlement(
        db,
        limit=body.limit,
        settled_by=principal.user_id,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_REWARD_SETTLE_DUE",
        resource_type="supplier_reward_batch",
        after=result,
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, result, "到期奖励结算已执行")


@router.post("/admin/supplier-rewards/{reward_id}/settle")
def settle_one_reward(
    reward_id: str,
    body: SupplierRewardSettleOneBody,
    request: Request,
    principal=Depends(require_permissions("reward.manage")),
    db: Session = Depends(get_db),
):
    result = settle_supplier_reward(
        db,
        reward_id=reward_id,
        settled_by=principal.user_id,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_REWARD_SETTLE",
        resource_type="supplier_reward",
        resource_id=result.reward.id,
        company_id=result.reward.supplier_company_id,
        after={
            "status": result.reward.status,
            "ledger_id": result.ledger.id if result.ledger else None,
            "idempotent": result.idempotent,
            "frozen": result.frozen,
        },
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        _reward_dict(db, result.reward, viewer=principal),
        "奖励存在申诉，已冻结" if result.frozen else "奖励已结算",
    )


@router.post("/admin/supplier-rewards/{reward_id}/reverse")
def reverse_reward(
    reward_id: str,
    body: SupplierRewardReversalBody,
    request: Request,
    principal=Depends(require_permissions("reward.reverse")),
    db: Session = Depends(get_db),
):
    before = reward_to_dict(get_reward(db, reward_id))
    result = reverse_supplier_reward(
        db,
        reward_id=reward_id,
        reason_code=body.reason_code,
        note=body.note,
        reversed_by=principal.user_id,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_REWARD_REVERSE",
        resource_type="supplier_reward",
        resource_id=result.reward.id,
        company_id=result.reward.supplier_company_id,
        before=before,
        after={
            "status": result.reward.status,
            "reversal_ledger_id": result.ledger.id,
            "reason_code": body.reason_code,
            "idempotent": result.idempotent,
        },
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        _reward_dict(db, result.reward, viewer=principal),
        "奖励异常冲正已完成",
    )


@router.get("/admin/supplier-reward-rules")
def list_reward_rules(
    request: Request,
    principal=Depends(require_permissions("reward.manage")),
    db: Session = Depends(get_db),
):
    items = db.scalars(
        select(SystemConfig)
        .where(
            SystemConfig.domain == REWARD_RULE_DOMAIN,
            SystemConfig.key == REWARD_RULE_KEY,
        )
        .order_by(SystemConfig.version.desc())
    ).all()
    return ok(request, [reward_rule_config_to_dict(item) for item in items])


@router.get("/admin/supplier-reward-rules/current")
def current_reward_rule(
    request: Request,
    principal=Depends(require_permissions("reward.manage")),
    db: Session = Depends(get_db),
):
    return ok(request, resolve_supplier_reward_rule(db).snapshot())


@router.post("/admin/supplier-reward-rules")
def create_reward_rule(
    body: SupplierRewardRuleBody,
    request: Request,
    principal=Depends(require_permissions("reward.manage")),
    db: Session = Depends(get_db),
):
    item = create_supplier_reward_rule(
        db,
        values=body.rule_values(),
        created_by=principal.user_id,
        publish_immediately=body.publish_immediately,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_REWARD_RULE_CREATE",
        resource_type="system_config",
        resource_id=item.id,
        after={
            "domain": item.domain,
            "key": item.key,
            "version": item.version,
            "status": item.status,
            "value": item.value_json,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, reward_rule_config_to_dict(item), "供应商奖励规则已创建")


@router.post("/admin/supplier-reward-rules/{config_id}/publish")
def publish_reward_rule(
    config_id: str,
    body: SupplierRewardRulePublishBody,
    request: Request,
    principal=Depends(require_permissions("reward.manage")),
    db: Session = Depends(get_db),
):
    item = publish_supplier_reward_rule(
        db,
        config_id=config_id,
        published_by=principal.user_id,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_SUPPLIER_REWARD_RULE_PUBLISH",
        resource_type="system_config",
        resource_id=item.id,
        after={
            "domain": item.domain,
            "key": item.key,
            "version": item.version,
            "status": ConfigStatus.PUBLISHED.value,
            "value": item.value_json,
        },
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, reward_rule_config_to_dict(item), "供应商奖励规则已发布")
