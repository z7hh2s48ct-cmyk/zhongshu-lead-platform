from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import models_v12 as _models_v12  # noqa: F401
from ..core.models import (
    Assignment,
    Company,
    Lead,
    NotificationOutbox,
    ReturnRequest,
    Role,
    User,
    VerificationTask,
)
from ..core.models_v12 import SupplierLeadReward
from ..core.time import as_utc
from ..core.v12_enums import RewardStatus
from .notification_service import create_station_message, enqueue_outbox
from .company_assignment_v12 import resolve_direct_dispatch_recipients


def build_v12_deep_link(target: str, business_id: str, *, admin: bool = False) -> str:
    allowed = {
        "lead",
        "assignment",
        "return",
        "reward",
        "report",
        "audit",
        "notification",
        "profile",
        "review",
        "dispatch",
        "returns",
        "rewards",
        "telesales",
        "call",
    }
    normalized = target.strip().lower()
    if normalized not in allowed:
        normalized = "notification"
    if normalized == "call":
        return "/h5/call/#/verify"
    normalized = {"return": "returns", "reward": "rewards"}.get(normalized, normalized)
    root = "/admin/v12-operations.html" if admin else "/h5/v12-workbench.html"
    if admin:
        normalized = {"lead": "leads", "review": "leads"}.get(normalized, normalized)
    return f"{root}?{urlencode({'view': normalized, 'id': business_id})}"


def emit_business_notification(
    db: Session,
    *,
    event_key: str,
    event_type: str,
    company_id: str | None,
    title: str,
    body: str,
    target: str,
    business_id: str,
    business_ids: dict[str, str | None] | None = None,
    user_id: str | None = None,
    station_user_ids: set[str | None] | None = None,
    admin: bool = False,
) -> NotificationOutbox | None:
    """Create recipient-scoped station messages and one transactional outbox row.

    `event_key` is the cross-channel idempotency key. Re-running a command,
    retrying a request, or projecting the same domain transition cannot create
    duplicate messages.
    """

    if not company_id and not user_id:
        return None
    existing = db.scalar(
        select(NotificationOutbox).where(NotificationOutbox.event_key == event_key)
    )
    if existing:
        return existing

    deep_link = build_v12_deep_link(target, business_id, admin=admin)
    delivery_user_id = user_id
    if delivery_user_id is None and company_id:
        company = db.get(Company, company_id)
        if company and company.primary_user_id:
            delivery_user_id = company.primary_user_id
        elif company:
            delivery_user_id = db.scalar(
                select(User.id)
                .join(User.roles)
                .where(
                    User.company_id == company_id,
                    User.status == "ACTIVE",
                    Role.code == "FRANCHISE_OWNER",
                )
                .order_by(User.id.asc())
                .limit(1)
            )
    notification = create_station_message(
        db,
        user_id=delivery_user_id,
        company_id=company_id,
        scene=event_type,
        title=title,
        body=body,
        deep_link=deep_link,
    )
    for recipient_user_id in sorted(
        {
            candidate
            for candidate in (station_user_ids or set())
            if candidate and candidate != delivery_user_id
        }
    ):
        create_station_message(
            db,
            user_id=recipient_user_id,
            company_id=company_id,
            scene=event_type,
            title=title,
            body=body,
            deep_link=deep_link,
        )
    payload = {
        "notification_id": notification.id,
        "company_id": company_id,
        "user_id": delivery_user_id,
        "deep_link": deep_link,
        "business_ids": {
            key: value for key, value in (business_ids or {}).items() if value
        },
    }
    return enqueue_outbox(
        db,
        event_key=event_key,
        event_type=event_type,
        aggregate_type=target,
        aggregate_id=business_id,
        payload=payload,
    )


