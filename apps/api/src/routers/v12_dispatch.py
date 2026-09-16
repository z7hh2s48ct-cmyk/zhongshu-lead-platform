from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import AssignmentStatus
from ..core.errors import AppError
from ..core.models import Assignment, AssignmentEvent, Company, FollowUp, Lead, User
from ..core.responses import ok, page
from ..core.security import decrypt_text, mask_phone
from ..core.v12_enums import LeadV12Status
from ..schemas.v12_dispatch import (
    InternalAssignmentBody,
    ManualDispatchBody,
    RefuseAssignmentBody,
)
from ..services.audit import write_audit
from ..services.claim_singleflight import run_claim_singleflight
from ..services.company_account_management import require_superadmin_reason
from ..services.company_assignment_v12 import assign_internal_employee
from ..services.dispatch_v12 import (
    CLAIMED_CONTACT_STATUSES,
    candidate_to_dict,
    claim_assignment,
    count_candidates,
    dispatch_manually_with_outcome,
    get_dispatch_lead,
    lead_pool_item,
    list_candidates,
    list_dispatch_pool,
    manual_dispatch_idempotency_guard,
    refuse_pending_assignment,
)
from ..services.pre_dispatch_v12 import latest_submitted_pre_dispatch_task_ids
from ..services.supplier_reward_v12 import confirmation_sql_expressions

router = APIRouter(prefix="/v1.2", tags=["v1.2-dispatch-claim"])


def _principal_company_id(principal) -> str:
    if not principal.company_id:
        raise AppError("COMPANY_REQUIRED", "当前账号未绑定公司", 403)
    return principal.company_id


@lru_cache(maxsize=4096)
def _masked_encrypted_phone(phone_encrypted: str) -> str:
    return mask_phone(decrypt_text(phone_encrypted))


def _receive_confirmation(claimed_at) -> tuple[str, str | None]:
    return (
        "CONFIRMED" if claimed_at else "PENDING",
        claimed_at.isoformat() if claimed_at else None,
    )


def _assignment_follow_status_expression():
    latest_followup_status = (
        select(FollowUp.status)
        .where(FollowUp.assignment_id == Assignment.id)
        .order_by(FollowUp.created_at.desc(), FollowUp.id.desc())
        .limit(1)
        .correlate(Assignment)
        .scalar_subquery()
    )
    return case(
        (Lead.current_assignment_id == Assignment.id, Lead.current_follow_status),
        else_=latest_followup_status,
    ).label("assignment_follow_status")


def _assignment_auto_confirmed_at_expression():
    return (
        select(func.max(AssignmentEvent.occurred_at))
        .where(
            AssignmentEvent.assignment_id == Assignment.id,
            AssignmentEvent.event_type == "V12_ASSIGNMENT_AUTO_CONFIRMED",
        )
        .correlate(Assignment)
        .scalar_subquery()
        .label("auto_confirmed_at")
    )


def _assignment_confirmation_expressions(dialect_name: str):
    confirmation = confirmation_sql_expressions(dialect_name=dialect_name)
    confirmed_at = case(
        (confirmation.effective_at <= func.now(), confirmation.effective_at),
        else_=None,
    ).label("transaction_confirmed_at")
    policy = case(
        (confirmation.effective_at <= func.now(), confirmation.policy),
        else_=None,
    ).label("transaction_confirmation_policy")
    return confirmed_at, policy


