"""Add supply points withdrawal applications (daily withdrawal, 2026-09-17).

Revision ID: 0023_supply_points_withdrawals
Revises: 0022_return_clock_pause

Creates `supply_points_withdrawals` for the confirmed S1 daily withdrawal flow
(申请 -> 审核冻结 -> 线下付款 -> 确认付款并扣分) with per-application payee
snapshots and super-admin reviewed payee change requests (S3). No exchange
rate is seeded; the rate stays backend-configured (不默认 1 积分 = 1 元).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_supply_points_withdrawals"
down_revision = "0022_return_clock_pause"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "supply_points_withdrawals" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "supply_points_withdrawals",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("company_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING_REVIEW"),
            sa.Column("points_requested", sa.BigInteger(), nullable=False),
            sa.Column("requested_by", sa.String(length=36), nullable=False),
            sa.Column("payee_name_encrypted", sa.Text(), nullable=False),
            sa.Column("payee_account_encrypted", sa.Text(), nullable=False),
            sa.Column("payment_method", sa.String(length=32), nullable=False),
            sa.Column("payee_qrcode_url", sa.Text()),
            sa.Column("cash_cents_per_point_snapshot", sa.BigInteger()),
            sa.Column("cash_amount_cents_snapshot", sa.BigInteger()),
            sa.Column("rate_config_id", sa.String(length=36)),
            sa.Column("review_note", sa.Text()),
            sa.Column("reviewed_by", sa.String(length=36)),
            sa.Column("approved_at", sa.DateTime(timezone=True)),
            sa.Column("payment_external_reference", sa.String(length=128)),
            sa.Column("payment_amount_cents", sa.BigInteger()),
            sa.Column("payment_note", sa.Text()),
            sa.Column("payment_proof_url", sa.Text()),
            sa.Column("payment_history_json", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("paid_at", sa.DateTime(timezone=True)),
            sa.Column("payment_recorded_by", sa.String(length=36)),
            sa.Column("writeoff_idempotency_key", sa.String(length=128)),
            sa.Column("change_payee_name_encrypted", sa.Text()),
            sa.Column("change_payee_account_encrypted", sa.Text()),
            sa.Column("change_payment_method", sa.String(length=32)),
            sa.Column("change_reason", sa.Text()),
            sa.Column("change_status", sa.String(length=32)),
            sa.Column("change_requested_by", sa.String(length=36)),
            sa.Column("change_reviewed_by", sa.String(length=36)),
            sa.Column("change_reviewed_at", sa.DateTime(timezone=True)),
            sa.Column("cancel_note", sa.Text()),
            sa.Column("cancelled_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["rate_config_id"], ["system_configs.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["payment_recorded_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["change_requested_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["change_reviewed_by"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_supply_withdrawal_company_created", "supply_points_withdrawals", ["company_id", "created_at"])
        op.create_index("ix_supply_withdrawal_status_created", "supply_points_withdrawals", ["status", "created_at"])
        op.create_index("ix_supply_withdrawals_payment_external_reference", "supply_points_withdrawals", ["payment_external_reference"], unique=True)
        op.create_index("ix_supply_withdrawals_writeoff_idempotency_key", "supply_points_withdrawals", ["writeoff_idempotency_key"], unique=True)
        op.create_index(op.f("ix_supply_points_withdrawals_change_status"), "supply_points_withdrawals", ["change_status"])


def downgrade() -> None:
    if "supply_points_withdrawals" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("supply_points_withdrawals")
