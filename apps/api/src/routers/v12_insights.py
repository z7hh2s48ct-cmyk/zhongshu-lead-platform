from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import csv
import hashlib
from io import StringIO
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from ..core import models_v12 as _models_v12  # noqa: F401
from ..core import reward_models_v12 as _reward_models_v12  # noqa: F401
from ..core.auth import CurrentPrincipal, require_permissions
from ..core.config import get_settings
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import (
    Assignment,
    AuditLog,
    Company,
    FollowUp,
    Lead,
    LeadExportTask,
    Notification,
    NotificationOutbox,
    PointsAccount,
    PointsLedger,
    ReturnRequest,
    User,
    VerificationTask,
)
from ..core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12, SupplierLeadReward
from ..core.responses import ok, page
from ..core.security import decrypt_text, mask_phone
from ..core.time import as_utc
from ..core.v12_enums import VerificationTaskType
from ..schemas.v12_reports import LeadExportRequestBody, LeadReportSearchBody
from ..services.audit import write_audit
from ..services.lead_export_v12 import lead_report_to_dicts, list_lead_report_rows
from ..services.return_v12 import return_request_to_dict
from ..services.storage import create_file_access_token, get_storage

router = APIRouter(prefix="/v1.2", tags=["v1.2-reports-audit"])

VERIFICATION_PENDING_LEAD_STATUSES = (
    "PENDING_REVIEW",
    "PENDING_TELESALES_VERIFY",
)
LEAD_EXPORT_ACTIVE_STATUSES = ("PENDING", "RUNNING")
_lead_export_queue_lock = Lock()


class ScopedLeadExportRequestBody(LeadExportRequestBody):
    scope: Literal["ALL_LEADS", "PUBLIC_POOL"] = "ALL_LEADS"
    keyword: str | None = Field(default=None, max_length=128)
    customer_source: Literal["OPERATION_ENTRY", "FRANCHISE_SUPPLIED"] | None = None
    completeness: Literal["COMPLETE", "INCOMPLETE"] | None = None
    duplicate_status: str | None = Field(default=None, max_length=32)

    @field_validator("customer_source", "completeness", mode="before")
    @classmethod
    def normalize_public_pool_choice(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) and value.strip() else None

    @field_validator("keyword", "duplicate_status")
    @classmethod
    def normalize_public_pool_text(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("duplicate_status")
    @classmethod
    def normalize_public_pool_code(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    def filters(self) -> dict[str, Any]:
        if self.scope == "PUBLIC_POOL":
            return {
                "scope": self.scope,
                "created_from": self.created_from.isoformat() if self.created_from else None,
                "created_to": self.created_to.isoformat() if self.created_to else None,
                "submitter_user_id": self.submitter_user_id,
                "keyword": self.keyword,
                "customer_source": self.customer_source,
                "source_kind": self.source_kind,
                "completeness": self.completeness,
                "duplicate_status": self.duplicate_status,
            }
        return {"scope": self.scope, **super().filters()}


@contextmanager
def _lead_export_queue_guard() -> Iterator[None]:
    with _lead_export_queue_lock:
        yield


def _acquire_lead_export_queue_lock(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(b"v12-lead-export-queue").digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_id)))


def _enforce_lead_export_queue_limits(
    db: Session,
    *,
    requested_by: str,
    now: datetime,
) -> None:
    settings = get_settings()
    requester_active = int(
        db.scalar(
            select(func.count(LeadExportTask.id)).where(
                LeadExportTask.requested_by == requested_by,
                LeadExportTask.status.in_(LEAD_EXPORT_ACTIVE_STATUSES),
            )
        )
        or 0
    )
    if requester_active >= settings.lead_export_active_per_user_limit:
        raise AppError(
            "LEAD_EXPORT_ACTIVE_LIMIT",
            "当前账号已有过多导出任务在处理，请完成后再试",
            409,
            {
                "scope": "REQUESTER",
                "active": requester_active,
                "limit": settings.lead_export_active_per_user_limit,
            },
        )

    global_active = int(
        db.scalar(
            select(func.count(LeadExportTask.id)).where(
                LeadExportTask.status.in_(LEAD_EXPORT_ACTIVE_STATUSES)
            )
        )
        or 0
    )
    if global_active >= settings.lead_export_active_global_limit:
        raise AppError(
            "LEAD_EXPORT_ACTIVE_LIMIT",
            "导出队列已满，请稍后再试",
            409,
            {
                "scope": "GLOBAL",
                "active": global_active,
                "limit": settings.lead_export_active_global_limit,
            },
        )

    rolling_start = now - timedelta(hours=24)
    requester_24h = int(
        db.scalar(
            select(func.count(LeadExportTask.id)).where(
                LeadExportTask.requested_by == requested_by,
                LeadExportTask.created_at >= rolling_start,
            )
        )
        or 0
    )
    if requester_24h < settings.lead_export_rolling_24h_per_user_limit:
        return
    earliest = db.scalar(
        select(func.min(LeadExportTask.created_at)).where(
            LeadExportTask.requested_by == requested_by,
            LeadExportTask.created_at >= rolling_start,
        )
    )
    retry_at = (as_utc(earliest) or now) + timedelta(hours=24)
    retry_after = max(1, int((retry_at - now).total_seconds()) + 1)
    raise AppError(
        "LEAD_EXPORT_RATE_LIMIT",
        "24 小时内导出次数已达上限",
        429,
        {
            "scope": "REQUESTER_24H",
            "count": requester_24h,
            "limit": settings.lead_export_rolling_24h_per_user_limit,
            "retry_after_seconds": retry_after,
        },
        {"Retry-After": str(retry_after)},
    )

OPERATION_PROCESSED_ACTIONS_BY_PERMISSION = {
    "lead.edit": frozenset({"LEAD_STAGING_UPDATE", "LEAD_DUPLICATE_DECISION"}),
    "lead.manual.manage": frozenset(
        {
            "V12_PLATFORM_LEAD_DRAFT_CREATE",
            "V12_PLATFORM_LEAD_DRAFT_UPDATE",
            "V12_PLATFORM_LEAD_FACT_CORRECTION",
            "V12_PLATFORM_LEAD_CORRECTION_RECHECK",
            "V12_PLATFORM_LEAD_CORRECTION_REDISPATCH",
            "V12_PLATFORM_LEAD_SUBMIT",
        }
    ),
    "lead.supplier.review": frozenset(
        {
            "V12_SUPPLIER_LEAD_REVIEW",
            "V12_PRE_DISPATCH_DISPOSITION",
        }
    ),
    "lead.dedup.override": frozenset({"V12_DEDUP_OVERRIDE"}),
    "lead.dispatch": frozenset({"LEAD_DISPATCH", "V12_MANUAL_DISPATCH"}),
    "assignment.release": frozenset({"ASSIGNMENT_RELEASE"}),
    "return.review": frozenset({"RETURN_REVIEW", "V12_RETURN_FINAL_REVIEW"}),
    "verification.read": frozenset(
        {
            "VERIFICATION_TASK_CREATE",
            "VERIFICATION_TASK_ASSIGN",
            "VERIFICATION_TASK_RECLAIM",
            "V12_PRE_DISPATCH_VERIFY_ASSIGN",
            "V12_RETURN_VERIFY_ASSIGN",
        }
    ),
    "company.profile.review": frozenset(
        {
            "COMPANY_CREATE",
            "COMPANY_SIMPLE_CREATE",
            "COMPANY_UPDATE",
            "V12_COMPANY_PROFILE_BULK_APPROVE",
            "V12_COMPANY_CAPABILITY_REVIEW",
            "V12_COMPANY_SERVICE_AREA_REVIEW",
            "V12_COMPANY_CAPABILITY_CONFIGURE",
            "V12_COMPANY_SERVICE_AREAS_CONFIGURE",
        }
    ),
    "company.account.manage": frozenset(
        {
            "COMPANY_WECHAT_UNBIND",
            "INVITE_CREATE",
            "INVITE_REVOKE",
            "COMPANY_ACCOUNT_REQUEST_APPROVE",
            "COMPANY_ACCOUNT_REQUEST_REJECT",
            "COMPANY_ACCOUNT_CREATE",
            "COMPANY_ACCOUNT_ENABLE",
            "COMPANY_ACCOUNT_DISABLE",
            "COMPANY_ACCOUNT_PASSWORD_RESET",
        }
    ),
}

