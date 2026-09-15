from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.auth import Principal
from ..core.errors import AppError
from ..core.models import Company, CompanyAccountRequest, Role, User
from ..core.role_contract import has_exactly_one_active_business_role
from ..core.security import decrypt_text, hash_password, normalize_phone, validate_internal_password
from .auth_service import create_internal_user
from .internal_user_management import generate_initial_password


COMPANY_ACCOUNT_ROLE_CODES = frozenset({"FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"})
COMPANY_ACCOUNT_REQUEST_TYPES = frozenset({"CREATE_EMPLOYEE", "DISABLE_EMPLOYEE"})
PENDING_COMPANY_ACCOUNT_REQUEST = "PENDING"


def require_superadmin_reason(principal: Principal, reason: str | None) -> str | None:
    normalized = reason.strip() if reason else None
    if principal.has_any_role("SUPER_ADMIN") and not normalized:
        raise AppError(
            "SUPER_ADMIN_REASON_REQUIRED",
            "超级管理员执行加盟商账号操作时必须填写原因",
            422,
        )
    return normalized


def _company_or_raise(db: Session, company_id: str, *, lock: bool = False) -> Company:
    stmt = select(Company).where(Company.id == company_id)
    if lock:
        stmt = stmt.with_for_update()
    company = db.scalar(stmt)
    if company is None:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    return company


def _account_role_codes(user: User) -> tuple[str, ...]:
    return tuple(sorted(role.code for role in user.roles))


def _is_company_account(user: User, company_id: str) -> bool:
    role_codes = _account_role_codes(user)
    return (
        user.company_id == company_id
        and has_exactly_one_active_business_role(role_codes)
        and role_codes[0] in COMPANY_ACCOUNT_ROLE_CODES
    )


def _load_company_account(db: Session, company_id: str, user_id: str, *, lock: bool = False) -> User:
    stmt = (
        select(User)
        .options(selectinload(User.roles), selectinload(User.wechat_identity))
        .where(User.id == user_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=User)
    user = db.scalar(stmt)
    if user is None or not _is_company_account(user, company_id):
        raise AppError("COMPANY_ACCOUNT_NOT_FOUND", "加盟商账号不存在", 404)
    return user


def _has_active_owner(db: Session, company_id: str) -> bool:
    return bool(
        db.scalar(
            select(User.id)
            .join(User.roles)
            .where(
                User.company_id == company_id,
                User.status == "ACTIVE",
                Role.code == "FRANCHISE_OWNER",
            )
            .limit(1)
        )
    )


def company_account_to_dict(user: User) -> dict[str, object]:
    role_codes = _account_role_codes(user)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "company_id": user.company_id,
        "role_code": role_codes[0] if len(role_codes) == 1 else None,
        "status": user.status,
        "wechat_bound": user.wechat_identity is not None,
        "credentials_ready": bool(user.username and user.password_hash),
        "session_version": user.session_version,
        "created_at": user.created_at.isoformat(),
    }


def company_account_directory_to_dict(user: User) -> dict[str, object]:
    """Return only the personnel fields a franchise owner needs to see."""

    record = company_account_to_dict(user)
    return {
        key: record[key]
        for key in (
            "id",
            "username",
            "display_name",
            "company_id",
            "role_code",
            "status",
            "created_at",
        )
    }


def company_account_request_to_dict(request: CompanyAccountRequest) -> dict[str, object]:
    return {
        "id": request.id,
        "company_id": request.company_id,
        "request_type": request.request_type,
        "status": request.status,
        "requested_by": request.requested_by,
        "target_user_id": request.target_user_id,
        "requested_username": request.requested_username,
        "requested_display_name": request.requested_display_name,
        "reason": request.reason,
        "decision_reason": request.decision_reason,
        "decided_by": request.decided_by,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "executed_user_id": request.executed_user_id,
        "created_at": request.created_at.isoformat(),
        "updated_at": request.updated_at.isoformat(),
    }


def list_company_accounts(db: Session, company_id: str) -> list[User]:
    _company_or_raise(db, company_id)
    users = db.scalars(
        select(User)
        .options(selectinload(User.roles), selectinload(User.wechat_identity))
        .join(User.roles)
        .where(
            User.company_id == company_id,
            Role.code.in_(COMPANY_ACCOUNT_ROLE_CODES),
        )
        .order_by(User.created_at.asc())
    ).unique().all()
    return [user for user in users if _is_company_account(user, company_id)]


