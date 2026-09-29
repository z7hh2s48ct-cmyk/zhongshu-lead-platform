from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, load_current_principal, require_permissions
from ..core.config import get_settings
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Company, InviteToken, User
from ..core.responses import ok
from ..core.security import (
    create_access_token,
    create_signed_state,
    decode_access_token,
    decode_signed_state,
    hash_password,
    validate_internal_password,
)
from ..core.time import as_utc
from ..integrations.wechat import WechatOAuthClient
from ..schemas.auth import (
    ChangeOwnPasswordBody,
    ChangeOwnUsernameBody,
    InviteConfirmStartBody,
    InviteCreateBody,
    LoginBody,
    WechatMockCallbackBody,
)
from ..services.audit import write_audit
from ..services.auth_service import (
    InternalAuthError,
    authenticate_internal,
    build_invite_copy_text,
    create_company_invite,
    create_owner_wechat_rebind_invite,
    list_company_invites,
    login_or_bind_wechat,
    revoke_company_invite,
    validate_invite,
    verify_internal_password_for_sensitive_action,
)
from ..services.notification_service import enqueue_outbox

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()
logger = logging.getLogger("zhongshu.auth")


def _request_ip(request: Request) -> str | None:
    # N1：x-real-ip 只有在部署方显式声明"前方是会强制覆写该头的受信反代"
    # 时才可信；默认取 TCP 对端地址，客户端伪造的头不能充当身份。
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-real-ip")
        if forwarded:
            return forwarded
    return request.client.host if request.client else None


# I15：匿名 confirm-start 无认证无限流——持一条有效邀请即可循环刷审计写入
# （审计表膨胀、写负载）。按 invite+IP 双键做进程内滑动窗口限流；短时重复
# 确认的审计去重由该限流压制（每分钟至多 10 条），不再单独比对历史审计。
# N8：键必须是定长哈希——原始 invite（16-128 字符垃圾串）直接作键会被
# 桶容量攻击撑爆；清扫后仍达容量上限时 fail-closed 拒绝新键，窗口过期
# 自动恢复，绝不让字典无界生长。
# M-A：同步端点跑线程池，读-改-写必须整体持锁，否则并发 burst 下多线程
# 同时通过计数检查，同键窗口内放行远超上限（限流被绕过）。
_CONFIRM_START_LOCK = threading.Lock()
_CONFIRM_START_BUCKETS: dict[str, list[float]] = {}
_CONFIRM_START_WINDOW_SECONDS = 60.0
_CONFIRM_START_MAX_PER_WINDOW = 10
_CONFIRM_START_MAX_BUCKETS = 4096
# M-A：单 IP 维度独立限流——invite 是请求体任意串，若仅有 invite+IP 桶，
# 单 IP 用随机 invite 即可在窗口内撞满桶表让 fail-closed 拒绝所有真实用户
# （全站 DoS）。IP 键空间由 TCP 对端或受信反代注入，无法凭请求体伪造；
# 该层把单 IP 能创建的 invite 桶数量硬性封顶，分布式填桶需每 IP 都超限。
_CONFIRM_START_IP_BUCKETS: dict[str, list[float]] = {}
_CONFIRM_START_MAX_PER_IP_PER_WINDOW = 30
_CONFIRM_START_MAX_IP_BUCKETS = 4096

def _confirm_start_rate_limited(invite: str, ip: str | None) -> bool:
    now = time.monotonic()
    with _CONFIRM_START_LOCK:
        return _confirm_start_rate_limited_locked(invite=invite, ip=ip, now=now)

def _confirm_start_rate_limited_locked(*, invite: str, ip: str | None, now: float) -> bool:
    ip_key = hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()
    ip_hits = [
        stamp
        for stamp in _CONFIRM_START_IP_BUCKETS.get(ip_key, [])
        if now - stamp < _CONFIRM_START_WINDOW_SECONDS
    ]
    if len(ip_hits) >= _CONFIRM_START_MAX_PER_IP_PER_WINDOW:
        _CONFIRM_START_IP_BUCKETS[ip_key] = ip_hits
        return True
    if len(_CONFIRM_START_IP_BUCKETS) >= _CONFIRM_START_MAX_IP_BUCKETS:
        for key, stamps in list(_CONFIRM_START_IP_BUCKETS.items()):
            alive = [stamp for stamp in stamps if now - stamp < _CONFIRM_START_WINDOW_SECONDS]
            if alive:
                _CONFIRM_START_IP_BUCKETS[key] = alive
            else:
                _CONFIRM_START_IP_BUCKETS.pop(key, None)
        if len(_CONFIRM_START_IP_BUCKETS) >= _CONFIRM_START_MAX_IP_BUCKETS:
            return True  # fail-closed：与 invite 桶同策略，宁拒不膨胀
    ip_hits.append(now)
    _CONFIRM_START_IP_BUCKETS[ip_key] = ip_hits
    # 惰性清理：桶数量达上限时整体清扫过期项，避免长驻进程的键无限累积。
    if len(_CONFIRM_START_BUCKETS) >= _CONFIRM_START_MAX_BUCKETS:
        for key, stamps in list(_CONFIRM_START_BUCKETS.items()):
            alive = [stamp for stamp in stamps if now - stamp < _CONFIRM_START_WINDOW_SECONDS]
            if alive:
                _CONFIRM_START_BUCKETS[key] = alive
            else:
                _CONFIRM_START_BUCKETS.pop(key, None)
        if len(_CONFIRM_START_BUCKETS) >= _CONFIRM_START_MAX_BUCKETS:
            return True  # fail-closed：宁可短暂拒绝 confirm-start，也不无界扩张
    key = hashlib.sha256(f"{invite}\x1f{ip or 'unknown'}".encode("utf-8")).hexdigest()
    hits = [stamp for stamp in _CONFIRM_START_BUCKETS.get(key, []) if now - stamp < _CONFIRM_START_WINDOW_SECONDS]
    if len(hits) >= _CONFIRM_START_MAX_PER_WINDOW:
        _CONFIRM_START_BUCKETS[key] = hits
        return True
    hits.append(now)
    _CONFIRM_START_BUCKETS[key] = hits
    return False


