from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Notification, NotificationOutbox
from ..core.responses import ok, page
from ..core.security import scrub_credentials
from ..integrations.wechat import WechatOfficialAccountClient
from ..services.audit import write_audit
from ..services.notification_service import visible_notification_condition
from ..services.outbox_worker import process_outbox

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/gate0")
def gate0(request: Request, principal=Depends(require_permissions("*"))):
    return ok(request, WechatOfficialAccountClient().gate0_diagnostics())


@router.get("")
def list_notifications(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    unread_only: bool = Query(default=False),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    visible = visible_notification_condition(principal)
    stmt = select(Notification).where(visible)
    count_stmt = select(func.count(Notification.id)).where(visible)
    unread_total = db.scalar(
        select(func.count(Notification.id)).where(
            visible,
            Notification.read_at.is_(None),
        )
    ) or 0
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
        count_stmt = count_stmt.where(Notification.read_at.is_(None))
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(Notification.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    data = page([{"id": x.id, "scene": x.scene, "title": x.title, "body": x.body, "deep_link": x.deep_link, "status": x.status, "read_at": x.read_at.isoformat() if x.read_at else None, "created_at": x.created_at.isoformat()} for x in items], total, page_no, page_size)
    data["unread_total"] = int(unread_total)
    return ok(request, data)


@router.post("/read-all")
def mark_all_read(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    visible = visible_notification_condition(principal)
    result = db.execute(
        update(Notification)
        .where(visible, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    marked_count = int(result.rowcount or 0)
    write_audit(
        db,
        principal=principal,
        action="NOTIFICATION_MARK_ALL_READ",
        resource_type="notification",
        after={"marked_count": marked_count},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"marked_count": marked_count, "unread_total": 0})


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    item = db.get(Notification, notification_id)
    is_legacy_company_message = bool(
        item
        and item.user_id is None
        and principal.has_any_role("FRANCHISE_OWNER")
        and item.company_id == principal.company_id
    )
    if not item or item.status == "CANCELLED" or (
        item.user_id != principal.user_id and not is_legacy_company_message
    ) or (item.company_id and item.company_id != principal.company_id):
        raise AppError("NOTIFICATION_NOT_FOUND", "消息不存在", 404)
    if item.read_at is None:
        item.read_at = datetime.now(timezone.utc)
    db.commit()
    return ok(request)


@router.get("/outbox/failed")
def failed_outbox(
    request: Request,
    principal=Depends(require_permissions("notification.retry")),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
):
    # N7：失败面板默认涵盖 FAILED/DEAD/MANUAL_ACTION_REQUIRED——转 DEAD 或
    # 人工终态后从面板消失等于静默丢失运维信号；显式传 status 时保留精确过滤。
    condition = (
        NotificationOutbox.status.in_(["FAILED", "DEAD", "MANUAL_ACTION_REQUIRED"])
        if status is None
        else NotificationOutbox.status == status
    )
    items = db.scalars(select(NotificationOutbox).where(condition).order_by(NotificationOutbox.created_at.desc()).limit(500)).all()
    # N3：存量脏 last_error（脱敏上线前落库）出参前同样打码兜底。
    return ok(request, [{"id": x.id, "event_type": x.event_type, "aggregate_id": x.aggregate_id, "status": x.status, "attempts": x.attempts, "last_error": scrub_credentials(x.last_error), "next_attempt_at": x.next_attempt_at.isoformat() if x.next_attempt_at else None, "created_at": x.created_at.isoformat()} for x in items])


@router.post("/outbox/{outbox_id}/retry")
def retry_outbox(outbox_id: str, request: Request, principal=Depends(require_permissions("notification.retry")), db: Session = Depends(get_db)):
    item = db.scalar(select(NotificationOutbox).where(NotificationOutbox.id == outbox_id).with_for_update())
    if not item:
        raise AppError("OUTBOX_NOT_FOUND", "通知任务不存在", 404)
    if item.status not in {"FAILED", "DEAD", "MANUAL_ACTION_REQUIRED"}:
        raise AppError("OUTBOX_NOT_RETRYABLE", "仅失败的通知支持重新投递", 409)
    item.status = "PENDING"
    item.next_attempt_at = None
    item.last_error = None
    write_audit(db, principal=principal, action="NOTIFICATION_RETRY", resource_type="outbox", resource_id=item.id, request_id=request.state.request_id)
    db.commit()
    return ok(request)


@router.post("/jobs/process-outbox")
def process(request: Request, principal=Depends(require_permissions("*")), db: Session = Depends(get_db), limit: int = Query(default=100, ge=1, le=1000)):
    result = process_outbox(db, limit=limit, commit_batches=True)
    write_audit(db, principal=principal, action="JOB_PROCESS_OUTBOX", resource_type="job", after=result, request_id=request.state.request_id)
    db.commit()
    return ok(request, result)