OPERATION_PROCESSED_ACTION_PRIORITY = {
    action: 10
    for actions in OPERATION_PROCESSED_ACTIONS_BY_PERMISSION.values()
    for action in actions
}
OPERATION_PROCESSED_ACTION_PRIORITY.update(
    {
        "V12_MANUAL_DISPATCH": 1,
        "V12_COMPANY_PROFILE_BULK_APPROVE": 1,
        "V12_PRE_DISPATCH_VERIFY_ASSIGN": 20,
        "V12_RETURN_VERIFY_ASSIGN": 20,
    }
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _safe_csv_cell(value: Any) -> str:
    """Prevent spreadsheet applications from evaluating exported user text."""

    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return f"'{text}"
    return text


def _latest_datetime(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    if not present:
        return None

    def sort_key(value: datetime) -> float:
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.timestamp()

    return max(present, key=sort_key)


def _time(column, start: datetime | None, end: datetime | None) -> list[Any]:
    return ([column >= start] if start else []) + ([column <= end] if end else [])


def _validated_query_time_range(
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    for value in (start, end):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise AppError(
                "DATE_TIMEZONE_REQUIRED",
                "日期时间必须包含时区，例如 +08:00",
                422,
            )
    normalized_start = start.astimezone(timezone.utc) if start else None
    normalized_end = end.astimezone(timezone.utc) if end else None
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise AppError("DATE_RANGE_INVALID", "起始时间不能晚于结束时间", 422)
    return normalized_start, normalized_end


def _period_window(period: str | None) -> tuple[str, datetime, datetime]:
    normalized = (period or "month").strip().lower()
    days_by_period = {"day": 1, "week": 7, "month": 30}
    if normalized not in days_by_period:
        raise AppError("PERIOD_INVALID", "统计周期仅支持 day/week/month", 422)
    now = datetime.now(timezone.utc)
    return normalized, now - timedelta(days=days_by_period[normalized]), now


def _counts(db: Session, model, filters: list[Any]) -> dict[str, int]:
    rows = db.execute(select(model.status, func.count(model.id)).where(*filters).group_by(model.status)).all()
    return {str(status): int(count) for status, count in rows}


def _summary(db: Session, model, filters: list[Any]) -> dict[str, Any]:
    return {
        "total": int(db.scalar(select(func.count(model.id)).where(*filters)) or 0),
        "by_status": _counts(db, model, filters),
    }


def _exception_breakdown(
    db: Session,
    *,
    assignment_filters: list[Any],
    return_filters: list[Any],
) -> dict[str, int]:
    """Keep the three user-facing rejection concepts separate."""

    return_requested_statuses = (
        "SUBMITTED",
        "VERIFYING",
        "REVIEWING",
        "NEED_MORE_EVIDENCE",
        "APPROVED",
        "REJECTED",
    )
    return {
        "refused_claim": int(
            db.scalar(
                select(func.count(Assignment.id)).where(
                    *assignment_filters,
                    Assignment.status == "RELEASED",
                    Assignment.release_reason == "REFUSED_CLAIM",
                )
            )
            or 0
        ),
        "return_requested": int(
            db.scalar(
                select(func.count(ReturnRequest.id)).where(
                    *return_filters,
                    ReturnRequest.status.in_(return_requested_statuses),
                )
            )
            or 0
        ),
        "confirmed_invalid": int(
            db.scalar(
                select(func.count(ReturnRequest.id)).where(
                    *return_filters,
                    ReturnRequest.status == "APPROVED",
                )
            )
            or 0
        ),
    }


def _consumed_points(db: Session, filters: list[Any]) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(-PointsLedger.delta), 0)).where(
                PointsLedger.ledger_type == "CLAIM",
                PointsLedger.delta < 0,
                *filters,
            )
        )
        or 0
    )


def _count_subquery(model, *filters: Any):
    return select(func.count(model.id)).where(*filters).scalar_subquery()


def _management_overview(db: Session, principal: CurrentPrincipal) -> dict[str, Any]:
    """Return bounded operational counts for the two desktop platform homepages."""

    now = datetime.now(timezone.utc)
    verification_open = ("PENDING", "ASSIGNED", "IN_PROGRESS")
    problem_lead_statuses = (
        "DRAFT",
        *VERIFICATION_PENDING_LEAD_STATUSES,
        "PENDING_OPERATION_DISPOSITION",
        "DUPLICATE",
        "INVALID",
    )
    metrics = [
        _count_subquery(Lead, Lead.source_kind.is_not(None)).label("lead_total"),
        _count_subquery(Lead, Lead.status == "READY_DISPATCH").label("lead_unassigned"),
        _count_subquery(Assignment, Assignment.status == "PENDING_CLAIM").label("lead_dispatching"),
        _count_subquery(Lead, Lead.status.in_(problem_lead_statuses)).label("lead_problem"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status.in_(("PENDING", "ASSIGNED")),
        ).label("verification_pending"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status == "IN_PROGRESS",
        ).label("verification_in_progress"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status == "SUBMITTED",
        ).label("verification_awaiting_operation"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.PRE_DISPATCH_VERIFY.value,
            VerificationTask.status.in_(verification_open),
            VerificationTask.due_at.is_not(None),
            VerificationTask.due_at < now,
        ).label("verification_overdue"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value,
            VerificationTask.status.in_(("PENDING", "ASSIGNED")),
        ).label("return_verification_pending"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value,
            VerificationTask.status == "IN_PROGRESS",
        ).label("return_verification_in_progress"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value,
            VerificationTask.status == "SUBMITTED",
        ).label("return_verification_awaiting_operation"),
        _count_subquery(
            VerificationTask,
            VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value,
            VerificationTask.status.in_(verification_open),
            VerificationTask.due_at.is_not(None),
            VerificationTask.due_at < now,
        ).label("return_verification_overdue"),
        _count_subquery(ReturnRequest, ReturnRequest.status == "REVIEWING").label("return_final_review"),
        _count_subquery(
            CompanyLeadCapability,
            CompanyLeadCapability.review_status == "PENDING",
        ).label("company_capability_review"),
        _count_subquery(
            CompanyServiceAreaV12,
            CompanyServiceAreaV12.review_status == "PENDING",
        ).label("company_area_review"),
        _count_subquery(
            NotificationOutbox,
            NotificationOutbox.status.in_(("FAILED", "DEAD", "MANUAL_ACTION_REQUIRED")),
        ).label("failed_notification"),
        _count_subquery(Company, Company.status == "DISABLED").label("disabled_company"),
    ]
    if principal.can("points.read") or principal.can("*"):
        metrics.append(
            _count_subquery(
                SupplierLeadReward,
                SupplierLeadReward.status == "FROZEN",
            ).label("frozen_reward")
        )
    totals = db.execute(select(*metrics)).one()
    result = {
        "lead_pool": {
            "total": int(totals.lead_total),
            "unassigned": int(totals.lead_unassigned),
            "dispatching": int(totals.lead_dispatching),
            "problem": int(totals.lead_problem),
        },
        "verification": {
            "pending": int(totals.verification_pending),
            "in_progress": int(totals.verification_in_progress),
            "awaiting_operation": int(totals.verification_awaiting_operation),
            "overdue": int(totals.verification_overdue),
        },
        "return_verification": {
            "pending": int(totals.return_verification_pending),
            "in_progress": int(totals.return_verification_in_progress),
            "awaiting_operation": int(totals.return_verification_awaiting_operation),
            "overdue": int(totals.return_verification_overdue),
        },
        "exceptions": {
            "return_final_review": int(totals.return_final_review),
            "company_review": int(totals.company_capability_review) + int(totals.company_area_review),
            "failed_notification": int(totals.failed_notification),
            "disabled_company": int(totals.disabled_company),
        },
    }
    if principal.can("points.read") or principal.can("*"):
        result["funds"] = {
            "frozen_reward": int(totals.frozen_reward),
        }
    return result


@router.get("/reports/overview")
def overview(
    request: Request,
    principal=Depends(require_permissions("report.v12.read")),
    db: Session = Depends(get_db),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    company_id: str | None = Query(default=None),
):
    lead_f = [Lead.source_kind.is_not(None), *_time(Lead.created_at, created_from, created_to)]
    assignment_f = _time(Assignment.created_at, created_from, created_to)
    return_f = _time(ReturnRequest.created_at, created_from, created_to)
    reward_f = _time(SupplierLeadReward.created_at, created_from, created_to)
    points_f = [PointsLedger.business_type.like("V12_%"), *_time(PointsLedger.created_at, created_from, created_to)]
    if company_id:
        lead_f.append(Lead.supplier_company_id == company_id)
        assignment_f.append(or_(Assignment.company_id == company_id, Assignment.receiver_company_id == company_id, Assignment.supplier_company_id == company_id))
        return_f.append(ReturnRequest.company_id == company_id)
        reward_f.append(or_(SupplierLeadReward.supplier_company_id == company_id, SupplierLeadReward.receiver_company_id == company_id))
        points_f.append(PointsLedger.company_id == company_id)
    rewards = _summary(db, SupplierLeadReward, reward_f)
    rewards["points"] = int(db.scalar(select(func.coalesce(func.sum(SupplierLeadReward.reward_points), 0)).where(*reward_f)) or 0)
    data = {
        "scope": {"company_id": company_id, "created_from": _iso(created_from), "created_to": _iso(created_to)},
        "leads": _summary(db, Lead, lead_f),
        "assignments": _summary(db, Assignment, assignment_f),
        "returns": _summary(db, ReturnRequest, return_f),
        "exception_breakdown": _exception_breakdown(
            db,
            assignment_filters=assignment_f,
            return_filters=return_f,
        ),
        "supplier_rewards": rewards,
        "management": _management_overview(db, principal),
    }
    if any(principal.can(code) for code in ("*", "points.read", "dashboard.finance.read")):
        points = db.execute(
            select(func.count(PointsLedger.id), func.coalesce(func.sum(PointsLedger.delta), 0)).where(*points_f)
        ).one()
        data["points_ledger"] = {"count": int(points[0]), "net_delta": int(points[1])}
    return ok(request, data)


