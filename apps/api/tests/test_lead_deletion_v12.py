from __future__ import annotations

from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import (
    Assignment,
    Company,
    Lead,
    LeadImportIssue,
    ReturnRequest,
    User,
    VerificationSubmission,
    VerificationTask,
)
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.core.time import as_utc
from apps.api.src.services.lead_deletion_v12 import (
    delete_operation_lead,
    preview_lead_deletion,
)


def _principal(user_id: str, role: str = "OPERATION") -> Principal:
    return Principal(
        user_id=user_id,
        display_name="删除验收运营",
        company_id=None,
        role_codes=frozenset({role}),
        permission_codes=frozenset({"*"} if role == "SUPER_ADMIN" else {"lead.manual.manage"}),
        session_version=1,
    )


@pytest.fixture()
def owned_lead(db: Session):
    user = User(display_name="客资原录入人", status="ACTIVE")
    db.add(user)
    db.flush()
    lead = Lead(
        source_type="PLATFORM_MANUAL",
        source_kind="PLATFORM_MANUAL",
        submitter_user_id=user.id,
        customer_name="误录客户",
        phone_encrypted=encrypt_text("13800139101"),
        phone_hash=hash_phone("13800139101"),
        status="DRAFT",
        review_status="DRAFT",
    )
    db.add(lead)
    db.commit()
    return lead, _principal(user.id)


@pytest.mark.parametrize("source", ["PLATFORM_MANUAL", "FEISHU_IMPORT"])
@pytest.mark.parametrize(
    "status",
    ["DRAFT", "DUPLICATE", "READY_DISPATCH", "PUBLIC_POOL", "PENDING_REVIEW", "PENDING_OPERATION_DISPOSITION"],
)
def test_owner_can_soft_delete_each_allowed_source_and_state(db, owned_lead, source, status):
    lead, principal = owned_lead
    lead.source_kind = source
    lead.status = status
    db.flush()

    preview = preview_lead_deletion(db, lead_id=lead.id, principal=principal)
    assert preview["deletable"] is True
    assert preview["blockers"] == []
    assert preview["deleted"] is False

    result = delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="  重复录入  ")
    db.flush()
    assert result.idempotent is False
    assert result.lead is lead
    assert lead.deleted_at is not None
    assert lead.deleted_by == principal.user_id
    assert lead.delete_reason == "重复录入"
    assert lead.status == status
    assert db.get(Lead, lead.id) is lead


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "TELESALES", "FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"])
def test_only_operation_role_can_delete_even_its_own_lead(db, owned_lead, role):
    lead, owner = owned_lead
    principal = _principal(owner.user_id, role)
    for action in (
        lambda: preview_lead_deletion(db, lead_id=lead.id, principal=principal),
        lambda: delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="本人误录"),
    ):
        with pytest.raises(AppError) as error:
            action()
        assert error.value.status_code == 403
    assert lead.deleted_at is None


def test_another_operation_can_delete_the_lead(db, owned_lead):
    lead, _ = owned_lead
    other = _principal("another-operation")
    assert preview_lead_deletion(db, lead_id=lead.id, principal=other)["deletable"] is True
    delete_operation_lead(db, lead_id=lead.id, principal=other, reason="运营清理")
    assert lead.deleted_at is not None


@pytest.mark.parametrize("source", ["SUPPLIER_H5", "FEISHU_LEGACY", None])
def test_operation_can_delete_every_lead_source(db, owned_lead, source):
    lead, principal = owned_lead
    lead.source_kind = source
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="停止处理")
    assert lead.deleted_at is not None


@pytest.mark.parametrize("status", ["PENDING_TELESALES_VERIFY", "DISPATCHED", "CLAIMED", "FOLLOWING", "COMPLETED", "INVALID", "CLOSED"])
def test_operation_can_delete_every_lead_state(db, owned_lead, status):
    lead, principal = owned_lead
    lead.status = status
    preview = preview_lead_deletion(db, lead_id=lead.id, principal=principal)
    assert preview["deletable"] is True
    assert preview["blockers"] == []
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="停止处理")
    assert lead.deleted_at is not None


