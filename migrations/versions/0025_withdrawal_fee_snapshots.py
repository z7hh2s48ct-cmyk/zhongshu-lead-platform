"""提现手续费快照列（2026-09-23 反馈 D2：额度/费率由超管配置）。

Revision ID: 0025_withdrawal_fee_snapshots
Revises: 0024_withdrawal_change_qrcode

审核通过时除兑换比例与现金金额快照外，再记录手续费率（万分比）与
手续费金额快照；实际应付 = cash_amount_cents_snapshot - fee_cents_snapshot。
两列均可空，历史数据保持原语义（视为未收手续费）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_withdrawal_fee_snapshots"
down_revision = "0024_withdrawal_change_qrcode"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "fee_rate_bp_snapshot" not in _columns("supply_points_withdrawals"):
        op.add_column(
            "supply_points_withdrawals",
            sa.Column("fee_rate_bp_snapshot", sa.Integer()),
        )
    if "fee_cents_snapshot" not in _columns("supply_points_withdrawals"):
        op.add_column(
            "supply_points_withdrawals",
            sa.Column("fee_cents_snapshot", sa.BigInteger()),
        )


def downgrade() -> None:
    cols = _columns("supply_points_withdrawals")
    if "fee_cents_snapshot" in cols:
        op.drop_column("supply_points_withdrawals", "fee_cents_snapshot")
    if "fee_rate_bp_snapshot" in cols:
        op.drop_column("supply_points_withdrawals", "fee_rate_bp_snapshot")