@router.get("/reports/management-dashboard")
def management_dashboard(
    request: Request,
    principal=Depends(require_permissions("report.v12.read")),
    db: Session = Depends(get_db),
    days: int = Query(default=30, ge=7, le=365),
):
    """Return the compact decision view used by the platform operating overview."""

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days - 1)
    v12_lead = Lead.source_kind.is_not(None)
    claimed_statuses = ("CLAIMED", "FOLLOWING", "RETURN_PENDING", "COMPLETED")
    active_return_statuses = ("VERIFYING", "REVIEWING", "NEED_MORE_EVIDENCE")
    verified_lead_statuses = ("DRAFT", *VERIFICATION_PENDING_LEAD_STATUSES)
    pending_reward_points = (
        select(func.coalesce(func.sum(SupplierLeadReward.reward_points), 0))
        .where(SupplierLeadReward.status == "OBSERVING")
        .scalar_subquery()
    )
    totals = db.execute(
        select(
            _count_subquery(Lead, v12_lead, Lead.created_at >= start).label("new_leads"),
            _count_subquery(
                Lead,
                v12_lead,
                Lead.status.in_(VERIFICATION_PENDING_LEAD_STATUSES),
            ).label("pending_verification"),
            _count_subquery(Lead, v12_lead, Lead.status == "READY_DISPATCH").label("ready_dispatch"),
            _count_subquery(Lead, v12_lead).label("submitted"),
            _count_subquery(
                Lead,
                v12_lead,
                Lead.status.not_in(verified_lead_statuses),
            ).label("verified"),
            _count_subquery(Lead, v12_lead, Lead.status == "PENDING_OPERATION_DISPOSITION").label(
                "awaiting_operation"
            ),
            _count_subquery(Assignment).label("dispatched"),
            _count_subquery(Assignment, Assignment.status.in_(claimed_statuses)).label("claimed"),
            _count_subquery(Assignment, Assignment.status == "COMPLETED").label("completed"),
            _count_subquery(ReturnRequest, ReturnRequest.status.in_(active_return_statuses)).label(
                "return_exceptions"
            ),
            _count_subquery(SupplierLeadReward, SupplierLeadReward.status == "OBSERVING").label(
                "pending_reward_count"
            ),
            pending_reward_points.label("pending_reward_points"),
        )
    ).one()

    completed_leads = (
        select(Assignment.lead_id.label("lead_id"))
        .where(Assignment.status == "COMPLETED")
        .distinct()
        .subquery()
    )

    trend_rows = {
        (start + timedelta(days=offset)).date().isoformat(): {
            "date": (start + timedelta(days=offset)).date().isoformat(),
            "new_leads": 0,
            "effective_completed": 0,
        }
        for offset in range(days)
    }
    daily_rows = db.execute(
        select(
            func.date(Lead.created_at).label("day"),
            func.count(Lead.id).label("new_leads"),
            func.count(completed_leads.c.lead_id).label("effective_completed"),
        )
        .select_from(Lead)
        .outerjoin(completed_leads, completed_leads.c.lead_id == Lead.id)
        .where(v12_lead, Lead.created_at >= start)
        .group_by(func.date(Lead.created_at))
    ).all()
    for row in daily_rows:
        key = row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day)
        if key in trend_rows:
            trend_rows[key]["new_leads"] = int(row.new_leads)
            trend_rows[key]["effective_completed"] = int(row.effective_completed)
    trend = [
        {
            **item,
            "effective_rate": round(
                item["effective_completed"] / item["new_leads"] * 100, 1
            )
            if item["new_leads"]
            else 0,
        }
        for item in trend_rows.values()
    ]

    source_names = {
        "PLATFORM_MANUAL": "平台录入",
        "FEISHU_IMPORT": "飞书导入",
        "SUPPLIER_H5": "加盟商提供",
    }

    def grouped_distribution(key_expression) -> list[Any]:
        lead_count = func.count(Lead.id)
        return db.execute(
            select(
                key_expression.label("key"),
                lead_count.label("leads"),
                func.count(completed_leads.c.lead_id).label("completed"),
            )
            .select_from(Lead)
            .outerjoin(completed_leads, completed_leads.c.lead_id == Lead.id)
            .where(v12_lead, Lead.created_at >= start)
            .group_by(key_expression)
            .order_by(lead_count.desc(), key_expression.asc())
            .limit(8)
        ).all()

    source_rows = grouped_distribution(func.coalesce(Lead.source_kind, "UNKNOWN"))
    region_rows = grouped_distribution(func.coalesce(Lead.city, "未填写地区"))
    provider_rows = grouped_distribution(func.coalesce(Lead.supplier_company_id, "PLATFORM"))
    provider_ids = [str(row.key) for row in provider_rows if row.key != "PLATFORM"]
    company_names = dict(
        db.execute(select(Company.id, Company.name).where(Company.id.in_(provider_ids))).all()
    ) if provider_ids else {}

    def distribution(rows: list[Any], labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
        items = []
        for row in rows:
            key = str(row.key)
            leads_count = int(row.leads)
            completed_count = int(row.completed)
            label = labels.get(key, "其他来源") if labels is not None else key
            items.append(
                {
                    "key": key,
                    "label": label,
                    "leads": leads_count,
                    "completed": completed_count,
                    "effective_rate": round(completed_count / leads_count * 100, 1)
                    if leads_count
                    else 0,
                }
            )
        return items

    provider_labels = {"PLATFORM": "平台运营", **company_names}
    claimed = int(totals.claimed)
    completed = int(totals.completed)
    return_exceptions = int(totals.return_exceptions)

    data = {
        "period": {"days": days, "from": start.date().isoformat(), "to": now.date().isoformat()},
        "kpis": {
            "new_leads": int(totals.new_leads),
            "pending_verification": int(totals.pending_verification),
            "ready_dispatch": int(totals.ready_dispatch),
            "claimed": claimed,
            "effective_completed": completed,
            "effective_completion_rate": round(completed / claimed * 100, 1) if claimed else 0,
            "returned_exceptions": return_exceptions,
            "pending_reward_settlement": {
                "count": int(totals.pending_reward_count),
                "points": int(totals.pending_reward_points),
            },
        },
        "trend": trend,
        "funnel": [
            {"key": "submitted", "label": "录入", "value": int(totals.submitted)},
            {"key": "verified", "label": "核实", "value": int(totals.verified)},
            {"key": "dispatched", "label": "派送", "value": int(totals.dispatched)},
            {"key": "claimed", "label": "领取", "value": claimed},
            {"key": "completed", "label": "确认完成", "value": completed},
        ],
        "source_distribution": distribution(source_rows, source_names),
        "region_distribution": distribution(region_rows),
        "provider_distribution": distribution(provider_rows, provider_labels),
        "exceptions": [
            {"label": "待退回终审", "count": return_exceptions, "view": "returns"},
            {"label": "待运营处置", "count": int(totals.awaiting_operation), "view": "telesales"},
            {"label": "待派发", "count": int(totals.ready_dispatch), "view": "dispatch"},
        ],
    }
    return ok(request, data)


@router.get("/reports/finance-dashboard")
def finance_dashboard(
    request: Request,
    principal=Depends(require_permissions("report.v12.read")),
    db: Session = Depends(get_db),
    days: int = Query(default=30, ge=7, le=365),
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
):
    """Return finance summaries first; the existing finance pages remain the drill-down surface."""

    if not (principal.can("*") or principal.can("points.read") or principal.can("dashboard.finance.read")):
        raise AppError("FORBIDDEN", "无权查看资金经营看板", 403)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days - 1)
    normalized_status = status.strip().upper() if status else None
    normalized_source = source.strip().upper() if source else None
    reward_filters: list[Any] = [SupplierLeadReward.created_at >= start]
    if normalized_status:
        reward_filters.append(SupplierLeadReward.status == normalized_status)
    if normalized_source:
        reward_filters.append(Lead.source_kind == normalized_source)

    reward_status_rows = db.execute(
        select(
            SupplierLeadReward.status,
            func.count(SupplierLeadReward.id).label("count"),
            func.coalesce(func.sum(SupplierLeadReward.reward_points), 0).label("points"),
        )
        .select_from(SupplierLeadReward)
        .join(Lead, Lead.id == SupplierLeadReward.lead_id)
        .where(*reward_filters)
        .group_by(SupplierLeadReward.status)
    ).all()
    by_status = {
        item: {"count": 0, "points": 0}
        for item in ("OBSERVING", "SETTLED", "FROZEN", "CANCELLED", "REVERSED")
    }
    for row in reward_status_rows:
        if row.status in by_status:
            by_status[row.status] = {"count": int(row.count), "points": int(row.points)}

    reward_trend_rows = db.execute(
        select(
            func.date(SupplierLeadReward.created_at).label("day"),
            func.coalesce(
                func.sum(
                    case(
                        (SupplierLeadReward.status == "OBSERVING", SupplierLeadReward.reward_points),
                        else_=0,
                    )
                ),
                0,
            ).label("pending_points"),
            func.coalesce(
                func.sum(
                    case(
                        (SupplierLeadReward.status == "SETTLED", SupplierLeadReward.reward_points),
                        else_=0,
                    )
                ),
                0,
            ).label("settled_points"),
        )
        .select_from(SupplierLeadReward)
        .join(Lead, Lead.id == SupplierLeadReward.lead_id)
        .where(*reward_filters)
        .group_by(func.date(SupplierLeadReward.created_at))
        .order_by(func.date(SupplierLeadReward.created_at))
    ).all()
    reward_trend = [
        {
            "date": row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day),
            "pending_points": int(row.pending_points),
            "settled_points": int(row.settled_points),
        }
        for row in reward_trend_rows
    ]

    ranking_points = func.coalesce(func.sum(SupplierLeadReward.reward_points), 0)
    ranking_rows = db.execute(
        select(
            SupplierLeadReward.supplier_company_id.label("company_id"),
            func.coalesce(Company.name, "加盟商").label("label"),
            func.count(SupplierLeadReward.id).label("rewards"),
            ranking_points.label("points"),
            func.coalesce(
                func.sum(
                    case(
                        (SupplierLeadReward.status == "SETTLED", SupplierLeadReward.reward_points),
                        else_=0,
                    )
                ),
                0,
            ).label("settled_points"),
        )
        .select_from(SupplierLeadReward)
        .join(Lead, Lead.id == SupplierLeadReward.lead_id)
        .outerjoin(Company, Company.id == SupplierLeadReward.supplier_company_id)
        .where(*reward_filters)
        .group_by(SupplierLeadReward.supplier_company_id, Company.name)
        .order_by(ranking_points.desc(), func.coalesce(Company.name, "加盟商").asc())
        .limit(10)
    ).all()
    source_ranking = [
        {
            "company_id": row.company_id,
            "label": row.label,
            "rewards": int(row.rewards),
            "points": int(row.points),
            "settled_points": int(row.settled_points),
        }
        for row in ranking_rows
    ]

    reward_detail_rows = db.execute(
        select(
            SupplierLeadReward.id,
            SupplierLeadReward.status,
            SupplierLeadReward.reward_points,
            SupplierLeadReward.created_at,
            Lead.source_kind.label("source"),
            func.coalesce(Company.name, "加盟商").label("provider"),
        )
        .select_from(SupplierLeadReward)
        .join(Lead, Lead.id == SupplierLeadReward.lead_id)
        .outerjoin(Company, Company.id == SupplierLeadReward.supplier_company_id)
        .where(*reward_filters)
        .order_by(SupplierLeadReward.created_at.desc())
        .limit(20)
    ).all()

    recharge = aliased(PointsLedger, name="recharge")
    reversal_totals = (
        select(
            PointsLedger.related_ledger_id.label("original_id"),
            func.coalesce(func.sum(PointsLedger.delta), 0).label("reversal_delta"),
            func.count(PointsLedger.id).label("reversal_count"),
            func.max(PointsLedger.id).label("reversal_ledger_id"),
            func.max(PointsLedger.created_at).label("reversed_at"),
        )
        .where(
            PointsLedger.ledger_type == "REVERSAL",
            PointsLedger.related_ledger_id.is_not(None),
        )
        .group_by(PointsLedger.related_ledger_id)
        .subquery()
    )
    reversal_delta = func.coalesce(reversal_totals.c.reversal_delta, 0)
    net_recharge_points = recharge.delta + reversal_delta
    is_active_recharge = reversal_totals.c.original_id.is_(None)
    in_period = recharge.created_at >= start
    recharge_summary = db.execute(
        select(
            func.coalesce(func.sum(case((in_period, recharge.delta), else_=0)), 0).label(
                "period_gross_points"
            ),
            func.coalesce(func.sum(case((in_period, -reversal_delta), else_=0)), 0).label(
                "period_reversed_points"
            ),
            func.coalesce(func.sum(case((in_period, net_recharge_points), else_=0)), 0).label(
                "period_net_points"
            ),
            func.coalesce(func.sum(case((in_period, 1), else_=0)), 0).label("period_gross_count"),
            func.coalesce(
                func.sum(case((and_(in_period, is_active_recharge), 1), else_=0)),
                0,
            ).label("period_active_count"),
            func.coalesce(
                func.sum(case((and_(in_period, ~is_active_recharge), 1), else_=0)),
                0,
            ).label("period_reversal_count"),
            func.coalesce(func.sum(recharge.delta), 0).label("total_gross_points"),
            func.coalesce(func.sum(-reversal_delta), 0).label("total_reversed_points"),
            func.coalesce(func.sum(net_recharge_points), 0).label("total_net_points"),
            func.count(recharge.id).label("total_gross_count"),
            func.coalesce(func.sum(case((is_active_recharge, 1), else_=0)), 0).label(
                "total_active_count"
            ),
            func.coalesce(func.sum(case((~is_active_recharge, 1), else_=0)), 0).label(
                "total_reversal_count"
            ),
        )
        .select_from(recharge)
        .outerjoin(reversal_totals, reversal_totals.c.original_id == recharge.id)
        .where(recharge.ledger_type == "RECHARGE")
    ).one()

    recharge_trend_rows = db.execute(
        select(
            func.date(recharge.created_at).label("day"),
            func.coalesce(func.sum(net_recharge_points), 0).label("points"),
            func.coalesce(func.sum(case((is_active_recharge, 1), else_=0)), 0).label("count"),
            func.coalesce(func.sum(recharge.delta), 0).label("gross_points"),
            func.coalesce(func.sum(-reversal_delta), 0).label("reversed_points"),
        )
        .select_from(recharge)
        .outerjoin(reversal_totals, reversal_totals.c.original_id == recharge.id)
        .where(recharge.ledger_type == "RECHARGE", in_period)
        .group_by(func.date(recharge.created_at))
        .order_by(func.date(recharge.created_at))
    ).all()
    recharge_trend = [
        {
            "date": row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day),
            "points": int(row.points),
            "count": int(row.count),
            "gross_points": int(row.gross_points),
            "reversed_points": int(row.reversed_points),
        }
        for row in recharge_trend_rows
    ]

    recent_recharges = db.execute(
        select(
            recharge.id,
            recharge.company_id,
            func.coalesce(Company.name, "加盟商").label("company_name"),
            recharge.delta.label("original_points"),
            net_recharge_points.label("points"),
            recharge.balance_after,
            recharge.external_reference,
            recharge.created_at,
            reversal_totals.c.reversal_ledger_id,
            reversal_totals.c.reversed_at,
        )
        .select_from(recharge)
        .outerjoin(reversal_totals, reversal_totals.c.original_id == recharge.id)
        .outerjoin(Company, Company.id == recharge.company_id)
        .where(recharge.ledger_type == "RECHARGE")
        .order_by(recharge.created_at.desc())
        .limit(20)
    ).all()
    remaining_points = int(db.scalar(select(func.coalesce(func.sum(PointsAccount.balance), 0))) or 0)
    return ok(request, {
        "period": {"days": days, "from": start.date().isoformat(), "to": now.date().isoformat()},
        "filters": {"status": normalized_status, "source": normalized_source},
        "summary": {
            "pending_settlement": by_status["OBSERVING"],
            "settled": by_status["SETTLED"],
            "disputed": by_status["FROZEN"],
            "voided": {
                "count": by_status["CANCELLED"]["count"] + by_status["REVERSED"]["count"],
                "points": by_status["CANCELLED"]["points"] + by_status["REVERSED"]["points"],
            },
        },
        "trend": reward_trend,
        "source_ranking": source_ranking,
        "recharge_summary": {
            "period_recharged_points": int(recharge_summary.period_net_points),
            "period_recharge_count": int(recharge_summary.period_active_count),
            "period_gross_recharged_points": int(recharge_summary.period_gross_points),
            "period_reversed_recharge_points": int(recharge_summary.period_reversed_points),
            "period_net_recharged_points": int(recharge_summary.period_net_points),
            "period_gross_recharge_count": int(recharge_summary.period_gross_count),
            "period_reversal_count": int(recharge_summary.period_reversal_count),
            "total_recharged_points": int(recharge_summary.total_net_points),
            "total_recharge_count": int(recharge_summary.total_active_count),
            "total_gross_recharged_points": int(recharge_summary.total_gross_points),
            "total_reversed_recharge_points": int(recharge_summary.total_reversed_points),
            "total_net_recharged_points": int(recharge_summary.total_net_points),
            "total_gross_recharge_count": int(recharge_summary.total_gross_count),
            "total_reversal_count": int(recharge_summary.total_reversal_count),
            "remaining_points": remaining_points,
        },
        "recharge": {
            "trend": recharge_trend,
            "recent_records": [
                {
                    "id": item.id,
                    "company_id": item.company_id,
                    "company_name": item.company_name,
                    "original_points": int(item.original_points),
                    "points": int(item.points),
                    "balance_after": int(item.balance_after),
                    "external_reference": item.external_reference,
                    "created_at": _iso(item.created_at),
                    "reversed": item.reversed_at is not None,
                    "reversal_ledger_id": item.reversal_ledger_id,
                    "reversed_at": _iso(item.reversed_at),
                }
                for item in recent_recharges
            ],
        },
        "details": [
            {
                "id": row.id,
                "status": row.status,
                "points": int(row.reward_points or 0),
                "source": row.source,
                "provider": row.provider,
                "created_at": _iso(row.created_at),
            }
            for row in reward_detail_rows
        ],
    })