@pytest.mark.parametrize("reason", ["", " ", "错", " 错 "])
def test_delete_requires_a_reason_of_at_least_two_characters(db, owned_lead, reason):
    lead, principal = owned_lead
    with pytest.raises(AppError) as error:
        delete_operation_lead(db, lead_id=lead.id, principal=principal, reason=reason)
    assert error.value.status_code == 422
    assert lead.deleted_at is None


def _assignment(db, lead, principal, status="RELEASED"):
    company = Company(code="DELETE-HISTORY", name="历史接收公司", status="ACTIVE")
    db.add(company)
    db.flush()
    assignment = Assignment(
        lead_id=lead.id,
        company_id=company.id,
        assigned_by=principal.user_id,
        points_price=100,
        status=status,
    )
    db.add(assignment)
    db.flush()
    return assignment


def test_released_assignment_history_is_reported_and_preserved_after_deletion(db, owned_lead):
    lead, principal = owned_lead
    _assignment(db, lead, principal)
    lead.status = "READY_DISPATCH"
    lead.current_assignment_id = None
    preview = preview_lead_deletion(db, lead_id=lead.id, principal=principal)
    assert preview["impact"]["assignment_history"] == 1
    assert preview["blockers"] == []
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="停止处理")
    assert lead.deleted_at is not None
    assert db.scalar(select(Assignment.id).where(Assignment.lead_id == lead.id)) is not None


@pytest.mark.parametrize("status", ["PENDING", "ASSIGNED", "IN_PROGRESS"])
def test_active_verification_is_reported_but_does_not_block_deletion(db, owned_lead, status):
    lead, principal = owned_lead
    db.add(VerificationTask(lead_id=lead.id, status=status))
    db.flush()
    preview = preview_lead_deletion(db, lead_id=lead.id, principal=principal)
    assert preview["impact"]["active_verification_tasks"] == 1
    assert preview["blockers"] == []
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="停止处理")
    assert lead.deleted_at is not None


@pytest.mark.parametrize("status", ["DRAFT", "SUBMITTED", "VERIFYING", "REVIEWING", "NEED_MORE_EVIDENCE"])
def test_preview_reports_active_returns_even_with_other_dispatch_blockers(db, owned_lead, status):
    lead, principal = owned_lead
    assignment = _assignment(db, lead, principal)
    db.add(ReturnRequest(
        assignment_id=assignment.id,
        lead_id=lead.id,
        company_id=assignment.company_id,
        reason_code="EMPTY_NUMBER",
        description="退回处理中",
        status=status,
        submitted_by=principal.user_id,
    ))
    db.flush()
    preview = preview_lead_deletion(db, lead_id=lead.id, principal=principal)
    assert preview["impact"]["active_return_requests"] == 1
    assert preview["blockers"] == []
    assert preview["deletable"] is True


def test_soft_delete_preserves_completed_verification_import_and_source_identity(db, owned_lead):
    lead, principal = owned_lead
    lead.source_kind = "FEISHU_IMPORT"
    lead.source_app_token = "test-source-app"
    lead.source_table_id = "test-source-table"
    lead.source_record_id = "test-source-record"
    task = VerificationTask(lead_id=lead.id, status="SUBMITTED")
    issue = LeadImportIssue(lead_id=lead.id, issue_type="MISSING_FIELD", field_name="city", message="缺地区")
    db.add_all([task, issue])
    db.flush()
    submission = VerificationSubmission(task_id=task.id, lead_id=lead.id, result="NEED_MORE", submitted_by=principal.user_id)
    db.add(submission)
    db.flush()
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="不再处理")
    db.commit()
    assert db.get(VerificationTask, task.id) is not None
    assert db.get(VerificationSubmission, submission.id) is not None
    assert db.get(LeadImportIssue, issue.id) is not None
    assert db.scalar(select(Lead.id).where(Lead.source_record_id == "test-source-record")) == lead.id


