from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Company, InviteToken
from ..core.responses import ok
from ..core.security import hash_token
from ..core.time import as_utc, utcnow
from ..schemas.auth import InvitePreviewBody

router = APIRouter(prefix="/auth", tags=["auth"])


def _preview_invite(request: Request, invite: str, db: Session):
    invite_row = db.scalar(select(InviteToken).where(InviteToken.token_hash == hash_token(invite)))
    now = utcnow()
    expires_at = as_utc(invite_row.expires_at) if invite_row else None
    if not invite_row or invite_row.revoked_at or invite_row.used_at or not expires_at or expires_at <= now:
        raise AppError("AUTH_INVITE_INVALID", "邀请已失效，请联系平台", 400)
    company = db.get(Company, invite_row.company_id)
    if not company or company.status != "ACTIVE":
        raise AppError("AUTH_COMPANY_DISABLED", "加盟商公司不可用", 403)
    return ok(
        request,
        {
            "company_name": company.name,
            "owner_name": company.owner_name,
            "purpose": invite_row.purpose,
            "level_code": company.level_code,
            "region_codes": [row.region_code for row in company.service_regions if row.active],
            "capability_codes": [row.category_code for row in company.capabilities if row.active],
            "expires_at": expires_at.isoformat(),
        },
    )


@router.post("/invites/preview")
def invite_preview_post(
    body: InvitePreviewBody,
    request: Request,
    db: Session = Depends(get_db),
):
    return _preview_invite(request, body.invite, db)


@router.get("/invites/preview", deprecated=True)
def invite_preview(
    request: Request,
    invite: str = Query(min_length=16),
    db: Session = Depends(get_db),
):
    return _preview_invite(request, invite, db)