@router.get("/reports/own")
def own_report(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    period: str | None = Query(default="month"),
):
    if not principal.company_id:
        raise AppError("COMPANY_CONTEXT_REQUIRED", "当前账号未绑定公司", 403)
    if not any(principal.can(code) for code in ("*", "assignment.own.read", "assignment.employee.read", "supplier.lead.manage", "supplier.reward.own.read", "points.own.read")):
        raise AppError("FORBIDDEN", "无权查看公司业务报表", 403)
    company_id = principal.company_id
    normalized_period, period_start, period_end = _period_window(period)
    employee_scope = principal.has_any_role("FRANCHISE_EMPLOYEE") and principal.can("assignment.employee.read")
    if employee_scope:
        assignment_ids = select(Assignment.id).where(
            Assignment.company_id == company_id,
            Assignment.internal_assignee_user_id == principal.user_id,
        )
        lead_f = [
            Lead.supplier_company_id == company_id,
            Lead.submitter_user_id == principal.user_id,
            *_time(Lead.created_at, period_start, period_end),
        ]
        assignment_f = [
            Assignment.company_id == company_id,
            Assignment.internal_assignee_user_id == principal.user_id,
            *_time(Assignment.assigned_at, period_start, period_end),
        ]
        return_f = [
            ReturnRequest.company_id == company_id,
            or_(
                ReturnRequest.submitted_by == principal.user_id,
                ReturnRequest.assignment_id.in_(assignment_ids),
            ),
            *_time(ReturnRequest.created_at, period_start, period_end),
        ]
        points_f = [
            PointsLedger.company_id == company_id,
            PointsLedger.business_id.in_(assignment_ids),
            *_time(PointsLedger.created_at, period_start, period_end),
        ]
        rewards = {"total": 0, "by_status": {}, "points": 0}
    else:
        assignment_ids = select(Assignment.id).where(
            or_(Assignment.company_id == company_id, Assignment.receiver_company_id == company_id)
        )
        lead_f = [
            Lead.supplier_company_id == company_id,
            *_time(Lead.created_at, period_start, period_end),
        ]
        assignment_f = [
            or_(Assignment.company_id == company_id, Assignment.receiver_company_id == company_id),
            *_time(Assignment.assigned_at, period_start, period_end),
        ]
        return_f = [
            ReturnRequest.company_id == company_id,
            *_time(ReturnRequest.created_at, period_start, period_end),
        ]
        points_f = [
            PointsLedger.company_id == company_id,
            PointsLedger.business_id.in_(assignment_ids),
            *_time(PointsLedger.created_at, period_start, period_end),
        ]
        reward_f = [
            SupplierLeadReward.supplier_company_id == company_id,
            *_time(SupplierLeadReward.created_at, period_start, period_end),
        ]
        rewards = _summary(db, SupplierLeadReward, reward_f)
        rewards["points"] = int(
            db.scalar(select(func.coalesce(func.sum(SupplierLeadReward.reward_points), 0)).where(*reward_f)) or 0
        )
    supplier_leads = _summary(db, Lead, lead_f)
    received_assignments = _summary(db, Assignment, assignment_f)
    returns = _summary(db, ReturnRequest, return_f)
    exception_breakdown = _exception_breakdown(
        db,
        assignment_filters=assignment_f,
        return_filters=return_f,
    )
    consumed_points = _consumed_points(db, points_f)
    claimed = sum(
        int(received_assignments["by_status"].get(status, 0))
        for status in ("CLAIMED", "FOLLOWING", "RETURN_PENDING", "COMPLETED", "RETURNED")
    )
    statistics = {
        "period": normalized_period,
        "period_start": period_start.date().isoformat(),
        "scope": "employee" if employee_scope else "company",
        "received": int(received_assignments["total"]),
        "claimed": claimed,
        "effective": int(received_assignments["by_status"].get("COMPLETED", 0)),
        "refused_claims": exception_breakdown["refused_claim"],
        "return_requests": exception_breakdown["return_requested"],
        "confirmed_invalid": exception_breakdown["confirmed_invalid"],
        "consumed_points": consumed_points,
    }
    unread = db.scalar(select(func.count(Notification.id)).where(
        or_(Notification.user_id == principal.user_id, (Notification.user_id.is_(None)) & (Notification.company_id == company_id)),
        Notification.read_at.is_(None),
    )) or 0
    points = {"consumed_points": consumed_points}
    return ok(request, {
        "company_id": company_id,
        "scope": statistics["scope"],
        "period": normalized_period,
        "period_start": statistics["period_start"],
        "period_end": period_end.date().isoformat(),
        "statistics": statistics,
        "supplier_leads": supplier_leads,
        "received_assignments": received_assignments,
        "returns": returns,
        "exception_breakdown": exception_breakdown,
        "points": points,
        "finance": points,
        "supplier_rewards": rewards,
        "unread_notifications": int(unread),
    })


