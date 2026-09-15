"""Bound historical lead rework lookups by audit action and resource type.

Revision ID: 0020_audit_action_resource_index
Revises: 0019_return_48h
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020_audit_action_resource_index"
down_revision = "0019_return_48h"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_audit_action_resource_type"


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("audit_logs")
    }


def upgrade() -> None:
    if INDEX_NAME not in _indexes():
        op.create_index(
            INDEX_NAME,
            "audit_logs",
            ["action", "resource_type"],
        )


def downgrade() -> None:
    if INDEX_NAME in _indexes():
        op.drop_index(INDEX_NAME, table_name="audit_logs")