def require_company_owner(principal: Principal, company_id: str) -> None:
    if not principal.has_any_role("FRANCHISE_OWNER") or principal.company_id != company_id:
        raise AppError("FORBIDDEN", "仅加盟商负责人可操作本公司人员申请", 403)


def list_company_account_requests(
    db: Session,
    *,
    company_id: str,
) -> list[CompanyAccountRequest]:
    _company_or_raise(db, company_id)
    return list(
        db.scalars(
            select(CompanyAccountRequest)
            .where(CompanyAccountRequest.company_id == company_id)
            .order_by(CompanyAccountRequest.created_at.asc())
        )
    )


def _load_company_account_request(
    db: Session,
    *,
    company_id: str,
    request_id: str,
    lock: bool = False,
) -> CompanyAccountRequest:
    stmt = select(CompanyAccountRequest).where(
        CompanyAccountRequest.id == request_id,
        CompanyAccountRequest.company_id == company_id,
    )
    if lock:
        stmt = stmt.with_for_update(of=CompanyAccountRequest)
    request = db.scalar(stmt)
    if request is None:
        raise AppError("COMPANY_ACCOUNT_REQUEST_NOT_FOUND", "人员申请不存在", 404)
    return request


def _require_pending_request(request: CompanyAccountRequest) -> None:
    if request.status != PENDING_COMPANY_ACCOUNT_REQUEST:
        raise AppError("COMPANY_ACCOUNT_REQUEST_DECIDED", "该人员申请已经处理", 409)


def _require_unique_pending_request(
    db: Session,
    *,
    company_id: str,
    request_type: str,
    requested_username: str | None,
    target_user_id: str | None,
) -> None:
    stmt = select(CompanyAccountRequest.id).where(
        CompanyAccountRequest.company_id == company_id,
        CompanyAccountRequest.request_type == request_type,
        CompanyAccountRequest.status == PENDING_COMPANY_ACCOUNT_REQUEST,
    )
    if requested_username is not None:
        stmt = stmt.where(CompanyAccountRequest.requested_username == requested_username)
    if target_user_id is not None:
        stmt = stmt.where(CompanyAccountRequest.target_user_id == target_user_id)
    if db.scalar(stmt.limit(1)):
        raise AppError("COMPANY_ACCOUNT_REQUEST_PENDING", "同一人员申请正在等待运营处理", 409)


def create_company_account_request(
    db: Session,
    *,
    company_id: str,
    requested_by: str,
    request_type: str,
    username: str | None,
    display_name: str | None,
    target_user_id: str | None,
    reason: str,
) -> CompanyAccountRequest:
    if request_type not in COMPANY_ACCOUNT_REQUEST_TYPES:
        raise AppError("COMPANY_ACCOUNT_REQUEST_INVALID", "不支持的人员申请类型", 422)
    _company_or_raise(db, company_id, lock=True)
    if request_type == "CREATE_EMPLOYEE":
        if not username or not display_name:
            raise AppError("COMPANY_ACCOUNT_REQUEST_INVALID", "新增员工申请信息不完整", 422)
        if db.scalar(select(User.id).where(User.username == username).limit(1)):
            raise AppError("USERNAME_EXISTS", "登录账号已存在", 409)
        _require_unique_pending_request(
            db,
            company_id=company_id,
            request_type=request_type,
            requested_username=username,
            target_user_id=None,
        )
    else:
        if not target_user_id:
            raise AppError("COMPANY_ACCOUNT_REQUEST_INVALID", "停用员工申请必须指定员工", 422)
        user = _load_company_account(db, company_id, target_user_id)
        if _account_role_codes(user) != ("FRANCHISE_EMPLOYEE",):
            raise AppError("COMPANY_ACCOUNT_REQUEST_TARGET_INVALID", "只能申请停用加盟商员工", 422)
        if user.status != "ACTIVE":
            raise AppError("COMPANY_ACCOUNT_REQUEST_TARGET_DISABLED", "该员工账号已经停用", 409)
        _require_unique_pending_request(
            db,
            company_id=company_id,
            request_type=request_type,
            requested_username=None,
            target_user_id=target_user_id,
        )
    request = CompanyAccountRequest(
        company_id=company_id,
        request_type=request_type,
        requested_by=requested_by,
        target_user_id=target_user_id,
        requested_username=username,
        requested_display_name=display_name,
        reason=reason,
    )
    db.add(request)
    db.flush()
    return request


