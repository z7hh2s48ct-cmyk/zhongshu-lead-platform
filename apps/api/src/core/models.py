from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(128))
    phone_encrypted: Mapped[str | None] = mapped_column(Text)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False, index=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    roles: Mapped[list[Role]] = relationship(secondary="user_roles", back_populates="users")
    company: Mapped[Company | None] = relationship(back_populates="members", foreign_keys=[company_id])
    wechat_identity: Mapped[WechatIdentity | None] = relationship(back_populates="user", uselist=False)


class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    system_role: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list[User]] = relationship(secondary="user_roles", back_populates="roles")
    permissions: Mapped[list[Permission]] = relationship(secondary="role_permissions", back_populates="roles")


class Permission(Base, TimestampMixin):
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    roles: Mapped[list[Role]] = relationship(secondary="role_permissions", back_populates="permissions")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_roles_single_business_role"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False, index=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(64))
    contact_phone_encrypted: Mapped[str | None] = mapped_column(Text)
    contact_phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    level_code: Mapped[str] = mapped_column(String(32), default="V1", nullable=False)
    primary_user_id: Mapped[str | None] = mapped_column(String(36))
    notes: Mapped[str | None] = mapped_column(Text)

    members: Mapped[list[User]] = relationship(back_populates="company", foreign_keys="User.company_id")
    service_regions: Mapped[list[CompanyServiceRegion]] = relationship(back_populates="company", cascade="all, delete-orphan")
    capabilities: Mapped[list[CompanyCapability]] = relationship(back_populates="company", cascade="all, delete-orphan")
    points_account: Mapped[PointsAccount | None] = relationship(back_populates="company", uselist=False, cascade="all, delete-orphan")


