from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal
from ..core.database import get_db
from ..core.enums import AssignmentStatus
from ..core.errors import AppError
from ..core.models import Assignment, Lead, User
from ..core.security import decrypt_text, mask_phone
from ..services.audit import write_audit

router = APIRouter(prefix="/v1.2", tags=["v1.2-franchise-export"])

# 加盟商端已领取客资导出（2026-09-17 S6）：严格 7 列、顺序固定。
CLAIMED_LEAD_EXPORT_HEADERS = ["姓名", "电话", "地址", "备注", "分配时间", "领取时间", "领取员工"]

CLAIMED_ASSIGNMENT_STATUSES = (
    AssignmentStatus.CLAIMED.value,
    AssignmentStatus.FOLLOWING.value,
    AssignmentStatus.COMPLETED.value,
)

_BEIJING_TZ = timezone(timedelta(hours=8))


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def build_claimed_lead_export_rows(
    db: Session,
    *,
    principal: CurrentPrincipal,
) -> list[tuple[str, ...]]:
    """按访问角色导出已领取客资：负责人=本公司，员工=本人，覆盖全部分页。"""

    if not principal.company_id:
        raise AppError("FORBIDDEN", "仅加盟商账号可导出已领取客资", 403)
    if principal.has_any_role("FRANCHISE_EMPLOYEE") and not principal.can(
        "assignment.own.read"
    ):
        # 员工只导出本人有权查看的已领取客资。
        scope_filters = [
            Assignment.company_id == principal.company_id,
            Assignment.internal_assignee_user_id == principal.user_id,
        ]
    else:
        scope_filters = [Assignment.company_id == principal.company_id]

    rows = db.execute(
        select(Assignment, Lead, User)
        .join(Lead, Lead.id == Assignment.lead_id)
        .outerjoin(User, User.id == Assignment.internal_assignee_user_id)
        .where(
            Lead.deleted_at.is_(None),
            Assignment.status.in_(CLAIMED_ASSIGNMENT_STATUSES),
            *scope_filters,
        )
        .order_by(Assignment.assigned_at.desc(), Assignment.id.desc())
        .limit(20000)
    ).all()

    reveal_phone = principal.can("lead.own.phone.read") or principal.can("*")
    result: list[tuple[str, ...]] = []
    for assignment, lead, assignee in rows:
        phone = decrypt_text(lead.phone_encrypted) or ""
        result.append(
            (
                lead.customer_name or "",
                phone if reveal_phone else mask_phone(phone),
                "".join(part or "" for part in (lead.province, lead.city, lead.district)),
                lead.need_summary or "",
                _format_time(assignment.assigned_at),
                _format_time(assignment.claimed_at),
                (assignee.display_name if assignee else "") or "",
            )
        )
    return result


@router.get("/company/claimed-leads/export")
def export_claimed_leads(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    if not principal.has_any_role("FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"):
        raise AppError("FORBIDDEN", "仅加盟商负责人或员工可导出已领取客资", 403)
    if not (
        principal.can("assignment.own.read")
        or principal.can("assignment.employee.read")
        or principal.can("*")
    ):
        raise AppError("FORBIDDEN", "无权导出已领取客资", 403)
    rows = build_claimed_lead_export_rows(db, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_COMPANY_CLAIMED_LEADS_EXPORT",
        resource_type="company",
        resource_id=principal.company_id,
        metadata={"row_count": len(rows)},
        request_id=request.state.request_id,
    )
    db.commit()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CLAIMED_LEAD_EXPORT_HEADERS)
    writer.writerows(rows)
    payload = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
    filename = f"claimed-leads-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
