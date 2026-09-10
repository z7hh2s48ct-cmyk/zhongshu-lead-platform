from __future__ import annotations

import secrets
import string

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from ..core.auth_models import AuthLoginState
from ..core.database import Base
from ..core.errors import AppError
from ..core.models import Notification, Role, User
from ..core.security import hash_password, validate_internal_password
from .auth_service import create_internal_user
from .rbac import assign_role


INTERNAL_ROLE_CODES = frozenset(
    {
        "SUPER_ADMIN",
        "OPERATION",
        "TELESALES",
    }
)
_SUPERADMIN_LOCK_KEY = "zhongshu.internal-user.superadmin-roster"
_NON_BUSINESS_USER_TABLES = frozenset(
    {
        "users",
        "user_roles",
        "auth_login_state",
        "wechat_identities",
        "notifications",
        "audit_logs",
    }
)


def normalize_internal_roles(role_codes: list[str]) -> list[str]:
    normalized = sorted({code.strip() for code in role_codes if code.strip()})
    if not normalized:
        raise AppError("INTERNAL_ROLE_REQUIRED", "至少选择一个内部角色", 400)
    if len(normalized) != 1:
        raise AppError("INTERNAL_ROLE_SINGLE_REQUIRED", "一个账号只能选择一个业务角色", 400)
    invalid = sorted(set(normalized) - INTERNAL_ROLE_CODES)
    if invalid:
        raise AppError(
            "INTERNAL_ROLE_INVALID",
            "包含不可分配的内部角色",
            400,
            {"invalid_role_codes": invalid},
        )
    return normalized


def validate_managed_password(password: str) -> None:
    try:
        validate_internal_password(password)
    except ValueError as exc:
        raise AppError("PASSWORD_POLICY_INVALID", str(exc), 400) from exc


def generate_initial_password(username: str) -> str:
    """Generate a one-time internal account password that satisfies policy."""

    normalized_username = username.strip()
    if not normalized_username:
        raise AppError(
            "INTERNAL_IDENTITY_INVALID",
            "登录账号不能为空或包含首尾空格",
            400,
        )
    alphabet = string.ascii_letters + string.digits
    rng = secrets.SystemRandom()
    password = "".join(rng.choice(alphabet) for _ in range(8))
    validate_internal_password(password)
    return password


def _validate_identity(username: str, display_name: str) -> tuple[str, str]:
    normalized_username = username.strip()
    normalized_display_name = display_name.strip()
    if normalized_username != username or normalized_display_name != display_name:
        raise AppError(
            "INTERNAL_IDENTITY_INVALID",
            "登录账号和显示名称首尾不能有空格",
            400,
        )
    return normalized_username, normalized_display_name


def _acquire_superadmin_roster_lock(db: Session) -> None:
    # PostgreSQL advisory locking serializes create/demote/disable decisions.
    # SQLite is used only by isolated tests and has no equivalent row-lock API.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": _SUPERADMIN_LOCK_KEY},
        )


def _is_internal_user(user: User) -> bool:
    role_codes = {role.code for role in user.roles}
    return bool(
        user.company_id is None
        and user.username
        and user.password_hash
        and user.wechat_identity is None
        and role_codes.issubset(INTERNAL_ROLE_CODES)
    )


def _load_internal_user(db: Session, user_id: str) -> User:
    user = db.scalar(
        select(User)
        .options(selectinload(User.roles), selectinload(User.wechat_identity))
        .where(User.id == user_id)
        .with_for_update(of=User)
    )
    if user is None:
        raise AppError("USER_NOT_FOUND", "账号不存在", 404)
    if not _is_internal_user(user):
        raise AppError(
            "INTERNAL_USER_REQUIRED",
            "该账号不属于平台内部账号管理范围",
            409,
        )
    return user


def _active_superadmins(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(User.roles)
            .where(
                Role.code == "SUPER_ADMIN",
                User.status == "ACTIVE",
                User.company_id.is_(None),
                User.username.is_not(None),
                User.password_hash.is_not(None),
                ~User.wechat_identity.has(),
                ~User.roles.any(Role.code.notin_(INTERNAL_ROLE_CODES)),
            )
            .order_by(User.id)
            .with_for_update(of=User)
        ).unique()
    )


def _protect_last_superadmin(db: Session, user: User) -> None:
    current_roles = {role.code for role in user.roles}
    if user.status != "ACTIVE" or "SUPER_ADMIN" not in current_roles:
        return
    active_superadmins = _active_superadmins(db)
    if len(active_superadmins) <= 1:
        raise AppError(
            "LAST_SUPER_ADMIN_REQUIRED",
            "必须保留至少一个启用中的超级管理员",
            409,
        )


def list_internal_users(db: Session) -> list[User]:
    users = db.scalars(
        select(User)
        .options(selectinload(User.roles), selectinload(User.wechat_identity))
        .where(
            User.company_id.is_(None),
            User.username.is_not(None),
            User.password_hash.is_not(None),
            ~User.wechat_identity.has(),
            ~User.roles.any(Role.code.notin_(INTERNAL_ROLE_CODES)),
        )
        .order_by(User.created_at.desc())
        .limit(500)
    ).all()
    return [user for user in users if _is_internal_user(user)]


