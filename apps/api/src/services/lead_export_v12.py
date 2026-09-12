from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from ..core.models import (
    Assignment,
    Company,
    FollowUp,
    Lead,
    LeadExportTask,
    Role,
    StorageCleanupOutbox,
    User,
    UserRole,
)
from ..core.security import decrypt_text, hash_phone, mask_phone, normalize_phone
from .public_pool_v12 import public_pool_lead_conditions
from .storage import get_storage
from .storage_cleanup_worker import enqueue_storage_cleanup
from .xlsx_writer import WorksheetSpec, XlsxSizeLimitError, write_xlsx

logger = logging.getLogger("zhongshu.lead_export")
LEAD_EXPORT_ROWS_PER_FILE = 50_000
FOLLOWUP_EXPORT_ROWS_PER_FILE = 250_000
MAX_EXPORT_XML_BYTES = 512 * 1024 * 1024
MAX_EXPORT_WORKBOOK_BYTES = 512 * 1024 * 1024
EXPORT_STREAM_BATCH_SIZE = 500
EXPORT_HEARTBEAT_ROW_INTERVAL = 1_000
LEAD_EXPORT_UPLOAD_LEASE_RENEW_SECONDS = 60
LEAD_EXPORT_LEASE_TIMEOUT = timedelta(hours=2)
LEAD_EXPORT_ATTEMPT_CLEANUP_DELAY = timedelta(days=1)


class LeadExportLimitError(RuntimeError):
    pass


class LeadExportLeaseLostError(RuntimeError):
    pass


class LeadExportDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LeadReportRow:
    lead: Lead
    assignment: Assignment | None
    receiver_company_name: str | None
    assigned_by_name: str | None
    submitter_name: str | None
    supplier_company_name: str | None
    internal_assignee_name: str | None
    internal_assignee_role_code: str | None
    latest_followup_status: str | None
    latest_followup_note: str | None
    latest_followup_next_at: datetime | None
    latest_followup_by_name: str | None
    latest_followup_at: datetime | None


