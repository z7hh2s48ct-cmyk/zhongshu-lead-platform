from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.enums import AssignmentStatus
from ..core.errors import AppError
from ..core.models import Assignment, AssignmentEvent, Company, Role, User


_ACTIVE_INTERNAL_ASSIGNMENT_STATUSES = {
    AssignmentStatus.CLAIMED.value,
    AssignmentStatus.FOLLOWING.value,
    AssignmentStatus.RETURN_PENDING.value,
}

_EMPLOYEE_BLOCKING_ASSIGNMENT_STATUSES = {
    AssignmentStatus.PENDING_CLAIM.value,
    *_ACTIVE_INTERNAL_ASSIGNMENT_STATUSES,
}


@dataclass(frozen=True)
class InternalAssignmentChange:
    assignment: Assignment
    previous_employee_user_id: str | None
    changed: bool


@dataclass(frozen=True)
class DirectDispatchRecipients:
    assignee: User
    owner: User
    assignee_role_code: str


def resolve_company_owner(db: Session, *, company_id: str) -> User:
    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "目标公司不存在", 404)

    owner: User | None = None
    if company.primary_user_id:
        owner = db.scalar(
            select(User)
            .join(User.roles)
            .where(
                User.id == company.primary_user_id,
                User.company_id == company_id,
                User.status == "ACTIVE",
                Role.code == "FRANCHISE_OWNER",
            )
        )
    if owner is None:
        owners = db.scalars(
            select(User)
            .join(User.roles)
            .where(
                User.company_id == company_id,
                User.status == "ACTIVE",
                Role.code == "FRANCHISE_OWNER",
            )
            .order_by(User.id.asc())
        ).all()
        if len(owners) == 1:
            owner = owners[0]
    if owner is None:
        raise AppError(
            "COMPANY_OWNER_RECIPIENT_REQUIRED",
            "目标加盟商缺少唯一有效负责人，暂不能派发",
            422,
        )
    return owner


def require_company_assignment_access(principal: Principal, assignment: Assignment) -> None:
    """Allow only the company owner or the employee explicitly assigned this record."""

    if not principal.company_id or assignment.company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权访问该加盟商客资", 403)
    if principal.has_any_role("FRANCHISE_OWNER"):
        return
    if principal.has_any_role("FRANCHISE_EMPLOYEE"):
        if assignment.internal_assignee_user_id == principal.user_id:
            return
        raise AppError("COMPANY_ASSIGNMENT_NOT_ASSIGNED", "该客资未分配给当前员工", 403)
    raise AppError("COMPANY_ASSIGNMENT_SCOPE_FORBIDDEN", "当前角色不能查看加盟商内部协作明细", 403)


def require_return_request_access(
    principal: Principal,
    assignment: Assignment,
    *,
    submitted_by: str | None,
) -> None:
    """Keep an employee's own return accessible after internal reassignment."""

    if (
        principal.has_any_role("FRANCHISE_EMPLOYEE")
        and submitted_by == principal.user_id
        and principal.company_id == assignment.company_id
    ):
        return
    require_company_assignment_access(principal, assignment)


def _owner_assignment_or_raise(
    db: Session,
    *,
    assignment_id: str,
    principal: Principal,
) -> Assignment:
    if not principal.has_any_role("FRANCHISE_OWNER") or not principal.company_id:
        raise AppError("COMPANY_ASSIGNMENT_OWNER_REQUIRED", "仅加盟商负责人可分配公司内部客资", 403)
    assignment = db.scalar(select(Assignment).where(Assignment.id == assignment_id).with_for_update())
    if assignment is None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    if assignment.company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权分配其他加盟商的客资", 403)
    if assignment.status not in _ACTIVE_INTERNAL_ASSIGNMENT_STATUSES:
        raise AppError("COMPANY_ASSIGNMENT_NOT_COLLABORATIVE", "仅已领取且未结束的客资可进行内部协作", 409)
    return assignment


def _active_employee_or_raise(db: Session, *, company_id: str, user_id: str) -> User:
    employee = db.scalar(
        select(User)
        .join(User.roles)
        .where(
            User.id == user_id,
            User.company_id == company_id,
            User.status == "ACTIVE",
            Role.code == "FRANCHISE_EMPLOYEE",
        )
    )
    if employee is None:
        raise AppError("COMPANY_EMPLOYEE_NOT_AVAILABLE", "目标员工不存在、已停用或不属于该加盟商", 422)
    return employee


def resolve_direct_dispatch_recipients(
    db: Session,
    *,
    company_id: str,
    employee_user_id: str | None,
) -> DirectDispatchRecipients:
    owner = resolve_company_owner(db, company_id=company_id)
    if employee_user_id:
        employee = _active_employee_or_raise(
            db,
            company_id=company_id,
            user_id=employee_user_id,
        )
        return DirectDispatchRecipients(
            assignee=employee,
            owner=owner,
            assignee_role_code="FRANCHISE_EMPLOYEE",
        )

    active_employee_id = db.scalar(
        select(User.id)
        .join(User.roles)
        .where(
            User.company_id == company_id,
            User.status == "ACTIVE",
            Role.code == "FRANCHISE_EMPLOYEE",
        )
        .limit(1)
    )
    if active_employee_id is not None:
        raise AppError(
            "DISPATCH_EMPLOYEE_REQUIRED",
            "该加盟商有在职员工，请选择具体接收员工",
            422,
        )
    return DirectDispatchRecipients(
        assignee=owner,
        owner=owner,
        assignee_role_code="FRANCHISE_OWNER",
    )


def assign_internal_employee(
    db: Session,
    *,
    assignment_id: str,
    principal: Principal,
    employee_user_id: str | None,
    reason: str,
) -> InternalAssignmentChange:
    assignment = _owner_assignment_or_raise(db, assignment_id=assignment_id, principal=principal)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise AppError("COMPANY_ASSIGNMENT_REASON_REQUIRED", "分配或回收客资必须填写原因", 422)
    if employee_user_id is not None:
        _active_employee_or_raise(db, company_id=assignment.company_id, user_id=employee_user_id)
    previous = assignment.internal_assignee_user_id
    if previous == employee_user_id:
        return InternalAssignmentChange(assignment=assignment, previous_employee_user_id=previous, changed=False)
    assignment.internal_assignee_user_id = employee_user_id
    assignment.internal_assigned_by = principal.user_id
    assignment.internal_assigned_at = datetime.now(timezone.utc)
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            event_type=(
                "V12_INTERNAL_ASSIGNMENT_ASSIGNED"
                if employee_user_id is not None
                else "V12_INTERNAL_ASSIGNMENT_RECALLED"
            ),
            actor_user_id=principal.user_id,
            payload={
                "previous_employee_user_id": previous,
                "employee_user_id": employee_user_id,
                "reason": normalized_reason,
            },
        )
    )
    db.flush()
    return InternalAssignmentChange(assignment=assignment, previous_employee_user_id=previous, changed=True)


def has_active_internal_assignments(db: Session, *, company_id: str, user_id: str) -> bool:
    return db.scalar(
        select(Assignment.id)
        .where(
            Assignment.company_id == company_id,
            Assignment.internal_assignee_user_id == user_id,
            Assignment.status.in_(_EMPLOYEE_BLOCKING_ASSIGNMENT_STATUSES),
        )
        .limit(1)
    ) is not None