def create_managed_internal_user(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str,
    role_codes: list[str],
    company_id: str | None,
    is_test: bool = False,
) -> User:
    if company_id is not None:
        raise AppError(
            "INTERNAL_COMPANY_FORBIDDEN",
            "内部账号不能绑定加盟商公司",
            400,
        )
    normalized_username, normalized_display_name = _validate_identity(
        username,
        display_name,
    )
    roles = normalize_internal_roles(role_codes)
    validate_managed_password(password)
    if "SUPER_ADMIN" in roles:
        _acquire_superadmin_roster_lock(db)
    user = create_internal_user(
        db,
        username=normalized_username,
        password=password,
        display_name=normalized_display_name,
        role_code=roles[0],
    )
    user.is_test = is_test
    db.flush()
    db.refresh(user, attribute_names=["roles"])
    return user


def _internal_user_business_counts(db: Session, user_id: str) -> dict[str, int]:
    """Count every non-technical row that still references the account."""

    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _NON_BUSINESS_USER_TABLES:
            continue
        user_columns = [
            column
            for column in table.columns
            if any(
                foreign_key.column.table.name == "users"
                and foreign_key.column.name == "id"
                for foreign_key in column.foreign_keys
            )
        ]
        if not user_columns:
            continue
        row_count = db.scalar(
            select(func.count()).select_from(table).where(
                or_(*(column == user_id for column in user_columns))
            )
        )
        if row_count:
            counts[table.name] = int(row_count)
    return counts


def _require_disabled_internal_user(user: User) -> None:
    if user.status != "DISABLED":
        raise AppError(
            "INTERNAL_USER_MUST_BE_DISABLED",
            "请先停用该内部账号，再执行测试数据清理",
            409,
        )


def _require_exact_username(user: User, confirm_username: str) -> None:
    if confirm_username != user.username:
        raise AppError(
            "INTERNAL_USER_CONFIRMATION_MISMATCH",
            "输入的完整登录账号不匹配",
            409,
        )


def mark_internal_user_as_test(
    db: Session,
    *,
    user_id: str,
    confirm_username: str,
) -> tuple[User, dict[str, int], bool]:
    user = _load_internal_user(db, user_id)
    _require_disabled_internal_user(user)
    _require_exact_username(user, confirm_username)
    if user.is_test:
        return user, {}, False
    counts = _internal_user_business_counts(db, user.id)
    if counts:
        raise AppError(
            "INTERNAL_USER_TEST_MARK_BLOCKED",
            "该账号已有业务数据，不能标记为测试账号",
            409,
            {"blocking_tables": sorted(counts), "counts": counts},
        )
    user.is_test = True
    db.flush()
    return user, counts, True


def delete_test_internal_user(
    db: Session,
    *,
    user_id: str,
    confirm_username: str,
) -> dict[str, object]:
    user = _load_internal_user(db, user_id)
    _require_disabled_internal_user(user)
    if not user.is_test:
        raise AppError(
            "INTERNAL_USER_DELETE_TEST_ONLY",
            "只允许删除已标记的测试账号",
            409,
        )
    _require_exact_username(user, confirm_username)
    counts = _internal_user_business_counts(db, user.id)
    if counts:
        raise AppError(
            "INTERNAL_USER_DELETE_BLOCKED",
            "该账号已有业务数据，只能保持停用，不能删除",
            409,
            {"blocking_tables": sorted(counts), "counts": counts},
        )
    snapshot: dict[str, object] = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "status": user.status,
        "is_test": user.is_test,
        "roles": sorted(role.code for role in user.roles),
    }
    db.execute(delete(AuthLoginState).where(AuthLoginState.user_id == user.id))
    db.execute(delete(Notification).where(Notification.user_id == user.id))
    db.delete(user)
    db.flush()
    return snapshot


def update_internal_roles(
    db: Session,
    *,
    user_id: str,
    role_codes: list[str],
) -> tuple[User, list[str], bool]:
    roles = normalize_internal_roles(role_codes)
    _acquire_superadmin_roster_lock(db)
    user = _load_internal_user(db, user_id)
    previous_roles = sorted(role.code for role in user.roles)
    if previous_roles == roles:
        return user, previous_roles, False
    if "SUPER_ADMIN" in previous_roles and "SUPER_ADMIN" not in roles:
        _protect_last_superadmin(db, user)

    role_rows = list(
        db.scalars(select(Role).where(Role.code.in_(roles)).order_by(Role.code)).all()
    )
    if len(role_rows) != len(roles):
        raise AppError("INTERNAL_ROLE_INVALID", "内部角色尚未初始化", 409)
    user.roles = role_rows
    user.session_version += 1
    db.flush()
    return user, previous_roles, True


def update_internal_display_name(
    db: Session,
    *,
    user_id: str,
    display_name: str,
) -> tuple[User, str, bool]:
    user = _load_internal_user(db, user_id)
    previous_display_name = user.display_name
    if previous_display_name == display_name:
        return user, previous_display_name, False
    user.display_name = display_name
    db.flush()
    return user, previous_display_name, True


def set_internal_user_status(
    db: Session,
    *,
    user_id: str,
    status: str,
) -> tuple[User, str, bool]:
    if status not in {"ACTIVE", "DISABLED"}:
        raise ValueError(f"unsupported internal user status: {status}")
    _acquire_superadmin_roster_lock(db)
    user = _load_internal_user(db, user_id)
    previous_status = user.status
    if previous_status == status:
        return user, previous_status, False
    if status == "DISABLED":
        _protect_last_superadmin(db, user)
    user.status = status
    user.session_version += 1
    db.flush()
    return user, previous_status, True


def reset_internal_password(
    db: Session,
    *,
    user_id: str,
    new_password: str,
) -> tuple[User, int]:
    user = _load_internal_user(db, user_id)
    validate_managed_password(new_password)
    previous_session_version = user.session_version
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    db.flush()
    return user, previous_session_version
