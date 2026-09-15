"""Separate supplier reward points and add auditable supply termination settlement.

Revision ID: 0021_supply_termination_wallets
Revises: 0020_audit_action_resource_index

Historical split rule:
- invalid negative/mismatched accounts are marked BLOCKED and left unsplit;
- otherwise supply=min(old balance, max(0, net historical supplier rewards));
- customer=old balance-supply;
- paired zero-sum migration ledgers preserve the total while making each wallet
  reconcile independently. No cash conversion rate is seeded by this migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0021_supply_termination_wallets"
down_revision = "0020_audit_action_resource_index"
branch_labels = None
depends_on = None


def _id() -> str:
    return str(uuid4())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _reward_link_issue(bind, reward_table, ledger_rows, company_id: str) -> str | None:
    reward_ledgers = [
        item for item in ledger_rows
        if item["business_type"] in {"V12_SUPPLIER_REWARD", "V12_SUPPLIER_REWARD_REVERSAL"}
    ]
    linked_ids = {item["business_id"] for item in reward_ledgers}
    linked_rewards = {
        item["id"]: item
        for item in bind.execute(sa.select(reward_table).where(
            reward_table.c.id.in_(linked_ids)
        )).mappings().all()
    } if linked_ids else {}
    if linked_ids != set(linked_rewards):
        return "MISSING_REWARD_ASSOCIATION"

    company_rewards = bind.execute(sa.select(reward_table).where(
        reward_table.c.supplier_company_id == company_id
    )).mappings().all()
    ledger_by_id = {item["id"]: item for item in ledger_rows}
    for reward in company_rewards:
        if reward["status"] not in {"SETTLED", "REVERSED"}:
            continue
        original = ledger_by_id.get(reward["ledger_id"])
        if original is None:
            return "MISSING_REWARD_LEDGER"
        if not (
            original["company_id"] == company_id
            and original["ledger_type"] == "REWARD"
            and original["business_type"] == "V12_SUPPLIER_REWARD"
            and original["business_id"] == reward["id"]
            and int(original["delta"]) == int(reward["reward_points"])
        ):
            return "REWARD_ASSOCIATION_MISMATCH"
        if reward["status"] == "REVERSED":
            reversal = ledger_by_id.get(reward["reversal_ledger_id"])
            if reversal is None:
                return "MISSING_REWARD_LEDGER"
            if not (
                reversal["company_id"] == company_id
                and reversal["ledger_type"] == "REVERSAL"
                and reversal["business_type"] == "V12_SUPPLIER_REWARD_REVERSAL"
                and reversal["business_id"] == reward["id"]
                and int(reversal["delta"]) == -abs(int(reward["reward_points"]))
                and reversal["related_ledger_id"] == reward["ledger_id"]
            ):
                return "REWARD_ASSOCIATION_MISMATCH"

    for ledger in reward_ledgers:
        reward = linked_rewards[ledger["business_id"]]
        if reward["supplier_company_id"] != company_id:
            return "REWARD_ASSOCIATION_MISMATCH"
        if ledger["business_type"] == "V12_SUPPLIER_REWARD":
            if reward["status"] not in {"SETTLED", "REVERSED"}:
                return "REWARD_STATUS_LEDGER_MISMATCH"
            valid = (
                reward["ledger_id"] == ledger["id"]
                and ledger["ledger_type"] == "REWARD"
                and int(ledger["delta"]) == int(reward["reward_points"])
            )
        else:
            if reward["status"] != "REVERSED":
                return "REWARD_STATUS_LEDGER_MISMATCH"
            valid = (
                reward["reversal_ledger_id"] == ledger["id"]
                and ledger["ledger_type"] == "REVERSAL"
                and int(ledger["delta"]) == -abs(int(reward["reward_points"]))
                and ledger["related_ledger_id"] == reward["ledger_id"]
            )
        if not valid:
            return "REWARD_ASSOCIATION_MISMATCH"
    return None


def upgrade() -> None:
    if "supplier_cooperation_status" not in _columns("companies"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column("supplier_cooperation_status", sa.String(32), nullable=False, server_default="ACTIVE"))
    if "ix_companies_supplier_cooperation_status" not in _indexes("companies"):
        op.create_index("ix_companies_supplier_cooperation_status", "companies", ["supplier_cooperation_status"])
    account_columns = _columns("points_accounts")
    for name, column in [
        ("supply_balance", sa.Column("supply_balance", sa.BigInteger(), nullable=False, server_default="0")),
        ("frozen_customer_points", sa.Column("frozen_customer_points", sa.BigInteger(), nullable=False, server_default="0")),
        ("frozen_supply_points", sa.Column("frozen_supply_points", sa.BigInteger(), nullable=False, server_default="0")),
        ("points_split_status", sa.Column("points_split_status", sa.String(32), nullable=False, server_default="READY")),
        ("points_split_snapshot_json", sa.Column("points_split_snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
    ]:
        if name not in account_columns:
            op.add_column("points_accounts", column)
    if "point_kind" not in _columns("points_ledgers"):
        op.add_column("points_ledgers", sa.Column("point_kind", sa.String(16), nullable=False, server_default="CUSTOMER"))
    if "legacy_balance_after" not in _columns("points_ledgers"):
        op.add_column("points_ledgers", sa.Column("legacy_balance_after", sa.BigInteger(), nullable=True))
    if "ix_points_ledgers_point_kind" not in _indexes("points_ledgers"):
        op.create_index("ix_points_ledgers_point_kind", "points_ledgers", ["point_kind"])

    bind = op.get_bind()
    meta = sa.MetaData()
    accounts = sa.Table("points_accounts", meta, autoload_with=bind)
    ledgers = sa.Table("points_ledgers", meta, autoload_with=bind)
    reward_table = sa.Table("supplier_lead_rewards", meta, autoload_with=bind)
    bind.execute(ledgers.update().where(
        ledgers.c.point_kind.not_in(("CUSTOMER", "SUPPLY"))
    ).values(point_kind="CUSTOMER"))
    bind.execute(ledgers.update().where(
        ledgers.c.legacy_balance_after.is_(None),
        ledgers.c.business_type != "POINTS_WALLET_SPLIT",
    ).values(legacy_balance_after=ledgers.c.balance_after))
    now = datetime.now(timezone.utc)
    rows = bind.execute(sa.select(accounts.c.id, accounts.c.company_id, accounts.c.balance)).mappings().all()
    for row in rows:
        company_id = row["company_id"]
        old_balance = int(row["balance"])
        ledger_rows = bind.execute(sa.select(ledgers).where(
            ledgers.c.company_id == company_id
        ).order_by(ledgers.c.created_at, ledgers.c.id)).mappings().all()
        total = sum(int(item["delta"]) for item in ledger_rows)
        running = 0
        sequence_valid = True
        account_links_valid = True
        for item in ledger_rows:
            running += int(item["delta"])
            sequence_valid = sequence_valid and int(item["balance_after"]) == running
            account_links_valid = account_links_valid and item["account_id"] == row["id"]
        reward_issue = _reward_link_issue(bind, reward_table, ledger_rows, company_id)
        if old_balance < 0:
            reason = "NEGATIVE_BALANCE"
        elif total != old_balance:
            reason = "LEDGER_TOTAL_MISMATCH"
        elif not account_links_valid:
            reason = "LEDGER_ACCOUNT_MISMATCH"
        elif not sequence_valid:
            reason = "LEDGER_SEQUENCE_MISMATCH"
        elif reward_issue:
            reason = reward_issue
        else:
            reason = None
        if reason:
            bind.execute(accounts.update().where(accounts.c.id == row["id"]).values(
                points_split_status="BLOCKED",
                points_split_snapshot_json={
                    "rule_version": 1, "old_balance": old_balance,
                    "ledger_total": total, "reward_net": None,
                    "customer_result": old_balance, "supply_result": 0,
                    "exception_reason": reason,
                },
            ))
            continue

        reward_ids = {
            item["id"] for item in ledger_rows
            if item["business_type"] in {"V12_SUPPLIER_REWARD", "V12_SUPPLIER_REWARD_REVERSAL"}
        }
        if reward_ids:
            bind.execute(ledgers.update().where(ledgers.c.id.in_(reward_ids)).values(point_kind="SUPPLY"))
        split_supply_ids = {
            item["id"] for item in ledger_rows
            if item["business_type"] == "POINTS_WALLET_SPLIT"
            and str(item["idempotency_key"]).endswith(":supply")
        }
        if split_supply_ids:
            bind.execute(ledgers.update().where(ledgers.c.id.in_(split_supply_ids)).values(point_kind="SUPPLY"))
        reward_net = sum(int(item["delta"]) for item in ledger_rows if item["id"] in reward_ids)
        supply_target = min(old_balance, max(0, reward_net))
        customer_target = old_balance - supply_target
        supply_current = sum(
            int(item["delta"]) for item in ledger_rows
            if item["id"] in reward_ids or item["id"] in split_supply_ids
        )
        transfer = supply_current - supply_target
        split_snapshot = {
            "rule_version": 1, "old_balance": old_balance,
            "ledger_total": total, "reward_net": reward_net,
            "customer_result": customer_target, "supply_result": supply_target,
            "exception_reason": None,
        }
        if transfer:
            business_id = f"wallet-split:{company_id}"
            bind.execute(ledgers.insert(), [
                {
                    "id": _id(), "account_id": row["id"], "company_id": company_id,
                    "ledger_type": "MIGRATION_SPLIT", "point_kind": "SUPPLY",
                    "delta": -transfer, "balance_after": supply_target,
                    "business_type": "POINTS_WALLET_SPLIT", "business_id": business_id,
                    "idempotency_key": f"{business_id}:supply", "metadata_json": {"zero_sum": True, **split_snapshot},
                    "created_at": now,
                },
                {
                    "id": _id(), "account_id": row["id"], "company_id": company_id,
                    "ledger_type": "MIGRATION_SPLIT", "point_kind": "CUSTOMER",
                    "delta": transfer, "balance_after": customer_target,
                    "business_type": "POINTS_WALLET_SPLIT", "business_id": business_id,
                    "idempotency_key": f"{business_id}:customer", "metadata_json": {"zero_sum": True, **split_snapshot},
                    "created_at": now,
                },
            ])
        bind.execute(accounts.update().where(accounts.c.id == row["id"]).values(
            balance=customer_target, supply_balance=supply_target,
            points_split_status="READY", points_split_snapshot_json=split_snapshot,
        ))

        # Existing balance_after represented the old combined wallet. Recalculate
        # it per wallet so each immutable stream has a valid sequence after split.
        running = {"CUSTOMER": 0, "SUPPLY": 0}
        current = bind.execute(sa.select(ledgers.c.id, ledgers.c.point_kind, ledgers.c.delta).where(
            ledgers.c.company_id == company_id
        ).order_by(ledgers.c.created_at, ledgers.c.id)).mappings().all()
        for entry in current:
            kind = entry["point_kind"]
            running[kind] += int(entry["delta"])
            bind.execute(ledgers.update().where(ledgers.c.id == entry["id"]).values(balance_after=running[kind]))

    if "supply_termination_requests" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "supply_termination_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="REQUESTED"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("payee_name_encrypted", sa.Text(), nullable=False),
        sa.Column("payee_account_encrypted", sa.Text(), nullable=False),
        sa.Column("payment_method", sa.String(32), nullable=False),
        sa.Column("blockers_json", sa.JSON(), nullable=False),
        sa.Column("customer_points_snapshot", sa.BigInteger()),
        sa.Column("supply_points_snapshot", sa.BigInteger()),
        sa.Column("general_points_snapshot", sa.BigInteger()),
        sa.Column("cash_cents_per_point_snapshot", sa.BigInteger()),
        sa.Column("cash_amount_cents_snapshot", sa.BigInteger()),
        sa.Column("rate_config_id", sa.String(36), sa.ForeignKey("system_configs.id", ondelete="RESTRICT")),
        sa.Column("review_note", sa.Text()),
        sa.Column("reviewed_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("payment_external_reference", sa.String(128), unique=True),
        sa.Column("payment_note", sa.Text()),
        sa.Column("payment_proof_url", sa.Text()),
        sa.Column("payment_history_json", sa.JSON(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("payment_amount_cents", sa.BigInteger()),
        sa.Column("payment_recorded_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("writeoff_idempotency_key", sa.String(128), unique=True),
        sa.Column("terminated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_supply_termination_requests_company_id", "supply_termination_requests", ["company_id"])
    op.create_index("ix_supply_termination_requests_status", "supply_termination_requests", ["status"])
    op.create_index("ix_supply_termination_company_created", "supply_termination_requests", ["company_id", "created_at"])
    op.create_index("ix_supply_termination_status_created", "supply_termination_requests", ["status", "created_at"])
    op.create_index("ix_supply_termination_requests_payment_external_reference", "supply_termination_requests", ["payment_external_reference"])


def downgrade() -> None:
    bind = op.get_bind()
    if "supply_termination_requests" in sa.inspect(bind).get_table_names():
        request_count = int(bind.execute(sa.text(
            "SELECT count(*) FROM supply_termination_requests"
        )).scalar_one())
        if request_count:
            raise RuntimeError(
                "cannot downgrade while supply termination history exists; export and archive it first"
            )
    op.drop_table("supply_termination_requests")
    meta = sa.MetaData()
    accounts = sa.Table("points_accounts", meta, autoload_with=bind)
    ledgers = sa.Table("points_ledgers", meta, autoload_with=bind)
    bind.execute(accounts.update().values(balance=accounts.c.balance + accounts.c.supply_balance))
    bind.execute(ledgers.delete().where(ledgers.c.business_type == "POINTS_WALLET_SPLIT"))
    company_ids = bind.execute(sa.select(ledgers.c.company_id).distinct()).scalars().all()
    for company_id in company_ids:
        running = 0
        entries = bind.execute(sa.select(ledgers.c.id, ledgers.c.delta).where(
            ledgers.c.company_id == company_id
        ).order_by(ledgers.c.created_at, ledgers.c.id)).mappings().all()
        for entry in entries:
            running += int(entry["delta"])
            bind.execute(ledgers.update().where(ledgers.c.id == entry["id"]).values(balance_after=running))
    with op.batch_alter_table("points_ledgers") as batch:
        batch.drop_index("ix_points_ledgers_point_kind")
        batch.drop_column("legacy_balance_after")
        batch.drop_column("point_kind")
    with op.batch_alter_table("points_accounts") as batch:
        batch.drop_column("points_split_snapshot_json")
        batch.drop_column("points_split_status")
        batch.drop_column("frozen_supply_points")
        batch.drop_column("frozen_customer_points")
        batch.drop_column("supply_balance")
    with op.batch_alter_table("companies") as batch:
        batch.drop_index("ix_companies_supplier_cooperation_status")
        batch.drop_column("supplier_cooperation_status")