def emit_platform_role_notifications(
    db: Session,
    *,
    event_key: str,
    event_type: str,
    role_codes: set[str],
    title: str,
    body: str,
    target: str,
    business_id: str,
    business_ids: dict[str, str | None] | None = None,
) -> int:
    """Notify active platform staff individually so deep links are permission scoped."""

    users = db.scalars(
        select(User)
        .join(User.roles)
        .where(Role.code.in_(sorted(role_codes)), User.status == "ACTIVE")
        .distinct()
    ).all()
    emitted = 0
    for user in users:
        item = emit_business_notification(
            db,
            event_key=f"{event_key}:user:{user.id}",
            event_type=event_type,
            company_id=None,
            user_id=user.id,
            title=title,
            body=body,
            target=target,
            business_id=business_id,
            business_ids=business_ids,
            admin=True,
        )
        emitted += int(item is not None)
    return emitted


def _notify_reward_state(db: Session, reward: SupplierLeadReward) -> None:
    status = reward.status
    copies: dict[str, tuple[str, str, str]] = {
        RewardStatus.OBSERVING.value: (
            "V12_SUPPLIER_REWARD_OBSERVING",
            "客资奖励进入观察期",
            "客资已被领取，奖励进入 3 个工作日观察期。",
        ),
        RewardStatus.FROZEN.value: (
            "V12_SUPPLIER_REWARD_FROZEN",
            "客资奖励已冻结",
            "接收方已发起退回申诉，奖励将在终审完成前保持冻结。",
        ),
        RewardStatus.SETTLED.value: (
            "V12_SUPPLIER_REWARD_SETTLED",
            "客资奖励已到账",
            f"本次供应商奖励 {int(reward.reward_points)} 积分已结算。",
        ),
        RewardStatus.CANCELLED.value: (
            "V12_SUPPLIER_REWARD_CANCELLED",
            "客资奖励已取消",
            "退回申诉终审通过，本次供应商奖励已取消。",
        ),
        RewardStatus.REVERSED.value: (
            "V12_SUPPLIER_REWARD_REVERSED",
            "客资奖励已冲正",
            "该笔奖励因异常处理已冲正，请进入奖励详情查看原因。",
        ),
    }
    copy = copies.get(status)
    if not copy:
        return
    event_type, title, body = copy
    lead = db.get(Lead, reward.lead_id)
    emit_business_notification(
        db,
        event_key=f"v12:reward:{reward.id}:{status.lower()}",
        event_type=event_type,
        company_id=reward.supplier_company_id,
        station_user_ids={lead.submitter_user_id} if lead else None,
        title=title,
        body=body,
        target="reward",
        business_id=reward.id,
        business_ids={
            "lead_id": reward.lead_id,
            "assignment_id": reward.assignment_id,
            "reward_id": reward.id,
        },
    )
    if status in {
        RewardStatus.FROZEN.value,
        RewardStatus.SETTLED.value,
        RewardStatus.CANCELLED.value,
        RewardStatus.REVERSED.value,
    }:
        emit_platform_role_notifications(
            db,
            event_key=f"v12:reward:{reward.id}:{status.lower()}:platform",
            event_type=event_type,
            # 积分资金的最终处置属于超级管理员职责；旧财务/平台主管角色
            # 已禁止作为业务身份继续接收通知。
            role_codes={"SUPER_ADMIN"},
            title=title,
            body=body,
            target="rewards",
            business_id=reward.id,
            business_ids={
                "lead_id": reward.lead_id,
                "assignment_id": reward.assignment_id,
                "reward_id": reward.id,
            },
        )


def _lead_event_round(
    after: dict[str, Any],
    lead: Lead,
    *,
    timestamp_field: str,
) -> str:
    """Return a short idempotency scope for one submit or review transition."""

    snapshot = after.get("submission_snapshot")
    snapshot_value = snapshot.get(timestamp_field) if isinstance(snapshot, dict) else None
    value = after.get(timestamp_field) or snapshot_value or getattr(lead, timestamp_field, None)
    if isinstance(value, datetime):
        value = as_utc(value).isoformat()
    return sha256(str(value or "initial").encode("utf-8")).hexdigest()[:16]