class CompanyAccountRequest(Base, TimestampMixin):
    """A franchise owner's employee-account request, decided by platform staff."""

    __tablename__ = "company_account_requests"
    __table_args__ = (
        Index("ix_company_account_requests_company_status", "company_id", "status"),
        Index("ix_company_account_requests_target_status", "target_user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    request_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    target_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    requested_username: Mapped[str | None] = mapped_column(String(64))
    requested_display_name: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)


class CompanyServiceRegion(Base, TimestampMixin):
    __tablename__ = "company_service_regions"
    __table_args__ = (UniqueConstraint("company_id", "region_code", name="uq_company_region"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    region_code: Mapped[str] = mapped_column(String(32), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    company: Mapped[Company] = relationship(back_populates="service_regions")


class CompanyCapability(Base, TimestampMixin):
    __tablename__ = "company_capabilities"
    __table_args__ = (UniqueConstraint("company_id", "category_code", "brand_code", name="uq_company_capability"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    category_code: Mapped[str] = mapped_column(String(64), index=True)
    brand_code: Mapped[str | None] = mapped_column(String(64), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    company: Mapped[Company] = relationship(back_populates="capabilities")


class Region(Base, TimestampMixin):
    __tablename__ = "regions"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_code: Mapped[str | None] = mapped_column(String(32), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DictionaryItem(Base, TimestampMixin):
    __tablename__ = "dictionary_items"
    __table_args__ = (UniqueConstraint("domain", "code", "version", name="uq_dictionary_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    domain: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class InviteToken(Base, TimestampMixin):
    __tablename__ = "invite_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # P2-01：邀请发出时的展示对象快照——公司/负责人后续改名不影响历史追溯。
    invitee_name_snapshot: Mapped[str | None] = mapped_column(String(64))
    company_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    # N9：消费邀请的真实使用者——绑定事务内写回，归因不随主账号换绑漂移；
    # 存量行（N9 前）无值，展示层按「未记录」处理，禁止用当前主账号猜测。
    # 约束名与 0008 迁移显式对齐：0001 建表走 create_all，无名 FK 会被
    # PostgreSQL 自动命名为 invite_tokens_used_by_user_id_fkey，导致迁移
    # downgrade 按名删除时失配（CI postgres-migration 曾因此失败）。
    used_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_invite_tokens_used_by_user")
    )


class WechatIdentity(Base, TimestampMixin):
    __tablename__ = "wechat_identities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    openid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    unionid: Mapped[str | None] = mapped_column(String(128), index=True)
    nickname: Mapped[str | None] = mapped_column(String(128))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)

    user: Mapped[User] = relationship(back_populates="wechat_identity")


class SyncBatch(Base, TimestampMixin):
    __tablename__ = "sync_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    source: Mapped[str] = mapped_column(String(32), default="FEISHU", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(255))
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    error_message: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadExportTask(Base, TimestampMixin):
    __tablename__ = "lead_export_tasks"
    __table_args__ = (
        Index("ix_lead_export_status_created", "status", "created_at"),
        Index("ix_lead_export_requester_created", "requested_by", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    requested_by_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    attempt_token: Mapped[str | None] = mapped_column(String(64))
    filters_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    include_full_phone: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512))
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Lead(Base, TimestampMixin):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("source_app_token", "source_table_id", "source_record_id", name="uq_lead_source_record"),
        Index("ix_leads_status_region", "status", "region_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    source_type: Mapped[str] = mapped_column(String(32), default="FEISHU", nullable=False)
    source_app_token: Mapped[str | None] = mapped_column(String(128))
    source_table_id: Mapped[str | None] = mapped_column(String(128))
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    source_channel: Mapped[str | None] = mapped_column(String(64), index=True)
    source_detail: Mapped[str | None] = mapped_column(String(128))
    customer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    province: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(64), index=True)
    district: Mapped[str | None] = mapped_column(String(64))
    region_code: Mapped[str | None] = mapped_column(String(32), index=True)
    category_code: Mapped[str | None] = mapped_column(String(64), index=True)
    brand_code: Mapped[str | None] = mapped_column(String(64), index=True)
    need_summary: Mapped[str | None] = mapped_column(Text)
    budget_min: Mapped[int | None] = mapped_column(Integer)
    budget_max: Mapped[int | None] = mapped_column(Integer)
    acquisition_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_test: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="IMPORTED", nullable=False, index=True)
    pending_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    current_assignment_id: Mapped[str | None] = mapped_column(String(36), index=True)
    current_follow_status: Mapped[str | None] = mapped_column(String(32), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", name="fk_leads_deleted_by_user", ondelete="SET NULL"), index=True
    )
    delete_reason: Mapped[str | None] = mapped_column(Text)
    snapshot_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LeadImportIssue(Base, TimestampMixin):
    __tablename__ = "lead_import_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str | None] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    sync_batch_id: Mapped[str | None] = mapped_column(ForeignKey("sync_batches.id", ondelete="SET NULL"), index=True)
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    field_name: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class LeadDuplicateRelation(Base, TimestampMixin):
    __tablename__ = "lead_duplicate_relations"
    __table_args__ = (UniqueConstraint("lead_id", "duplicate_lead_id", name="uq_duplicate_pair"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    duplicate_lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(32))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationTemplate(Base, TimestampMixin):
    __tablename__ = "verification_templates"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_verification_template_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationTask(Base, TimestampMixin):
    __tablename__ = "verification_tasks"
    __table_args__ = (Index("ix_verification_assignee_status", "assignee_user_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[str | None] = mapped_column(ForeignKey("verification_templates.id", ondelete="SET NULL"))
    template_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    assignee_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    assigned_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class VerificationSubmission(Base, TimestampMixin):
    __tablename__ = "verification_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    task_id: Mapped[str] = mapped_column(ForeignKey("verification_tasks.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    invalid_reason: Mapped[str | None] = mapped_column(String(64))
    answers_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    corrections_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class Assignment(Base, TimestampMixin):
    __tablename__ = "assignments"
    __table_args__ = (
        Index("ix_assignment_company_status", "company_id", "status"),
        Index("ix_assignment_lead_status", "lead_id", "status"),
        Index("ix_assignment_internal_assignee_status", "internal_assignee_user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="RESTRICT"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING_CLAIM", nullable=False, index=True)
    points_price: Mapped[int] = mapped_column(Integer, nullable=False)
    price_rule_id: Mapped[str | None] = mapped_column(String(36))
    price_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lead_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    assigned_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_followup_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    # 公司内部协作不改变平台对公司的派发结果；仅负责人和获分配员工可见。
    internal_assignee_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    internal_assigned_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    internal_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssignmentEvent(Base):
    __tablename__ = "assignment_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PointsAccount(Base, TimestampMixin):
    __tablename__ = "points_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    company: Mapped[Company] = relationship(back_populates="points_account")


class PointsLedger(Base):
    __tablename__ = "points_ledgers"
    __table_args__ = (
        UniqueConstraint("company_id", "idempotency_key", name="uq_points_idempotency"),
        Index("ix_points_company_created", "company_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    account_id: Mapped[str] = mapped_column(ForeignKey("points_accounts.id", ondelete="RESTRICT"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), index=True)
    ledger_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_type: Mapped[str] = mapped_column(String(64), nullable=False)
    business_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(128), index=True)
    related_ledger_id: Mapped[str | None] = mapped_column(ForeignKey("points_ledgers.id", ondelete="SET NULL"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PointsPackage(Base, TimestampMixin):
    __tablename__ = "points_packages"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_points_package_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cash_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bonus_points: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    level_code: Mapped[str] = mapped_column(String(32), default="V1", nullable=False)
    entitlements_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadPriceRule(Base, TimestampMixin):
    __tablename__ = "lead_price_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    region_code: Mapped[str | None] = mapped_column(String(32), index=True)
    category_code: Mapped[str | None] = mapped_column(String(64), index=True)
    brand_code: Mapped[str | None] = mapped_column(String(64), index=True)
    level_code: Mapped[str | None] = mapped_column(String(32), index=True)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReturnRequest(Base, TimestampMixin):
    __tablename__ = "return_requests"
    __table_args__ = (UniqueConstraint("assignment_id", name="uq_return_assignment"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="RESTRICT"), index=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="RESTRICT"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False, index=True)
    submitted_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    refund_points: Mapped[int | None] = mapped_column(Integer)
    refund_ledger_id: Mapped[str | None] = mapped_column(ForeignKey("points_ledgers.id", ondelete="SET NULL"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReturnEvidence(Base, TimestampMixin):
    __tablename__ = "return_evidences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    return_request_id: Mapped[str] = mapped_column(ForeignKey("return_requests.id", ondelete="CASCADE"), index=True)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("event_key", name="uq_outbox_event_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageCleanupOutbox(Base):
    __tablename__ = "storage_cleanup_outbox"
    __table_args__ = (UniqueConstraint("event_key", name="uq_storage_cleanup_event_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_namespace: Mapped[str | None] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    scene: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deep_link: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SystemConfig(Base, TimestampMixin):
    __tablename__ = "system_configs"
    __table_args__ = (UniqueConstraint("domain", "key", "version", name="uq_config_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    domain: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_actor_created", "actor_user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    actor_role_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
