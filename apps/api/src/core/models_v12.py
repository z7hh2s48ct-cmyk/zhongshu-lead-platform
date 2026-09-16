from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import Assignment, Lead, ReturnRequest, TimestampMixin, VerificationTask, uuid_str


def _extend_mapped_column(model: type, name: str, column: Column[Any]) -> None:
    """Append a V1.2 column to an existing mapped table and mapper exactly once.

    V1.0.1 mapped classes remain source-compatible while V1.2 services get
    strongly named attributes. Database DDL is owned by migration 0002.
    """

    table = model.__table__
    if name not in table.c:
        table.append_column(column)
    if name not in model.__mapper__.attrs:
        model.__mapper__.add_property(name, table.c[name])


def _ensure_index(name: str, *columns: Column[Any]) -> None:
    table = columns[0].table
    if not any(index.name == name for index in table.indexes):
        Index(name, *columns)


_extend_mapped_column(Lead, "source_kind", Column("source_kind", String(32), nullable=True))
_extend_mapped_column(
    Lead,
    "submitter_user_id",
    Column("submitter_user_id", String(36), ForeignKey("users.id", name="fk_leads_submitter_user_v12", ondelete="SET NULL"), nullable=True),
)
_extend_mapped_column(
    Lead,
    "supplier_company_id",
    Column("supplier_company_id", String(36), ForeignKey("companies.id", name="fk_leads_supplier_company_v12", ondelete="SET NULL"), nullable=True),
)
_extend_mapped_column(
    Lead,
    "consent_confirmed",
    Column("consent_confirmed", Boolean, nullable=False, default=False, server_default=false()),
)
_extend_mapped_column(Lead, "phone_fingerprint", Column("phone_fingerprint", String(64), nullable=True))
_extend_mapped_column(Lead, "duplicate_status", Column("duplicate_status", String(32), nullable=True))
_extend_mapped_column(Lead, "review_status", Column("review_status", String(32), nullable=True))
_extend_mapped_column(Lead, "review_note", Column("review_note", Text, nullable=True))
_extend_mapped_column(Lead, "submitted_at", Column("submitted_at", DateTime(timezone=True), nullable=True))
_extend_mapped_column(Lead, "reviewed_at", Column("reviewed_at", DateTime(timezone=True), nullable=True))

_extend_mapped_column(Assignment, "claim_points", Column("claim_points", Integer, nullable=True))
_extend_mapped_column(Assignment, "appeal_deadline_at", Column("appeal_deadline_at", DateTime(timezone=True), nullable=True))
_extend_mapped_column(Assignment, "appeal_paused_at", Column("appeal_paused_at", DateTime(timezone=True), nullable=True))
_extend_mapped_column(Assignment, "appeal_remaining_seconds", Column("appeal_remaining_seconds", Integer, nullable=True))
_extend_mapped_column(Assignment, "appeal_resumed_at", Column("appeal_resumed_at", DateTime(timezone=True), nullable=True))
_extend_mapped_column(Assignment, "reward_due_at", Column("reward_due_at", DateTime(timezone=True), nullable=True))
_extend_mapped_column(
    Assignment,
    "supplier_company_id",
    Column("supplier_company_id", String(36), ForeignKey("companies.id", name="fk_assignments_supplier_company_v12", ondelete="SET NULL"), nullable=True),
)
_extend_mapped_column(
    Assignment,
    "receiver_company_id",
    Column("receiver_company_id", String(36), ForeignKey("companies.id", name="fk_assignments_receiver_company_v12", ondelete="SET NULL"), nullable=True),
)

_extend_mapped_column(VerificationTask, "task_type", Column("task_type", String(32), nullable=True))
_extend_mapped_column(
    VerificationTask,
    "return_request_id",
    Column("return_request_id", String(36), ForeignKey("return_requests.id", name="fk_verification_return_request_v12", ondelete="SET NULL"), nullable=True),
)
_extend_mapped_column(
    VerificationTask,
    "assignment_id",
    Column("assignment_id", String(36), ForeignKey("assignments.id", name="fk_verification_assignment_v12", ondelete="SET NULL"), nullable=True),
)
_extend_mapped_column(VerificationTask, "contact_result", Column("contact_result", String(64), nullable=True))
_extend_mapped_column(
    VerificationTask,
    "verification_conclusion",
    Column("verification_conclusion", String(64), nullable=True),
)

_extend_mapped_column(ReturnRequest, "appeal_deadline_at", Column("appeal_deadline_at", DateTime(timezone=True), nullable=True))
_extend_mapped_column(
    ReturnRequest,
    "verification_task_id",
    Column("verification_task_id", String(36), ForeignKey("verification_tasks.id", name="fk_return_verification_task_v12", ondelete="SET NULL"), nullable=True),
)
_extend_mapped_column(ReturnRequest, "final_decision_reason", Column("final_decision_reason", Text, nullable=True))