def approve_company_account_request(
    db: Session,
    *,
    company_id: str,
    request_id: str,
    decided_by: str,
    decision_reason: str,
) -> tuple[CompanyAccountRequest, User, str | None, str | None]:
    request = _load_company_account_request(
        db,
        company_id=company_id,
        request_id=request_id,
        lock=True,
    )
    _require_pending_request(request)
    initial_password: str | None = None
    previous_status: str | None = None
    if request.request_type == "CREATE_EMPLOYEE":
        if not request.requested_username or not request.requested_display_name:
            raise AppError("COMPANY_ACCOUNT_REQUEST_INVALID", "新增员工申请信息不完整", 409)
        initial_password = initial_password_for_company_account(request.requested_username, None)
        user = create_company_account(
            db,
            company_id=company_id,
            username=request.requested_username,
            password=initial_password,
            display_name=request.requested_display_name,
            role_code="FRANCHISE_EMPLOYEE",
        )
    elif request.request_type == "DISABLE_EMPLOYEE":
        if not request.target_user_id:
            raise AppError("COMPANY_ACCOUNT_REQUEST_INVALID", "停用员工申请信息不完整", 409)
        user, previous_status, changed = set_company_account_status(
            db,
            company_id=company_id,
            user_id=request.target_user_id,
            status="DISABLED",
        )
        if not changed:
            raise AppError(
                "COMPANY_ACCOUNT_REQUEST_TARGET_DISABLED",
                "该员工账号已经停用，请驳回或关闭这条申请",
                409,
            )
    else:
        raise AppError("COMPANY_ACCOUNT_REQUEST_INVALID", "不支持的人员申请类型", 409)
    request.status = "APPROVED"
    request.decision_reason = decision_reason
    request.decided_by = decided_by
    request.decided_at = datetime.now(timezone.utc)
    request.executed_user_id = user.id
    db.flush()
    return request, user, initial_password, previous_status


def reject_company_account_request(
    db: Session,
    *,
    company_id: str,
    request_id: str,
    decided_by: str,
    decision_reason: str,
) -> CompanyAccountRequest:
    request = _load_company_account_request(
        db,
        company_id=company_id,
        request_id=request_id,
        lock=True,
    )
    _require_pending_request(request)
    request.status = "REJECTED"
    request.decision_reason = decision_reason
    request.decided_by = decided_by
    request.decided_at = datetime.now(timezone.utc)
    db.flush()
    return request


def get_company_account(db: Session, company_id: str, user_id: str) -> User:
    """Load one company-scoped account for read-before-write response shaping."""

    return _load_company_account(db, company_id, user_id)


def create_company_account(
    db: Session,
    *,
    company_id: str,
    username: str,
    password: str,
    display_name: str,
    role_code: str,
) -> User:
    if role_code not in COMPANY_ACCOUNT_ROLE_CODES:
        raise AppError("COMPANY_ACCOUNT_ROLE_INVALID", "仅可创建加盟商负责人或员工账号", 422)
    company = _company_or_raise(db, company_id, lock=True)
    if company.status != "ACTIVE":
        raise AppError("COMPANY_DISABLED", "已停用的加盟商公司不能开通账号", 409)
    if role_code == "FRANCHISE_OWNER" and _has_active_owner(db, company_id):
        raise AppError("COMPANY_OWNER_EXISTS", "该加盟商已有启用中的负责人账号", 409)
    if role_code == "FRANCHISE_EMPLOYEE" and not _has_active_owner(db, company_id):
        raise AppError("COMPANY_OWNER_REQUIRED", "请先开通加盟商负责人账号", 409)
    if username != username.strip() or display_name != display_name.strip():
        raise AppError("COMPANY_ACCOUNT_IDENTITY_INVALID", "登录账号和显示名称首尾不能有空格", 422)
    try:
        validate_internal_password(password)
    except ValueError as exc:
        raise AppError("PASSWORD_POLICY_INVALID", str(exc), 400) from exc
    user = create_internal_user(
        db,
        username=username,
        password=password,
        display_name=display_name,
        role_code=role_code,
        company_id=company_id,
    )
    if role_code == "FRANCHISE_OWNER":
        company.primary_user_id = user.id
    db.flush()
    db.refresh(user, attribute_names=["roles", "wechat_identity"])
    return user


