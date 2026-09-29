from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from ..core.auth_models import AuthLoginState
from ..core.config import get_settings
from ..core.errors import AppError
from ..core.models import Company, InviteToken, User, WechatIdentity
from ..core.role_contract import has_exactly_one_active_business_role
from ..core.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from ..core.time import as_utc, utcnow
from .audit import write_audit
from .rbac import assign_role

settings = get_settings()
_DUMMY_PASSWORD_HASH = hash_password("internal-login-timing-padding-value")


@dataclass(frozen=True)
class InternalAuthResult:
    user: User
    token: str
    lock_released: bool = False


class InternalAuthError(AppError):
    """Authentication error carrying server-only audit context."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        audit_action: str,
        user_id: str | None = None,
        failure_count: int | None = None,
        locked_until: datetime | None = None,
        lock_released: bool = False,
    ) -> None:
        super().__init__(code, message, status_code)
        self.audit_action = audit_action
        self.user_id = user_id
        self.failure_count = failure_count
        self.locked_until = locked_until
        self.lock_released = lock_released


def role_codes_for_user(user: User) -> list[str]:
    role_codes = sorted(role.code for role in user.roles)
    if not has_exactly_one_active_business_role(role_codes):
        raise AppError(
            "AUTH_ROLE_MIGRATION_REQUIRED",
            "账号角色尚未完成迁移，请联系平台管理员处理",
            403,
        )
    return role_codes


def _new_login_state(db: Session, user_id: str) -> AuthLoginState:
    state = AuthLoginState(user_id=user_id, failed_count=0)
    db.add(state)
    db.flush()
    return state


def _clear_login_state(state: AuthLoginState) -> None:
    state.failed_count = 0
    state.last_failed_at = None
    state.locked_until = None


def _generic_login_error(
    *,
    audit_action: str,
    user_id: str | None = None,
    failure_count: int | None = None,
    locked_until: datetime | None = None,
    lock_released: bool = False,
) -> InternalAuthError:
    return InternalAuthError(
        "AUTH_LOGIN_FAILED",
        "用户名或密码错误",
        401,
        audit_action=audit_action,
        user_id=user_id,
        failure_count=failure_count,
        locked_until=locked_until,
        lock_released=lock_released,
    )


def _record_failed_attempt_sqlite(
    db: Session,
    *,
    user_id: str,
    now: datetime,
) -> tuple[int, datetime | None]:
    table = AuthLoginState.__table__
    window_start = now - timedelta(minutes=settings.login_failure_window_minutes)
    new_lock_until = now + timedelta(minutes=settings.login_lock_minutes)
    active_lock = and_(table.c.locked_until.is_not(None), table.c.locked_until > now)
    reset_window = or_(
        table.c.last_failed_at.is_(None),
        table.c.last_failed_at < window_start,
        and_(table.c.locked_until.is_not(None), table.c.locked_until <= now),
    )
    next_count = case(
        (active_lock, table.c.failed_count),
        (reset_window, 1),
        else_=table.c.failed_count + 1,
    )
    next_lock_until = case(
        (active_lock, table.c.locked_until),
        (next_count >= settings.login_max_failed_attempts, new_lock_until),
        else_=None,
    )
    initial_lock_until = new_lock_until if settings.login_max_failed_attempts <= 1 else None
    stmt = (
        sqlite_insert(AuthLoginState)
        .values(
            user_id=user_id,
            failed_count=1,
            last_failed_at=now,
            locked_until=initial_lock_until,
        )
        .on_conflict_do_update(
            index_elements=[AuthLoginState.user_id],
            set_={
                "failed_count": next_count,
                "last_failed_at": now,
                "locked_until": next_lock_until,
            },
        )
        .returning(AuthLoginState.failed_count, AuthLoginState.locked_until)
    )
    row = db.execute(stmt.execution_options(synchronize_session=False)).one()
    db.expire_all()
    return int(row.failed_count), as_utc(row.locked_until)


def authenticate_internal(db: Session, username: str, password: str) -> InternalAuthResult:
    now = utcnow()
    normalized_username = username.strip()
    user = db.scalar(select(User).where(User.username == normalized_username).with_for_update())
    if user is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise _generic_login_error(audit_action="AUTH_LOGIN_FAILED")

    state = db.get(AuthLoginState, user.id)
    locked_until = as_utc(state.locked_until) if state else None
    lock_released = bool(locked_until and locked_until <= now)
    if locked_until and locked_until > now:
        raise _generic_login_error(
            audit_action="AUTH_LOGIN_BLOCKED",
            user_id=user.id,
            failure_count=state.failed_count if state else 0,
            locked_until=locked_until,
        )

    last_failed_at = as_utc(state.last_failed_at) if state else None
    failure_window = timedelta(minutes=settings.login_failure_window_minutes)
    reset_state = bool(
        state
        and (
            lock_released
            or (last_failed_at and now - last_failed_at > failure_window)
        )
    )

    if not user.password_hash or not verify_password(password, user.password_hash):
        if db.get_bind().dialect.name == "sqlite":
            failure_count, next_locked_until = _record_failed_attempt_sqlite(
                db,
                user_id=user.id,
                now=now,
            )
        else:
            if state is None:
                state = _new_login_state(db, user.id)
            elif reset_state:
                _clear_login_state(state)
            state.failed_count += 1
            state.last_failed_at = now
            if state.failed_count >= settings.login_max_failed_attempts:
                state.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            failure_count = state.failed_count
            next_locked_until = as_utc(state.locked_until)

        if next_locked_until and next_locked_until > now and failure_count >= settings.login_max_failed_attempts:
            raise _generic_login_error(
                audit_action="AUTH_LOGIN_LOCKED",
                user_id=user.id,
                failure_count=failure_count,
                locked_until=next_locked_until,
                lock_released=lock_released,
            )
        raise _generic_login_error(
            audit_action="AUTH_LOGIN_FAILED",
            user_id=user.id,
            failure_count=failure_count,
            lock_released=lock_released,
        )

    if user.status != "ACTIVE":
        if state is not None and reset_state:
            _clear_login_state(state)
        raise _generic_login_error(
            audit_action="AUTH_LOGIN_BLOCKED",
            user_id=user.id,
            failure_count=state.failed_count if state else 0,
            lock_released=lock_released,
        )

    if user.company_id:
        company = db.get(Company, user.company_id)
        if company is None or company.status != "ACTIVE":
            if state is not None:
                _clear_login_state(state)
            raise InternalAuthError(
                "AUTH_COMPANY_DISABLED",
                "加盟商已停用，暂时无法登录",
                403,
                audit_action="AUTH_LOGIN_BLOCKED",
                user_id=user.id,
                failure_count=state.failed_count if state else 0,
                lock_released=lock_released,
            )

    if state is not None:
        _clear_login_state(state)
    user.last_login_at = datetime.now(timezone.utc)
    try:
        roles = role_codes_for_user(user)
    except AppError as exc:
        raise InternalAuthError(
            exc.code,
            exc.message,
            exc.status_code,
            audit_action="AUTH_LOGIN_BLOCKED",
            user_id=user.id,
            failure_count=state.failed_count if state else 0,
            lock_released=lock_released,
        ) from exc
    token = create_access_token(user.id, user.session_version, roles, user.company_id)
    return InternalAuthResult(user=user, token=token, lock_released=lock_released)


def verify_internal_password_for_sensitive_action(db: Session, user: User, password: str) -> None:
    """Use the same account-level failure state for password-gated profile changes."""

    now = utcnow()
    state = db.get(AuthLoginState, user.id)
    locked_until = as_utc(state.locked_until) if state else None
    if locked_until and locked_until > now:
        raise AppError(
            "AUTH_SENSITIVE_ACTION_LOCKED",
            "当前密码连续输错次数过多，请稍后再试",
            429,
            {"failure_count": state.failed_count if state else 0, "locked_until": locked_until.isoformat()},
        )

    last_failed_at = as_utc(state.last_failed_at) if state else None
    failure_window = timedelta(minutes=settings.login_failure_window_minutes)
    reset_state = bool(
        state
        and (
            (locked_until and locked_until <= now)
            or (last_failed_at and now - last_failed_at > failure_window)
        )
    )

    if not user.password_hash or not verify_password(password, user.password_hash):
        if db.get_bind().dialect.name == "sqlite":
            failure_count, next_locked_until = _record_failed_attempt_sqlite(db, user_id=user.id, now=now)
        else:
            if state is None:
                state = _new_login_state(db, user.id)
            elif reset_state:
                _clear_login_state(state)
            state.failed_count += 1
            state.last_failed_at = now
            if state.failed_count >= settings.login_max_failed_attempts:
                state.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            failure_count = state.failed_count
            next_locked_until = as_utc(state.locked_until)

        if next_locked_until and next_locked_until > now and failure_count >= settings.login_max_failed_attempts:
            raise AppError(
                "AUTH_SENSITIVE_ACTION_LOCKED",
                "当前密码连续输错次数过多，请稍后再试",
                429,
                {"failure_count": failure_count, "locked_until": next_locked_until.isoformat()},
            )
        raise AppError(
            "AUTH_CURRENT_PASSWORD_INVALID",
            "当前密码不正确",
            422,
            {"failure_count": failure_count},
        )

    if state is not None:
        _clear_login_state(state)


def create_company_invite(
    db: Session,
    company_id: str,
    created_by: str | None,
    expires_hours: int,
) -> tuple[InviteToken, str, list[str]]:
    """Create the single valid invite for a company (P0-06).

    The SELECT ... FOR UPDATE serializes concurrent creations on PostgreSQL
    (no-op on SQLite), so revoking previous invites and inserting the new one
    happen under the company row lock. Returns the invite, its raw token, and
    the ids of invites revoked in the same transaction.
    """

    company = db.execute(
        select(Company).where(Company.id == company_id).with_for_update()
    ).scalar_one_or_none()
    if company is None:
        raise AppError("COMPANY_NOT_AVAILABLE", "加盟商公司不存在或已停用", 404)
    if company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    if company.primary_user_id:
        raise AppError("AUTH_COMPANY_ALREADY_BOUND", "该公司已绑定微信主账号，无需重复邀请", 409)
    now = utcnow()
    superseded_ids = [
        row.id
        for row in db.execute(
            update(InviteToken)
            .where(
                InviteToken.company_id == company_id,
                InviteToken.revoked_at.is_(None),
                InviteToken.used_at.is_(None),
                InviteToken.expires_at > now,
            )
            .values(revoked_at=now)
            .returning(InviteToken.id)
        ).all()
    ]
    # 带显式 RETURNING 的 UPDATE 不会回填 session 内对象，
    # 统一过期后重读，与 _consume_invite 的同步策略一致。
    if superseded_ids:
        db.expire_all()
    raw = generate_token(32)
    invite = InviteToken(
        token_hash=hash_token(raw),
        company_id=company_id,
        created_by=created_by,
        expires_at=now + timedelta(hours=expires_hours),
        # P2-01：快照发出时的邀请对象与公司名，运营侧追溯不随改名失真。
        invitee_name_snapshot=company.owner_name,
        company_name_snapshot=company.name,
    )
    db.add(invite)
    db.flush()
    return invite, raw, superseded_ids


def create_owner_wechat_rebind_invite(
    db: Session,
    company_id: str,
    created_by: str | None,
    expires_hours: int,
) -> tuple[InviteToken, str, list[str]]:
    company = db.scalar(select(Company).where(Company.id == company_id).with_for_update())
    if company is None:
        raise AppError("COMPANY_NOT_AVAILABLE", "加盟商公司不存在", 404)
    if company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    owner = db.get(User, company.primary_user_id) if company.primary_user_id else None
    if owner is None or owner.status != "ACTIVE" or role_codes_for_user(owner) != ["FRANCHISE_OWNER"]:
        raise AppError("AUTH_OWNER_NOT_BOUND", "负责人账号未完成绑定", 409)
    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == owner.id))
    if identity is None:
        raise AppError("AUTH_OWNER_NOT_BOUND", "负责人微信尚未绑定", 409)
    now = utcnow()
    superseded_ids = [row.id for row in db.execute(
        update(InviteToken).where(
            InviteToken.company_id == company_id,
            InviteToken.revoked_at.is_(None),
            InviteToken.used_at.is_(None),
            InviteToken.expires_at > now,
        ).values(revoked_at=now).returning(InviteToken.id)
    ).all()]
    if superseded_ids:
        db.expire_all()
    raw = generate_token(32)
    invite = InviteToken(
        token_hash=hash_token(raw), company_id=company_id,
        purpose="OWNER_WECHAT_REBIND", target_user_id=owner.id,
        created_by=created_by, expires_at=now + timedelta(hours=expires_hours),
        invitee_name_snapshot=company.owner_name,
        company_name_snapshot=company.name,
    )
    db.add(invite)
    db.flush()
    return invite, raw, superseded_ids


def build_invite_copy_text(
    owner_name: str | None,
    company_name: str | None,
    url: str,
    expires_at: str,
) -> str:
    """Assemble the admin-copyable invite message (pure function, P0-01)."""

    greeting = f"{owner_name}，您好" if owner_name else "您好"
    company_label = company_name or "加盟商"
    return f"{greeting}：这是【{company_label}】的微信绑定邀请，请在微信内打开：{url}，有效期至：{expires_at}"


def revoke_company_invite(db: Session, *, invite_id: str, principal, request_id: str | None) -> None:
    """N4：撤销邀请的唯一业务实现（router 与 PG 并发 e2e 共用，不再镜像）。

    W1/I8：行锁读取——无锁 check-then-act 在并发撤销/绑定竞争下会把
    revoked_at 盖写到 USED 邀请上；只锁邀请行不触碰 company，与 I5 的
    「公司→邀请」锁序一致（SQLite 上 no-op，语义由 PG 并发测试守护）。
    I8：撤销前校验生命周期——已撤销/已使用/已过期的邀请不得重复撤销，
    也不得把 revoked_at 盖写到 used 邀请上，运营端得到明确错误码。
    """

    invite = db.scalar(select(InviteToken).where(InviteToken.id == invite_id).with_for_update())
    if invite is None:
        # M4：撤销不存在的邀请必须明确失败，运营端撤销按钮依赖该语义。
        raise AppError("INVITE_NOT_FOUND", "邀请不存在或已被删除", 404)
    now = datetime.now(timezone.utc)
    if invite.revoked_at is not None:
        raise AppError("INVITE_ALREADY_REVOKED", "邀请已撤销，无需重复操作", 409)
    if invite.used_at is not None:
        raise AppError("INVITE_ALREADY_USED", "邀请已被使用，不可撤销", 409)
    if as_utc(invite.expires_at) is None or as_utc(invite.expires_at) <= now:
        raise AppError("INVITE_ALREADY_EXPIRED", "邀请已过期，不可撤销", 409)
    invite.revoked_at = now
    write_audit(
        db,
        principal=principal,
        action="INVITE_REVOKE",
        resource_type="invite",
        resource_id=invite.id,
        company_id=invite.company_id,
        request_id=request_id,
    )
    db.commit()


def list_company_invites(db: Session, company_id: str) -> list[dict[str, Any]]:
    """P1-01/P1-02/N9: read-only invite records for the admin console.

    Returns lifecycle records without token material. Usage attribution comes
    from used_by_user_id captured in the binding transaction itself — it never
    drifts with later primary-user changes; unverifiable history stays None
    so the UI shows 「未记录」.
    """

    company = db.get(Company, company_id)
    if company is None:
        raise AppError("COMPANY_NOT_AVAILABLE", "加盟商公司不存在或已停用", 404)
    now = utcnow()
    creator = aliased(User)
    consumer = aliased(User)
    rows = db.execute(
        select(InviteToken, creator.display_name, consumer.display_name)
        .outerjoin(creator, creator.id == InviteToken.created_by)
        .outerjoin(consumer, consumer.id == InviteToken.used_by_user_id)
        .where(InviteToken.company_id == company_id)
        .order_by(InviteToken.created_at.desc())
        .limit(50)
    ).all()
    items: list[dict[str, Any]] = []
    for invite, creator_name, used_by_name in rows:
        if invite.used_at is not None:
            status = "USED"
        elif invite.revoked_at is not None:
            status = "REVOKED"
        elif as_utc(invite.expires_at) <= now:
            status = "EXPIRED"
        else:
            status = "ACTIVE"
        items.append(
            {
                "id": invite.id,
                "status": status,
                "created_at": as_utc(invite.created_at),
                "created_by_name": creator_name,
                "expires_at": as_utc(invite.expires_at),
                "used_at": as_utc(invite.used_at),
                "revoked_at": as_utc(invite.revoked_at),
                "used_by_name": used_by_name,
                # P2-01：发出时的对象快照原样返回；迁移前的存量行无快照即为
                # None，前端显示「未记录」——与 P1-02 一样不用当前值冒充快照。
                "invitee_name": invite.invitee_name_snapshot,
                "company_name": invite.company_name_snapshot,
            }
        )
    return items


def _invite_matchers(raw_token: str | None, invite_id: str | None) -> list:
    """I13/N13：双参 AND 语义的单一来源——raw_token 与 invite_id 同给时
    必须指向同一条邀请（条件并列查询）；validate 与 consume 共用，
    不得各自演化（I13 曾因双实现分裂而 raw 静默优先）。"""

    matchers = []
    if raw_token is not None:
        matchers.append(InviteToken.token_hash == hash_token(raw_token))
    if invite_id is not None:
        matchers.append(InviteToken.id == invite_id)
    return matchers


def validate_invite(
    db: Session,
    *,
    raw_token: str | None = None,
    invite_id: str | None = None,
) -> InviteToken:
    """Read-only invite validation without consuming it.

    I13/N13：双参解析走 _invite_matchers 的 AND 语义，与 _consume_invite
    完全同构——显式给出的参数解析不到同一条邀请即拒绝，无兜底。"""

    matchers = _invite_matchers(raw_token, invite_id)
    invite = db.scalar(select(InviteToken).where(*matchers)) if matchers else None
    now = utcnow()
    invite_expires_at = as_utc(invite.expires_at) if invite else None
    if not invite or invite.revoked_at or invite.used_at or not invite_expires_at or invite_expires_at <= now:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    company = db.get(Company, invite.company_id)
    if not company or company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    return invite


def _consume_invite(
    db: Session,
    *,
    raw_token: str | None = None,
    invite_id: str | None = None,
    expected_company_id: str | None = None,
    used_by_user_id: str | None = None,
) -> InviteToken:
    """Atomically consume a one-time invite.

    The conditional UPDATE is the concurrency boundary for both PostgreSQL and
    SQLite. Exactly one transaction can transition used_at from NULL.
    N9：used_by_user_id 同语句写回——归因与消费原子落库，不依赖事后补写。
    """

    matchers = _invite_matchers(raw_token, invite_id)
    if not matchers:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    now = utcnow()
    filters = [
        *matchers,
        InviteToken.revoked_at.is_(None),
        InviteToken.used_at.is_(None),
        InviteToken.expires_at > now,
    ]
    if expected_company_id is not None:
        filters.append(InviteToken.company_id == expected_company_id)
    row = db.execute(
        update(InviteToken)
        .where(*filters)
        .values(used_at=now, used_by_user_id=used_by_user_id)
        .returning(InviteToken.id)
        .execution_options(synchronize_session=False)
    ).first()
    if row is None:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    db.expire_all()
    invite = db.get(InviteToken, row.id)
    if invite is None:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    company = db.get(Company, invite.company_id)
    if not company or company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    return invite


def _login_bound_identity(
    db: Session,
    identity: WechatIdentity,
    *,
    unionid: str | None = None,
    nickname: str | None = None,
    invite_token: str | None = None,
    invite_id: str | None = None,
    expected_company_id: str | None = None,
    consume_invite: bool = True,
) -> tuple[User, str]:
    """已绑定身份的登录路径（login_or_bind_wechat 的 identity 命中分支）。

    consume_invite=False 供 I14 并发冲突后的幂等重放使用——并发方已消费
    邀请并完成绑定，本请求只需登录，不再重复消费。
    """
    user = db.get(User, identity.user_id)
    if not user or user.status != "ACTIVE":
        raise AppError("AUTH_ACCOUNT_DISABLED", "账号已停用", 403)
    company = db.get(Company, user.company_id) if user.company_id else None
    if not company or company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    role_codes = role_codes_for_user(user)
    if role_codes != ["FRANCHISE_OWNER"] or company.primary_user_id != user.id:
        raise AppError(
            "AUTH_WECHAT_IDENTITY_CONFLICT",
            "该微信身份与加盟商负责人记录不一致，请联系平台处理",
            409,
        )
    if invite_token or invite_id:
        invite = validate_invite(db, raw_token=invite_token, invite_id=invite_id)
        if expected_company_id and invite.company_id != expected_company_id:
            raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
        if invite.company_id != company.id:
            raise AppError("AUTH_WECHAT_BOUND_OTHER_COMPANY", "该微信已绑定其他加盟商公司", 409)
        if consume_invite:
            _consume_invite(
                db,
                raw_token=invite_token,
                invite_id=invite_id,
                expected_company_id=company.id,
                used_by_user_id=identity.user_id,
            )
    identity.unionid = unionid or identity.unionid
    identity.nickname = nickname or identity.nickname
    user.last_login_at = utcnow()
    token = create_access_token(user.id, user.session_version, role_codes, company.id)
    return user, token


def _is_openid_unique_conflict(exc: IntegrityError) -> bool:
    """I14 判别：仅 wechat_identities.openid 唯一约束值得幂等重放。

    PG 报 diag.constraint_name=wechat_identities_openid_key，SQLite 报
    UNIQUE constraint failed: wechat_identities.openid——两方言的标识都含
    openid；其他约束冲突（user_roles、其他唯一键）不在重放语义内。
    """
    orig = getattr(exc, "orig", None)
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", "") or ""
    haystack = f"{constraint} {orig} {exc}".lower()
    return "openid" in haystack


def _replay_after_conflict(
    db: Session,
    *,
    openid: str,
    unionid: str | None,
    nickname: str | None,
    expected_company_id: str | None,
) -> tuple[User, str]:
    """I14：WechatIdentity.openid 唯一约束冲突后的幂等重放。

    并发同 openid 回调在无锁查询双双 miss 后各自插入，后到者撞约束。
    回滚本事务的半途写入（用户/角色/身份/邀请消费），重读身份并按已
    绑定路径登录；邀请已被并发方消费，不再重复消费。身份仍不存在视为
    不可恢复的竞态消失，按通用失败拒绝。
    """
    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.openid == openid))
    if identity is None:
        raise AppError("AUTH_FAILED", "微信绑定处理异常，请稍后重试", 500)
    if expected_company_id:
        existing_user = db.get(User, identity.user_id)
        if existing_user and existing_user.company_id and existing_user.company_id != expected_company_id:
            raise AppError("AUTH_WECHAT_BOUND_OTHER_COMPANY", "该微信已绑定其他加盟商公司", 409)
    return _login_bound_identity(
        db,
        identity,
        unionid=unionid,
        nickname=nickname,
        expected_company_id=expected_company_id,
        consume_invite=False,
    )


def login_or_bind_wechat(
    db: Session,
    *,
    openid: str,
    unionid: str | None = None,
    nickname: str | None = None,
    invite_token: str | None = None,
    invite_id: str | None = None,
    expected_company_id: str | None = None,
) -> tuple[User, str]:
    if invite_token or invite_id:
        invite = validate_invite(db, raw_token=invite_token, invite_id=invite_id)
        if invite.purpose == "OWNER_WECHAT_REBIND":
            return _rebind_owner_wechat(
                db, invite=invite, openid=openid, unionid=unionid, nickname=nickname,
                invite_token=invite_token, invite_id=invite_id,
                expected_company_id=expected_company_id,
            )
    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.openid == openid))
    if identity:
        return _login_bound_identity(
            db,
            identity,
            unionid=unionid,
            nickname=nickname,
            invite_token=invite_token,
            invite_id=invite_id,
            expected_company_id=expected_company_id,
        )

    if not invite_token and not invite_id:
        raise AppError("AUTH_WECHAT_NOT_BOUND", "该微信尚未绑定加盟商，请使用邀请链接进入", 403)
    # I5：统一 company 行锁先行——与 create_company_invite 的「公司→邀请」加锁顺序
    # 一致，消除「创建（公司→邀请）× 绑定（邀请→公司）」的 AB-BA 死锁面。
    # validate_invite 只读不加锁，行锁只落在随后的 FOR UPDATE 上。
    invite = validate_invite(db, raw_token=invite_token, invite_id=invite_id)
    if expected_company_id and invite.company_id != expected_company_id:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    company = db.scalar(
        select(Company).where(Company.id == invite.company_id).with_for_update()
    )
    # I7：锁定后复核公司状态，停用公司不得被占用主账号。
    if company is None or company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    _consume_invite(
        db,
        raw_token=invite_token,
        invite_id=invite_id,
        expected_company_id=expected_company_id,
    )
    user = User(
        display_name=nickname or company.owner_name or "加盟商负责人",
        company_id=company.id,
        status="ACTIVE",
        last_login_at=utcnow(),
    )
    db.add(user)
    db.flush()
    assign_role(db, user, "FRANCHISE_OWNER")
    db.add(WechatIdentity(openid=openid, unionid=unionid, nickname=nickname, user_id=user.id, subscribed=False))
    # P0-05 原子占用主账号：条件 UPDATE 保证并发下仅一个微信成为 primary；
    # I7：占用同时要求公司仍为 ACTIVE（行锁下的纵深防御，前置读被竞态绕过也不放行）。
    # 占用失败时不手动 rollback——AppError 冒泡后由请求作用域 session.close()
    # 隐式回滚整个事务（含邀请消费与用户创建），由 M2 计数断言锁定。
    # I14：try 必须罩住 UPDATE 与收尾 flush 两个 identity INSERT 落点——
    # autoflush 的 session 会在 UPDATE 处推 INSERT，autoflush=False 的
    # session（生产 get_db/e2e factory）推迟到显式 flush。并发同 openid
    # 的后到者在此撞唯一约束，交由幂等重放按已绑定登录处理，而非 409。
    try:
        claimed = db.execute(
            update(Company)
            .where(
                Company.id == company.id,
                Company.primary_user_id.is_(None),
                Company.status == "ACTIVE",
            )
            .values(primary_user_id=user.id)
        ).rowcount
        if not claimed:
            raise AppError("AUTH_COMPANY_ALREADY_BOUND", "该公司已绑定微信主账号", 409)
        # N9：新用户路径在 user 建档后补写归因——与邀请消费同一事务提交。
        db.execute(
            update(InviteToken)
            .where(InviteToken.id == invite.id, InviteToken.used_by_user_id.is_(None))
            .values(used_by_user_id=user.id)
            .execution_options(synchronize_session=False)
        )
        db.flush()
    except IntegrityError as exc:
        # codex #3：仅 openid 唯一冲突走幂等重放——同一段 flush 覆盖
        # user_roles 等其他待写约束，误吞会掩盖真实数据错误；其余冲突
        # re-raise，由 router 统一收敛为 AUTH_FAILED 302。
        if not _is_openid_unique_conflict(exc):
            raise
        db.rollback()
        return _replay_after_conflict(
            db,
            openid=openid,
            unionid=unionid,
            nickname=nickname,
            expected_company_id=expected_company_id,
        )
    token = create_access_token(user.id, user.session_version, role_codes_for_user(user), company.id)
    return user, token


def _rebind_owner_wechat(
    db: Session,
    *,
    invite: InviteToken,
    openid: str,
    unionid: str | None,
    nickname: str | None,
    invite_token: str | None,
    invite_id: str | None,
    expected_company_id: str | None,
) -> tuple[User, str]:
    if expected_company_id and invite.company_id != expected_company_id:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    company = db.scalar(select(Company).where(Company.id == invite.company_id).with_for_update())
    if company is None or company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    owner = db.get(User, invite.target_user_id) if invite.target_user_id else None
    if owner is None or owner.status != "ACTIVE" or owner.company_id != company.id or company.primary_user_id != owner.id:
        raise AppError("AUTH_OWNER_CHANGED", "负责人账号已变化，请重新发起换绑", 409)
    roles = role_codes_for_user(owner)
    if roles != ["FRANCHISE_OWNER"]:
        raise AppError("AUTH_OWNER_CHANGED", "负责人账号已变化，请重新发起换绑", 409)
    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == owner.id).with_for_update())
    if identity is None:
        raise AppError("AUTH_OWNER_NOT_BOUND", "负责人微信尚未绑定", 409)
    occupied = db.scalar(select(WechatIdentity).where(WechatIdentity.openid == openid))
    if occupied is not None:
        raise AppError("AUTH_WECHAT_ALREADY_BOUND", "该微信已绑定账号，请使用未绑定的微信", 409)
    _consume_invite(
        db, raw_token=invite_token, invite_id=invite_id,
        expected_company_id=company.id, used_by_user_id=owner.id,
    )
    identity.openid = openid
    identity.unionid = unionid
    identity.nickname = nickname
    owner.session_version += 1
    owner.last_login_at = utcnow()
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if _is_openid_unique_conflict(exc):
            raise AppError("AUTH_WECHAT_ALREADY_BOUND", "该微信已绑定账号，请使用未绑定的微信", 409) from exc
        raise
    return owner, create_access_token(owner.id, owner.session_version, roles, company.id)


def bind_wechat_by_invite(db: Session, raw_token: str, openid: str, nickname: str) -> tuple[User, str]:
    """I13：显式测试缝——生产路由已全部走 invite_id；raw_token 通道仅为存量
    哈希邀请（P0-06 前）与测试保留，行为与 invite_id 通道完全同构。"""
    return login_or_bind_wechat(db, openid=openid, nickname=nickname, invite_token=raw_token)


def create_internal_user(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str,
    role_code: str,
    company_id: str | None = None,
) -> User:
    if db.scalar(select(User).where(User.username == username)):
        raise AppError("USER_EXISTS", "账号已存在", 409)
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        company_id=company_id,
        status="ACTIVE",
    )
    db.add(user)
    db.flush()
    assign_role(db, user, role_code)
    return user