def _return_event_round(request: ReturnRequest, verification_task_id: str | None) -> str:
    """Keep multi-round return notifications distinct within the outbox key limit."""
    value = f"{request.id}:{verification_task_id or 'unassigned'}"
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def project_v12_notifications(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    company_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Project audited V1.2 state changes into notification/outbox records."""

    if not action.startswith("V12_") or not resource_id:
        return
    after = after or {}
    metadata = metadata or {}

    if action == "V12_SUPPLIER_LEAD_SUBMIT":
        lead = db.get(Lead, resource_id)
        if lead and lead.supplier_company_id:
            event_round = _lead_event_round(after, lead, timestamp_field="submitted_at")
            needs_telesales = str(lead.status).upper() == "PENDING_TELESALES_VERIFY"
            emit_business_notification(
                db,
                event_key=f"v12:lead:{lead.id}:submitted:{event_round}",
                event_type="V12_SUPPLIER_LEAD_SUBMITTED",
                company_id=lead.supplier_company_id,
                station_user_ids={lead.submitter_user_id},
                title="客资已进入电销核实" if needs_telesales else "客资已进入待派发池",
                body=(
                    "平台已收到客资资料，电销核实与运营处置结果会通过消息通知。"
                    if needs_telesales
                    else "手机号和授权已通过基础校验，平台会匹配候选接收公司。"
                ),
                target="lead",
                business_id=lead.id,
                business_ids={"lead_id": lead.id},
            )
            if needs_telesales:
                emit_platform_role_notifications(
                    db,
                    event_key=f"v12:lead:{lead.id}:submitted:{event_round}:platform",
                    event_type="V12_SUPPLIER_LEAD_REVIEW_REQUIRED",
                    role_codes={"OPERATION", "SUPER_ADMIN"},
                    title="有新的加盟商客资待分配电销核实",
                    body="请分配电销人员核对客户意向、资料完整性和服务所在地。",
                    target="telesales",
                    business_id=lead.id,
                    business_ids={"lead_id": lead.id},
                )
            else:
                emit_platform_role_notifications(
                    db,
                    event_key=f"v12:lead:{lead.id}:ready-dispatch:{event_round}:platform",
                    event_type="V12_LEAD_DISPATCH_REQUIRED",
                    role_codes={"OPERATION", "SUPER_ADMIN"},
                    title="有新的加盟商客资待人工派发",
                    body="请进入待派发池选择符合区域、能力、去重和积分条件的接收公司。",
                    target="dispatch",
                    business_id=lead.id,
                    business_ids={"lead_id": lead.id},
                )
        return

    if action == "V12_SUPPLIER_LEAD_REVIEW":
        lead = db.get(Lead, resource_id)
        if lead and lead.supplier_company_id:
            decision = str(after.get("review_decision") or "").upper()
            event_round = _lead_event_round(after, lead, timestamp_field="reviewed_at")
            approved = str(lead.review_status or "").upper() == "APPROVED"
            if decision == "INFO_INCOMPLETE":
                emit_business_notification(
                    db,
                    event_key=f"v12:lead:{lead.id}:review:{event_round}:info-incomplete",
                    event_type="V12_SUPPLIER_LEAD_TELESALES_VERIFY_REQUIRED",
                    company_id=lead.supplier_company_id,
                    station_user_ids={lead.submitter_user_id},
                    title="客资已安排电话核验",
                    body="平台正在核对客户意向和资料完整性，核验结论会同步至供客进度。",
                    target="lead",
                    business_id=lead.id,
                    business_ids={"lead_id": lead.id},
                )
                return
            if decision == "DUPLICATE":
                emit_business_notification(
                    db,
                    event_key=f"v12:lead:{lead.id}:review:{event_round}:duplicate",
                    event_type="V12_SUPPLIER_LEAD_DUPLICATE_REVIEW",
                    company_id=lead.supplier_company_id,
                    station_user_ids={lead.submitter_user_id},
                    title="客资进入重复核查",
                    body="平台正在核对重复记录，处理结果会通过消息通知。",
                    target="lead",
                    business_id=lead.id,
                    business_ids={"lead_id": lead.id},
                )
                return
            event_type = (
                "V12_SUPPLIER_LEAD_APPROVED"
                if approved
                else "V12_SUPPLIER_LEAD_REJECTED"
            )
            emit_business_notification(
                db,
                event_key=f"v12:lead:{lead.id}:review:{event_round}:{str(lead.review_status).lower()}",
                event_type=event_type,
                company_id=lead.supplier_company_id,
                station_user_ids={lead.submitter_user_id},
                title="客资初审已通过" if approved else "客资初审未通过",
                body=(
                    "客资已进入待人工派发池。"
                    if approved
                    else "请进入客资详情查看平台说明并补充资料。"
                ),
                target="lead",
                business_id=lead.id,
                business_ids={"lead_id": lead.id},
            )
            if approved:
                emit_platform_role_notifications(
                    db,
                    event_key=f"v12:lead:{lead.id}:ready-dispatch:{event_round}:platform",
                    event_type="V12_LEAD_DISPATCH_REQUIRED",
                    role_codes={"OPERATION", "SUPER_ADMIN"},
                    title="客资初审通过，等待人工派发",
                    body="请进入待派发池选择符合区域、能力、去重和积分条件的接收公司。",
                    target="dispatch",
                    business_id=lead.id,
                    business_ids={"lead_id": lead.id},
                )
        return

    if action in {
        "V12_COMPANY_CAPABILITY_REVIEW",
        "V12_COMPANY_SERVICE_AREA_REVIEW",
        "V12_COMPANY_PROFILE_BULK_APPROVE",
    }:
        if metadata.get("bulk_profile_approval"):
            return
        company = db.get(Company, company_id) if company_id else None
        if company is None:
            return
        removal = (
            str(after.get("request_type") or "").upper() == "REMOVE"
            or str(after.get("review_note") or "").startswith("[REMOVE_REQUEST]")
        )
        approved = action == "V12_COMPANY_PROFILE_BULK_APPROVE" or (
            not bool(after.get("active")) if removal else bool(after.get("active"))
        )
        event_type = (
            "V12_COMPANY_PROFILE_APPROVED"
            if approved
            else "V12_COMPANY_PROFILE_REJECTED"
        )
        if action == "V12_COMPANY_PROFILE_BULK_APPROVE":
            capability_count = len(after.get("capability_codes") or [])
            area_count = len(after.get("service_area_codes") or [])
            title = "公司接单资料已一次审核通过"
            body = f"已开通 {capability_count} 项客资能力和 {area_count} 个服务区域，可进入派发候选。"
        elif action == "V12_COMPANY_CAPABILITY_REVIEW":
            capability = str(after.get("capability_code") or "客资能力")
            title = "公司客资能力已通过" if approved else "公司客资能力未通过"
            body = (
                f"{capability} 已生效，可在满足其他条件时参与客资流程。"
                if approved
                else f"{capability} 未通过，请查看平台审核说明后调整。"
            )
        else:
            if removal:
                title = "服务区域移除已通过" if approved else "服务区域移除未通过"
                body = (
                    "该服务区域已按申请停止参与客资派发。"
                    if approved
                    else "该服务区域仍保持生效，请查看平台审核说明。"
                )
            else:
                title = "服务区域已通过" if approved else "服务区域未通过"
                body = (
                    "服务区域已生效，可在对应区域参与客资派发。"
                    if approved
                    else "服务区域未通过，请查看平台审核说明后调整。"
                )
        review_round = sha256(
            f"{action}:{resource_id}:{after.get('reviewed_at') or after}".encode("utf-8")
        ).hexdigest()[:16]
        emit_business_notification(
            db,
            event_key=f"v12:company-profile:{company.id}:review:{review_round}",
            event_type=event_type,
            company_id=company.id,
            title=title,
            body=body,
            target="profile",
            business_id=company.id,
            business_ids={
                "company_id": company.id,
                "profile_review_resource_id": resource_id,
            },
        )
        return

    if action == "V12_MANUAL_DISPATCH":
        assignment = db.get(Assignment, resource_id)
        if assignment:
            receiver_company_id = assignment.receiver_company_id or assignment.company_id
            owner_user_id = None
            if assignment.internal_assignee_user_id:
                owner_user_id = resolve_direct_dispatch_recipients(
                    db,
                    company_id=receiver_company_id,
                    employee_user_id=assignment.internal_assignee_user_id,
                ).owner.id
                emit_business_notification(
                    db,
                    event_key=f"v12:assignment:{assignment.id}:dispatched:employee",
                    event_type="V12_ASSIGNMENT_DISPATCHED",
                    company_id=receiver_company_id,
                    user_id=assignment.internal_assignee_user_id,
                    title="新客资已派发给您",
                    body="您有一条新的客资待领取，请在有效期内处理。",
                    target="assignment",
                    business_id=assignment.id,
                    business_ids={
                        "lead_id": assignment.lead_id,
                        "assignment_id": assignment.id,
                    },
                )
            emit_business_notification(
                db,
                event_key=(
                    f"v12:assignment:{assignment.id}:dispatched:owner"
                    if assignment.internal_assignee_user_id
                    else f"v12:assignment:{assignment.id}:dispatched"
                ),
                event_type="V12_ASSIGNMENT_DISPATCHED",
                company_id=receiver_company_id,
                user_id=owner_user_id,
                title="新客资已直派给员工" if assignment.internal_assignee_user_id else "新客资已派发",
                body=(
                    "平台已将一条新客资直接派发给公司员工，请及时关注处理进度。"
                    if assignment.internal_assignee_user_id
                    else "您有一条新的客资待领取，请在有效期内处理。"
                ),
                target="assignment",
                business_id=assignment.id,
                business_ids={
                    "lead_id": assignment.lead_id,
                    "assignment_id": assignment.id,
                },
            )
        return

    if action == "V12_ASSIGNMENT_CLAIM":
        assignment = db.get(Assignment, resource_id)
        if assignment:
            receiver_company_id = assignment.receiver_company_id or assignment.company_id
            emit_business_notification(
                db,
                event_key=f"v12:assignment:{assignment.id}:claimed",
                event_type="V12_ASSIGNMENT_CLAIMED",
                company_id=receiver_company_id,
                station_user_ids={assignment.internal_assignee_user_id},
                title="客资领取成功",
                body="客户联系方式已解锁，请按跟进时限完成首次联系。",
                target="assignment",
                business_id=assignment.id,
                business_ids={
                    "lead_id": assignment.lead_id,
                    "assignment_id": assignment.id,
                },
            )
            reward = db.scalar(
                select(SupplierLeadReward).where(
                    SupplierLeadReward.assignment_id == assignment.id
                )
            )
            if reward:
                _notify_reward_state(db, reward)
        return

    if action == "V12_RETURN_SUBMIT":
        request = db.get(ReturnRequest, resource_id)
        if request:
            event_round = _return_event_round(request, request.verification_task_id)
            emit_business_notification(
                db,
                event_key=f"v12:return:{request.id}:submit:{event_round}",
                event_type="V12_RETURN_SUBMITTED",
                company_id=request.company_id,
                station_user_ids={request.submitted_by},
                title="退回申诉已提交",
                body="申诉已进入后置电销核验，终审结果将通过消息通知。",
                target="return",
                business_id=request.id,
                business_ids={
                    "lead_id": request.lead_id,
                    "assignment_id": request.assignment_id,
                    "return_id": request.id,
                },
            )
            emit_platform_role_notifications(
                db,
                event_key=(
                    f"v12:return:{request.id}:verify:{event_round}:plat"
                ),
                event_type="V12_RETURN_VERIFY_REQUIRED",
                role_codes={"OPERATION", "SUPER_ADMIN"},
                title="有新的退回申诉待后置核验",
                body="请为退回事实核验指定电销人员，并在结论提交后完成终审。",
                target="returns",
                business_id=request.id,
                business_ids={
                    "lead_id": request.lead_id,
                    "assignment_id": request.assignment_id,
                    "return_id": request.id,
                },
            )
            reward = db.scalar(
                select(SupplierLeadReward).where(
                    SupplierLeadReward.assignment_id == request.assignment_id
                )
            )
            if reward:
                _notify_reward_state(db, reward)
        return

    if action in {"V12_PRE_DISPATCH_VERIFY_ASSIGN", "V12_RETURN_VERIFY_ASSIGN"}:
        task = db.get(VerificationTask, resource_id)
        if task and task.assignee_user_id:
            is_pre_dispatch = action == "V12_PRE_DISPATCH_VERIFY_ASSIGN"
            emit_business_notification(
                db,
                event_key=(
                    f"v12:verification:{task.id}:assigned:{task.assignee_user_id}:"
                    f"{task.lock_version}"
                ),
                event_type=(
                    "V12_PRE_DISPATCH_VERIFY_ASSIGNED"
                    if is_pre_dispatch
                    else "V12_RETURN_VERIFY_ASSIGNED"
                ),
                company_id=None,
                user_id=task.assignee_user_id,
                title="有新的前置电话核验任务" if is_pre_dispatch else "有新的退回事实核验任务",
                body="运营人员已派发任务，请在电销工作台开始核验。",
                target="call",
                business_id=task.id,
                business_ids={"lead_id": task.lead_id, "verification_task_id": task.id},
            )
        return

    if action == "V12_PRE_DISPATCH_VERIFY_SUBMIT":
        task = db.get(VerificationTask, resource_id)
        lead = db.get(Lead, task.lead_id) if task else None
        if task and lead:
            source_title = "平台客资" if lead.source_kind == "PLATFORM_MANUAL" else "加盟商客资"
            emit_platform_role_notifications(
                db,
                event_key=f"v12:verification:{task.id}:submitted:{task.lock_version}:platform",
                event_type="V12_PRE_DISPATCH_OPERATION_REQUIRED",
                role_codes={"OPERATION", "SUPER_ADMIN"},
                title=f"{source_title}电话核验已完成，等待运营处置",
                body="请结合电销事实结论决定进入派发池、补充资料、重复处理或关闭。",
                target="telesales",
                business_id=task.id,
                business_ids={"lead_id": lead.id, "verification_task_id": task.id},
            )
        return

    if action == "V12_PRE_DISPATCH_DISPOSITION":
        lead = db.get(Lead, resource_id)
        if lead is None:
            return
        event_round = _lead_event_round(after, lead, timestamp_field="reviewed_at")
        is_platform = lead.source_kind == "PLATFORM_MANUAL"
        source_title = "平台客资" if is_platform else "加盟商客资"
        copy_by_status = {
            "DRAFT": (f"{source_title}需要补充", "请补充资料后重新处理。"),
            "DUPLICATE": (f"{source_title}进入重复处理", "平台正在处理重复记录，请留意后续结果。"),
            "INVALID": (f"{source_title}无法核实", "请查看运营填写的原因，补充资料后可重新提交。"),
            "CLOSED": (f"{source_title}已关闭", "该条客资已完成关闭处置。"),
            "READY_DISPATCH": (f"{source_title}已进入待派发池", "客资将按派发规则匹配接收公司。"),
        }
        title, body = copy_by_status.get(
            str(lead.status).upper(),
            (f"{source_title}已完成运营处置", "请进入客资详情查看最新处理结果。"),
        )
        emit_business_notification(
            db,
            event_key=f"v12:lead:{lead.id}:pre-disposition:{event_round}",
            event_type=(
                "V12_PLATFORM_LEAD_DISPOSITION"
                if is_platform
                else "V12_SUPPLIER_LEAD_DISPOSITION"
            ),
            company_id=None if is_platform else lead.supplier_company_id,
            user_id=lead.submitter_user_id if is_platform else None,
            station_user_ids={lead.submitter_user_id} if not is_platform else None,
            title=title,
            body=body,
            target="lead",
            business_id=lead.id,
            business_ids={"lead_id": lead.id},
            admin=is_platform,
        )
        return

    if action == "V12_RETURN_VERIFY_SUBMIT":
        task_return_id = str(metadata.get("return_request_id") or "")
        request = db.get(ReturnRequest, task_return_id) if task_return_id else None
        if request is None:
            request = db.scalar(
                select(ReturnRequest).where(
                    ReturnRequest.verification_task_id == resource_id
                )
            )
        if request:
            event_round = _return_event_round(request, resource_id)
            emit_platform_role_notifications(
                db,
                event_key=(
                    f"v12:return:{request.id}:finalreq:{event_round}:plat"
                ),
                event_type="V12_RETURN_FINAL_REVIEW_REQUIRED",
                role_codes={"OPERATION", "SUPER_ADMIN"},
                title="退回事实核验已完成，等待终审",
                body="请结合申诉证据和电销核验结论完成平台终审。",
                target="returns",
                business_id=request.id,
                business_ids={
                    "lead_id": request.lead_id,
                    "assignment_id": request.assignment_id,
                    "return_id": request.id,
                    "verification_task_id": resource_id,
                },
            )
        return

    if action == "V12_RETURN_FINAL_REVIEW":
        request = db.get(ReturnRequest, resource_id)
        if request:
            status = str(request.status).upper()
            event_round = _return_event_round(request, request.verification_task_id)
            approved = status == "APPROVED"
            needs_more_evidence = status == "NEED_MORE_EVIDENCE"
            emit_business_notification(
                db,
                event_key=(
                    f"v12:return:{request.id}:final:{status.lower()}:{event_round}"
                ),
                event_type=(
                    "V12_RETURN_APPROVED"
                    if approved
                    else "V12_RETURN_NEED_MORE"
                    if needs_more_evidence
                    else "V12_RETURN_REJECTED"
                ),
                company_id=request.company_id,
                station_user_ids={request.submitted_by},
                title=(
                    "退回申诉终审通过"
                    if approved
                    else "退回申诉需要补证"
                    if needs_more_evidence
                    else "退回申诉终审未通过"
                ),
                body=(
                    "退回已生效，相关积分已按规则处理。"
                    if approved
                    else "请按平台说明补充证据后重新提交，原有证据会保留。"
                    if needs_more_evidence
                    else "客资继续有效，请按业务流程继续跟进。"
                ),
                target="return",
                business_id=request.id,
                business_ids={
                    "lead_id": request.lead_id,
                    "assignment_id": request.assignment_id,
                    "return_id": request.id,
                },
            )
            reward = db.scalar(
                select(SupplierLeadReward).where(
                    SupplierLeadReward.assignment_id == request.assignment_id
                )
            )
            if reward:
                _notify_reward_state(db, reward)
        return

    if action in {
        "V12_SUPPLIER_REWARD_SETTLE",
        "V12_SUPPLIER_REWARD_REVERSE",
    }:
        reward = db.get(SupplierLeadReward, resource_id)
        if reward:
            _notify_reward_state(db, reward)


def drain_due_supplier_reward_settlement_notified(
    db: Session,
    *,
    as_of: datetime | None = None,
    batch_size: int = 500,
    max_batches: int = 20,
    settled_by: str | None = None,
) -> dict[str, Any]:
    """Run the bounded reward drain and project final reward states."""

    from .supplier_reward_v12 import drain_due_supplier_reward_settlement

    now = as_utc(as_of) or datetime.now(timezone.utc)
    safe_limit = max(1, min(int(batch_size), 1000)) * max(
        1, min(int(max_batches), 100)
    )
    candidate_ids = list(
        db.scalars(
            select(SupplierLeadReward.id)
            .where(
                SupplierLeadReward.status == RewardStatus.OBSERVING.value,
                SupplierLeadReward.reward_due_at.is_not(None),
                SupplierLeadReward.reward_due_at <= now,
            )
            .order_by(
                SupplierLeadReward.reward_due_at.asc(),
                SupplierLeadReward.id.asc(),
            )
            .limit(safe_limit)
        ).all()
    )
    result = drain_due_supplier_reward_settlement(
        db,
        as_of=now,
        batch_size=batch_size,
        max_batches=max_batches,
        settled_by=settled_by,
    )
    if candidate_ids:
        rewards = db.scalars(
            select(SupplierLeadReward).where(
                SupplierLeadReward.id.in_(candidate_ids)
            )
        ).all()
        for reward in rewards:
            if reward.status in {
                RewardStatus.FROZEN.value,
                RewardStatus.SETTLED.value,
            }:
                _notify_reward_state(db, reward)
    result["notification_candidates"] = len(candidate_ids)
    return result