def _lead_export_task_dict(task: LeadExportTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "requested_by_user_id": task.requested_by,
        "requested_by_name": task.requested_by_name,
        "status": task.status,
        "filters": task.filters_json,
        "include_full_phone": task.include_full_phone,
        "row_count": task.row_count,
        "file_name": task.file_name,
        "file_size": task.file_size,
        "error_message": task.error_message,
        "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
        "expires_at": _iso(task.expires_at),
        "created_at": _iso(task.created_at),
    }


def _owned_lead_export_task(
    db: Session,
    *,
    task_id: str,
    principal: CurrentPrincipal,
) -> LeadExportTask:
    task = db.get(LeadExportTask, task_id)
    if task is None:
        raise AppError("LEAD_EXPORT_NOT_FOUND", "导出任务不存在", 404)
    if not principal.can("*") and task.requested_by != principal.user_id:
        raise AppError("FORBIDDEN", "无权查看其他人的导出任务", 403)
    return task


def _existing_lead_export_task(
    db: Session,
    *,
    idempotency_key: str,
    requested_by: str,
    filters: dict[str, Any],
) -> LeadExportTask | None:
    existing = db.scalar(
        select(LeadExportTask).where(
            LeadExportTask.idempotency_key == idempotency_key
        )
    )
    if existing is None:
        return None
    if existing.requested_by != requested_by or existing.filters_json != filters:
        raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已被其他导出请求使用", 409)
    return existing


@router.get("/reports/leads/filter-options")
def lead_report_filter_options(
    request: Request,
    _principal=Depends(require_permissions("lead.read")),
    db: Session = Depends(get_db),
):
    submitters = db.execute(
        select(User.id, User.display_name)
        .join(Lead, Lead.submitter_user_id == User.id)
        .where(Lead.source_kind.is_not(None))
        .distinct()
        .order_by(User.display_name.asc(), User.id.asc())
    ).all()
    receiver_companies = db.execute(
        select(Company.id, Company.name, Company.status).order_by(
            Company.name.asc(),
            Company.id.asc(),
        )
    ).all()
    supplier_companies = db.execute(
        select(Company.id, Company.name, Company.status)
        .join(Lead, Lead.supplier_company_id == Company.id)
        .where(Lead.source_kind.is_not(None), Lead.deleted_at.is_(None))
        .distinct()
        .order_by(Company.name.asc(), Company.id.asc())
    ).all()
    assigners = db.execute(
        select(User.id, User.display_name, User.status)
        .join(Assignment, Assignment.assigned_by == User.id)
        .join(Lead, Lead.id == Assignment.lead_id)
        .where(Lead.source_kind.is_not(None))
        .distinct()
        .order_by(User.display_name.asc(), User.id.asc())
    ).all()
    return ok(
        request,
        {
            "submitters": [
                {"id": item.id, "name": item.display_name} for item in submitters
            ],
            "receiver_companies": [
                {"id": item.id, "name": item.name, "status": item.status}
                for item in receiver_companies
            ],
            "supplier_companies": [
                {"id": item.id, "name": item.name, "status": item.status}
                for item in supplier_companies
            ],
            "assigners": [
                {"id": item.id, "name": item.display_name, "status": item.status}
                for item in assigners
            ],
        },
    )


