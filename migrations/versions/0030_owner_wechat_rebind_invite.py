"""Distinguish first owner binding from owner WeChat replacement invitations."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_owner_wechat_rebind_invite"
down_revision = "0029_lead_customer_wechat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "invite_tokens" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("invite_tokens")}
    if "purpose" not in columns:
        op.add_column("invite_tokens", sa.Column("purpose", sa.String(32), nullable=False, server_default="OWNER_BIND"))
    if "target_user_id" not in columns:
        op.add_column("invite_tokens", sa.Column("target_user_id", sa.String(36), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("invite_tokens")}
    if "target_user_id" in columns:
        op.drop_column("invite_tokens", "target_user_id")
    if "purpose" in columns:
        op.drop_column("invite_tokens", "purpose")
