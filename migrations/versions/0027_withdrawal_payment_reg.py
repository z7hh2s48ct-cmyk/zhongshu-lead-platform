"""提现付款内部登记编号与外部流水拆分（2026-09-24 资金修复方案·增量2）。

Revision ID: 0027_withdrawal_payment_reg
Revises: 0026_telesales_filter_indexes

把「平台内部付款登记编号」与「外部交易流水号」两个概念分开：

- 新增 payment_registration_no（全局唯一，内部检索/对账标识，非银行流水）
  与 payment_registration_no_source（SYSTEM / MANUAL，保留编号来源）；
- 保留 payment_external_reference 作为付款渠道实际提供的外部交易流水号。

历史记录此前把操作员填写的凭据号存入 payment_external_reference；迁移时按
原值回填内部登记编号（来源记为 MANUAL），使唯一约束成立并兼容历史。所有新增
列均可空，downgrade 直接删除，可安全回退。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_withdrawal_payment_reg"
down_revision = "0026_telesales_filter_indexes"
branch_labels = None
depends_on = None

TABLE = "supply_points_withdrawals"
INDEX_NAME = "ix_supply_withdrawals_payment_registration_no"
# 全新库经 0001_initial_schema 的 Base.metadata.create_all() 建表时，模型列的
# index=True 会额外生成 SQLAlchemy 默认命名索引；downgrade 需一并清理，否则
# drop_column 会因残留索引报错（no such column）。
AUTO_INDEX_NAME = "ix_supply_points_withdrawals_payment_registration_no"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    return {item["name"] for item in _inspector().get_columns(TABLE)}


def _indexes() -> set[str]:
    return {item["name"] for item in _inspector().get_indexes(TABLE)}


def upgrade() -> None:
    if TABLE not in _inspector().get_table_names():
        return
    columns = _columns()
    if "payment_registration_no" not in columns:
        op.add_column(TABLE, sa.Column("payment_registration_no", sa.String(length=64)))
    if "payment_registration_no_source" not in columns:
        op.add_column(TABLE, sa.Column("payment_registration_no_source", sa.String(length=16)))
    if INDEX_NAME not in _indexes():
        op.create_index(INDEX_NAME, TABLE, ["payment_registration_no"], unique=True)
    # 兼容历史：已登记付款但没有内部编号的记录，用原外部凭据号回填（来源 MANUAL）。
    op.execute(
        sa.text(
            f"UPDATE {TABLE} "
            "SET payment_registration_no = payment_external_reference, "
            "payment_registration_no_source = 'MANUAL' "
            "WHERE payment_registration_no IS NULL "
            "AND payment_external_reference IS NOT NULL"
        )
    )


def downgrade() -> None:
    if TABLE not in _inspector().get_table_names():
        return
    indexes = _indexes()
    for name in (INDEX_NAME, AUTO_INDEX_NAME):
        if name in indexes:
            op.drop_index(name, table_name=TABLE)
    columns = _columns()
    if "payment_registration_no_source" in columns:
        op.drop_column(TABLE, "payment_registration_no_source")
    if "payment_registration_no" in columns:
        op.drop_column(TABLE, "payment_registration_no")
