"""Keep an audit trail when operators remove their own unassigned leads.

Revision ID: 0018_lead_soft_delete
Revises: 0017_pre_dispatch_template
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0018_lead_soft_delete"
down_revision = "0017_pre_dispatch_template"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("leads")}


def _indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("leads")}


def upgrade() -> None:
    existing = _columns()
    with op.batch_alter_table("leads") as batch:
        if "deleted_at" not in existing:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        if "deleted_by" not in existing:
            batch.add_column(
                sa.Column(
                    "deleted_by",
                    sa.String(36),
                    sa.ForeignKey("users.id", name="fk_leads_deleted_by_user", ondelete="SET NULL"),
                    nullable=True,
                )
            )
        if "delete_reason" not in existing:
            batch.add_column(sa.Column("delete_reason", sa.Text(), nullable=True))
    indexes = _indexes()
    for column in ("deleted_at", "deleted_by"):
        index = f"ix_leads_{column}"
        if index not in indexes:
            op.create_index(index, "leads", [column])


def downgrade() -> None:
    indexes = _indexes()
    for index in ("ix_leads_deleted_at", "ix_leads_deleted_by"):
        if index in indexes:
            op.drop_index(index, table_name="leads")
    existing = _columns()
    with op.batch_alter_table("leads") as batch:
        if "deleted_by" in existing:
            batch.drop_constraint("fk_leads_deleted_by_user", type_="foreignkey")
        for column in ("delete_reason", "deleted_by", "deleted_at"):
            if column in existing:
                batch.drop_column(column)