def _assignment_dict(
    assignment: Assignment,
    lead: Lead,
    *,
    reveal_phone: bool = False,
    follow_status: str | None = None,
    auto_confirmed_at=None,
    transaction_confirmed_at=None,
    transaction_confirmation_policy: str | None = None,
) -> dict:
    reveal_phone = reveal_phone and lead.pending_reason != "CORRECTION_REVIEW_REQUIRED"
    phone = decrypt_text(lead.phone_encrypted) if reveal_phone else None
    phone_masked = (
        mask_phone(phone)
        if phone is not None
        else _masked_encrypted_phone(lead.phone_encrypted)
    )
    receive_confirmation_status, receive_confirmed_at = _receive_confirmation(
        assignment.claimed_at
    )
    return {
        "id": assignment.id,
        "lead_id": assignment.lead_id,
        "company_id": assignment.company_id,
        "supplier_company_id": assignment.supplier_company_id,
        "receiver_company_id": assignment.receiver_company_id,
        "status": assignment.status,
        "lead_status": lead.status,
        "lead_pending_reason": lead.pending_reason,
        "correction_issues": list(
            (lead.raw_payload or {}).get("correction_issues") or []
        ),
        "current_follow_status": (
            follow_status
            if follow_status is not None
            else lead.current_follow_status
            if lead.current_assignment_id == assignment.id
            else None
        ),
        "receive_confirmation_status": receive_confirmation_status,
        "receive_confirmed_at": receive_confirmed_at,
        "auto_confirmed_at": auto_confirmed_at.isoformat()
        if auto_confirmed_at
        else None,
        "transaction_confirmed_at": transaction_confirmed_at.isoformat()
        if transaction_confirmed_at
        else None,
        "transaction_confirmation_policy": transaction_confirmation_policy,
        "points_price": assignment.points_price,
        "claim_points": assignment.claim_points,
        "price_rule_id": assignment.price_rule_id,
        "price_version": assignment.price_version,
        "customer_name": lead.customer_name,
        "phone": phone,
        "phone_masked": phone_masked,
        "city": lead.city,
        "district": lead.district,
        "region_code": lead.region_code,
        "need_summary": lead.need_summary,
        "assigned_at": assignment.assigned_at.isoformat(),
        "expires_at": assignment.expires_at.isoformat()
        if assignment.expires_at
        else None,
        "claimed_at": assignment.claimed_at.isoformat()
        if assignment.claimed_at
        else None,
        "released_at": assignment.released_at.isoformat()
        if assignment.released_at
        else None,
        "release_reason": assignment.release_reason,
        "appeal_paused_at": assignment.appeal_paused_at.isoformat() if assignment.appeal_paused_at else None,
        "appeal_remaining_seconds": assignment.appeal_remaining_seconds,
        "appeal_resumed_at": assignment.appeal_resumed_at.isoformat() if assignment.appeal_resumed_at else None,
        "appeal_deadline_at": assignment.appeal_deadline_at.isoformat()
        if assignment.appeal_deadline_at
        else None,
        "reward_due_at": assignment.reward_due_at.isoformat()
        if assignment.reward_due_at
        else None,
        "first_followup_due_at": assignment.first_followup_due_at.isoformat()
        if assignment.first_followup_due_at
        else None,
        "internal_assignee_user_id": assignment.internal_assignee_user_id,
        "internal_assigned_at": assignment.internal_assigned_at.isoformat()
        if assignment.internal_assigned_at
        else None,
    }


def _assignment_detail_projection(
    assignment_id: str,
    company_id: str,
    dialect_name: str = "postgresql",
):
    transaction_confirmed_at, transaction_confirmation_policy = (
        _assignment_confirmation_expressions(dialect_name)
    )
    return (
        select(
            Assignment.id,
            Assignment.lead_id,
            Assignment.company_id,
            Assignment.supplier_company_id,
            Assignment.receiver_company_id,
            Assignment.status,
            Assignment.points_price,
            Assignment.claim_points,
            Assignment.price_rule_id,
            Assignment.price_version,
            Assignment.assigned_at,
            Assignment.expires_at,
            Assignment.claimed_at,
            Assignment.released_at,
            Assignment.release_reason,
            Assignment.appeal_deadline_at,
            Assignment.appeal_paused_at,
            Assignment.appeal_remaining_seconds,
            Assignment.appeal_resumed_at,
            Assignment.reward_due_at,
            Assignment.first_followup_due_at,
            Assignment.internal_assignee_user_id,
            Assignment.internal_assigned_at,
            Lead.customer_name,
            Lead.phone_encrypted,
            Lead.city,
            Lead.district,
            Lead.region_code,
            Lead.need_summary,
            Lead.status.label("lead_status"),
            Lead.pending_reason.label("lead_pending_reason"),
            _assignment_follow_status_expression(),
            _assignment_auto_confirmed_at_expression(),
            transaction_confirmed_at,
            transaction_confirmation_policy,
        )
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(
            Assignment.id == assignment_id,
            Assignment.company_id == company_id,
            Lead.deleted_at.is_(None),
        )
    )


