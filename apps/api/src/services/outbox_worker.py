from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.models import (
    Company,
    InviteToken,
    Notification,
    NotificationOutbox,
    SystemConfig,
    WechatIdentity,
)
from ..core.security import scrub_credentials
from ..core.time import as_utc
from ..integrations.wechat import WechatOfficialAccountClient

settings = get_settings()
logger = logging.getLogger("zhongshu.outbox")

# N7：确定性投递失败（模板未发布、邀请对象无 openid）重试不可能自愈——
# 直接终态化 MANUAL_ACTION_REQUIRED 交运营兜底，不空转 5 次退避重试污染
# 失败率；运营修好配置/收件人后经重试按钮手动重置 PENDING。
_MANUAL_ACTION_ERROR_CODES = frozenset(
    {"TEMPLATE_NOT_CONFIGURED", "TEMPLATE_CONFIG_INVALID", "NO_RECIPIENT"}
)


def _template_for_scene(db: Session, scene: str) -> dict[str, Any] | None:
    config = db.scalar(select(SystemConfig).where(
        SystemConfig.domain == "wechat_template",
        SystemConfig.key == scene,
        SystemConfig.status == "PUBLISHED",
    ).order_by(SystemConfig.version.desc()))
    return dict(config.value_json) if config and config.value_json.get("template_id") else None


def _ready_for_delivery(now: datetime):
    return or_(
        and_(
            NotificationOutbox.status.in_(["PENDING", "FAILED"]),
            or_(NotificationOutbox.next_attempt_at.is_(None), NotificationOutbox.next_attempt_at <= now),
        ),
        and_(NotificationOutbox.status == "PROCESSING", NotificationOutbox.next_attempt_at <= now),
    )


def process_outbox(db: Session, limit: int = 100, *, commit_batches: bool = False) -> dict[str, int]:
    """Deliver bounded work; job callers explicitly opt into transaction ownership.

    next_attempt_at is a delivery lease while PROCESSING. A terminated worker's
    lease can be reclaimed, while the ordinary API transaction retains its locks.
    External delivery remains at-least-once if its receipt is lost.
    """
    now = datetime.now(timezone.utc)
    row_ids = db.scalars(select(NotificationOutbox.id).where(
        _ready_for_delivery(now),
    ).order_by(NotificationOutbox.created_at, NotificationOutbox.id).limit(limit)).all()
    client = WechatOfficialAccountClient()
    sent = failed = dead = manual = cancelled = processed = 0
    for item_id in row_ids:
        claimed_at = datetime.now(timezone.utc)
        item = db.scalar(select(NotificationOutbox).where(
            NotificationOutbox.id == item_id, _ready_for_delivery(claimed_at),
        ).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if item is None:
            continue
        item.status = "PROCESSING"
        item.attempts += 1
        attempt = item.attempts
        item.next_attempt_at = claimed_at + timedelta(minutes=5)
        db.flush()
        if commit_batches:
            db.commit()
        processed += 1
        try:
            result = _send(db, client, item)
        except Exception as exc:  # final delivery boundary
            result = {"success": False, "error_code": type(exc).__name__, "error_message": scrub_credentials(str(exc))}

        if commit_batches:
            # Do not overwrite a newer consumer's outcome after a lost lease.
            with db.no_autoflush:
                owned = db.scalar(select(NotificationOutbox).where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.status == "PROCESSING",
                    NotificationOutbox.attempts == attempt,
                ).with_for_update().execution_options(populate_existing=True))
            if owned is None:
                db.rollback()
                logger.warning("notification delivery lease lost outbox_id=%s attempt=%s", item_id, attempt)
                continue
            item = owned
        if result["success"]:
            item.status, item.sent_at, item.last_error = "SENT", datetime.now(timezone.utc), None
            item.next_attempt_at = None
            sent += 1
        elif result.get("cancelled"):
            item.status = "CANCELLED"
            item.next_attempt_at = None
            item.last_error = scrub_credentials(str(result.get("error_message") or "通知已取消"))
            cancelled += 1
        elif result.get("error_code") in _MANUAL_ACTION_ERROR_CODES:
            item.last_error = scrub_credentials(f"{result.get('error_code')}: {result.get('error_message')}")
            item.status = "MANUAL_ACTION_REQUIRED"
            item.next_attempt_at = None
            manual += 1
        else:
            item.last_error = scrub_credentials(f"{result.get('error_code')}: {result.get('error_message')}")
            if item.attempts >= 5:
                item.status = "DEAD"
                dead += 1
            else:
                item.status = "FAILED"
                item.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=min(60, 2**item.attempts))
                failed += 1
        if item.status in {"FAILED", "DEAD", "MANUAL_ACTION_REQUIRED"}:
            logger.warning("notification delivery failed outbox_id=%s event_type=%s status=%s error=%s", item.id, item.event_type, item.status, item.last_error)
        if commit_batches:
            db.commit()
    return {
        "processed": processed,
        "sent": sent,
        "failed": failed,
        "dead": dead,
        "manual": manual,
        "cancelled": cancelled,
    }


