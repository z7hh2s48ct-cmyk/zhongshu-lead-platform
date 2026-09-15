from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.config import get_settings
from ..core.enums import AssignmentStatus
from ..core.errors import AppError
from ..core.models import Assignment, Lead
from ..core.security import ensure_canonical_jwt

settings = get_settings()

_ALLOWED_LINK_STATUSES = {
    AssignmentStatus.PENDING_CLAIM,
    AssignmentStatus.CLAIMED,
    AssignmentStatus.FOLLOWING,
    AssignmentStatus.RETURN_PENDING,
    AssignmentStatus.COMPLETED,
}


def create_assignment_link_token(
    assignment_id: str,
    company_id: str,
    *,
    expires_hours: int | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    lifetime = expires_hours or max(settings.assignment_expire_hours + 24, 720)
    return jwt.encode(
        {
            "sub": assignment_id,
            "company_id": company_id,
            "aud": "assignment-link",
            "purpose": "assignment-access",
            "jti": uuid4().hex,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(hours=lifetime)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_assignment_link_token(token: str) -> dict[str, Any]:
    try:
        ensure_canonical_jwt(token)
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience="assignment-link",
            options={"require": ["sub", "company_id", "purpose", "jti", "iat", "nbf", "exp"]},
        )
    except InvalidTokenError as exc:
        raise AppError("DEEPLINK_INVALID", "链接已失效，请从最新消息重新进入", 400) from exc
    if payload.get("purpose") != "assignment-access":
        raise AppError("DEEPLINK_INVALID", "链接用途无效", 400)
    return payload


def resolve_assignment_link(db: Session, token: str, principal: Principal) -> Assignment:
    if not principal.company_id:
        raise AppError("COMPANY_CONTEXT_REQUIRED", "当前账号未绑定加盟商公司", 403)
    payload = decode_assignment_link_token(token)
    if payload.get("company_id") != principal.company_id:
        raise AppError("DEEPLINK_COMPANY_MISMATCH", "链接不属于当前加盟商", 403)
    assignment = db.get(Assignment, str(payload["sub"]))
    if not assignment or assignment.company_id != principal.company_id:
        raise AppError("DEEPLINK_ASSIGNMENT_NOT_FOUND", "客资链接对应的订单不存在", 404)
    if assignment.status not in _ALLOWED_LINK_STATUSES:
        raise AppError("DEEPLINK_ASSIGNMENT_INACTIVE", "客资已回收、退回或过期", 409)
    lead = db.get(Lead, assignment.lead_id)
    if not lead or lead.deleted_at is not None or lead.current_assignment_id != assignment.id:
        raise AppError("DEEPLINK_ASSIGNMENT_INACTIVE", "客资已重新分配，请从最新消息进入", 409)
    return assignment