def _projected_assignment_dict(row, *, reveal_phone: bool = False) -> dict:
    reveal_phone = (
        reveal_phone
        and row.lead_pending_reason != "CORRECTION_REVIEW_REQUIRED"
    )
    phone = decrypt_text(row.phone_encrypted) if reveal_phone else None
    phone_masked = (
        mask_phone(phone) if phone is not None else _masked_encrypted_phone(row.phone_encrypted)
    )
    receive_confirmation_status, receive_confirmed_at = _receive_confirmation(row.claimed_at)
    return {
        "id": row.id,
        "lead_id": row.lead_id,
        "company_id": row.company_id,
        "supplier_company_id": row.supplier_company_id,
        "receiver_company_id": row.receiver_company_id,
        "status": row.status,
        "lead_status": row.lead_status,
        "lead_pending_reason": row.lead_pending_reason,
        "correction_issues": [],
        "current_follow_status": row.assignment_follow_status,
        "receive_confirmation_status": receive_confirmation_status,
        "receive_confirmed_at": receive_confirmed_at,
        "auto_confirmed_at": row.auto_confirmed_at.isoformat()
        if row.auto_confirmed_at
        else None,
        "transaction_confirmed_at": row.transaction_confirmed_at.isoformat()
        if row.transaction_confirmed_at
        else None,
        "transaction_confirmation_policy": row.transaction_confirmation_policy,
        "points_price": row.points_price,
        "claim_points": row.claim_points,
        "price_rule_id": row.price_rule_id,
        "price_version": row.price_version,
        "customer_name": row.customer_name,
        "phone": phone,
        "phone_masked": phone_masked,
        "city": row.city,
        "district": row.district,
        "region_code": row.region_code,
        "need_summary": row.need_summary,
        "assigned_at": row.assigned_at.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "released_at": row.released_at.isoformat() if row.released_at else None,
        "release_reason": row.release_reason,
        "appeal_paused_at": row.appeal_paused_at.isoformat() if row.appeal_paused_at else None,
        "appeal_remaining_seconds": row.appeal_remaining_seconds,
        "appeal_resumed_at": row.appeal_resumed_at.isoformat() if row.appeal_resumed_at else None,
        "appeal_deadline_at": row.appeal_deadline_at.isoformat()
        if row.appeal_deadline_at
        else None,
        "reward_due_at": row.reward_due_at.isoformat() if row.reward_due_at else None,
        "first_followup_due_at": row.first_followup_due_at.isoformat()
        if row.first_followup_due_at
        else None,
        "internal_assignee_user_id": row.internal_assignee_user_id,
        "internal_assigned_at": row.internal_assigned_at.isoformat()
        if row.internal_assigned_at
        else None,
    }


