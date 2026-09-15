from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, Principal, require_permissions
from ..core.database import get_db
from ..core.responses import ok
from ..schemas.company_accounts import (
    CompanyAccountCreateBody,
    CompanyAccountPasswordBody,
    CompanyOwnerCredentialBody,
    CompanyAccountRequestCreateBody,
    CompanyAccountRequestDecisionBody,
    CompanyAccountReasonBody,
)
from ..services.audit import write_audit
from ..services.company_account_management import (
    approve_company_account_request,
    company_account_directory_to_dict,
    company_account_to_dict,
    company_account_request_to_dict,
    create_company_account_request,
    create_company_account,
    get_company_account,
    initial_password_for_company_account,
    list_company_account_requests,
    list_company_accounts,
    reject_company_account_request,
    require_company_owner,
    require_superadmin_reason,
    reset_company_account_password,
    set_company_account_status,
    provision_owner_credentials,
)


router = APIRouter(prefix="/companies/{company_id}/accounts", tags=["company-accounts"])
request_router = APIRouter(prefix="/companies/{company_id}/account-requests", tags=["company-account-requests"])
directory_router = APIRouter(prefix="/companies/{company_id}/account-directory", tags=["company-account-directory"])


def _audit_metadata(principal: Principal, reason: str | None) -> dict[str, object]:
    return {
        "operator_scope": "SUPER_ADMIN" if principal.has_any_role("SUPER_ADMIN") else "OPERATION",
        "reason_required": principal.has_any_role("SUPER_ADMIN"),
        "reason": reason,
    }


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("")
def list_company_account_endpoint(
    company_id: str,
    request: Request,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    return ok(
        request,
        [company_account_to_dict(user) for user in list_company_accounts(db, company_id)],
    )


@directory_router.get("")
def list_company_account_directory_endpoint(
    company_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    if not principal.can("company.account.manage"):
        require_company_owner(principal, company_id)
    return ok(
        request,
        [company_account_directory_to_dict(user) for user in list_company_accounts(db, company_id)],
    )


@request_router.get("")
def list_company_account_request_endpoint(
    company_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    if not principal.can("company.account.manage"):
        require_company_owner(principal, company_id)
    return ok(
        request,
        [
            company_account_request_to_dict(item)
            for item in list_company_account_requests(db, company_id=company_id)
        ],
    )


@request_router.post("")
def create_company_account_request_endpoint(
    company_id: str,
    body: CompanyAccountRequestCreateBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    require_company_owner(principal, company_id)
    account_request = create_company_account_request(
        db,
        company_id=company_id,
        requested_by=principal.user_id,
        request_type=body.request_type,
        username=body.username,
        display_name=body.display_name,
        target_user_id=body.target_user_id,
        reason=body.reason,
    )
    record = company_account_request_to_dict(account_request)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_ACCOUNT_REQUEST_CREATE",
        resource_type="company_account_request",
        resource_id=account_request.id,
        company_id=company_id,
        before=None,
        after=record,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, record, "员工账号申请已提交")


def _decide_company_account_request(
    *,
    company_id: str,
    request_id: str,
    body: CompanyAccountRequestDecisionBody,
    request: Request,
    principal: Principal,
    db: Session,
    approved: bool,
):
    reason = require_superadmin_reason(principal, body.reason)
    if approved:
        account_request, user, initial_password, previous_status = approve_company_account_request(
            db,
            company_id=company_id,
            request_id=request_id,
            decided_by=principal.user_id,
            decision_reason=reason or body.reason,
        )
        account_record = company_account_to_dict(user)
        write_audit(
            db,
            principal=principal,
            action=(
                "COMPANY_ACCOUNT_CREATE"
                if account_request.request_type == "CREATE_EMPLOYEE"
                else "COMPANY_ACCOUNT_DISABLE"
            ),
            resource_type="company_account",
            resource_id=user.id,
            company_id=company_id,
            before={"status": previous_status} if previous_status else None,
            after=account_record,
            metadata={
                **_audit_metadata(principal, reason),
                "account_request_id": account_request.id,
                "requested_by": account_request.requested_by,
            },
            request_id=request.state.request_id,
        )
        message = "员工账号申请已执行"
    else:
        account_request = reject_company_account_request(
            db,
            company_id=company_id,
            request_id=request_id,
            decided_by=principal.user_id,
            decision_reason=reason or body.reason,
        )
        account_record = None
        initial_password = None
        message = "员工账号申请已驳回"
    record = company_account_request_to_dict(account_request)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_ACCOUNT_REQUEST_APPROVE" if approved else "COMPANY_ACCOUNT_REQUEST_REJECT",
        resource_type="company_account_request",
        resource_id=account_request.id,
        company_id=company_id,
        before={"status": "PENDING"},
        after=record,
        metadata=_audit_metadata(principal, reason),
        request_id=request.state.request_id,
    )
    db.commit()
    if account_record is not None:
        record["executed_account"] = account_record
    if initial_password is not None:
        record["initial_password"] = initial_password
    return ok(request, record, message)


@request_router.post("/{request_id}/approve")
def approve_company_account_request_endpoint(
    company_id: str,
    request_id: str,
    body: CompanyAccountRequestDecisionBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    result = _decide_company_account_request(
        company_id=company_id,
        request_id=request_id,
        body=body,
        request=request,
        principal=principal,
        db=db,
        approved=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@request_router.post("/{request_id}/reject")
def reject_company_account_request_endpoint(
    company_id: str,
    request_id: str,
    body: CompanyAccountRequestDecisionBody,
    request: Request,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    return _decide_company_account_request(
        company_id=company_id,
        request_id=request_id,
        body=body,
        request=request,
        principal=principal,
        db=db,
        approved=False,
    )


@router.post("")
def create_company_account_endpoint(
    company_id: str,
    body: CompanyAccountCreateBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    reason = require_superadmin_reason(principal, body.reason)
    initial_password = initial_password_for_company_account(body.username, body.password)
    user = create_company_account(
        db,
        company_id=company_id,
        username=body.username,
        password=initial_password,
        display_name=body.display_name,
        role_code=body.role_code,
    )
    record = company_account_to_dict(user)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_ACCOUNT_CREATE",
        resource_type="company_account",
        resource_id=user.id,
        company_id=company_id,
        before=None,
        after=record,
        metadata=_audit_metadata(principal, reason),
        request_id=request.state.request_id,
    )
    db.commit()
    _no_store(response)
    if body.password is None:
        record["initial_password"] = initial_password
    return ok(request, record, "加盟商账号已开通")


def _change_status(
    *,
    company_id: str,
    user_id: str,
    status: str,
    body: CompanyAccountReasonBody,
    request: Request,
    principal: Principal,
    db: Session,
):
    reason = require_superadmin_reason(principal, body.reason)
    user, previous_status, changed = set_company_account_status(
        db,
        company_id=company_id,
        user_id=user_id,
        status=status,
    )
    record = company_account_to_dict(user)
    if changed:
        write_audit(
            db,
            principal=principal,
            action="COMPANY_ACCOUNT_ENABLE" if status == "ACTIVE" else "COMPANY_ACCOUNT_DISABLE",
            resource_type="company_account",
            resource_id=user.id,
            company_id=company_id,
            before={"status": previous_status, "session_version": user.session_version - 1},
            after=record,
            metadata=_audit_metadata(principal, reason),
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(request, record, "加盟商账号状态已更新")


@router.post("/{user_id}/enable")
def enable_company_account_endpoint(
    company_id: str,
    user_id: str,
    body: CompanyAccountReasonBody,
    request: Request,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    return _change_status(
        company_id=company_id,
        user_id=user_id,
        status="ACTIVE",
        body=body,
        request=request,
        principal=principal,
        db=db,
    )


@router.post("/{user_id}/disable")
def disable_company_account_endpoint(
    company_id: str,
    user_id: str,
    body: CompanyAccountReasonBody,
    request: Request,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    return _change_status(
        company_id=company_id,
        user_id=user_id,
        status="DISABLED",
        body=body,
        request=request,
        principal=principal,
        db=db,
    )


@router.post("/{user_id}/reset-password")
def reset_company_account_password_endpoint(
    company_id: str,
    user_id: str,
    body: CompanyAccountPasswordBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    reason = require_superadmin_reason(principal, body.reason)
    user = get_company_account(db, company_id, user_id)
    generated_password = initial_password_for_company_account(user.username or "", body.new_password)
    user, previous_session_version = reset_company_account_password(
        db,
        company_id=company_id,
        user_id=user_id,
        new_password=generated_password,
    )
    record = company_account_to_dict(user)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_ACCOUNT_PASSWORD_RESET",
        resource_type="company_account",
        resource_id=user.id,
        company_id=company_id,
        before={"session_version": previous_session_version},
        after={"session_version": user.session_version},
        metadata=_audit_metadata(principal, reason),
        request_id=request.state.request_id,
    )
    db.commit()
    _no_store(response)
    record["initial_password"] = generated_password
    return ok(request, record, "加盟商账号密码已重置")


@router.post("/{user_id}/provision-owner-credentials")
def provision_owner_credentials_endpoint(
    company_id: str,
    user_id: str,
    body: CompanyOwnerCredentialBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    reason = require_superadmin_reason(principal, body.reason)
    user, initial_password = provision_owner_credentials(
        db,
        company_id=company_id,
        user_id=user_id,
        username=body.username,
        password=body.password,
    )
    record = company_account_to_dict(user)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_OWNER_CREDENTIALS_PROVISION",
        resource_type="company_account",
        resource_id=user.id,
        company_id=company_id,
        after={"username": user.username, "session_version": user.session_version},
        metadata=_audit_metadata(principal, reason),
        request_id=request.state.request_id,
    )
    db.commit()
    _no_store(response)
    record["initial_password"] = initial_password
    return ok(request, record, "负责人登录账号已补齐")
