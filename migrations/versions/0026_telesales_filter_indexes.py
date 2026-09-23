"""电销页筛选性能索引（2026-09-23 反馈 H1）。

Revision ID: 0026_telesales_filter_indexes
Revises: 0025_withdrawal_fee_snapshots

事实结论（verification_tasks.verification_conclusion）与客户姓名
（leads.customer_name）此前无索引；电销页新增按结论/姓名筛选后，
数据量增大时需要索引支撑。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_telesales_filter_indexes"
down_revision = "0025_withdrawal_fee_snapshots"
branch_labels = None
depends_on = None


def _indexes(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    if "ix_verification_conclusion" not in _indexes("verification_tasks"):
        op.create_index(
            "ix_verification_conclusion",
            "verification_tasks",
            ["verification_conclusion"],
        )
    if "ix_leads_customer_name" not in _indexes("leads"):
        op.create_index("ix_leads_customer_name", "leads", ["customer_name"])


def downgrade() -> None:
    if "ix_leads_customer_name" in _indexes("leads"):
        op.drop_index("ix_leads_customer_name", table_name="leads")
    if "ix_verification_conclusion" in _indexes("verification_tasks"):
        op.drop_index("ix_verification_conclusion", table_name="verification_tasks")