@router.get("/dispatch-pool")
def dispatch_pool(
    request: Request,
    principal=Depends(require_permissions("lead.dispatch")),
    db: Session = Depends(get_db),
    region_code: str | None = Query(default=None),
    source_kind: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    items, total = list_dispatch_pool(
        db,
        region_code=region_code,
        source_kind=source_kind.strip().upper() if source_kind else None,
        page_no=page_no,
        page_size=page_size,
    )
    verification_task_ids = latest_submitted_pre_dispatch_task_ids(
        db,
        [item.id for item in items],
    )
    supplier_ids = {item.supplier_company_id for item in items if item.supplier_company_id}
    submitter_ids = {item.submitter_user_id for item in items if item.submitter_user_id}
    supplier_names = dict(
        db.execute(select(Company.id, Company.name).where(Company.id.in_(supplier_ids))).all()
    ) if supplier_ids else {}
    submitter_names = dict(
        db.execute(select(User.id, User.display_name).where(User.id.in_(submitter_ids))).all()
    ) if submitter_ids else {}
    reveal_phone = principal.can("lead.dispatch.phone.read") or principal.can("*")
    payload = []
    for item in items:
        item_payload = lead_pool_item(
            item,
            reveal_phone=reveal_phone,
            supplier_company_name=supplier_names.get(item.supplier_company_id),
            submitter_name=submitter_names.get(item.submitter_user_id),
        )
        task_id = verification_task_ids.get(item.id)
        item_payload.update(
            {
                "has_verification_info": task_id is not None,
                "pre_dispatch_task_id": task_id,
            }
        )
        payload.append(item_payload)
    if reveal_phone and items:
        write_audit(
            db,
            principal=principal,
            action="V12_DISPATCH_POOL_PHONE_READ",
            resource_type="dispatch_pool",
            after={"lead_ids": [item.id for item in items], "count": len(items)},
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(request, page(payload, total, page_no, page_size))


@router.get("/dispatch-pool/{lead_id}/candidates")
def dispatch_candidates(
    lead_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.dispatch")),
    db: Session = Depends(get_db),
    keyword: str | None = Query(default=None, max_length=128),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    lead = get_dispatch_lead(db, lead_id)
    if lead.status != LeadV12Status.READY_DISPATCH.value or lead.current_assignment_id:
        raise AppError(
            "LEAD_NOT_READY_DISPATCH",
            "客资当前不在待派发池",
            409,
            {"status": lead.status, "current_assignment_id": lead.current_assignment_id},
        )
    items = list_candidates(
        db,
        lead=lead,
        keyword=keyword,
        page_no=page_no,
        page_size=page_size,
    )
    total = count_candidates(db, keyword=keyword)
    include_financials = principal.can("points.read") or principal.can("*")
    return ok(
        request,
        {
            "lead": lead_pool_item(lead),
            "page_eligible_count": sum(1 for item in items if item.eligible),
            "total": total,
            "page": page_no,
            "page_size": page_size,
            "has_more": page_no * page_size < total,
            "candidates": [
                candidate_to_dict(item, include_financials=include_financials) for item in items
            ],
        },
    )


@router.post("/dispatch-pool/{lead_id}/dispatch")
def manual_dispatch(
    lead_id: str,
    body: ManualDispatchBody,
    request: Request,
    principal=Depends(require_permissions("lead.dispatch")),
    db: Session = Depends(get_db),
):
    with manual_dispatch_idempotency_guard(body.idempotency_key):
        outcome = dispatch_manually_with_outcome(
            db,
            lead_id=lead_id,
            company_id=body.company_id,
            employee_user_id=body.employee_user_id,
            assigned_by=principal.user_id,
            idempotency_key=body.idempotency_key,
            note=body.note,
            return_receiver_override=body.return_receiver_override,
            return_receiver_override_reason=body.return_receiver_override_reason,
        )
        assignment = outcome.assignment
        lead = get_dispatch_lead(db, lead_id)
        if outcome.created:
            write_audit(
                db,
                principal=principal,
                action="V12_MANUAL_DISPATCH",
                resource_type="assignment",
                resource_id=assignment.id,
                company_id=assignment.company_id,
                after={
                    "lead_id": lead_id,
                    "company_id": assignment.company_id,
                    "employee_user_id": body.employee_user_id,
                    "recipient_user_id": assignment.internal_assignee_user_id,
                    "recipient_role_code": (assignment.lead_snapshot or {}).get(
                        "direct_recipient_role_code"
                    ),
                    "status": assignment.status,
                    "points_price": assignment.points_price,
                    "manual": True,
                    "return_receiver_override": body.return_receiver_override,
                    "return_receiver_override_reason": body.return_receiver_override_reason,
                },
                reason=body.return_receiver_override_reason or body.note,
                request_id=request.state.request_id,
            )
        db.commit()
    return ok(request, _assignment_dict(assignment, lead), "客资已人工派发")


@router.get("/assignments")
def own_assignments(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    company_id = _principal_company_id(principal)
    if principal.has_any_role("FRANCHISE_OWNER") and principal.can("assignment.own.read"):
        filters = [Assignment.company_id == company_id]
    elif principal.has_any_role("FRANCHISE_EMPLOYEE") and principal.can("assignment.employee.read"):
        filters = [
            Assignment.company_id == company_id,
            Assignment.internal_assignee_user_id == principal.user_id,
        ]
    else:
        raise AppError("FORBIDDEN", "无权查看加盟商领取客资", 403)
    if status:
        statuses = list(
            dict.fromkeys(
                item.strip().upper() for item in status.split(",") if item.strip()
            )
        )
        if statuses:
            filters.append(Assignment.status.in_(statuses))
    visible_assignments = (
        select(func.count(Assignment.id))
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(*filters, Lead.deleted_at.is_(None))
    )
    total = db.scalar(visible_assignments) or 0
    transaction_confirmed_at, transaction_confirmation_policy = (
        _assignment_confirmation_expressions(db.get_bind().dialect.name)
    )
    rows = db.execute(
        select(
            Assignment,
            Lead,
            _assignment_follow_status_expression(),
            _assignment_auto_confirmed_at_expression(),
            transaction_confirmed_at,
            transaction_confirmation_policy,
        )
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(*filters, Lead.deleted_at.is_(None))
        .order_by(Assignment.assigned_at.desc(), Assignment.id.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        _assignment_dict(
            assignment,
            lead,
            reveal_phone=assignment.status in CLAIMED_CONTACT_STATUSES,
            follow_status=follow_status,
            auto_confirmed_at=auto_confirmed_at,
            transaction_confirmed_at=transaction_confirmed_at,
            transaction_confirmation_policy=transaction_confirmation_policy,
        )
        for assignment, lead, follow_status, auto_confirmed_at, transaction_confirmed_at, transaction_confirmation_policy in rows
    ]
    return ok(request, page(items, int(total), page_no, page_size))


@router.get("/assignments/{assignment_id}")
def own_assignment_detail(
    assignment_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _principal_company_id(principal)
    statement = _assignment_detail_projection(
        assignment_id,
        company_id,
        db.get_bind().dialect.name,
    )
    if principal.has_any_role("FRANCHISE_OWNER") and principal.can("assignment.own.read"):
        pass
    elif principal.has_any_role("FRANCHISE_EMPLOYEE") and principal.can("assignment.employee.read"):
        statement = statement.where(Assignment.internal_assignee_user_id == principal.user_id)
    else:
        raise AppError("FORBIDDEN", "无权查看该加盟商客资", 403)
    row = db.execute(statement).one_or_none()
    if row is None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    return ok(
        request,
        _projected_assignment_dict(
            row,
            reveal_phone=row.status in CLAIMED_CONTACT_STATUSES,
        ),
    )


@router.post("/assignments/{assignment_id}/claim")
def claim_own_assignment(
    assignment_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _principal_company_id(principal)
    assignment_scope = db.get(Assignment, assignment_id)
    if assignment_scope is None or assignment_scope.company_id != company_id:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    if assignment_scope.internal_assignee_user_id:
        employee_claim = (
            principal.has_any_role("FRANCHISE_EMPLOYEE")
            and principal.can("assignment.employee.claim")
            and assignment_scope.internal_assignee_user_id == principal.user_id
        )
        historical_owner_replay = (
            principal.has_any_role("FRANCHISE_OWNER")
            and principal.can("assignment.own.claim")
            and assignment_scope.claimed_at is not None
            and assignment_scope.internal_assignee_user_id == principal.user_id
            and assignment_scope.internal_assigned_by == principal.user_id
        )
        direct_owner_assignment = (
            principal.has_any_role("FRANCHISE_OWNER")
            and principal.can("assignment.own.claim")
            and assignment_scope.internal_assignee_user_id == principal.user_id
            and (assignment_scope.lead_snapshot or {}).get("direct_recipient_role_code")
            == "FRANCHISE_OWNER"
        )
        if not (employee_claim or historical_owner_replay or direct_owner_assignment):
            raise AppError("FORBIDDEN", "该客资仅限被指定员工领取", 403)
    elif not (
        principal.has_any_role("FRANCHISE_OWNER")
        and principal.can("assignment.own.claim")
    ):
        raise AppError("FORBIDDEN", "仅加盟商负责人可领取历史未指定员工的客资", 403)

    def execute_claim() -> dict:
        result = claim_assignment(
            db,
            assignment_id=assignment_id,
            company_id=company_id,
            claimed_by=principal.user_id,
        )
        lead = get_dispatch_lead(db, result.assignment.lead_id)
        if not result.idempotent:
            write_audit(
                db,
                principal=principal,
                action="V12_ASSIGNMENT_CLAIM",
                resource_type="assignment",
                resource_id=result.assignment.id,
                company_id=company_id,
                after={
                    "lead_id": lead.id,
                    "status": result.assignment.status,
                    "points": result.assignment.points_price,
                    "ledger_id": result.ledger.id,
                    "appeal_deadline_at": result.assignment.appeal_deadline_at.isoformat()
                    if result.assignment.appeal_deadline_at
                    else None,
                    "reward_id": result.reward.id if result.reward else None,
                    "idempotent": False,
                },
                request_id=request.state.request_id,
            )
        db.commit()
        return {
            "assignment": _assignment_dict(result.assignment, lead, reveal_phone=True),
            "ledger": {
                "id": result.ledger.id,
                "delta": result.ledger.delta,
                "balance_after": result.ledger.balance_after,
            },
            "reward": {
                "id": result.reward.id,
                "status": result.reward.status,
                "reward_points": result.reward.reward_points,
                "reward_due_at": result.reward.reward_due_at.isoformat()
                if result.reward.reward_due_at
                else None,
            }
            if result.reward
            else None,
            "idempotent": result.idempotent,
        }

    payload, coalesced = run_claim_singleflight(
        f"{company_id}:{assignment_id}",
        execute_claim,
        before_wait=db.rollback,
    )
    if coalesced:
        payload["idempotent"] = True
    if payload["idempotent"]:
        # PII 访问留痕：重放/合并 follower 同样拿到明文手机号，
        # 需可追溯“哪些账号看过该客资明文”（区别于业务事实审计，不产生通知投影）。
        write_audit(
            db,
            principal=principal,
            action="V12_ASSIGNMENT_CLAIM_REPLAY",
            resource_type="assignment",
            resource_id=assignment_id,
            company_id=company_id,
            metadata={"coalesced": bool(coalesced)},
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(request, payload, "派发单已领取" if payload["idempotent"] else "领取成功")


@router.post("/assignments/{assignment_id}/refuse")
def refuse_own_assignment(
    assignment_id: str,
    body: RefuseAssignmentBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    company_id = _principal_company_id(principal)
    assignment_scope = db.get(Assignment, assignment_id)
    if assignment_scope is None or assignment_scope.company_id != company_id:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    if assignment_scope.internal_assignee_user_id:
        employee_refusal = (
            principal.has_any_role("FRANCHISE_EMPLOYEE")
            and principal.can("assignment.employee.claim")
            and assignment_scope.internal_assignee_user_id == principal.user_id
        )
        direct_owner_refusal = (
            principal.has_any_role("FRANCHISE_OWNER")
            and principal.can("assignment.own.claim")
            and assignment_scope.internal_assignee_user_id == principal.user_id
            and (assignment_scope.lead_snapshot or {}).get("direct_recipient_role_code")
            == "FRANCHISE_OWNER"
        )
        if not (employee_refusal or direct_owner_refusal):
            raise AppError("FORBIDDEN", "该客资仅限被指定员工拒绝领取", 403)
    elif not (
        principal.has_any_role("FRANCHISE_OWNER")
        and principal.can("assignment.own.claim")
    ):
        raise AppError("FORBIDDEN", "仅加盟商负责人可拒绝历史未指定员工的客资", 403)
    assignment, lead = refuse_pending_assignment(
        db,
        assignment_id=assignment_id,
        company_id=company_id,
        refused_by=principal.user_id,
        reason=body.reason,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_ASSIGNMENT_REFUSE",
        resource_type="assignment",
        resource_id=assignment.id,
        company_id=company_id,
        before={
            "status": AssignmentStatus.PENDING_CLAIM.value,
            "lead_status": LeadV12Status.DISPATCHED.value,
        },
        after={
            "status": assignment.status,
            "lead_status": lead.status,
            "release_reason": assignment.release_reason,
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        {
            "assignment_id": assignment.id,
            "lead_id": lead.id,
            "status": assignment.status,
            "lead_status": lead.status,
            "released_at": assignment.released_at.isoformat() if assignment.released_at else None,
            "release_reason": assignment.release_reason,
        },
        "已拒绝领取，客资已回到待派发池",
    )


@router.post("/assignments/{assignment_id}/internal-assignee")
def assign_internal_assignee(
    assignment_id: str,
    body: InternalAssignmentBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    change = assign_internal_employee(
        db,
        assignment_id=assignment_id,
        principal=principal,
        employee_user_id=body.employee_user_id,
        reason=body.reason,
    )
    if change.changed:
        write_audit(
            db,
            principal=principal,
            action=(
                "V12_INTERNAL_ASSIGNMENT_ASSIGN"
                if body.employee_user_id is not None
                else "V12_INTERNAL_ASSIGNMENT_RECALL"
            ),
            resource_type="assignment",
            resource_id=change.assignment.id,
            company_id=change.assignment.company_id,
            before={"internal_assignee_user_id": change.previous_employee_user_id},
            after={"internal_assignee_user_id": change.assignment.internal_assignee_user_id},
            reason=body.reason,
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(
        request,
        {
            "assignment_id": change.assignment.id,
            "internal_assignee_user_id": change.assignment.internal_assignee_user_id,
            "changed": change.changed,
        },
        "公司内部客资分配已更新",
    )


@router.get("/companies/{company_id}/assignment-summary")
def company_assignment_summary(
    company_id: str,
    request: Request,
    principal=Depends(require_permissions("assignment.read")),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(Assignment.status, func.count(Assignment.id))
        .where(Assignment.company_id == company_id)
        .group_by(Assignment.status)
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    return ok(
        request,
        {"company_id": company_id, "total": sum(counts.values()), "by_status": counts},
    )


@router.get("/companies/{company_id}/assignments")
def company_assignments(
    company_id: str,
    request: Request,
    principal=Depends(require_permissions("assignment.read")),
    db: Session = Depends(get_db),
    assignment_status: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "加盟商不存在", 404)
    filters = [Assignment.company_id == company_id]
    if assignment_status:
        filters.append(Assignment.status == assignment_status.strip().upper())
    total = db.scalar(select(func.count(Assignment.id)).where(*filters)) or 0
    rows = db.execute(
        select(Assignment, Lead, _assignment_follow_status_expression())
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(*filters)
        .order_by(Assignment.assigned_at.desc(), Assignment.id.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    payload = page(
        [
            _assignment_dict(
                assignment,
                lead,
                reveal_phone=principal.can("lead.phone.read") or principal.can("*"),
                follow_status=follow_status,
            )
            for assignment, lead, follow_status in rows
        ],
        int(total),
        page_no,
        page_size,
    )
    payload["company_id"] = company.id
    payload["company_name"] = company.name
    payload["assignment_status"] = assignment_status.strip().upper() if assignment_status else None
    return ok(request, payload)


@router.get("/admin/companies/{company_id}/internal-assignments")
def superadmin_internal_assignment_audit(
    company_id: str,
    request: Request,
    principal: CurrentPrincipal,
    reason: str | None = Query(default=None, max_length=500),
    db: Session = Depends(get_db),
):
    if not principal.has_any_role("SUPER_ADMIN"):
        raise AppError("FORBIDDEN", "仅超级管理员可查询公司内部协作明细", 403)
    normalized_reason = require_superadmin_reason(principal, reason)
    from ..core.models import User

    rows = db.execute(
        select(Assignment, User.display_name)
        .outerjoin(User, User.id == Assignment.internal_assignee_user_id)
        .where(Assignment.company_id == company_id)
        .order_by(Assignment.assigned_at.desc())
    ).all()
    write_audit(
        db,
        principal=principal,
        action="V12_INTERNAL_ASSIGNMENT_DETAIL_READ",
        resource_type="company",
        resource_id=company_id,
        company_id=company_id,
        metadata={"reason": normalized_reason, "record_count": len(rows)},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        [
            {
                "assignment_id": assignment.id,
                "status": assignment.status,
                "employee_user_id": assignment.internal_assignee_user_id,
                "employee_display_name": display_name,
                "assigned_at": assignment.internal_assigned_at.isoformat()
                if assignment.internal_assigned_at
                else None,
            }
            for assignment, display_name in rows
        ],
    )
