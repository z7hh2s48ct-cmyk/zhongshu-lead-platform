from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from ..core.enums import PointsLedgerType
from ..core.models import Assignment, Lead, PointsAccount, PointsLedger, ReturnEvidence, ReturnRequest
from ..core.models_v12 import SupplierLeadReward, V12MigrationCheckpoint
from ..core.state_machine_v12 import LEGACY_LEAD_STATUS_MAP, LEGACY_RETURN_STATUS_MAP
from ..core.v12_enums import LeadV12Status, ReturnV12Status, RewardStatus
from .migration_v12 import PHONE_FINGERPRINT_CHECKPOINT

_ACTIVE_ASSIGNMENT_STATUSES = ("PENDING_CLAIM", "CLAIMED", "FOLLOWING", "RETURN_PENDING")
_MAX_SEMANTIC_SAMPLES = 50


@dataclass(slots=True)
class ReconciliationReport:
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, **asdict(self)}


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _count(db: Session, model: type, *criteria: Any) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(db.scalar(statement) or 0)


def _ledger_mismatch_fields(
    db: Session,
    ledger: PointsLedger | None,
    *,
    company_id: str,
    ledger_type: str,
    business_types: set[str],
    business_id: str,
    delta: int,
    related_ledger_id: str | None = None,
    require_related_match: bool = False,
) -> list[str]:
    if ledger is None:
        return ["missing"]
    mismatches: list[str] = []
    if ledger.company_id != company_id:
        mismatches.append("company_id")
    account = db.get(PointsAccount, ledger.account_id)
    if account is None:
        mismatches.append("account_id_missing")
    elif account.company_id != ledger.company_id:
        mismatches.append("account_id_company")
    if ledger.ledger_type != ledger_type:
        mismatches.append("ledger_type")
    if ledger.business_type not in business_types:
        mismatches.append("business_type")
    if ledger.business_id != business_id:
        mismatches.append("business_id")
    if int(ledger.delta) != int(delta):
        mismatches.append("delta")
    if require_related_match and ledger.related_ledger_id != related_ledger_id:
        mismatches.append("related_ledger_id")
    return mismatches