def _datetime_value(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def normalized_lead_report_filters(filters: dict[str, Any]) -> dict[str, Any]:
    phone = _text(filters.get("phone"))
    phone_hash = _text(filters.get("phone_hash"))
    return {
        "created_from": _datetime_value(filters.get("created_from")),
        "created_to": _datetime_value(filters.get("created_to")),
        "source_kind": _upper(filters.get("source_kind")),
        "supplier_company_id": _text(filters.get("supplier_company_id")),
        "submitter_user_id": _text(filters.get("submitter_user_id")),
        "phone_hash": phone_hash
        or (hash_phone(normalize_phone(phone)) if phone else None),
        "region": _text(filters.get("region")),
        "receiver_company_id": _text(filters.get("receiver_company_id")),
        "lead_status": _upper(filters.get("lead_status")),
        "assignment_status": _upper(filters.get("assignment_status")),
        "assigned_by_user_id": _text(filters.get("assigned_by_user_id")),
        "pending_reason": _upper(filters.get("pending_reason")),
    }


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _upper(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text else None


def _conditions(filters: dict[str, Any], current_assignment) -> list[Any]:
    values = normalized_lead_report_filters(filters)
    conditions = [Lead.source_kind.is_not(None), Lead.deleted_at.is_(None)]
    if values["created_from"]:
        conditions.append(Lead.created_at >= values["created_from"])
    if values["created_to"]:
        conditions.append(Lead.created_at < values["created_to"])
    if values["source_kind"]:
        conditions.append(Lead.source_kind == values["source_kind"])
    if values["supplier_company_id"]:
        conditions.append(Lead.supplier_company_id == values["supplier_company_id"])
    if values["submitter_user_id"]:
        conditions.append(Lead.submitter_user_id == values["submitter_user_id"])
    if values["phone_hash"]:
        conditions.append(Lead.phone_hash == values["phone_hash"])
    if values["region"]:
        conditions.append(
            or_(
                Lead.region_code == values["region"],
                Lead.province == values["region"],
                Lead.city == values["region"],
                Lead.district == values["region"],
            )
        )
    if values["receiver_company_id"]:
        conditions.append(
            func.coalesce(
                current_assignment.receiver_company_id,
                current_assignment.company_id,
            )
            == values["receiver_company_id"]
        )
    if values["lead_status"]:
        conditions.append(Lead.status == values["lead_status"])
    if values["assignment_status"]:
        conditions.append(current_assignment.status == values["assignment_status"])
    if values["assigned_by_user_id"]:
        conditions.append(current_assignment.assigned_by == values["assigned_by_user_id"])
    if values["pending_reason"]:
        conditions.append(Lead.pending_reason == values["pending_reason"])
    return conditions


def _report_select(
    filters: dict[str, Any],
    *,
    lead_ids: list[str] | None = None,
    include_latest_followup: bool = False,
):
    current_assignment = aliased(Assignment, name="current_assignment")
    receiver = aliased(Company, name="current_receiver")
    supplier = aliased(Company, name="lead_supplier")
    assigned_by = aliased(User, name="assigned_by_user")
    submitter = aliased(User, name="lead_submitter")
    internal_assignee = aliased(User, name="internal_assignee")
    internal_user_role = aliased(UserRole, name="internal_user_role")
    internal_role = aliased(Role, name="internal_assignee_role")
    selected = [
        Lead,
        current_assignment,
        receiver.name.label("receiver_company_name"),
        assigned_by.display_name.label("assigned_by_name"),
        submitter.display_name.label("submitter_name"),
        supplier.name.label("supplier_company_name"),
        internal_assignee.display_name.label("internal_assignee_name"),
        internal_role.code.label("internal_assignee_role_code"),
    ]
    statement = (
        select(*selected)
        .outerjoin(current_assignment, current_assignment.id == Lead.current_assignment_id)
        .outerjoin(
            receiver,
            receiver.id
            == func.coalesce(
                current_assignment.receiver_company_id,
                current_assignment.company_id,
            ),
        )
        .outerjoin(assigned_by, assigned_by.id == current_assignment.assigned_by)
        .outerjoin(submitter, submitter.id == Lead.submitter_user_id)
        .outerjoin(supplier, supplier.id == Lead.supplier_company_id)
        .outerjoin(
            internal_assignee,
            internal_assignee.id == current_assignment.internal_assignee_user_id,
        )
        .outerjoin(
            internal_user_role,
            internal_user_role.user_id == internal_assignee.id,
        )
        .outerjoin(internal_role, internal_role.id == internal_user_role.role_id)
        .where(*_conditions(filters, current_assignment))
    )
    if include_latest_followup:
        latest_followup_creator = aliased(User, name="latest_followup_creator")
        ranked_followups = (
            select(
                FollowUp.assignment_id.label("assignment_id"),
                FollowUp.status.label("status"),
                FollowUp.note.label("note"),
                FollowUp.next_followup_at.label("next_followup_at"),
                FollowUp.created_by.label("created_by"),
                FollowUp.created_at.label("created_at"),
                func.row_number()
                .over(
                    partition_by=FollowUp.assignment_id,
                    order_by=(FollowUp.created_at.desc(), FollowUp.id.desc()),
                )
                .label("row_number"),
            )
            .subquery("ranked_latest_followup")
        )
        statement = (
            statement.add_columns(
                ranked_followups.c.status.label("latest_followup_status"),
                ranked_followups.c.note.label("latest_followup_note"),
                ranked_followups.c.next_followup_at.label("latest_followup_next_at"),
                latest_followup_creator.display_name.label("latest_followup_by_name"),
                ranked_followups.c.created_at.label("latest_followup_at"),
            )
            .outerjoin(
                ranked_followups,
                and_(
                    ranked_followups.c.assignment_id == current_assignment.id,
                    ranked_followups.c.row_number == 1,
                ),
            )
            .outerjoin(
                latest_followup_creator,
                latest_followup_creator.id == ranked_followups.c.created_by,
            )
        )
    if lead_ids is not None:
        statement = statement.where(Lead.id.in_(lead_ids))
    return statement, current_assignment


def _lead_report_row(row: Any) -> LeadReportRow:
    return LeadReportRow(
        lead=row[0],
        assignment=row[1],
        receiver_company_name=row.receiver_company_name,
        assigned_by_name=row.assigned_by_name,
        submitter_name=row.submitter_name,
        supplier_company_name=row.supplier_company_name,
        internal_assignee_name=row.internal_assignee_name,
        internal_assignee_role_code=row.internal_assignee_role_code,
        latest_followup_status=getattr(row, "latest_followup_status", None),
        latest_followup_note=getattr(row, "latest_followup_note", None),
        latest_followup_next_at=getattr(row, "latest_followup_next_at", None),
        latest_followup_by_name=getattr(row, "latest_followup_by_name", None),
        latest_followup_at=getattr(row, "latest_followup_at", None),
    )


def _franchise_handler(row: LeadReportRow) -> tuple[str | None, str | None]:
    if row.internal_assignee_role_code == "FRANCHISE_EMPLOYEE":
        return row.internal_assignee_name, "FRANCHISE_EMPLOYEE"
    if row.receiver_company_name:
        return row.receiver_company_name, "FRANCHISE_COMPANY"
    return None, None


def list_lead_report_rows(
    db: Session,
    *,
    filters: dict[str, Any],
    page_no: int,
    page_size: int,
) -> tuple[list[LeadReportRow], int]:
    statement, current_assignment = _report_select(filters)
    total = int(
        db.scalar(
            select(func.count(Lead.id))
            .outerjoin(current_assignment, current_assignment.id == Lead.current_assignment_id)
            .where(*_conditions(filters, current_assignment))
        )
        or 0
    )
    rows = db.execute(
        statement.order_by(Lead.created_at.desc(), Lead.id.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    return [_lead_report_row(row) for row in rows], total


def _followup_dict(item: FollowUp, *, created_by_name: str | None = None) -> dict[str, Any]:
    return {
        "id": item.id,
        "assignment_id": item.assignment_id,
        "status": item.status,
        "note": item.note,
        "next_followup_at": item.next_followup_at.isoformat()
        if item.next_followup_at
        else None,
        "created_by_user_id": item.created_by,
        "created_by_name": created_by_name,
        "created_at": item.created_at.isoformat(),
    }


def lead_report_to_dicts(
    db: Session,
    rows: list[LeadReportRow],
    *,
    include_full_phone: bool = False,
    include_latest_followup: bool = True,
) -> list[dict[str, Any]]:
    assignment_ids = {
        row.assignment.id for row in rows if row.assignment is not None
    }
    latest_followups: dict[str, dict[str, Any]] = {}
    if assignment_ids and include_latest_followup:
        creator = aliased(User, name="followup_creator")
        ranked_followups = (
            select(
                FollowUp.id.label("followup_id"),
                func.row_number()
                .over(
                    partition_by=FollowUp.assignment_id,
                    order_by=(FollowUp.created_at.desc(), FollowUp.id.desc()),
                )
                .label("row_number"),
            )
            .where(FollowUp.assignment_id.in_(assignment_ids))
            .subquery()
        )
        followup_rows = db.execute(
            select(FollowUp, creator.display_name.label("created_by_name"))
            .join(
                ranked_followups,
                ranked_followups.c.followup_id == FollowUp.id,
            )
            .outerjoin(creator, creator.id == FollowUp.created_by)
            .where(ranked_followups.c.row_number == 1)
        ).all()
        for followup, created_by_name in followup_rows:
            latest_followups[followup.assignment_id] = _followup_dict(
                followup,
                created_by_name=created_by_name,
            )
    result: list[dict[str, Any]] = []
    for row in rows:
        lead = row.lead
        assignment = row.assignment
        franchise_handler_name, franchise_handler_kind = _franchise_handler(row)
        phone = decrypt_text(lead.phone_encrypted)
        source_display = (
            f"其他（{lead.source_detail}）"
            if lead.source_channel == "OTHER" and lead.source_detail
            else lead.source_channel
        )
        result.append(
            {
                "id": lead.id,
                "is_test": bool(getattr(lead, "is_test", False)),
                "customer_name": lead.customer_name,
                "phone": phone if include_full_phone else None,
                "phone_masked": mask_phone(phone),
                "source_kind": lead.source_kind,
                "source_channel": lead.source_channel,
                "source_detail": lead.source_detail,
                "source_display": source_display,
                "submitter_user_id": lead.submitter_user_id,
                "submitter_name": row.submitter_name,
                "province": lead.province,
                "city": lead.city,
                "district": lead.district,
                "region_code": lead.region_code,
                "need_summary": lead.need_summary,
                "lead_status": lead.status,
                "review_status": lead.review_status,
                "pending_reason": lead.pending_reason,
                "correction_issues": list(
                    (lead.raw_payload or {}).get("correction_issues") or []
                ),
                "snapshot_version": lead.snapshot_version,
                "current_follow_status": lead.current_follow_status,
                "supplier_company_id": lead.supplier_company_id,
                "supplier_company_name": row.supplier_company_name,
                "current_assignment_id": assignment.id if assignment else None,
                "assignment_status": assignment.status if assignment else None,
                "receiver_company_id": (
                    assignment.receiver_company_id or assignment.company_id
                    if assignment
                    else None
                ),
                "receiver_company_name": row.receiver_company_name,
                "assigned_by_user_id": assignment.assigned_by if assignment else None,
                "assigned_by_name": row.assigned_by_name,
                "assigned_at": assignment.assigned_at.isoformat() if assignment else None,
                "franchise_handler_name": franchise_handler_name,
                "franchise_handler_kind": franchise_handler_kind,
                "internal_assigned_at": (
                    assignment.internal_assigned_at.isoformat()
                    if assignment
                    and assignment.internal_assigned_at
                    and franchise_handler_kind == "FRANCHISE_EMPLOYEE"
                    else None
                ),
                "latest_followup": latest_followups.get(assignment.id) if assignment else None,
                "created_at": lead.created_at.isoformat(),
            }
        )
    return result


LEAD_EXPORT_FIELDS = [
    "客资编号",
    "客户姓名",
    "完整手机号",
    "来源类型",
    "来源渠道",
    "录入人员",
    "省份",
    "城市",
    "区县",
    "地区编码",
    "咨询类别",
    "品牌",
    "客户需求",
    "预算下限",
    "预算上限",
    "客资状态",
    "当前跟进状态",
    "派发状态",
    "接收加盟商",
    "加盟商跟进人",
    "内部分配时间",
    "派发运营人员",
    "派发时间",
    "最新跟进状态",
    "最新跟进内容",
    "下次跟进时间",
    "最新跟进人",
    "最新跟进时间",
    "创建时间",
]

FOLLOWUP_EXPORT_FIELDS = [
    "客资编号",
    "客户姓名",
    "派发单编号",
    "接收加盟商",
    "跟进状态",
    "跟进内容",
    "下次跟进时间",
    "跟进人",
    "跟进时间",
]


def _iter_report_rows(db: Session, filters: dict[str, Any]):
    statement, _current_assignment = _report_select(
        filters,
        include_latest_followup=True,
    )
    rows = db.execute(
        statement.order_by(Lead.created_at.desc(), Lead.id.desc()).execution_options(
            yield_per=EXPORT_STREAM_BATCH_SIZE
        )
    )
    for row in rows:
        yield _lead_report_row(row)


def _public_pool_values(filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_from": _datetime_value(filters.get("created_from")),
        "created_to": _datetime_value(filters.get("created_to")),
        "submitter_user_id": _text(filters.get("submitter_user_id")),
        "keyword": _text(filters.get("keyword")),
        "phone_hash": _text(filters.get("phone_hash")),
        "customer_source": _upper(filters.get("customer_source")),
        "source_kind": _upper(filters.get("source_kind")),
        "completeness": _upper(filters.get("completeness")),
        "duplicate_status": _upper(filters.get("duplicate_status")),
    }


def _iter_public_pool_report_rows(db: Session, filters: dict[str, Any]):
    conditions = public_pool_lead_conditions(**_public_pool_values(filters))
    statement, _current_assignment = _report_select({}, include_latest_followup=False)
    rows = db.execute(
        statement.where(*conditions)
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .execution_options(yield_per=EXPORT_STREAM_BATCH_SIZE)
    )
    for row in rows:
        yield _lead_report_row(row)


def _count_public_pool_report_rows(db: Session, filters: dict[str, Any]) -> int:
    conditions = public_pool_lead_conditions(**_public_pool_values(filters))
    return int(
        db.scalar(select(func.count(Lead.id)).where(*conditions))
        or 0
    )


def _followup_export_statement(filters: dict[str, Any]):
    current_assignment = aliased(Assignment, name="filtered_current_assignment")
    followup_assignment = aliased(Assignment, name="followup_assignment")
    receiver = aliased(Company, name="followup_receiver")
    creator = aliased(User, name="followup_creator_export")
    return (
        select(
            Lead.id.label("lead_id"),
            Lead.customer_name,
            followup_assignment.id.label("assignment_id"),
            receiver.name.label("receiver_company_name"),
            FollowUp.status,
            FollowUp.note,
            FollowUp.next_followup_at,
            creator.display_name.label("created_by_name"),
            FollowUp.created_at,
        )
        .join(followup_assignment, followup_assignment.lead_id == Lead.id)
        .join(FollowUp, FollowUp.assignment_id == followup_assignment.id)
        .outerjoin(current_assignment, current_assignment.id == Lead.current_assignment_id)
        .outerjoin(
            receiver,
            receiver.id
            == func.coalesce(
                followup_assignment.receiver_company_id,
                followup_assignment.company_id,
            ),
        )
        .outerjoin(creator, creator.id == FollowUp.created_by)
        .where(*_conditions(filters, current_assignment))
        .order_by(Lead.created_at.desc(), FollowUp.created_at.desc())
    )


def _iter_followup_export_rows(db: Session, filters: dict[str, Any]):
    rows = db.execute(
        _followup_export_statement(filters).execution_options(
            yield_per=EXPORT_STREAM_BATCH_SIZE
        )
    )
    for row in rows:
        yield {
            "客资编号": row.lead_id,
            "客户姓名": row.customer_name,
            "派发单编号": row.assignment_id,
            "接收加盟商": row.receiver_company_name,
            "跟进状态": row.status,
            "跟进内容": row.note,
            "下次跟进时间": row.next_followup_at.isoformat()
            if row.next_followup_at
            else None,
            "跟进人": row.created_by_name,
            "跟进时间": row.created_at.isoformat(),
        }


def _count_export_followups(db: Session, filters: dict[str, Any]) -> int:
    current_assignment = aliased(Assignment, name="count_current_assignment")
    followup_assignment = aliased(Assignment, name="count_followup_assignment")
    return int(
        db.scalar(
            select(func.count(FollowUp.id))
            .select_from(Lead)
            .join(followup_assignment, followup_assignment.lead_id == Lead.id)
            .join(FollowUp, FollowUp.assignment_id == followup_assignment.id)
            .outerjoin(
                current_assignment,
                current_assignment.id == Lead.current_assignment_id,
            )
            .where(*_conditions(filters, current_assignment))
        )
        or 0
    )


def _lead_export_row(row: LeadReportRow) -> dict[str, Any]:
    lead = row.lead
    assignment = row.assignment
    phone = decrypt_text(lead.phone_encrypted)
    if lead.phone_encrypted and phone is None:
        logger.error("lead export phone decrypt failed lead_id=%s", lead.id)
        raise LeadExportDataError("完整手机号解密失败")
    source_display = (
        f"其他（{lead.source_detail}）"
        if lead.source_channel == "OTHER" and lead.source_detail
        else lead.source_channel
    )
    franchise_handler_name, franchise_handler_kind = _franchise_handler(row)
    return {
        "客资编号": lead.id,
        "客户姓名": lead.customer_name,
        "完整手机号": phone,
        "来源类型": lead.source_kind,
        "来源渠道": source_display,
        "录入人员": row.submitter_name,
        "省份": lead.province,
        "城市": lead.city,
        "区县": lead.district,
        "地区编码": lead.region_code,
        "咨询类别": lead.category_code,
        "品牌": lead.brand_code,
        "客户需求": lead.need_summary,
        "预算下限": lead.budget_min,
        "预算上限": lead.budget_max,
        "客资状态": lead.status,
        "当前跟进状态": lead.current_follow_status,
        "派发状态": assignment.status if assignment else None,
        "接收加盟商": row.receiver_company_name,
        "加盟商跟进人": franchise_handler_name,
        "内部分配时间": (
            assignment.internal_assigned_at.isoformat()
            if assignment
            and assignment.internal_assigned_at
            and franchise_handler_kind == "FRANCHISE_EMPLOYEE"
            else None
        ),
        "派发运营人员": row.assigned_by_name,
        "派发时间": assignment.assigned_at.isoformat() if assignment else None,
        "最新跟进状态": row.latest_followup_status,
        "最新跟进内容": row.latest_followup_note,
        "下次跟进时间": (
            row.latest_followup_next_at.isoformat()
            if row.latest_followup_next_at
            else None
        ),
        "最新跟进人": row.latest_followup_by_name,
        "最新跟进时间": (
            row.latest_followup_at.isoformat() if row.latest_followup_at else None
        ),
        "创建时间": lead.created_at.isoformat(),
    }


def build_lead_export_workbook(
    db: Session,
    filters: dict[str, Any],
    *,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[Path, int]:
    beat = heartbeat or (lambda: None)
    beat()
    public_pool_scope = _upper(filters.get("scope")) == "PUBLIC_POOL"
    if public_pool_scope:
        total = _count_public_pool_report_rows(db, filters)
        report_rows = _iter_public_pool_report_rows(db, filters)
        followup_total = 0
        followup_rows = ()
    else:
        _sample, total = list_lead_report_rows(
            db,
            filters=filters,
            page_no=1,
            page_size=1,
        )
        report_rows = _iter_report_rows(db, filters)
        followup_total = _count_export_followups(db, filters)
        followup_rows = _iter_followup_export_rows(db, filters)
    temporary = NamedTemporaryFile(
        prefix="zhongshu-lead-export-",
        suffix=".xlsx",
        delete=False,
    )
    workbook_path = Path(temporary.name)
    temporary.close()
    try:
        row_heartbeat = beat if db.get_bind().dialect.name == "postgresql" else (lambda: None)
        worksheets = [
            WorksheetSpec(
                name="公海池明细" if public_pool_scope else "客资明细",
                fieldnames=LEAD_EXPORT_FIELDS,
                rows=(_lead_export_row(row) for row in report_rows),
                rows_per_sheet=LEAD_EXPORT_ROWS_PER_FILE,
                total_rows=total,
            )
        ]
        if not public_pool_scope:
            worksheets.append(
                WorksheetSpec(
                    name="跟进记录",
                    fieldnames=FOLLOWUP_EXPORT_FIELDS,
                    rows=followup_rows,
                    rows_per_sheet=FOLLOWUP_EXPORT_ROWS_PER_FILE,
                    total_rows=followup_total,
                )
            )
        try:
            row_counts = write_xlsx(
                workbook_path,
                worksheets,
                heartbeat=row_heartbeat,
                heartbeat_row_interval=EXPORT_HEARTBEAT_ROW_INTERVAL,
                max_uncompressed_bytes=MAX_EXPORT_XML_BYTES,
            )
        except XlsxSizeLimitError as exc:
            raise LeadExportLimitError(str(exc)) from exc
        if workbook_path.stat().st_size > MAX_EXPORT_WORKBOOK_BYTES:
            raise LeadExportLimitError(
                "导出的 Excel 文件超过安全大小，请缩小筛选范围后分批导出"
            )
        beat()
        return workbook_path, row_counts[worksheets[0].name]
    except Exception:
        workbook_path.unlink(missing_ok=True)
        raise


def _claim_next_lead_export_task(
    db: Session,
    *,
    stale_before: datetime,
) -> tuple[str, str] | None:
    now = datetime.now(timezone.utc)
    task = db.scalar(
        select(LeadExportTask)
        .where(
            or_(
                LeadExportTask.status == "PENDING",
                (
                    (LeadExportTask.status == "RUNNING")
                    & (LeadExportTask.started_at < stale_before)
                ),
            )
        )
        .order_by(LeadExportTask.created_at.asc(), LeadExportTask.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task is None:
        db.rollback()
        return None
    attempt_token = uuid.uuid4().hex
    task.status = "RUNNING"
    task.started_at = now
    task.completed_at = None
    task.error_message = None
    task.attempt_token = attempt_token
    db.commit()
    return task.id, attempt_token


def _renew_lead_export_lease(
    db: Session,
    *,
    task_id: str,
    attempt_token: str,
) -> None:
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        # PostgreSQL yield_per uses a server-side cursor. Renewing the lease on
        # the export Session would commit that cursor's transaction mid-stream.
        _renew_lead_export_lease_from_bind(
            bind,
            task_id=task_id,
            attempt_token=attempt_token,
        )
        return
    _renew_lead_export_lease_in_session(
        db,
        task_id=task_id,
        attempt_token=attempt_token,
    )


def _renew_lead_export_lease_from_bind(
    bind,
    *,
    task_id: str,
    attempt_token: str,
) -> None:
    engine = getattr(bind, "engine", bind)
    with Session(bind=engine, expire_on_commit=False) as lease_db:
        _renew_lead_export_lease_in_session(
            lease_db,
            task_id=task_id,
            attempt_token=attempt_token,
        )


def _lead_export_upload_progress(
    db: Session,
    *,
    task_id: str,
    attempt_token: str,
    report_progress: Callable[[], None],
    clock: Callable[[], float] = monotonic,
) -> Callable[[], None]:
    """Report real upload progress and renew a PostgreSQL lease at most once/minute."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return report_progress
    renewal_lock = Lock()
    last_renewed_at = clock()

    def progress() -> None:
        nonlocal last_renewed_at
        report_progress()
        with renewal_lock:
            now = clock()
            if now - last_renewed_at < LEAD_EXPORT_UPLOAD_LEASE_RENEW_SECONDS:
                return
            _renew_lead_export_lease_from_bind(
                bind,
                task_id=task_id,
                attempt_token=attempt_token,
            )
            last_renewed_at = now

    return progress


def _renew_lead_export_lease_in_session(
    lease_db: Session,
    *,
    task_id: str,
    attempt_token: str,
) -> None:
    result = lease_db.execute(
        update(LeadExportTask)
        .where(
            LeadExportTask.id == task_id,
            LeadExportTask.status == "RUNNING",
            LeadExportTask.attempt_token == attempt_token,
        )
        .values(started_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        lease_db.rollback()
        raise LeadExportLeaseLostError("导出任务租约已被其他工作进程接管")
    lease_db.commit()


def _attempt_cleanup_event_key(task_id: str, attempt_token: str) -> str:
    return f"lead-export-attempt:{task_id}:{attempt_token}"


def _attempt_object_key(
    *,
    task_id: str,
    attempt_token: str,
    created_at: datetime,
) -> str:
    return f"lead-exports/{created_at:%Y/%m}/{task_id}/{attempt_token}.xlsx"


def _persist_attempt_cleanup_intent(
    db: Session,
    *,
    object_key: str,
    task_id: str,
    attempt_token: str,
) -> StorageCleanupOutbox:
    cleanup = enqueue_storage_cleanup(
        db,
        event_key=_attempt_cleanup_event_key(task_id, attempt_token),
        object_key=object_key,
        source_type="lead_export_attempt",
        source_id=task_id,
        reason="完整手机号导出尝试的中断兜底清理",
    )
    cleanup.next_attempt_at = (
        datetime.now(timezone.utc) + LEAD_EXPORT_ATTEMPT_CLEANUP_DELAY
    )
    db.commit()
    return cleanup


def _delete_or_schedule_attempt_cleanup(
    db: Session,
    *,
    storage,
    object_key: str,
    task_id: str,
    attempt_token: str,
) -> None:
    event_key = _attempt_cleanup_event_key(task_id, attempt_token)
    try:
        storage.delete(object_key)
    except Exception as exc:
        logger.warning(
            "lead export orphan immediate cleanup failed task_id=%s",
            task_id,
            exc_info=True,
        )
        cleanup = enqueue_storage_cleanup(
            db,
            event_key=event_key,
            object_key=object_key,
            source_type="lead_export_attempt",
            source_id=task_id,
            reason="导出任务租约失效或失败后的敏感文件清理",
        )
        cleanup.status = "PENDING"
        cleanup.next_attempt_at = datetime.now(timezone.utc)
        cleanup.last_error = f"{type(exc).__name__}: immediate cleanup failed"
        db.commit()
        return
    cleanup = db.scalar(
        select(StorageCleanupOutbox).where(StorageCleanupOutbox.event_key == event_key)
    )
    if cleanup is None:
        cleanup = enqueue_storage_cleanup(
            db,
            event_key=event_key,
            object_key=object_key,
            source_type="lead_export_attempt",
            source_id=task_id,
            reason="导出任务租约失效或失败后的敏感文件清理",
        )
    cleanup.status = "DELETED"
    cleanup.deleted_at = datetime.now(timezone.utc)
    cleanup.next_attempt_at = None
    cleanup.last_error = None
    db.commit()


def process_lead_export_tasks(
    db: Session,
    *,
    limit: int = 10,
    progress: Callable[[], None] | None = None,
) -> dict[str, int]:
    stale_before = datetime.now(timezone.utc) - LEAD_EXPORT_LEASE_TIMEOUT
    report_progress = progress or (lambda: None)

    claimed = 0
    completed = 0
    failed = 0
    superseded = 0
    storage = get_storage()
    for _ in range(max(0, int(limit))):
        claim = _claim_next_lead_export_task(db, stale_before=stale_before)
        if claim is None:
            break
        task_id, attempt_token = claim
        claimed += 1
        report_progress()
        attempt_object_key: str | None = None
        cleanup_intent: StorageCleanupOutbox | None = None
        workbook_path: Path | None = None
        try:
            task = db.get(LeadExportTask, task_id)
            if task is None or task.attempt_token != attempt_token:
                db.rollback()
                superseded += 1
                continue
            def renew_lease_and_report_progress() -> None:
                _renew_lead_export_lease(
                    db,
                    task_id=task_id,
                    attempt_token=attempt_token,
                )
                report_progress()

            workbook_path, row_count = build_lead_export_workbook(
                db,
                task.filters_json,
                heartbeat=renew_lease_and_report_progress,
            )
            completed_at = datetime.now(timezone.utc)
            expires_at = completed_at + timedelta(days=7)
            file_name = f"客资完整导出_{completed_at:%Y%m%d_%H%M%S}.xlsx"
            attempt_object_key = _attempt_object_key(
                task_id=task_id,
                attempt_token=attempt_token,
                created_at=completed_at,
            )
            cleanup_intent = _persist_attempt_cleanup_intent(
                db,
                object_key=attempt_object_key,
                task_id=task_id,
                attempt_token=attempt_token,
            )
            renew_lease_and_report_progress()
            upload_progress = _lead_export_upload_progress(
                db,
                task_id=task_id,
                attempt_token=attempt_token,
                report_progress=report_progress,
            )
            stored = storage.save_file(
                workbook_path,
                prefix=f"lead-exports/{completed_at:%Y/%m}/{task.id}",
                filename=file_name,
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                object_key=attempt_object_key,
                progress_callback=upload_progress,
            )
            renew_lease_and_report_progress()
            workbook_path.unlink()
            workbook_path = None
            result = db.execute(
                update(LeadExportTask)
                .where(
                    LeadExportTask.id == task_id,
                    LeadExportTask.status == "RUNNING",
                    LeadExportTask.attempt_token == attempt_token,
                )
                .values(
                    status="COMPLETED",
                    attempt_token=None,
                    row_count=row_count,
                    object_key=stored.object_key,
                    file_name=file_name,
                    mime_type=stored.mime_type,
                    file_size=stored.size,
                    sha256=stored.sha256,
                    completed_at=completed_at,
                    expires_at=expires_at,
                )
            )
            if result.rowcount != 1:
                db.rollback()
                _delete_or_schedule_attempt_cleanup(
                    db,
                    storage=storage,
                    object_key=stored.object_key,
                    task_id=task_id,
                    attempt_token=attempt_token,
                )
                attempt_object_key = None
                cleanup_intent = None
                superseded += 1
                continue
            cleanup_intent.event_key = f"lead-export-expire:{task_id}"
            cleanup_intent.source_type = "lead_export_task"
            cleanup_intent.reason = "完整手机号导出文件到期清理"
            cleanup_intent.next_attempt_at = expires_at
            db.commit()
            attempt_object_key = None
            cleanup_intent = None
            completed += 1
        except Exception as exc:  # pragma: no cover - storage/driver boundary
            db.rollback()
            if attempt_object_key is not None and cleanup_intent is not None:
                _delete_or_schedule_attempt_cleanup(
                    db,
                    storage=storage,
                    object_key=attempt_object_key,
                    task_id=task_id,
                    attempt_token=attempt_token,
                )
                attempt_object_key = None
                cleanup_intent = None
            error_message = (
                str(exc)
                if isinstance(exc, (LeadExportLimitError, LeadExportDataError))
                else f"{type(exc).__name__}: 导出任务处理失败"
            )
            result = db.execute(
                update(LeadExportTask)
                .where(
                    LeadExportTask.id == task_id,
                    LeadExportTask.status == "RUNNING",
                    LeadExportTask.attempt_token == attempt_token,
                )
                .values(
                    status="FAILED",
                    attempt_token=None,
                    error_message=error_message,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount == 1:
                db.commit()
                failed += 1
            else:
                db.rollback()
                superseded += 1
            logger.exception("lead export task failed task_id=%s", task_id)
        finally:
            if workbook_path is not None:
                try:
                    workbook_path.unlink(missing_ok=True)
                except OSError:
                    logger.error(
                        "lead export temporary workbook cleanup failed task_id=%s path=%s",
                        task_id,
                        workbook_path,
                        exc_info=True,
                    )
    return {
        "claimed": claimed,
        "completed": completed,
        "failed": failed,
        "superseded": superseded,
    }
