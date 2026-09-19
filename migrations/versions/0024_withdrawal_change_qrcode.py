"""Add change_payee_qrcode_url to supply_points_withdrawals (review follow-up).

Revision ID: 0024_withdrawal_change_qrcode
Revises: 0023_supply_points_withdrawals

The payee-change request (S3) must persist the new QR-code image so an
approved BANK -> WECHAT/ALIPAY_QR change actually switches the payment
credential instead of silently keeping the old one.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_withdrawal_change_qrcode"
down_revision = "0023_supply_points_withdrawals"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "supply_points_withdrawals" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if "change_payee_qrcode_url" not in _columns("supply_points_withdrawals"):
        op.add_column(
            "supply_points_withdrawals",
            sa.Column("change_payee_qrcode_url", sa.Text()),
        )


def downgrade() -> None:
    if "supply_points_withdrawals" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if "change_payee_qrcode_url" in _columns("supply_points_withdrawals"):
        op.drop_column("supply_points_withdrawals", "change_payee_qrcode_url")