def test_repeated_deletion_preserves_first_actor_reason_and_time(db, owned_lead):
    lead, principal = owned_lead
    first = delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="首次原因")
    db.commit()
    deleted_at = first.lead.deleted_at
    second = delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="不同原因")
    assert second.idempotent is True
    assert as_utc(second.lead.deleted_at) == as_utc(deleted_at)
    assert second.lead.delete_reason == "首次原因"
    assert second.lead.deleted_by == principal.user_id


def test_deletion_uses_a_row_lock_and_does_not_commit_the_callers_transaction(db, owned_lead):
    lead, principal = owned_lead
    lead_id = lead.id
    locking_queries = []

    def observe(execute_state):
        if execute_state.is_select and getattr(execute_state.statement, "_for_update_arg", None) is not None:
            locking_queries.append(execute_state.statement)

    event.listen(db, "do_orm_execute", observe)
    try:
        delete_operation_lead(db, lead_id=lead_id, principal=principal, reason="可回滚删除")
    finally:
        event.remove(db, "do_orm_execute", observe)
    assert locking_queries
    db.rollback()
    assert db.get(Lead, lead_id).deleted_at is None


def test_execution_refreshes_state_after_another_transaction_changes_the_lead(db, owned_lead):
    lead, principal = owned_lead
    assert preview_lead_deletion(db, lead_id=lead.id, principal=principal)["deletable"] is True
    with Session(db.get_bind()) as other_session:
        other_session.execute(update(Lead).where(Lead.id == lead.id).values(status="COMPLETED"))
        other_session.commit()
    assert lead.status == "DRAFT"
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="使用旧页面删除")
    assert lead.status == "COMPLETED"
    assert lead.deleted_at is not None


def test_execution_preserves_dispatch_history_added_after_a_successful_preview(db, owned_lead):
    lead, principal = owned_lead
    assert preview_lead_deletion(db, lead_id=lead.id, principal=principal)["deletable"] is True
    _assignment(db, lead, principal)
    delete_operation_lead(db, lead_id=lead.id, principal=principal, reason="使用旧页面删除")
    assert lead.deleted_at is not None
    assert db.scalar(select(Assignment.id).where(Assignment.lead_id == lead.id)) is not None


def test_missing_lead_returns_not_found(db):
    with pytest.raises(AppError) as error:
        preview_lead_deletion(db, lead_id="missing", principal=_principal("operator"))
    assert error.value.status_code == 404


def test_soft_delete_migration_preserves_rows_and_reverses_cleanly():
    migration = import_module("migrations.versions.0018_lead_soft_delete")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.String(36), primary_key=True))
    leads = sa.Table(
        "leads",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_name", sa.String(64), nullable=False),
    )
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(users.insert().values(id="operator"))
            connection.execute(leads.insert().values(id="existing-lead", customer_name="保留客户"))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
                migration.upgrade()
                columns = {item["name"] for item in inspect(connection).get_columns("leads")}
                assert {"deleted_at", "deleted_by", "delete_reason"} <= columns
                foreign_key = next(
                    item for item in inspect(connection).get_foreign_keys("leads")
                    if item["constrained_columns"] == ["deleted_by"]
                )
                assert foreign_key["referred_table"] == "users"
                assert foreign_key["options"]["ondelete"] == "SET NULL"
                assert connection.execute(sa.text("SELECT customer_name FROM leads")).scalar_one() == "保留客户"
                migration.downgrade()
                migration.downgrade()
                assert {item["name"] for item in inspect(connection).get_columns("leads")} == {"id", "customer_name"}
                assert connection.execute(sa.text("SELECT customer_name FROM leads")).scalar_one() == "保留客户"
    finally:
        engine.dispose()
