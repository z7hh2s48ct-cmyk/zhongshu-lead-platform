"""电销后四位搜索（2026-09-28 反馈第 2 条）。

Revision ID: 0028_lead_phone_tail4
Revises: 0027_withdrawal_payment_reg

leads 新增 phone_tail4 列（明文后四位，等值搜索用）。脱敏格式
`138****8000` 本就向任务指派对象展示后四位，存储后四位不扩大暴露面；
完整号码仍哈希存储。存量数据按 phone_encrypted 解密回填，
解密失败（历史脏数据）置 NULL 保持不可搜索，不阻断迁移。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_lead_phone_tail4"
down_revision = "0027_withdrawal_payment_reg"
branch_labels = None
depends_on = None

_BATCH_SIZE = 500


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _column_exists(inspector, "leads", "phone_tail4"):
        op.add_column("leads", sa.Column("phone_tail4", sa.String(4), nullable=True))
        inspector = sa.inspect(bind)
    if "ix_leads_phone_tail4" not in {
        item["name"] for item in inspector.get_indexes("leads")
    }:
        op.create_index("ix_leads_phone_tail4", "leads", ["phone_tail4"])

    from apps.api.src.core.security import decrypt_text, normalize_phone

    rows = bind.execute(
        sa.text(
            "SELECT id, phone_encrypted FROM leads "
            "WHERE phone_tail4 IS NULL AND phone_encrypted IS NOT NULL"
        )
    ).fetchall()
    for row_id, encrypted in rows:
        # decrypt_text 解密失败（历史脏数据）返回 None，normalize 后长度不足 4 位
        # 同样跳过——这些客资保持不可按后四位搜索。
        normalized = normalize_phone(decrypt_text(encrypted) or "")
        if len(normalized) >= 4:
            bind.execute(
                sa.text("UPDATE leads SET phone_tail4 = :tail WHERE id = :id"),
                {"tail": normalized[-4:], "id": row_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ix_leads_phone_tail4" in {
        item["name"] for item in inspector.get_indexes("leads")
    }:
        op.drop_index("ix_leads_phone_tail4", table_name="leads")
    if _column_exists(inspector, "leads", "phone_tail4"):
        op.drop_column("leads", "phone_tail4")