# N11：callback 失败审计按 IP+reason+根因异常类 节流——匿名 callback 无限
# 流，攻击者循环打坏 state 可把审计表当垃圾场刷爆；首条审计落库，同键
# 60s 内的重复失败降级为已有的 warning 日志（可观测不丢，写库有界）。
# 根因异常类由服务端代码路径决定、客户端无法轮换；不同根因（如
# IntegrityError 转译 vs 未知异常，均显示 AUTH_FAILED）各自保留审计。
_CALLBACK_AUDIT_THROTTLE: dict[str, float] = {}
_CALLBACK_AUDIT_THROTTLE_WINDOW_SECONDS = 60.0
_CALLBACK_AUDIT_THROTTLE_MAX_KEYS = 4096

def _callback_audit_throttled(ip: str | None, reason_code: str, exc: Exception, stage: str = "callback") -> bool:
    now = time.monotonic()
    cause = type(exc.__cause__).__name__ if exc.__cause__ else "-"
    key = hashlib.sha256(f"{ip or 'unknown'}\x1f{reason_code}\x1f{cause}\x1f{stage}".encode("utf-8")).hexdigest()
    # M-A 同源加固：同步端点跑线程池，读-改-写同样持锁串行化，避免并发
    # burst 同键同时通过节流检查短时多写审计（fail-open 语义不变，仅收紧窗口）。
    with _CONFIRM_START_LOCK:
        last = _CALLBACK_AUDIT_THROTTLE.get(key)
        if last is not None and now - last < _CALLBACK_AUDIT_THROTTLE_WINDOW_SECONDS:
            return True
        if len(_CALLBACK_AUDIT_THROTTLE) >= _CALLBACK_AUDIT_THROTTLE_MAX_KEYS:
            for stale_key, stamp in list(_CALLBACK_AUDIT_THROTTLE.items()):
                if now - stamp >= _CALLBACK_AUDIT_THROTTLE_WINDOW_SECONDS:
                    _CALLBACK_AUDIT_THROTTLE.pop(stale_key, None)
            if len(_CALLBACK_AUDIT_THROTTLE) >= _CALLBACK_AUDIT_THROTTLE_MAX_KEYS:
                return False  # 审计是安全记录：键表满时宁可放行落库也不静默丢审计
        _CALLBACK_AUDIT_THROTTLE[key] = now
        return False

def _audit_unlock(db: Session, *, user_id: str | None, request: Request) -> None:
    if not user_id:
        return
    write_audit(
        db,
        principal=None,
        action="AUTH_LOGIN_UNLOCKED",
        resource_type="user",
        resource_id=user_id,
        metadata={"reason_code": "LOCK_EXPIRED"},
        request_id=request.state.request_id,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        secure=settings.app_env.lower() == "production",
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
        path="/",
    )