def _send(db: Session, client: WechatOfficialAccountClient, outbox: NotificationOutbox) -> dict[str, Any]:
    # P2-03：邀请事件发生在负责人绑定微信之前——create_company_invite 拒绝已
    # 绑定公司，所以不存在可解析的主账号；走渠道投递分支而非通用收件人解析。
    if outbox.event_type == "INVITE_CREATED":
        return _send_invite_created(db, client, outbox)
    company_id = outbox.payload.get("company_id")
    user_id = outbox.payload.get("user_id")
    if not user_id and company_id:
        company = db.get(Company, company_id)
        user_id = company.primary_user_id if company else None
    if not user_id:
        return {"success": False, "error_code": "NO_RECIPIENT", "error_message": "未绑定微信负责人"}
    identity = db.scalar(select(WechatIdentity).where(WechatIdentity.user_id == user_id))
    if not identity:
        return {"success": False, "error_code": "NO_WECHAT_IDENTITY", "error_message": "未绑定微信身份"}

    notification = None
    notification_id = outbox.payload.get("notification_id")
    if notification_id:
        notification = db.get(Notification, str(notification_id))
    relative_url = outbox.payload.get("deep_link")
    if not relative_url and outbox.event_type in {"V12_ASSIGNMENT_REMINDER", "V12_ASSIGNMENT_EXPIRED"}:
        relative_url = f"/h5/v12-workbench.html?view=assignments&id={outbox.aggregate_id}"
    if notification is None and relative_url:
        # Historical events must resolve their own business link, never the latest
        # same-scene notification belonging to a different lead.
        notification = db.scalar(select(Notification).where(
            or_(
                Notification.user_id == user_id,
                (Notification.user_id.is_(None)) & (Notification.company_id == company_id),
            ),
            Notification.scene == _scene_from_event(outbox.event_type),
            Notification.deep_link == relative_url,
        ).order_by(Notification.created_at.desc()))

    title = notification.title if notification else _default_title(outbox.event_type)
    body = notification.body if notification else "您有一条业务消息，请点击查看详情。"
    relative_url = notification.deep_link if notification and notification.deep_link else relative_url or "/h5/"
    template = _template_for_scene(db, outbox.event_type)
    result = client.send_scene_message(
        openid=identity.openid,
        scene=outbox.event_type,
        title=title,
        body=body,
        url=settings.app_base_url.rstrip("/") + relative_url,
        template_id=str(template["template_id"]) if template else None,
        field_map=template.get("field_map") if template else None,
    )
    if notification:
        notification.status = "SENT" if result.success else "FAILED"
    return {"success": result.success, "message_id": result.message_id, "error_code": result.error_code, "error_message": result.error_message}