@router.get("/reports/leads")
def lead_report_list(
    request: Request,
    principal=Depends(require_permissions("lead.read")),
    db: Session = Depends(get_db),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    source_kind: str | None = Query(default=None),
    supplier_company_id: str | None = Query(default=None),
    submitter_user_id: str | None = Query(default=None),
    region: str | None = Query(default=None, max_length=64),
    receiver_company_id: str | None = Query(default=None),
    lead_status: str | None = Query(default=None),
    assignment_status: str | None = Query(default=None),
    assigned_by_user_id: str | None = Query(default=None),
    pending_reason: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    created_from, created_to = _validated_query_time_range(
        created_from,
        created_to,
    )
    filters = {
        "created_from": created_from,
        "created_to": created_to,
        "source_kind": source_kind,
        "supplier_company_id": supplier_company_id,
        "submitter_user_id": submitter_user_id,
        "phone_hash": None,
        "region": region,
        "receiver_company_id": receiver_company_id,
        "lead_status": lead_status,
        "assignment_status": assignment_status,
        "assigned_by_user_id": assigned_by_user_id,
        "pending_reason": pending_reason,
    }
    rows, total = list_lead_report_rows(
        db,
        filters=filters,
        page_no=page_no,
        page_size=page_size,
    )
    return ok(
        request,
        page(
            lead_report_to_dicts(
                db,
                rows,
                include_full_phone=principal.can("*") or principal.can("lead.phone.read"),
            ),
            total,
            page_no,
            page_size,
        ),
    )


@router.post("/reports/leads/search")
def search_lead_report(
    body: LeadReportSearchBody,
    request: Request,
    principal=Depends(require_permissions("lead.read")),
    db: Session = Depends(get_db),
):
    if body.phone and not principal.can("lead.phone.export"):
        raise AppError("FORBIDDEN", "无权按完整手机号筛选", 403)
    filters = body.filters()
    rows, total = list_lead_report_rows(
        db,
        filters=filters,
        page_no=body.page,
        page_size=body.page_size,
    )
    return ok(
        request,
        page(
            lead_report_to_dicts(
                db,
                rows,
                include_full_phone=principal.can("*") or principal.can("lead.phone.read"),
            ),
            total,
            body.page,
            body.page_size,
        ),
    )


@router.post("/reports/leads/exports")
def request_lead_export(
    body: ScopedLeadExportRequestBody,
    request: Request,
    principal=Depends(require_permissions("lead.phone.export")),
    db: Session = Depends(get_db),
):
    if body.scope == "PUBLIC_POOL" and not principal.can("lead.manual.manage"):
        raise AppError("FORBIDDEN", "无权导出公海池客资", 403)
    filters = body.filters()
    with _lead_export_queue_guard():
        _acquire_lead_export_queue_lock(db)
        existing = _existing_lead_export_task(
            db,
            idempotency_key=body.idempotency_key,
            requested_by=principal.user_id,
            filters=filters,
        )
        if existing is not None:
            return ok(request, _lead_export_task_dict(existing), "导出任务已创建")
        _enforce_lead_export_queue_limits(
            db,
            requested_by=principal.user_id,
            now=datetime.now(timezone.utc),
        )
        task = LeadExportTask(
            requested_by=principal.user_id,
            requested_by_name=principal.display_name,
            status="PENDING",
            filters_json=filters,
            include_full_phone=True,
            idempotency_key=body.idempotency_key,
            row_count=0,
        )
        try:
            db.add(task)
            db.flush()
            write_audit(
                db,
                principal=principal,
                action="V12_LEAD_EXPORT_REQUESTED",
                resource_type="lead_export_task",
                resource_id=task.id,
                after={"status": task.status, "include_full_phone": True},
                metadata={"filters": filters},
                request_id=request.state.request_id,
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _existing_lead_export_task(
                db,
                idempotency_key=body.idempotency_key,
                requested_by=principal.user_id,
                filters=filters,
            )
            if existing is None:
                raise
            return ok(request, _lead_export_task_dict(existing), "导出任务已创建")
        return ok(request, _lead_export_task_dict(task), "客资完整信息导出任务已提交")


@router.get("/reports/leads/exports")
def list_lead_exports(
    request: Request,
    principal=Depends(require_permissions("lead.phone.export")),
    db: Session = Depends(get_db),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    filters = [] if principal.can("*") else [LeadExportTask.requested_by == principal.user_id]
    total = int(db.scalar(select(func.count(LeadExportTask.id)).where(*filters)) or 0)
    items = list(
        db.scalars(
            select(LeadExportTask)
            .where(*filters)
            .order_by(LeadExportTask.created_at.desc())
            .offset((page_no - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return ok(
        request,
        page(
            [_lead_export_task_dict(task) for task in items],
            total,
            page_no,
            page_size,
        ),
    )


@router.get("/reports/leads/exports/{task_id}")
def get_lead_export(
    task_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.phone.export")),
    db: Session = Depends(get_db),
):
    task = _owned_lead_export_task(db, task_id=task_id, principal=principal)
    return ok(request, _lead_export_task_dict(task))


@router.get("/reports/leads/exports/{task_id}/download")
def download_lead_export(
    task_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.phone.export")),
    db: Session = Depends(get_db),
):
    task = _owned_lead_export_task(db, task_id=task_id, principal=principal)
    if task.status != "COMPLETED" or not task.object_key:
        raise AppError("LEAD_EXPORT_NOT_READY", "导出文件尚未生成", 409)
    if task.expires_at and as_utc(task.expires_at) < datetime.now(timezone.utc):
        raise AppError("LEAD_EXPORT_EXPIRED", "导出文件已过期，请重新导出", 410)
    storage = get_storage()
    filename = task.file_name or "lead-export.xlsx"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store",
    }
    if task.file_size is not None:
        headers["Content-Length"] = str(task.file_size)
    write_audit(
        db,
        principal=principal,
        action="V12_LEAD_EXPORT_DOWNLOADED",
        resource_type="lead_export_task",
        resource_id=task.id,
        after={"status": task.status, "file_name": filename},
        metadata={
            "owner_user_id": task.requested_by,
            "filters": task.filters_json,
            "file_sha256": task.sha256,
            "file_size": task.file_size,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    return StreamingResponse(
        storage.iter_read(task.object_key),
        media_type=(
            task.mime_type
            or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers=headers,
    )


@router.get("/reports/leads/export.csv")
def export_leads_csv(
    _principal=Depends(require_permissions("lead.read")),
    period: str | None = Query(default="month"),
    limit: int = Query(default=500, ge=1, le=2000),
    source_kind: str | None = Query(default=None),
    receiver_company_id: str | None = Query(default=None),
    lead_status: str | None = Query(default=None),
    assignment_status: str | None = Query(default=None),
    return_status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _normalized_period, period_start, period_end = _period_window(period)
    creator = aliased(User)
    supplier = aliased(Company)
    receiver = aliased(Company)
    filters = list(_time(Lead.created_at, period_start, period_end))
    if source_kind:
        filters.append(Lead.source_kind == source_kind.strip().upper())
    if receiver_company_id:
        filters.append(
            func.coalesce(Assignment.receiver_company_id, Assignment.company_id)
            == receiver_company_id.strip()
        )
    if lead_status:
        filters.append(Lead.status == lead_status.strip().upper())
    if assignment_status:
        filters.append(Assignment.status == assignment_status.strip().upper())
    if return_status:
        filters.append(ReturnRequest.status == return_status.strip().upper())
    rows = db.execute(
        select(
            Lead,
            Assignment,
            ReturnRequest,
            creator.display_name.label("creator_name"),
            supplier.name.label("supplier_company"),
            receiver.name.label("receiver_company"),
        )
        .outerjoin(creator, creator.id == Lead.submitter_user_id)
        .outerjoin(supplier, supplier.id == Lead.supplier_company_id)
        .outerjoin(Assignment, Assignment.lead_id == Lead.id)
        .outerjoin(receiver, receiver.id == func.coalesce(Assignment.receiver_company_id, Assignment.company_id))
        .outerjoin(ReturnRequest, ReturnRequest.assignment_id == Assignment.id)
        .where(*filters)
        .order_by(Lead.created_at.desc(), Assignment.assigned_at.desc())
        .limit(limit)
    ).all()
    buffer = StringIO()
    fieldnames = [
        "lead_id",
        "assignment_id",
        "return_id",
        "customer_name",
        "lead_source",
        "source_kind",
        "creator_name",
        "supplier_company",
        "receiver_company",
        "receiver_company_id",
        "lead_status",
        "review_status",
        "current_follow_status",
        "pending_reason",
        "assignment_status",
        "assigned_at",
        "refusal_reason",
        "return_status",
        "return_submitted_at",
        "return_reviewed_at",
        "processing_outcome",
        "last_handled_at",
        "created_at",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        lead, assignment, return_request = row[0], row[1], row[2]
        receiver_id = (
            assignment.receiver_company_id or assignment.company_id
            if assignment is not None
            else ""
        )
        processing_outcome = (
            lead.current_follow_status
            or (return_request.status if return_request is not None else None)
            or (assignment.status if assignment is not None else None)
            or lead.status
        )
        last_handled_at = _latest_datetime(
            lead.updated_at,
            assignment.updated_at if assignment is not None else None,
            assignment.assigned_at if assignment is not None else None,
            assignment.released_at if assignment is not None else None,
            return_request.updated_at if return_request is not None else None,
            return_request.submitted_at if return_request is not None else None,
            return_request.reviewed_at if return_request is not None else None,
        )
        csv_row = {
            "lead_id": lead.id,
            "assignment_id": assignment.id if assignment is not None else "",
            "return_id": return_request.id if return_request is not None else "",
            "customer_name": lead.customer_name,
            "lead_source": lead.source_channel or lead.source_kind or lead.source_type or "",
            "source_kind": lead.source_kind or "",
            "creator_name": row.creator_name or "",
            "supplier_company": row.supplier_company or "",
            "receiver_company": row.receiver_company or "",
            "receiver_company_id": receiver_id,
            "lead_status": lead.status,
            "review_status": lead.review_status or "",
            "current_follow_status": lead.current_follow_status or "",
            "pending_reason": lead.pending_reason or "",
            "assignment_status": assignment.status if assignment is not None else "",
            "assigned_at": _iso(assignment.assigned_at) if assignment is not None else "",
            "refusal_reason": assignment.release_reason if assignment is not None else "",
            "return_status": return_request.status if return_request is not None else "",
            "return_submitted_at": _iso(return_request.submitted_at) if return_request is not None else "",
            "return_reviewed_at": _iso(return_request.reviewed_at) if return_request is not None else "",
            "processing_outcome": processing_outcome,
            "last_handled_at": _iso(last_handled_at) or "",
            "created_at": _iso(lead.created_at) or "",
        }
        writer.writerow({key: _safe_csv_cell(value) for key, value in csv_row.items()})
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="v12-leads.csv"'},
    )


def _audit(item: AuditLog, *, actor_name: str | None = None) -> dict[str, Any]:
    return {
        "id": item.id, "request_id": item.request_id, "actor_user_id": item.actor_user_id,
        "actor_name": actor_name or "系统自动处理",
        "actor_role_codes": list(item.actor_role_codes or []), "action": item.action,
        "resource_type": item.resource_type, "resource_id": item.resource_id,
        "company_id": item.company_id, "before": item.before_json, "after": item.after_json,
        "metadata": item.metadata_json, "created_at": item.created_at.isoformat(),
    }


def _related_ids(db: Session, lead_id: str | None, assignment_id: str | None, return_id: str | None, reward_id: str | None) -> set[str]:
    ids = {value for value in (lead_id, assignment_id, return_id, reward_id) if value}
    assignment = db.get(Assignment, assignment_id) if assignment_id else None
    return_request = db.get(ReturnRequest, return_id) if return_id else None
    reward = db.get(SupplierLeadReward, reward_id) if reward_id else None
    if assignment:
        ids.add(assignment.lead_id)
    if return_request:
        ids.update({return_request.lead_id, return_request.assignment_id})
        if return_request.verification_task_id:
            ids.add(return_request.verification_task_id)
    if reward:
        ids.update({reward.lead_id, reward.assignment_id})
        ids.update(value for value in (reward.ledger_id, reward.reversal_ledger_id) if value)
    return ids


@router.get("/audit-events")
def audit_events(
    request: Request,
    _principal=Depends(require_permissions("audit.read")),
    db: Session = Depends(get_db),
    business_id: str | None = None, lead_id: str | None = None, assignment_id: str | None = None,
    return_id: str | None = None, reward_id: str | None = None, action: str | None = None,
    company_id: str | None = None, request_id: str | None = None,
    created_from: datetime | None = None, created_to: datetime | None = None,
    page_no: int = Query(default=1, alias="page", ge=1), page_size: int = Query(default=50, ge=1, le=200),
):
    ids = _related_ids(db, lead_id, assignment_id, return_id, reward_id)
    if business_id:
        ids.add(business_id)
    filters: list[Any] = []
    if ids:
        filters.append(or_(AuditLog.resource_id.in_(ids), AuditLog.request_id.in_(ids)))
    if action:
        filters.append(AuditLog.action == action.strip().upper())
    if company_id:
        filters.append(AuditLog.company_id == company_id)
    if request_id:
        filters.append(AuditLog.request_id == request_id)
    filters += _time(AuditLog.created_at, created_from, created_to)
    total = int(db.scalar(select(func.count(AuditLog.id)).where(*filters)) or 0)
    items = db.scalars(select(AuditLog).where(*filters).order_by(AuditLog.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    actor_ids = {item.actor_user_id for item in items if item.actor_user_id}
    users = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(actor_ids))).all()
    } if actor_ids else {}
    return ok(
        request,
        page(
            [_audit(item, actor_name=_display_name(users.get(item.actor_user_id))) for item in items],
            total,
            page_no,
            page_size,
        ),
    )


def _display_name(user: User | None) -> str | None:
    if user is None:
        return None
    return user.display_name or user.username


@router.get("/operations/my-processed")
def my_processed_operations(
    request: Request,
    principal=Depends(
        require_permissions("dashboard.operation.read", "audit.read")
    ),
    db: Session = Depends(get_db),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    created_from, created_to = _validated_query_time_range(
        created_from,
        created_to,
    )
    if created_from is None and created_to is None:
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        created_from = local_start.astimezone(timezone.utc)
        created_to = (local_start + timedelta(days=1)).astimezone(timezone.utc)
        time_filters = [
            AuditLog.created_at >= created_from,
            AuditLog.created_at < created_to,
        ]
    else:
        time_filters = (
            ([AuditLog.created_at >= created_from] if created_from else [])
            + ([AuditLog.created_at < created_to] if created_to else [])
        )

    priority = case(
        *[
            (AuditLog.action == action, value)
            for action, value in OPERATION_PROCESSED_ACTION_PRIORITY.items()
        ],
        else_=99,
    )
    ranked = (
        select(
            AuditLog.id.label("audit_id"),
            func.row_number()
            .over(
                partition_by=func.coalesce(AuditLog.request_id, AuditLog.id),
                order_by=(priority.asc(), AuditLog.created_at.desc(), AuditLog.id.desc()),
            )
            .label("row_number"),
        )
        .where(
            AuditLog.actor_user_id == principal.user_id,
            AuditLog.action.in_(tuple(OPERATION_PROCESSED_ACTION_PRIORITY)),
            *time_filters,
        )
        .subquery()
    )
    total = int(
        db.scalar(
            select(func.count()).select_from(ranked).where(ranked.c.row_number == 1)
        )
        or 0
    )
    items = list(
        db.scalars(
            select(AuditLog)
            .join(ranked, ranked.c.audit_id == AuditLog.id)
            .where(ranked.c.row_number == 1)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page_no - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    data = page(
        [
            _audit(item, actor_name=principal.display_name)
            for item in items
        ],
        total,
        page_no,
        page_size,
    )
    data["scope"] = {
        "actor_user_id": principal.user_id,
        "created_from": _iso(created_from),
        "created_to": _iso(created_to),
        "timezone": "Asia/Shanghai",
    }
    return ok(request, data)


def _trace(
    db: Session,
    business_id: str,
    *,
    evidence_user_id: str | None = None,
    include_phone: bool = False,
) -> dict[str, Any]:
    lead = db.get(Lead, business_id)
    assignment = db.get(Assignment, business_id)
    return_request = db.get(ReturnRequest, business_id)
    reward = db.get(SupplierLeadReward, business_id)
    task = db.get(VerificationTask, business_id)
    lead_id = lead.id if lead else assignment.lead_id if assignment else return_request.lead_id if return_request else reward.lead_id if reward else task.lead_id if task else None
    assignment_id = assignment.id if assignment else return_request.assignment_id if return_request else reward.assignment_id if reward else getattr(task, "assignment_id", None) if task else None
    assignments = db.scalars(select(Assignment).where(Assignment.lead_id == lead_id).order_by(Assignment.created_at)).all() if lead_id else []
    assignment_ids = {item.id for item in assignments}
    if assignment_id:
        assignment_ids.add(assignment_id)
    relation_filter = lambda model: or_(model.lead_id == lead_id, model.assignment_id.in_(assignment_ids))
    returns = db.scalars(select(ReturnRequest).where(relation_filter(ReturnRequest)).order_by(ReturnRequest.created_at)).all() if lead_id or assignment_ids else []
    rewards = db.scalars(select(SupplierLeadReward).where(relation_filter(SupplierLeadReward)).order_by(SupplierLeadReward.created_at)).all() if lead_id or assignment_ids else []
    tasks = db.scalars(select(VerificationTask).where(relation_filter(VerificationTask)).order_by(VerificationTask.created_at)).all() if lead_id or assignment_ids else []
    ids = {business_id, *assignment_ids, *(item.id for item in returns), *(item.id for item in rewards), *(item.id for item in tasks)}
    if lead_id:
        ids.add(lead_id)
    for item in rewards:
        ids.update(value for value in (item.ledger_id, item.reversal_ledger_id) if value)
    followups = db.scalars(
        select(FollowUp).where(FollowUp.assignment_id.in_(assignment_ids)).order_by(FollowUp.created_at)
    ).all() if assignment_ids else []
    ledgers = db.scalars(
        select(PointsLedger)
        .where(or_(PointsLedger.business_id.in_(ids), PointsLedger.id.in_(ids)))
        .order_by(PointsLedger.created_at)
    ).all() if ids else []
    audits = db.scalars(select(AuditLog).where(or_(AuditLog.resource_id.in_(ids), AuditLog.request_id == business_id)).order_by(AuditLog.created_at)).all()
    outboxes = db.scalars(select(NotificationOutbox).where(NotificationOutbox.aggregate_id.in_(ids)).order_by(NotificationOutbox.created_at)).all()
    notification_ids = {str(item.payload.get("notification_id")) for item in outboxes if item.payload.get("notification_id")}
    notifications = db.scalars(select(Notification).where(Notification.id.in_(notification_ids)).order_by(Notification.created_at)).all() if notification_ids else []
    resolved_lead = db.get(Lead, lead_id) if lead_id else None
    user_ids = {
        value
        for value in (
            resolved_lead.submitter_user_id if resolved_lead else None,
            *(item.assigned_by for item in assignments),
            *(item.internal_assignee_user_id for item in assignments),
            *(item.submitted_by for item in returns),
            *(item.reviewed_by for item in returns),
            *(item.assignee_user_id for item in tasks),
            *(item.created_by for item in followups),
            *(item.created_by for item in ledgers),
            *(item.actor_user_id for item in audits),
        )
        if value
    }
    users = {
        item.id: item
        for item in db.scalars(select(User).where(User.id.in_(user_ids))).all()
    } if user_ids else {}
    company_ids = {
        value
        for value in (
            resolved_lead.supplier_company_id if resolved_lead else None,
            *(item.company_id for item in assignments),
            *(item.supplier_company_id for item in assignments),
            *(item.receiver_company_id for item in assignments),
            *(item.company_id for item in returns),
            *(item.supplier_company_id for item in rewards),
            *(item.receiver_company_id for item in rewards),
            *(item.company_id for item in ledgers),
        )
        if value
    }
    companies = {
        item.id: item
        for item in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    } if company_ids else {}
    audit_events = [_audit(item, actor_name=_display_name(users.get(item.actor_user_id))) for item in audits]
    timeline = [
        {
            "at": item["created_at"],
            "kind": "AUDIT",
            "id": item["id"],
            "action": item["action"],
            "resource_type": item["resource_type"],
            "resource_id": item["resource_id"],
            "actor_name": item["actor_name"],
            "summary": str((item["metadata"] or {}).get("reason") or "已完成本次处理"),
        }
        for item in audit_events
    ]
    timeline += [
        {
            "at": item.created_at.isoformat(),
            "kind": "NOTIFICATION",
            "id": item.id,
            "action": item.scene,
            "resource_type": "notification",
            "resource_id": item.id,
            "status": item.status,
            "summary": item.title,
        }
        for item in notifications
    ]
    timeline.sort(key=lambda item: (item["at"], item["kind"], item["id"]))
    trace_returns = []
    for item in returns:
        data = return_request_to_dict(
            db,
            item,
            include_evidence=True,
            include_phone=include_phone,
        )
        data["company_name"] = companies.get(item.company_id).name if item.company_id in companies else None
        data["submitted_by_name"] = _display_name(users.get(item.submitted_by))
        data["reviewed_by_name"] = _display_name(users.get(item.reviewed_by))
        if evidence_user_id:
            for evidence in data.get("evidences", []):
                evidence["access_token"] = create_file_access_token(evidence["id"], evidence_user_id)
        trace_returns.append(data)
    return {
        "business_id": business_id, "linked_ids": sorted(ids),
        "lead": {
            "id": resolved_lead.id,
            "customer_name": resolved_lead.customer_name,
            "phone": decrypt_text(resolved_lead.phone_encrypted) if include_phone else None,
            "phone_masked": mask_phone(decrypt_text(resolved_lead.phone_encrypted)),
            "status": resolved_lead.status,
            "source_kind": resolved_lead.source_kind,
            "source_channel": resolved_lead.source_channel,
            "review_status": resolved_lead.review_status,
            "review_note": resolved_lead.review_note,
            "duplicate_status": resolved_lead.duplicate_status,
            "city": resolved_lead.city,
            "district": resolved_lead.district,
            "need_summary": resolved_lead.need_summary,
            "submitted_at": _iso(resolved_lead.submitted_at),
            "created_at": _iso(resolved_lead.created_at),
            "supplier_company_id": resolved_lead.supplier_company_id,
            "supplier_company_name": companies.get(resolved_lead.supplier_company_id).name if resolved_lead.supplier_company_id in companies else None,
            "submitter_name": _display_name(users.get(resolved_lead.submitter_user_id)),
        } if resolved_lead else None,
        "assignments": [{
            "id": item.id, "lead_id": item.lead_id, "company_id": item.company_id,
            "company_name": companies.get(item.company_id).name if item.company_id in companies else None,
            "supplier_company_id": item.supplier_company_id,
            "supplier_company_name": companies.get(item.supplier_company_id).name if item.supplier_company_id in companies else None,
            "receiver_company_id": item.receiver_company_id,
            "receiver_company_name": companies.get(item.receiver_company_id).name if item.receiver_company_id in companies else None,
            "status": item.status, "current_follow_status": resolved_lead.current_follow_status if resolved_lead else None,
            "points_price": item.points_price, "claim_points": item.claim_points,
            "assigned_at": _iso(item.assigned_at), "claimed_at": _iso(item.claimed_at),
            "assigned_by_name": _display_name(users.get(item.assigned_by)),
            "internal_assignee_name": _display_name(users.get(item.internal_assignee_user_id)),
        } for item in assignments],
        "returns": trace_returns,
        "supplier_rewards": [{
            "id": item.id, "assignment_id": item.assignment_id, "lead_id": item.lead_id,
            "supplier_company_id": item.supplier_company_id,
            "supplier_company_name": companies.get(item.supplier_company_id).name if item.supplier_company_id in companies else None,
            "receiver_company_id": item.receiver_company_id,
            "receiver_company_name": companies.get(item.receiver_company_id).name if item.receiver_company_id in companies else None,
            "status": item.status, "claim_points": int(item.claim_points), "reward_points": int(item.reward_points),
            "reward_due_at": _iso(item.reward_due_at), "settled_at": _iso(item.settled_at),
            "ledger_id": item.ledger_id, "reversal_ledger_id": item.reversal_ledger_id,
            "exception_reason": item.exception_reason,
        } for item in rewards],
        "verification_tasks": [{
            "id": item.id, "lead_id": item.lead_id, "assignment_id": item.assignment_id,
            "return_request_id": item.return_request_id, "task_type": item.task_type,
            "status": item.status, "assignee_user_id": item.assignee_user_id,
            "assignee_name": _display_name(users.get(item.assignee_user_id)),
            "contact_result": item.contact_result, "conclusion": item.verification_conclusion,
            "created_at": _iso(item.created_at), "assigned_at": _iso(item.assigned_at),
            "started_at": _iso(item.started_at), "submitted_at": _iso(item.submitted_at),
        } for item in tasks],
        "followups": [{
            "id": item.id, "assignment_id": item.assignment_id, "status": item.status,
            "note": item.note, "next_followup_at": _iso(item.next_followup_at),
            "created_at": _iso(item.created_at), "created_by_name": _display_name(users.get(item.created_by)),
        } for item in followups],
        "points_ledgers": [{
            "id": item.id, "company_id": item.company_id,
            "company_name": companies.get(item.company_id).name if item.company_id in companies else None,
            "ledger_type": item.ledger_type, "delta": int(item.delta), "balance_after": int(item.balance_after),
            "business_type": item.business_type, "business_id": item.business_id,
            "created_at": _iso(item.created_at), "created_by_name": _display_name(users.get(item.created_by)),
        } for item in ledgers],
        "notifications": [{"id": item.id, "scene": item.scene, "title": item.title, "deep_link": item.deep_link, "status": item.status, "read_at": _iso(item.read_at), "created_at": _iso(item.created_at)} for item in notifications],
        "audit_events": audit_events, "timeline": timeline,
    }


@router.get("/trace/{business_id}")
def business_trace(business_id: str, request: Request, principal=Depends(require_permissions("audit.read")), db: Session = Depends(get_db)):
    data = _trace(
        db,
        business_id,
        evidence_user_id=principal.user_id,
        include_phone=principal.can("*") or principal.can("lead.phone.read"),
    )
    if len(data["linked_ids"]) == 1 and not data["audit_events"] and not data["notifications"] and data["lead"] is None:
        raise AppError("BUSINESS_TRACE_NOT_FOUND", "未找到该业务 ID", 404)
    return ok(request, data)