def _verify_current_password_or_audit(
    db: Session,
    *,
    user: User | None,
    password: str,
    principal: CurrentPrincipal,
    action: str,
    request: Request,
) -> None:
    try:
        if user is None:
            raise AppError("AUTH_CURRENT_PASSWORD_INVALID", "当前密码不正确", 422)
        verify_internal_password_for_sensitive_action(db, user, password)
    except AppError as exc:
        if exc.code in {"AUTH_CURRENT_PASSWORD_INVALID", "AUTH_SENSITIVE_ACTION_LOCKED"}:
            write_audit(
                db,
                principal=principal,
                action=action,
                resource_type="user",
                resource_id=principal.user_id,
                metadata={"reason_code": exc.code, **dict(exc.details or {})},
                request_id=request.state.request_id,
                ip_address=_request_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            db.commit()
        raise


def _resolve_binding_intent(state: str) -> tuple[str | None, str | None, str]:
    """Decode a signed OAuth state and split binding intents from legacy login states.

    Returns (invite_id, expected_company_id, return_url). Binding intents
    (purpose="wechat-oauth-bind") require an explicit binding_confirmed flag;
    legacy states only permit login of already-bound identities and never
    carry an invite reference.
    """
    try:
        bind_state = decode_signed_state(state, purpose="wechat-oauth-bind")
    except InvalidTokenError:
        bind_state = None
    if bind_state is not None:
        if bind_state.get("binding_confirmed") is not True:
            raise AppError("AUTH_BINDING_CONFIRM_REQUIRED", "请先在邀请页确认后继续", 400)
        invite_id = bind_state.get("invite_id")
        company_id = bind_state.get("company_id")
        if not invite_id or not company_id:
            raise AppError("AUTH_OAUTH_STATE_INVALID", "微信授权状态已失效，请重新进入", 400)
        return str(invite_id), str(company_id), str(bind_state.get("return_url") or "/h5/#/home")
    try:
        legacy_state = decode_signed_state(state, purpose="wechat-oauth")
    except InvalidTokenError as exc:
        raise AppError("AUTH_OAUTH_STATE_INVALID", "微信授权状态已失效，请重新进入", 400) from exc
    # legacy purpose 状态不再携带邀请：未绑定微信必须先走确认后授权流程
    return None, None, str(legacy_state.get("return_url") or "/h5/#/home")


# I12：绑定预授权 state 的显式 TTL（分钟）。binding_confirmed state 是纯
# bearer 令牌，不依赖 create_signed_state 的通用默认值，收紧签发窗口。
_BINDING_STATE_TTL_MINUTES = 5

# P2-5：callback 失败审计的分类口径。flow 区分这次失败发生在哪个环节
# （bind=绑定意图、login=legacy 重登、unknown=state 未通过验签），
# failure_class 区分失败性质（security=篡改/伪造、business=绑定规则、
# upstream=微信侧 5xx），与 /auth/login 按 audit_action 分类的口径对齐。
_CALLBACK_SECURITY_FAILURE_CODES = frozenset(
    {
        "AUTH_OAUTH_STATE_INVALID",
        "AUTH_BINDING_CONFIRM_REQUIRED",
    }
)

# P1-04：允许透传到 H5 状态页的错误码；白名单外的异常统一归并为
# AUTH_FAILED，避免把任意错误细节拼进重定向 URL。
# P2-1：微信通道故障码（未配置/不可用/授权失败）显式入白名单——仍走
# 302 状态页（微信浏览器不暴露裸 JSON），但携带具体码让 H5 给出「稍后
# 重试」而非「重新获取邀请」的指引；可观测性由 P2-5 的失败审计
# （failure_class=upstream）与下方结构化日志承载，不依赖响应码本身。
# P2-1：微信通道故障码的显式集合——logger.error 的告警条件用它而非
# 「任意 5xx」，避免内部未知 5xx 污染通道健康度指标（codex 评审 #2）；
# 未知 5xx 仍由失败审计（failure_class=upstream）完整留痕。
# N6：WECHAT_SCOPE_INVALID 是 authorization_url 的通道配置故障，经
# /wechat/start 透传，与健康度告警同口径。
_WECHAT_CHANNEL_FAILURE_CODES = frozenset(
    {
        "WECHAT_NOT_CONFIGURED",
        "WECHAT_OAUTH_UNAVAILABLE",
        "WECHAT_OAUTH_FAILED",
        "WECHAT_SCOPE_INVALID",
    }
)

# AUTH_FAILED，避免把任意错误细节拼进重定向 URL。
# P2-1：微信通道故障码（未配置/不可用/授权失败）显式入白名单——仍走
# 302 状态页（微信浏览器不暴露裸 JSON），但携带具体码让 H5 给出「稍后
# 重试」而非「重新获取邀请」的指引；可观测性由 P2-5 的失败审计
# （failure_class=upstream）与结构化日志承载，不依赖响应码本身。
# N6：WECHAT_SCOPE_INVALID 经 /wechat/start 的 302 收敛也会到达 H5
# 状态页（codex 评审 #1 的旧结论随 start 端点接入收敛而失效）。
# N13：通道码经并集纳入，与上方告警集合单一来源，不再整段重复枚举。
_H5_AUTH_ERROR_CODES = frozenset(
    {
        "AUTH_OAUTH_STATE_INVALID",
        "AUTH_BINDING_CONFIRM_REQUIRED",
        "AUTH_WECHAT_NOT_BOUND",
        "AUTH_WECHAT_BOUND_OTHER_COMPANY",
        "AUTH_COMPANY_DISABLED",
        "AUTH_COMPANY_ALREADY_BOUND",
        "AUTH_INVITE_INVALID",
        "AUTH_ACCOUNT_DISABLED",
        "AUTH_BINDING_REQUIRES_CLEAN_SESSION",
        "AUTH_WECHAT_IDENTITY_CONFLICT",
    }
) | _WECHAT_CHANNEL_FAILURE_CODES


def _request_session_principal(request: Request, db: Session) -> CurrentPrincipal | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        return None
    return load_current_principal(db, payload.get("sub"), payload.get("sv"))


def _require_clean_binding_session(request: Request, db: Session, invite: InviteToken | None = None) -> None:
    current = _request_session_principal(request, db)
    if current is not None and not (
        invite is not None
        and invite.purpose == "OWNER_WECHAT_REBIND"
        and current.user_id == invite.target_user_id
    ):
        raise AppError(
            "AUTH_BINDING_REQUIRES_CLEAN_SESSION",
            "当前浏览器已登录账号，请先退出后再用负责人本人微信打开邀请",
            409,
        )


def _h5_auth_error_url(error_code: str) -> str:
    return f"/h5/auth-error.html?code={error_code}"


def _sanitize_return_url(value: str) -> str:
    """Constrain return_url to the /h5/ app via a prefix whitelist.

    Browsers treat backslashes like slashes for special schemes, and WHATWG
    URL parsing strips tab/LF/CR before parsing — so "/\\t/evil.com" reaches
    the browser as "//evil.com". Any ASCII control character, backslash, or
    non-/h5/ prefix therefore collapses to the safe default instead of being
    pattern-matched against a blocklist.
    """
    if not value.startswith("/h5/"):
        return "/h5/#/home"
    if any(ord(ch) <= 0x1F or ord(ch) == 0x7F or ch == "\\" for ch in value):
        return "/h5/#/home"
    return value


def _sanitize_binding_return_url(value: str) -> str:
    """Keep franchise-owner binding away from other role workbenches."""

    safe_value = _sanitize_return_url(value)
    if safe_value in {"/h5/#/home", "/h5/v12-workbench.html"}:
        return safe_value
    return "/h5/#/home"


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Annotated[Session, Depends(get_db)]):
    try:
        result = authenticate_internal(db, body.username, body.password)
    except InternalAuthError as exc:
        if exc.lock_released:
            _audit_unlock(db, user_id=exc.user_id, request=request)
        write_audit(
            db,
            principal=None,
            action=exc.audit_action,
            resource_type="user",
            resource_id=exc.user_id,
            metadata={
                "reason_code": exc.code,
                "failure_count": exc.failure_count,
                "locked_until": exc.locked_until.isoformat() if exc.locked_until else None,
            },
            request_id=request.state.request_id,
            ip_address=_request_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        # Authentication failure state is security data and must survive the error response.
        db.commit()
        raise

    user = result.user
    if result.lock_released:
        _audit_unlock(db, user_id=user.id, request=request)
    principal = load_current_principal(db, user.id, user.session_version)
    write_audit(
        db,
        principal=principal,
        action="AUTH_LOGIN",
        resource_type="user",
        resource_id=user.id,
        request_id=request.state.request_id,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    _set_session_cookie(response, result.token)
    return ok(
        request,
        {
            "user": {
                "id": user.id,
                "display_name": user.display_name,
                "roles": [r.code for r in user.roles],
            }
        },
    )


@router.post("/logout")
def logout(request: Request, response: Response, principal: CurrentPrincipal, db: Annotated[Session, Depends(get_db)]):
    user = db.get(User, principal.user_id)
    if user:
        user.session_version += 1
    write_audit(db, principal=principal, action="AUTH_LOGOUT", resource_type="user", resource_id=principal.user_id, request_id=request.state.request_id)
    db.commit()
    response.delete_cookie(
        "access_token",
        path="/",
        secure=settings.app_env.lower() == "production",
        httponly=True,
        samesite="lax",
    )
    return ok(request, message="已退出")


@router.post("/change-password")
def change_own_password(
    body: ChangeOwnPasswordBody,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    db: Annotated[Session, Depends(get_db)],
):
    user = db.scalar(select(User).where(User.id == principal.user_id).with_for_update())
    assert user is not None
    first_backup_password = not bool(user.password_hash)
    if not first_backup_password:
        if not body.current_password:
            raise AppError("AUTH_CURRENT_PASSWORD_REQUIRED", "当前密码必填", 422)
        _verify_current_password_or_audit(
            db,
            user=user,
            password=body.current_password,
            principal=principal,
            action="AUTH_PASSWORD_CHANGE_FAILED",
            request=request,
        )
    try:
        validate_internal_password(body.new_password)
    except ValueError as exc:
        raise AppError("AUTH_PASSWORD_INVALID", str(exc), 422) from exc

    previous_session_version = user.session_version
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1
    write_audit(
        db,
        principal=principal,
        action="AUTH_BACKUP_PASSWORD_SET" if first_backup_password else "AUTH_PASSWORD_CHANGE",
        resource_type="user",
        resource_id=user.id,
        before={"session_version": previous_session_version},
        after={"session_version": user.session_version, "first_backup_password": first_backup_password},
        request_id=request.state.request_id,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    _set_session_cookie(
        response,
        create_access_token(
            user.id,
            user.session_version,
            sorted(principal.role_codes),
            user.company_id,
        ),
    )
    return ok(request, message="备用登录密码已设置" if first_backup_password else "密码已更新，其他登录会话已失效")


@router.post("/change-username")
def change_own_username(
    body: ChangeOwnUsernameBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Annotated[Session, Depends(get_db)],
):
    user = db.scalar(select(User).where(User.id == principal.user_id).with_for_update())
    _verify_current_password_or_audit(
        db,
        user=user,
        password=body.current_password,
        principal=principal,
        action="AUTH_USERNAME_CHANGE_FAILED",
        request=request,
    )
    assert user is not None
    if user.username == body.username:
        return ok(request, {"username": user.username}, "登录账号未变化")
    if db.scalar(select(User.id).where(User.username == body.username, User.id != user.id)):
        raise AppError("AUTH_USERNAME_EXISTS", "该登录账号已被使用", 409)

    previous_username = user.username
    user.username = body.username
    write_audit(
        db,
        principal=principal,
        action="AUTH_USERNAME_CHANGE",
        resource_type="user",
        resource_id=user.id,
        before={"username": previous_username},
        after={"username": user.username},
        request_id=request.state.request_id,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError("AUTH_USERNAME_EXISTS", "该登录账号已被使用", 409) from exc
    return ok(request, {"username": user.username}, "登录账号已更新")


@router.get("/me")
def me(request: Request, principal: CurrentPrincipal, db: Annotated[Session, Depends(get_db)]):
    user = db.get(User, principal.user_id)
    company = db.get(Company, principal.company_id) if principal.company_id else None
    return ok(
        request,
        {
            "id": principal.user_id,
            "display_name": principal.display_name,
            "username": user.username if user else None,
            "has_password": bool(user and user.password_hash),
            "company_id": principal.company_id,
            "company_name": company.name if company else "合家美宅平台",
            "roles": sorted(principal.role_codes),
            "permissions": sorted(principal.permission_codes),
        },
    )


@router.post("/companies/{company_id}/invites")
def create_invite(
    company_id: str,
    body: InviteCreateBody,
    request: Request,
    principal=Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    invite, raw, superseded_ids = create_company_invite(db, company_id, principal.user_id, body.expires_hours)
    write_audit(db, principal=principal, action="INVITE_CREATE", resource_type="invite", resource_id=invite.id, company_id=company_id, request_id=request.state.request_id)
    if superseded_ids:
        # 只记录被撤销邀请的 id，绝不落任何 token 原文或文案
        write_audit(
            db,
            principal=principal,
            action="INVITE_AUTO_REVOKE",
            resource_type="invite",
            resource_id=invite.id,
            company_id=company_id,
            metadata={"superseded_invite_ids": superseded_ids, "reason": "superseded_by_new_invite"},
            request_id=request.state.request_id,
        )
    # P2-03：邀请生成即入 Outbox 事件（业务事务内，幂等键 invite:{id}:created）。
    # 事件只描述快照事实，绝不携带 raw token——邀请链接仅经创建响应一次性下发。
    enqueue_outbox(
        db,
        event_key=f"invite:{invite.id}:created",
        event_type="INVITE_CREATED",
        aggregate_type="invite",
        aggregate_id=invite.id,
        payload={
            "company_id": company_id,
            "company_name": invite.company_name_snapshot,
            "invitee_name": invite.invitee_name_snapshot,
            "expires_at": invite.expires_at.isoformat(),
            "deep_link": "/h5/invite.html",
        },
    )
    db.commit()
    company = db.get(Company, company_id)
    assert company is not None
    url = f"{settings.app_base_url.rstrip('/')}/h5/invite.html#invite={raw}"
    expires_at = invite.expires_at.isoformat()
    return ok(
        request,
        {
            "invite_id": invite.id,
            "token": raw,
            "url": url,
            "company_name": company.name,
            "owner_name": company.owner_name,
            "copy_text": build_invite_copy_text(company.owner_name, company.name, url, expires_at),
            "expires_at": expires_at,
            "revoked_invite_count": len(superseded_ids),
        },
    )


@router.post("/companies/{company_id}/owner-wechat-rebind-invites")
def create_owner_rebind_invite(
    company_id: str,
    body: InviteCreateBody,
    request: Request,
    principal=Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    invite, raw, superseded_ids = create_owner_wechat_rebind_invite(
        db, company_id, principal.user_id, body.expires_hours
    )
    write_audit(
        db, principal=principal, action="WECHAT_REBIND_INVITE_CREATE",
        resource_type="invite", resource_id=invite.id, company_id=company_id,
        metadata={"target_user_id": invite.target_user_id, "superseded_invite_ids": superseded_ids},
        request_id=request.state.request_id,
    )
    db.commit()
    company = db.get(Company, company_id)
    assert company is not None
    url = f"{settings.app_base_url.rstrip('/')}/h5/invite.html#invite={raw}"
    expires_at = invite.expires_at.isoformat()
    return ok(request, {
        "invite_id": invite.id, "token": raw, "url": url,
        "company_name": company.name, "owner_name": company.owner_name,
        "copy_text": f"{company.owner_name or '负责人'}，请使用新的微信打开此链接完成账号换绑：{url}，有效期至：{expires_at}",
        "expires_at": expires_at, "revoked_invite_count": len(superseded_ids),
    })


@router.post("/invites/confirm-start")
def invite_confirm_start(
    body: InviteConfirmStartBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Exchange a confirmed invite for a short-lived binding OAuth intent (P0-04)."""

    # I15：限流在邀请校验之前——无效邀请的探测请求同样计数，防枚举刷。
    if _confirm_start_rate_limited(body.invite, _request_ip(request)):
        raise AppError("AUTH_RATE_LIMITED", "操作过于频繁，请稍后再试", 429)

    invite = validate_invite(db, raw_token=body.invite)
    _require_clean_binding_session(request, db, invite)
    return_url = _sanitize_binding_return_url(body.return_url)
    # I12：绑定预授权 state 显式收紧 TTL，不依赖 create_signed_state 的通用默认值。
    state = create_signed_state(
        {
            "invite_id": invite.id,
            "company_id": invite.company_id,
            "binding_confirmed": True,
            "return_url": return_url,
        },
        purpose="wechat-oauth-bind",
        expires_minutes=_BINDING_STATE_TTL_MINUTES,
    )
    # N6：先确认通道能产出授权 URL，再提交 INVITE_CONFIRM_START——顺序
    # 颠倒会让配置故障留下「已开始」的成功审计。失败改记
    # INVITE_CONFIRM_START_FAILED 并落通道告警，503 透传给前端状态页。
    try:
        authorization_url = WechatOAuthClient().authorization_url(state=state)
    except AppError as exc:
        logger.error(
            "wechat oauth channel failure at confirm-start",
            extra={
                "request_id": request.state.request_id,
                "reason_code": exc.code,
                "flow": "bind",
                "stage": "confirm-start",
                "status_code": exc.status_code,
            },
        )
        db.rollback()
        write_audit(
            db,
            principal=None,
            action="INVITE_CONFIRM_START_FAILED",
            resource_type="invite",
            resource_id=invite.id,
            company_id=invite.company_id,
            metadata={"reason_code": exc.code, "status_code": exc.status_code},
            request_id=request.state.request_id,
            ip_address=_request_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
        raise
    # I12：binding_confirmed state 是纯 bearer——预授权签发时记录客户端
    # IP 与 User-Agent，回调侧令牌异常时可对照审计定位挪用。
    write_audit(
        db,
        principal=None,
        action="INVITE_CONFIRM_START",
        resource_type="invite",
        resource_id=invite.id,
        company_id=invite.company_id,
        request_id=request.state.request_id,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    payload = decode_signed_state(state, purpose="wechat-oauth-bind")
    expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
    return ok(
        request,
        {
            "authorization_url": authorization_url,
            "expires_at": expires_at.isoformat(),
        },
    )


@router.get("/companies/{company_id}/invites")
def list_company_invites_endpoint(
    company_id: str,
    request: Request,
    principal=Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    """P1-01/P1-02：公司邀请记录与使用追溯（只读，绝不返回 token 原文或哈希）。"""

    return ok(request, {"items": list_company_invites(db, company_id)})


@router.post("/invites/{invite_id}/revoke")
def revoke_invite(
    invite_id: str,
    request: Request,
    principal=Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    """N4：撤销业务全部下沉 auth_service.revoke_company_invite——router
    只保留鉴权与响应组装，PG 并发 e2e 直接测生产实现，不再维护镜像。"""

    revoke_company_invite(db, invite_id=invite_id, principal=principal, request_id=request.state.request_id)
    return ok(request, message="邀请已撤销")


@router.post("/wechat/mock-callback")
def wechat_mock_callback(body: WechatMockCallbackBody, request: Request, response: Response, db: Session = Depends(get_db)):

    if not settings.wechat_dev_mock:
        raise AppError("AUTH_MOCK_DISABLED", "开发模拟登录已关闭", 403)
    # mock 通道与生产回调同一合同：绑定必须携带确认后的 signed state（P0-07）
    invite_id, expected_company_id, _ = _resolve_binding_intent(body.state)
    if invite_id:
        _require_clean_binding_session(request, db, db.get(InviteToken, invite_id))
    user, token = login_or_bind_wechat(
        db,
        openid=body.openid,
        nickname=body.nickname,
        invite_id=invite_id,
        expected_company_id=expected_company_id,
    )
    purpose = db.get(InviteToken, invite_id).purpose if invite_id else None
    write_audit(db, principal=None, action="WECHAT_REBIND" if purpose == "OWNER_WECHAT_REBIND" else "WECHAT_BIND", resource_type="user", resource_id=user.id, company_id=user.company_id, metadata={"invite_id": invite_id, "purpose": purpose} if invite_id else None, request_id=request.state.request_id)
    db.commit()
    response.set_cookie("access_token", token, httponly=True, secure=False, samesite="lax", max_age=settings.jwt_expire_minutes * 60, path="/")
    return ok(request, {"token": token, "user_id": user.id, "company_id": user.company_id})


@router.get("/wechat/start")
def wechat_start(
    request: Request,
    return_url: str = Query(default="/h5/#/home"),
    db: Session = Depends(get_db),
):
    # Phase 3.5/H2：旧邀请入口显式拒绝。只删参数声明时 FastAPI 会忽略
    # 未声明的 query 参数并返回 200，旧链接会静默走完一次微信跳转。
    # 首次绑定必须经 /auth/invites/confirm-start 取得确认后的 signed state。
    if "invite" in request.query_params:
        raise AppError("AUTH_INVITE_ENTRY_DEPRECATED", "邀请入口已更新，请从最新邀请链接重新进入", 400)
    return_url = _sanitize_return_url(return_url)
    state = create_signed_state({"return_url": return_url}, purpose="wechat-oauth")
    try:
        authorization_url = WechatOAuthClient().authorization_url(state=state)
    except AppError as exc:
        # N6：start 是浏览器直接导航——通道故障必须与 callback 同口径
        # （302 状态页 + 失败审计 + 通道告警），不能回裸 JSON。
        return _redirect_callback_failure(
            exc,
            request=request,
            db=db,
            invite_id=None,
            expected_company_id=None,
            return_url=return_url,
            action="WECHAT_OAUTH_START_FAILED",
            stage="start",
        )
    return RedirectResponse(url=authorization_url, status_code=302)


def _redirect_callback_failure(
    exc: AppError,
    *,
    request: Request,
    db: Session,
    invite_id: str | None,
    expected_company_id: str | None,
    return_url: str | None,
    action: str = "WECHAT_OAUTH_CALLBACK_FAILED",
    stage: str = "callback",
):
    """P2-4/P2-5：callback 一切失败的统一收敛——302 状态页 + 失败审计 +
    分级结构化日志，任何入口（AppError/IntegrityError/未知异常转译后）
    都不得让微信浏览器看到裸 JSON。N6：/wechat/start 的通道故障复用同一
    收敛口径，经 action/stage 区分审计来源。"""
    error_code = exc.code if exc.code in _H5_AUTH_ERROR_CODES else "AUTH_FAILED"
    # P2-5：三个解析变量还原失败发生的环节（state 未验签 = unknown；
    # 验签后 invite_id 有值为 bind，否则 login）。
    flow = "bind" if invite_id else ("login" if return_url else "unknown")
    failure_class = (
        "upstream"
        if exc.status_code >= 500
        else ("security" if exc.code in _CALLBACK_SECURITY_FAILURE_CODES else "business")
    )
    # P2-1：微信通道故障（宕机/密钥错误/未配置）落结构化 error 日志——
    # 302 状态页在网络监控里显示为正常跳转，通道健康度必须从日志告警；
    # 仅对显式通道码告警，不污染通道指标。
    if exc.code in _WECHAT_CHANNEL_FAILURE_CODES:
        logger.error(
            f"wechat oauth channel failure at {stage}",
            extra={
                "request_id": request.state.request_id,
                "reason_code": exc.code,
                "flow": flow,
                "stage": stage,
                "status_code": exc.status_code,
            },
        )
    else:
        # P2-4/codex #4：缺参、state 篡改与业务拒绝不能只有数据库审计——
        # request_completed 只见 302 不含原因，排障需要 warning 级留痕。
        logger.warning(
            "wechat oauth callback rejected",
            extra={
                "request_id": request.state.request_id,
                "reason_code": exc.code,
                "display_code": error_code,
                "flow": flow,
                "failure_class": failure_class,
            },
        )
    # P2-5：callback 失败与 /auth/login 的失败审计口径对齐——state 篡改、
    # 转发误绑、伪造意图等安全失败不能零痕迹。先 rollback 丢弃半途写入
    # （隐式回滚只覆盖请求结束路径），审计行携带 invite_id/company_id/
    # flow/failure_class 供追溯链关联；metadata 只落枚举与标量，不落
    # state/token/openid。failure_class 的 upstream 涵盖微信侧与本机微信
    # 通道（含未配置）的故障，business 为绑定规则拒绝。
    try:
        db.rollback()
        if _callback_audit_throttled(_request_ip(request), exc.code, exc, stage=stage):
            # N11：同 IP+同 reason 的重复失败不重复落库，warning 日志已留痕。
            return RedirectResponse(url=_h5_auth_error_url(error_code), status_code=302)
        write_audit(
            db,
            principal=None,
            action=action,
            resource_type="wechat_bind",
            resource_id=invite_id,
            company_id=expected_company_id,
            metadata={
                "reason_code": exc.code,
                "display_code": error_code,
                "status_code": exc.status_code,
                "flow": flow,
                "stage": stage,
                "failure_class": failure_class,
            },
            request_id=request.state.request_id,
            ip_address=_request_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
    except Exception:
        # 审计持久化失败（含连接失效导致 rollback 本身抛错）不得反噬
        # 302 契约：尽力 rollback 后以结构化日志兜底，让失败仍可观测
        # （codex 评审 #1），页面行为维持 P1-04 的重定向语义。
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception(
            "wechat oauth callback failure audit fell back to log",
            extra={
                "request_id": request.state.request_id,
                "reason_code": exc.code,
                "flow": flow,
                "failure_class": failure_class,
            },
        )
    return RedirectResponse(url=_h5_auth_error_url(error_code), status_code=302)


@router.get("/wechat/callback")
def wechat_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    db: Session = Depends(get_db),
):
    # P1-04：绑定类失败 302 到 H5 状态页；微信浏览器上下文不应看到裸 JSON。
    # P2-5：三个解析变量先置空——except 分支据此还原失败发生的环节
    # （state 未验签 = unknown；验签后 invite_id 有值为 bind，否则 login）。
    invite_id: str | None = None
    expected_company_id: str | None = None
    return_url: str | None = None
    try:
        # P2-4：code/state 声明为可选默认空——微信正常回跳必带两者，缺参
        # 属异常流量；必填参数会在函数体之前被 FastAPI 422 拒绝成裸 JSON，
        # 破坏 302 状态页契约。缺失时在 try 内显式拒绝，走统一的失败审计。
        if not state:
            raise AppError("AUTH_OAUTH_STATE_INVALID", "微信授权状态缺失", 400)
        if not code:
            raise AppError("AUTH_FAILED", "微信授权码缺失，请重新发起授权", 400)
        invite_id, expected_company_id, return_url = _resolve_binding_intent(state)
        if invite_id:
            _require_clean_binding_session(request, db, db.get(InviteToken, invite_id))
        identity = WechatOAuthClient().exchange_code(code)
        user, token = login_or_bind_wechat(
            db,
            openid=identity.openid,
            unionid=identity.unionid,
            nickname=identity.nickname or "微信加盟商",
            invite_id=invite_id,
            expected_company_id=expected_company_id,
        )
        # I4：区分首次绑定与重登——绑定意图（signed bind state）记 WECHAT_BIND
        # 并保留 invite_id 追溯；legacy 普通登录记 WECHAT_OAUTH_LOGIN，与 mock 通道对齐。
        if invite_id:
            invite_purpose = db.get(InviteToken, invite_id).purpose
            write_audit(
                db,
                principal=None,
                action="WECHAT_REBIND" if invite_purpose == "OWNER_WECHAT_REBIND" else "WECHAT_BIND",
                resource_type="user",
                resource_id=user.id,
                company_id=user.company_id,
                metadata={"invite_id": invite_id, "purpose": invite_purpose},
                request_id=request.state.request_id,
                ip_address=_request_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        else:
            write_audit(
                db,
                principal=None,
                action="WECHAT_OAUTH_LOGIN",
                resource_type="user",
                resource_id=user.id,
                company_id=user.company_id,
                request_id=request.state.request_id,
                ip_address=_request_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        db.commit()
    except AppError as exc:
        return _redirect_callback_failure(
            exc,
            request=request,
            db=db,
            invite_id=invite_id,
            expected_company_id=expected_company_id,
            return_url=return_url,
        )
    except IntegrityError as exc:
        # P2-4：service 层幂等重放之外的完整性冲突不穿透成全局处理器的
        # 裸 JSON。转译为 AppError 后走统一收敛；只记异常类名——
        # IntegrityError 的默认 message 携带 SQL 绑定参数（openid/unionid/
        # nickname），带 exc_info 的日志会把它们整体落盘（codex #5）。
        logger.error(
            "wechat oauth callback integrity conflict",
            extra={"request_id": request.state.request_id, "exception_class": type(exc).__name__},
        )
        translated = AppError("AUTH_FAILED", "微信绑定处理异常，请稍后重试", 500)
        translated.__cause__ = exc
        return _redirect_callback_failure(
            translated,
            request=request,
            db=db,
            invite_id=invite_id,
            expected_company_id=expected_company_id,
            return_url=return_url,
        )
    except Exception as exc:
        # P2-4/codex #2：未知异常（OperationalError、ValueError 等）同样
        # 不穿透成 500 裸 JSON——微信浏览器只能看到状态页；异常类名留痕
        # 供排障，服务端观测靠失败审计与这条 error 日志。
        logger.error(
            "wechat oauth callback unexpected failure",
            extra={"request_id": request.state.request_id, "exception_class": type(exc).__name__},
        )
        translated = AppError("AUTH_FAILED", "微信登录处理异常，请稍后重试", 500)
        translated.__cause__ = exc
        return _redirect_callback_failure(
            translated,
            request=request,
            db=db,
            invite_id=invite_id,
            expected_company_id=expected_company_id,
            return_url=return_url,
        )
    response = RedirectResponse(url=return_url, status_code=302)
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        secure=settings.app_env.lower() == "production",
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
        path="/",
    )
    return response