for index_name, columns in [
    ("ix_leads_source_kind", (Lead.__table__.c.source_kind,)),
    ("ix_leads_submitter_user_id", (Lead.__table__.c.submitter_user_id,)),
    ("ix_leads_supplier_company_id", (Lead.__table__.c.supplier_company_id,)),
    ("ix_leads_phone_fingerprint", (Lead.__table__.c.phone_fingerprint,)),
    ("ix_leads_duplicate_status", (Lead.__table__.c.duplicate_status,)),
    ("ix_leads_review_status", (Lead.__table__.c.review_status,)),
    ("ix_assignments_appeal_deadline_at", (Assignment.__table__.c.appeal_deadline_at,)),
    ("ix_assignments_reward_due_at", (Assignment.__table__.c.reward_due_at,)),
    ("ix_assignments_supplier_company_id", (Assignment.__table__.c.supplier_company_id,)),
    ("ix_assignments_receiver_company_id", (Assignment.__table__.c.receiver_company_id,)),
    ("ix_verification_tasks_task_type", (VerificationTask.__table__.c.task_type,)),
    ("ix_verification_tasks_return_request_id", (VerificationTask.__table__.c.return_request_id,)),
    ("ix_verification_tasks_assignment_id", (VerificationTask.__table__.c.assignment_id,)),
    ("ix_return_requests_appeal_deadline_at", (ReturnRequest.__table__.c.appeal_deadline_at,)),
    ("ix_return_requests_verification_task_id", (ReturnRequest.__table__.c.verification_task_id,)),
]:
    _ensure_index(index_name, *columns)


class CalendarDay(Base, TimestampMixin):
    __tablename__ = "calendar_days"
    __table_args__ = (
        Index("ix_calendar_days_is_workday", "is_workday"),
        Index("ix_calendar_days_updated_by", "updated_by"),
    )

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    is_workday: Mapped[bool] = mapped_column(Boolean, nullable=False)
    holiday_name: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32), default="MANUAL", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CompanyLeadCapability(Base, TimestampMixin):
    __tablename__ = "company_lead_capabilities"
    __table_args__ = (
        UniqueConstraint("company_id", "capability_code", name="uq_company_lead_capability"),
        Index("ix_company_lead_capability_company", "company_id"),
        Index("ix_company_lead_capability_code", "capability_code"),
        Index("ix_company_lead_capability_review", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    capability_code: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="APPROVED", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)


class CompanyServiceAreaV12(Base, TimestampMixin):
    __tablename__ = "company_service_areas_v12"
    __table_args__ = (
        UniqueConstraint("company_id", "region_code", name="uq_company_service_area_v12"),
        Index("ix_company_service_area_company", "company_id"),
        Index("ix_company_service_area_region_active", "region_code", "active"),
        Index("ix_company_service_area_review", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    region_level: Mapped[str] = mapped_column(String(16), nullable=False, default="DISTRICT")
    is_primary_city: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)


class LeadDedupEvent(Base, TimestampMixin):
    __tablename__ = "lead_dedup_events"
    __table_args__ = (
        Index("ix_lead_dedup_lead", "lead_id"),
        Index("ix_lead_dedup_fingerprint_created", "phone_fingerprint", "created_at"),
        Index("ix_lead_dedup_checkpoint", "checkpoint"),
        Index("ix_lead_dedup_decision", "decision"),
        Index("ix_lead_dedup_matched_lead", "matched_lead_id"),
        Index("ix_lead_dedup_matched_assignment", "matched_assignment_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    phone_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    matched_lead_id: Mapped[str | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    matched_assignment_id: Mapped[str | None] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"))
    window_days: Mapped[int | None] = mapped_column(Integer)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DedupOverride(Base, TimestampMixin):
    __tablename__ = "dedup_overrides"
    __table_args__ = (
        Index("ix_dedup_override_lead", "lead_id"),
        Index("ix_dedup_override_event", "dedup_event_id"),
        Index("ix_dedup_override_approved_by", "approved_by"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    dedup_event_id: Mapped[str | None] = mapped_column(ForeignKey("lead_dedup_events.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupplierLeadReward(Base, TimestampMixin):
    __tablename__ = "supplier_lead_rewards"
    __table_args__ = (
        UniqueConstraint("assignment_id", name="uq_supplier_reward_assignment"),
        CheckConstraint("reward_points >= 0", name="ck_supplier_reward_nonnegative"),
        Index("ix_supplier_reward_lead", "lead_id"),
        Index("ix_supplier_reward_supplier", "supplier_company_id"),
        Index("ix_supplier_reward_receiver", "receiver_company_id"),
        Index("ix_supplier_reward_status_due", "status", "reward_due_at"),
        Index("ix_supplier_reward_appeal_deadline", "appeal_deadline_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="RESTRICT"))
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="RESTRICT"), nullable=False)
    supplier_company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    receiver_company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(32), default="WAITING_CLAIM", nullable=False)
    claim_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reward_ratio_bps: Mapped[int] = mapped_column(Integer, default=3000, nullable=False)
    reward_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    appeal_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reward_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ledger_id: Mapped[str | None] = mapped_column(ForeignKey("points_ledgers.id", ondelete="SET NULL"))
    reversal_ledger_id: Mapped[str | None] = mapped_column(ForeignKey("points_ledgers.id", ondelete="SET NULL"))
    exception_reason: Mapped[str | None] = mapped_column(Text)


class V12MigrationCheckpoint(Base, TimestampMixin):
    __tablename__ = "v12_migration_checkpoints"
    __table_args__ = (Index("ix_v12_migration_status", "status"),)

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(255))
    processed_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