def set_company_account_status(
    db: Session,
    *,
    company_id: str,
    user_id: str,
    status: str,
) -> tuple[User, str, bool]:
    if status not in {"ACTIVE", "DISABLED"}:
        raise ValueError(f"unsupported company account status: {status}")
    company = _company_or_raise(db, company_id, lock=True)
    user = _load_company_account(db, company_id, user_id, lock=True)
    previous_status = user.status
    if previous_status == status:
        return user, previous_status, False
    if status == "DISABLED" and company.primary_user_id == user.id:
        raise AppError(
            "COMPANY_PRIMARY_ACCOUNT_PROTECTED",
            "请先完成负责人交接后再停用当前负责人账号",
            409,
        )
    if status == "DISABLED" and _account_role_codes(user) == ("FRANCHISE_EMPLOYEE",):
        from .company_assignment_v12 import has_active_internal_assignments

        if has_active_internal_assignments(db, company_id=company_id, user_id=user.id):
            raise AppError(
                "COMPANY_ACCOUNT_HANDOVER_REQUIRED",
                "该员工仍有进行中的内部客资，请先由负责人回收或转交",
                409,
            )
    user.status = status
    user.session_version += 1
    db.flush()
    return user, previous_status, True


def reset_company_account_password(
    db: Session,
    *,
    company_id: str,
    user_id: str,
    new_password: str,
) -> tuple[User, int]:
    user = _load_company_account(db, company_id, user_id, lock=True)
    if not user.username:
        raise AppError("COMPANY_ACCOUNT_PASSWORD_UNAVAILABLE", "微信绑定账号未设置登录账号，不能重置密码", 409)
    try:
        validate_internal_password(new_password)
    except ValueError as exc:
        raise AppError("PASSWORD_POLICY_INVALID", str(exc), 400) from exc
    previous_session_version = user.session_version
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    db.flush()
    return user, previous_session_version


def provision_owner_credentials(
    db: Session,
    *,
    company_id: str,
    user_id: str,
    username: str | None,
    password: str | None,
) -> tuple[User, str]:
    """Give a historical WeChat-only primary owner a deliverable login."""

    company = _company_or_raise(db, company_id, lock=True)
    user = _load_company_account(db, company_id, user_id, lock=True)
    if company.primary_user_id != user.id or _account_role_codes(user) != ("FRANCHISE_OWNER",):
        raise AppError("COMPANY_PRIMARY_OWNER_REQUIRED", "只能补齐当前加盟商负责人的登录账号", 409)
    if user.username and user.password_hash:
        raise AppError("COMPANY_OWNER_CREDENTIALS_EXIST", "负责人已经具备登录账号和密码", 409)

    resolved_username = (username or user.username or "").strip()
    if not resolved_username:
        resolved_username = normalize_phone(decrypt_text(company.contact_phone_encrypted) or "")
    if len(resolved_username) < 2:
        raise AppError("COMPANY_OWNER_USERNAME_REQUIRED", "无法从负责人手机号生成账号，请填写登录账号", 422)
    duplicate = db.scalar(
        select(User.id).where(User.username == resolved_username, User.id != user.id).limit(1)
    )
    if duplicate:
        raise AppError("USERNAME_EXISTS", "登录账号已存在", 409)

    initial_password = password or generate_initial_password(resolved_username)
    try:
        validate_internal_password(initial_password)
    except ValueError as exc:
        raise AppError("PASSWORD_POLICY_INVALID", str(exc), 400) from exc
    user.username = resolved_username
    user.password_hash = hash_password(initial_password)
    user.session_version += 1
    db.flush()
    return user, initial_password


def initial_password_for_company_account(username: str, password: str | None) -> str:
    return password or generate_initial_password(username)