def _reward_ledger_semantic_mismatches(db: Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    original_mismatches: list[dict[str, Any]] = []
    reversal_mismatches: list[dict[str, Any]] = []

    original_ledger = aliased(PointsLedger)
    original_rows = db.execute(
        select(SupplierLeadReward, original_ledger)
        .outerjoin(original_ledger, original_ledger.id == SupplierLeadReward.ledger_id)
        .where(SupplierLeadReward.status.in_((RewardStatus.SETTLED.value, RewardStatus.REVERSED.value)))
        .order_by(SupplierLeadReward.id)
    ).all()
    for reward, ledger in original_rows:
        fields = _ledger_mismatch_fields(
            db,
            ledger,
            company_id=reward.supplier_company_id,
            ledger_type=PointsLedgerType.REWARD.value,
            business_types={"V12_SUPPLIER_REWARD"},
            business_id=reward.id,
            delta=int(reward.reward_points),
        )
        if fields:
            original_mismatches.append(
                {
                    "reward_id": reward.id,
                    "ledger_id": reward.ledger_id,
                    "fields": fields,
                }
            )

    reversal_ledger = aliased(PointsLedger)
    reversal_rows = db.execute(
        select(SupplierLeadReward, reversal_ledger)
        .outerjoin(reversal_ledger, reversal_ledger.id == SupplierLeadReward.reversal_ledger_id)
        .where(SupplierLeadReward.status == RewardStatus.REVERSED.value)
        .order_by(SupplierLeadReward.id)
    ).all()
    for reward, ledger in reversal_rows:
        fields = _ledger_mismatch_fields(
            db,
            ledger,
            company_id=reward.supplier_company_id,
            ledger_type=PointsLedgerType.REVERSAL.value,
            business_types={"V12_SUPPLIER_REWARD_REVERSAL"},
            business_id=reward.id,
            delta=-abs(int(reward.reward_points)),
            related_ledger_id=reward.ledger_id,
            require_related_match=True,
        )
        if fields:
            reversal_mismatches.append(
                {
                    "reward_id": reward.id,
                    "ledger_id": reward.reversal_ledger_id,
                    "fields": fields,
                }
            )
    return original_mismatches, reversal_mismatches


def _return_refund_semantic_mismatches(db: Session) -> list[dict[str, Any]]:
    refund_ledger = aliased(PointsLedger)
    claim_ledger = aliased(PointsLedger)
    rows = db.execute(
        select(ReturnRequest, refund_ledger, claim_ledger)
        .outerjoin(refund_ledger, refund_ledger.id == ReturnRequest.refund_ledger_id)
        .outerjoin(claim_ledger, claim_ledger.id == refund_ledger.related_ledger_id)
        .where(ReturnRequest.status == ReturnV12Status.APPROVED.value)
        .order_by(ReturnRequest.id)
    ).all()
    mismatches: list[dict[str, Any]] = []
    for request, refund, claim in rows:
        fields: list[str] = []
        refund_points = int(request.refund_points or 0)
        if refund_points <= 0:
            fields.append("refund_points")
        fields.extend(
            f"refund.{field}"
            for field in _ledger_mismatch_fields(
                db,
                refund,
                company_id=request.company_id,
                ledger_type=PointsLedgerType.RETURN.value,
                business_types={"V12_RETURN_REFUND", "RETURN_REQUEST"},
                business_id=request.id,
                delta=refund_points,
            )
        )
        if refund is not None:
            if not refund.related_ledger_id:
                fields.append("refund.related_ledger_id")
            fields.extend(
                f"claim.{field}"
                for field in _ledger_mismatch_fields(
                    db,
                    claim,
                    company_id=request.company_id,
                    ledger_type=PointsLedgerType.CLAIM.value,
                    business_types={"V12_ASSIGNMENT_CLAIM", "ASSIGNMENT"},
                    business_id=request.assignment_id,
                    delta=-refund_points,
                )
            )
        if fields:
            mismatches.append(
                {
                    "return_id": request.id,
                    "refund_ledger_id": request.refund_ledger_id,
                    "claim_ledger_id": refund.related_ledger_id if refund is not None else None,
                    "fields": sorted(set(fields)),
                }
            )
    return mismatches


def _ledger_account_mismatches(db: Session) -> tuple[int, list[dict[str, Any]]]:
    mismatch_filter = (PointsAccount.id.is_(None)) | (PointsAccount.company_id != PointsLedger.company_id)
    total = int(
        db.scalar(
            select(func.count())
            .select_from(PointsLedger)
            .outerjoin(PointsAccount, PointsAccount.id == PointsLedger.account_id)
            .where(mismatch_filter)
        )
        or 0
    )
    rows = db.execute(
        select(
            PointsLedger.id,
            PointsLedger.account_id,
            PointsLedger.company_id,
            PointsAccount.company_id.label("account_company_id"),
        )
        .outerjoin(PointsAccount, PointsAccount.id == PointsLedger.account_id)
        .where(mismatch_filter)
        .order_by(PointsLedger.created_at.asc(), PointsLedger.id.asc())
        .limit(_MAX_SEMANTIC_SAMPLES)
    ).all()
    return total, [
        {
            "ledger_id": row.id,
            "account_id": row.account_id,
            "ledger_company_id": row.company_id,
            "account_company_id": row.account_company_id,
        }
        for row in rows
    ]


def reconcile_v12(db: Session, *, require_completed_backfill: bool = True) -> ReconciliationReport:
    """Perform read-only release reconciliation without exposing PII."""

    report = ReconciliationReport()
    total_leads = _count(db, Lead)
    missing_fingerprints = _count(db, Lead, Lead.phone_fingerprint.is_(None))
    report.metrics.update(
        {
            "leads_total": total_leads,
            "leads_missing_phone_fingerprint": missing_fingerprints,
            "assignments_total": _count(db, Assignment),
            "returns_total": _count(db, ReturnRequest),
            "supplier_rewards_total": _count(db, SupplierLeadReward),
            "return_evidence_total": _count(db, ReturnEvidence),
        }
    )
    checkpoint = db.get(V12MigrationCheckpoint, PHONE_FINGERPRINT_CHECKPOINT)
    report.metrics["phone_fingerprint_checkpoint"] = (
        {
            "status": checkpoint.status,
            "processed_count": checkpoint.processed_count,
            "error_count": checkpoint.error_count,
            "cursor": checkpoint.cursor,
        }
        if checkpoint
        else None
    )
    if require_completed_backfill and missing_fingerprints:
        report.errors.append(
            _issue(
                "PHONE_FINGERPRINT_INCOMPLETE",
                "仍有客资未生成 V1.2 手机号指纹",
                count=missing_fingerprints,
            )
        )
    if checkpoint and checkpoint.status == "COMPLETED_WITH_ERRORS":
        report.errors.append(
            _issue(
                "PHONE_FINGERPRINT_ROW_ERRORS",
                "手机号指纹回填存在失败行",
                error_count=checkpoint.error_count,
            )
        )
    elif require_completed_backfill and total_leads and (
        checkpoint is None or checkpoint.status != "COMPLETED"
    ):
        report.errors.append(
            _issue(
                "PHONE_FINGERPRINT_CHECKPOINT_NOT_COMPLETE",
                "手机号指纹回填检查点未完成",
                status=checkpoint.status if checkpoint else None,
            )
        )

    duplicate_active = db.execute(
        select(Assignment.lead_id, func.count(Assignment.id))
        .where(Assignment.status.in_(_ACTIVE_ASSIGNMENT_STATUSES))
        .group_by(Assignment.lead_id)
        .having(func.count(Assignment.id) > 1)
        .limit(50)
    ).all()
    report.metrics["duplicate_active_assignment_leads"] = len(duplicate_active)
    if duplicate_active:
        report.errors.append(
            _issue(
                "DUPLICATE_ACTIVE_ASSIGNMENT",
                "同一客资存在多个有效派发单",
                samples=[{"lead_id": row[0], "count": int(row[1])} for row in duplicate_active],
            )
        )

    allowed_lead_statuses = {item.value for item in LeadV12Status} | set(LEGACY_LEAD_STATUS_MAP)
    lead_statuses = set(db.scalars(select(Lead.status).distinct()).all())
    unknown_lead_statuses = sorted(status for status in lead_statuses if status not in allowed_lead_statuses)
    report.metrics["unknown_lead_statuses"] = unknown_lead_statuses
    if unknown_lead_statuses:
        report.errors.append(
            _issue("UNKNOWN_LEAD_STATUS", "发现无法映射的历史客资状态", statuses=unknown_lead_statuses)
        )

    allowed_return_statuses = {item.value for item in ReturnV12Status} | set(LEGACY_RETURN_STATUS_MAP)
    return_statuses = set(db.scalars(select(ReturnRequest.status).distinct()).all())
    unknown_return_statuses = sorted(status for status in return_statuses if status not in allowed_return_statuses)
    report.metrics["unknown_return_statuses"] = unknown_return_statuses
    if unknown_return_statuses:
        report.errors.append(
            _issue("UNKNOWN_RETURN_STATUS", "发现无法映射的历史退回状态", statuses=unknown_return_statuses)
        )

    allowed_reward_statuses = {item.value for item in RewardStatus}
    reward_statuses = set(db.scalars(select(SupplierLeadReward.status).distinct()).all())
    unknown_reward_statuses = sorted(status for status in reward_statuses if status not in allowed_reward_statuses)
    report.metrics["unknown_supplier_reward_statuses"] = unknown_reward_statuses
    if unknown_reward_statuses:
        report.errors.append(
            _issue(
                "UNKNOWN_SUPPLIER_REWARD_STATUS",
                "发现无法识别的供应奖励状态",
                statuses=unknown_reward_statuses,
            )
        )

    ledger_account_mismatch_count, ledger_account_mismatch_samples = _ledger_account_mismatches(db)
    report.metrics["points_ledger_account_mismatches"] = ledger_account_mismatch_count
    if ledger_account_mismatch_count:
        report.errors.append(
            _issue(
                "POINTS_LEDGER_ACCOUNT_MISMATCH",
                "积分流水 account_id 与 company_id 归属不一致",
                count=ledger_account_mismatch_count,
                samples=ledger_account_mismatch_samples,
            )
        )

    account_mismatches: list[dict[str, Any]] = []
    sequence_error_total = 0
    kind_errors: list[dict[str, Any]] = []
    accounts = list(db.scalars(select(PointsAccount).order_by(PointsAccount.company_id)).all())
    for account in accounts:
        ledgers = list(
            db.scalars(
                select(PointsLedger)
                .where(PointsLedger.account_id == account.id)
                .order_by(PointsLedger.created_at.asc(), PointsLedger.id.asc())
            ).all()
        )
        running = {"CUSTOMER": 0, "SUPPLY": 0}
        ledger_by_id = {ledger.id: ledger for ledger in ledgers}
        sequence_errors: list[dict[str, Any]] = []
        company_errors: list[dict[str, Any]] = []
        for ledger in ledgers:
            kind = ledger.point_kind
            if kind not in running:
                kind_errors.append({"ledger_id": ledger.id, "point_kind": kind, "reason": "UNKNOWN_POINT_KIND"})
                continue
            required_kind = {
                "REWARD": "SUPPLY",
                "RECHARGE": "CUSTOMER",
                "CLAIM": "CUSTOMER",
                "RETURN": "CUSTOMER",
            }.get(ledger.ledger_type)
            if required_kind and kind != required_kind:
                kind_errors.append({
                    "ledger_id": ledger.id,
                    "point_kind": kind,
                    "expected_point_kind": required_kind,
                    "reason": "BUSINESS_TYPE_MISMATCH",
                })
            if ledger.ledger_type == "REVERSAL":
                original = ledger_by_id.get(ledger.related_ledger_id)
                if original is None or original.point_kind != kind:
                    kind_errors.append({
                        "ledger_id": ledger.id,
                        "point_kind": kind,
                        "related_ledger_id": ledger.related_ledger_id,
                        "reason": "REVERSAL_KIND_MISMATCH",
                    })
            running[kind] += int(ledger.delta)
            if int(ledger.balance_after) != running[kind]:
                sequence_errors.append(
                    {
                        "ledger_id": ledger.id,
                        "point_kind": kind,
                        "expected_balance_after": running[kind],
                        "actual_balance_after": int(ledger.balance_after),
                    }
                )
            if ledger.company_id != account.company_id:
                company_errors.append(
                    {
                        "ledger_id": ledger.id,
                        "ledger_company_id": ledger.company_id,
                        "account_company_id": account.company_id,
                    }
                )
        sequence_error_total += len(sequence_errors)
        account_balances = {
            "CUSTOMER": int(account.balance),
            "SUPPLY": int(account.supply_balance),
        }
        if (
            running != account_balances
            or sequence_errors
            or company_errors
        ):
            account_mismatches.append(
                {
                    "company_id": account.company_id,
                    "account_balances": account_balances,
                    "ledger_sums": running,
                    "sequence_error_count": len(sequence_errors),
                    "sequence_errors": sequence_errors[:_MAX_SEMANTIC_SAMPLES],
                    "company_error_count": len(company_errors),
                    "company_errors": company_errors[:_MAX_SEMANTIC_SAMPLES],
                }
            )
    report.metrics["points_accounts_total"] = len(accounts)
    report.metrics["points_account_mismatches"] = len(account_mismatches)
    report.metrics["points_ledger_sequence_errors"] = sequence_error_total
    report.metrics["points_ledger_kind_errors"] = len(kind_errors)
    if account_mismatches:
        report.errors.append(
            _issue(
                "POINTS_RECONCILIATION_MISMATCH",
                "积分账户与不可变流水不一致",
                samples=account_mismatches[:_MAX_SEMANTIC_SAMPLES],
            )
        )
    if kind_errors:
        report.errors.append(
            _issue(
                "POINTS_LEDGER_KIND_MISMATCH",
                "积分流水业务类型与积分账户不一致",
                count=len(kind_errors),
                samples=kind_errors[:_MAX_SEMANTIC_SAMPLES],
            )
        )

    settled_without_ledger = _count(
        db,
        SupplierLeadReward,
        SupplierLeadReward.status == RewardStatus.SETTLED.value,
        SupplierLeadReward.ledger_id.is_(None),
    )
    reversed_without_ledger = _count(
        db,
        SupplierLeadReward,
        SupplierLeadReward.status == RewardStatus.REVERSED.value,
        SupplierLeadReward.reversal_ledger_id.is_(None),
    )
    approved_without_refund = _count(
        db,
        ReturnRequest,
        ReturnRequest.status == ReturnV12Status.APPROVED.value,
        ReturnRequest.refund_ledger_id.is_(None),
    )
    evidence_invalid = _count(
        db,
        ReturnEvidence,
        (ReturnEvidence.object_key == "")
        | (ReturnEvidence.sha256 == "")
        | (func.length(ReturnEvidence.sha256) != 64)
        | (ReturnEvidence.file_size <= 0),
    )
    reward_ledger_mismatches, reward_reversal_mismatches = _reward_ledger_semantic_mismatches(db)
    return_refund_mismatches = _return_refund_semantic_mismatches(db)
    report.metrics.update(
        {
            "settled_rewards_without_ledger": settled_without_ledger,
            "reversed_rewards_without_ledger": reversed_without_ledger,
            "approved_returns_without_refund_ledger": approved_without_refund,
            "reward_ledger_semantic_mismatches": len(reward_ledger_mismatches),
            "reward_reversal_ledger_semantic_mismatches": len(reward_reversal_mismatches),
            "return_refund_ledger_semantic_mismatches": len(return_refund_mismatches),
            "invalid_evidence_records": evidence_invalid,
        }
    )
    for code, message, count in (
        ("SETTLED_REWARD_LEDGER_MISSING", "已结算奖励缺少积分流水", settled_without_ledger),
        ("REVERSED_REWARD_LEDGER_MISSING", "已冲正奖励缺少反向流水", reversed_without_ledger),
        ("APPROVED_RETURN_REFUND_MISSING", "已通过退回缺少返分流水", approved_without_refund),
        ("INVALID_EVIDENCE_METADATA", "退回证据元数据不完整", evidence_invalid),
    ):
        if count:
            report.errors.append(_issue(code, message, count=count))
    if reward_ledger_mismatches:
        report.errors.append(
            _issue(
                "REWARD_LEDGER_SEMANTIC_MISMATCH",
                "供应商奖励结算流水与奖励事实不一致",
                count=len(reward_ledger_mismatches),
                samples=reward_ledger_mismatches[:_MAX_SEMANTIC_SAMPLES],
            )
        )
    if reward_reversal_mismatches:
        report.errors.append(
            _issue(
                "REWARD_REVERSAL_LEDGER_SEMANTIC_MISMATCH",
                "供应商奖励冲正流水与原奖励事实不一致",
                count=len(reward_reversal_mismatches),
                samples=reward_reversal_mismatches[:_MAX_SEMANTIC_SAMPLES],
            )
        )
    if return_refund_mismatches:
        report.errors.append(
            _issue(
                "RETURN_REFUND_LEDGER_SEMANTIC_MISMATCH",
                "退回返分流水与退回/原领取事实不一致",
                count=len(return_refund_mismatches),
                samples=return_refund_mismatches[:_MAX_SEMANTIC_SAMPLES],
            )
        )

    if not accounts:
        report.warnings.append(_issue("NO_POINTS_ACCOUNTS", "当前数据库没有积分账户"))
    if not total_leads:
        report.warnings.append(_issue("NO_LEADS", "当前数据库没有客资，无法完成生产数据抽样"))
    return report