def _send_invite_created(db: Session, client: WechatOfficialAccountClient, outbox: NotificationOutbox) -> dict[str, Any]:
    """P2-03 channel delivery for invite events (recipient not bound yet).

    dev mock renders the template message and succeeds; the real channel
    fails with TEMPLATE_NOT_CONFIGURED until a template_id is published via
    SystemConfig(wechat_template/INVITE_CREATED) — that FAILED row is the
    honest "channel pending" signal, and manual sending remains the fallback.
    The outbox payload never carries the raw invite token.
    """

    invite = db.get(InviteToken, outbox.aggregate_id)
    company = db.get(Company, invite.company_id) if invite else None
    if (
        invite is None
        or company is None
        or company.status != "ACTIVE"
        or invite.used_at is not None
        or invite.revoked_at is not None
        or (as_utc(invite.expires_at) or datetime.min.replace(tzinfo=timezone.utc))
        <= datetime.now(timezone.utc)
    ):
        return {
            "success": False,
            "cancelled": True,
            "error_code": "INVITE_INACTIVE",
            "error_message": "邀请已失效或所属加盟商已停用",
        }

    payload = outbox.payload
    invitee = str(payload.get("invitee_name") or "负责人")
    company_name = str(payload.get("company_name") or "加盟商")
    expires_at = str(payload.get("expires_at") or "")
    template = _template_for_scene(db, outbox.event_type)
    template_id = str(template["template_id"]) if template else None
    # S1：真实通道下收件人仍是占位符——发起 API 只会得到非法 openid 错误并
    # 空转 5 次退避重试后落 DEAD；明确返回 NO_RECIPIENT，运营经创建弹窗人工
    # 发送兜底。未发布模板时仍走 TEMPLATE_NOT_CONFIGURED 的诚实失败。
    if not settings.wechat_dev_mock and template_id:
        return {
            "success": False,
            "error_code": "NO_RECIPIENT",
            "error_message": "邀请对象尚未绑定微信，请运营经创建弹窗手动发送",
        }
    result = client.send_scene_message(
        openid="channel-pending-bind",
        scene=outbox.event_type,
        title=f"微信绑定邀请已生成（{company_name}）",
        body=f"{invitee}的绑定邀请已生成，有效期至 {expires_at[:16].replace('T', ' ')}。请运营通过创建弹窗发送邀请链接。",
        url=settings.app_base_url.rstrip("/") + str(payload.get("deep_link", "/h5/#/login")),
        template_id=template_id,
        field_map=template.get("field_map") if template else None,
    )
    return {"success": result.success, "message_id": result.message_id, "error_code": result.error_code, "error_message": result.error_message}


def _scene_from_event(event_type: str) -> str:
    return {
        "ASSIGNMENT_DISPATCHED": "NEW_LEAD", "ASSIGNMENT_REMINDER": "CLAIM_REMINDER",
        "ASSIGNMENT_CLAIMED": "CLAIM_SUCCESS", "FOLLOWUP_OVERDUE": "FOLLOWUP_OVERDUE",
        "V12_ASSIGNMENT_REMINDER": "V12_CLAIM_REMINDER",
        "RETURN_SUBMITTED": "RETURN_SUBMITTED", "RETURN_APPROVED": "RETURN_APPROVED",
        "RETURN_REJECTED": "RETURN_REJECTED", "RETURN_NEED_MORE": "RETURN_NEED_MORE",
        "POINTS_LOW_BALANCE": "LOW_POINTS", "POINTS_RECHARGED": "POINTS_RECHARGED",
    }.get(event_type, event_type)


def _default_title(event_type: str) -> str:
    return {
        "ASSIGNMENT_DISPATCHED": "新客资已派发", "ASSIGNMENT_REMINDER": "客资即将过期",
        "ASSIGNMENT_CLAIMED": "领取成功", "FOLLOWUP_OVERDUE": "跟进提醒",
        "V12_ASSIGNMENT_REMINDER": "客资即将过期", "V12_ASSIGNMENT_EXPIRED": "客资未领取已回收",
        "RETURN_APPROVED": "退回审核通过", "RETURN_REJECTED": "退回审核未通过",
        "POINTS_LOW_BALANCE": "积分余额不足提醒", "POINTS_RECHARGED": "积分充值到账",
    }.get(event_type, "业务通知")
