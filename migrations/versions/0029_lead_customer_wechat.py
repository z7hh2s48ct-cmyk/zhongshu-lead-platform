"""客户微信号作为独立联系方式（2026-09-29 反馈第 2 条）。

Revision ID: 0029_lead_customer_wechat
Revises: 0028_lead_phone_tail4

允许手机号为空，并新增加密客户微信号与精确检索哈希。客户微信与业务账号的
公众号身份无关。存在客户微信号数据时拒绝降级，避免删列丢失联系方式。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_lead_customer_wechat"
down_revision = "0028_lead_phone_tail4"
branch_labels = None
depends_on = None

TABLE = "leads"
WECHAT_HASH_INDEX = "ix_leads_customer_wechat_hash"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> dict[str, dict]:
    return {item["name"]: item for item in _inspector().get_columns(TABLE)}


def _indexes() -> set[str]:
    return {item["name"] for item in _inspector().get_indexes(TABLE)}


def upgrade() -> None:
    if TABLE not in _inspector().get_table_names():
        return
    columns = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if "customer_wechat_encrypted" not in columns:
            batch.add_column(sa.Column("customer_wechat_encrypted", sa.Text(), nullable=True))
        if "customer_wechat_hash" not in columns:
            batch.add_column(sa.Column("customer_wechat_hash", sa.String(64), nullable=True))
        if columns["phone_encrypted"]["nullable"] is False:
            batch.alter_column("phone_encrypted", existing_type=sa.Text(), nullable=True)
        if columns["phone_hash"]["nullable"] is False:
            batch.alter_column("phone_hash", existing_type=sa.String(64), nullable=True)
    if WECHAT_HASH_INDEX not in _indexes():
        op.create_index(WECHAT_HASH_INDEX, TABLE, ["customer_wechat_hash"])


def downgrade() -> None:
    if TABLE not in _inspector().get_table_names():
        return
    columns = _columns()
    if "customer_wechat_encrypted" in columns:
        leads = sa.table(
            TABLE,
            sa.column("id", sa.String()),
            sa.column("customer_wechat_encrypted", sa.Text()),
        )
        has_customer_wechat = op.get_bind().execute(
            sa.select(leads.c.id).where(
                leads.c.customer_wechat_encrypted.is_not(None),
            ).limit(1)
        ).first()
        if has_customer_wechat:
            raise RuntimeError("存在客户微信号客资，降级会丢失联系方式；请先迁移这些客资")
    if "phone_encrypted" in columns and "phone_hash" in columns:
        from apps.api.src.core.security import encrypt_text, hash_phone

        leads = sa.table(
            TABLE,
            sa.column("phone_encrypted", sa.Text()),
            sa.column("phone_hash", sa.String(64)),
        )
        op.execute(
            leads.update()
            .where(leads.c.phone_encrypted.is_(None))
            .values(phone_encrypted=encrypt_text(""))
        )
        op.execute(
            leads.update()
            .where(leads.c.phone_hash.is_(None))
            .values(phone_hash=hash_phone(""))
        )
    if WECHAT_HASH_INDEX in _indexes():
        op.drop_index(WECHAT_HASH_INDEX, table_name=TABLE)
    columns = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if columns["phone_encrypted"]["nullable"] is True:
            batch.alter_column("phone_encrypted", existing_type=sa.Text(), nullable=False)
        if columns["phone_hash"]["nullable"] is True:
            batch.alter_column("phone_hash", existing_type=sa.String(64), nullable=False)
        if "customer_wechat_hash" in columns:
            batch.drop_column("customer_wechat_hash")
        if "customer_wechat_encrypted" in columns:
            batch.drop_column("customer_wechat_encrypted")
