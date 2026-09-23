#!/usr/bin/env python3
"""缺县解卡：把被旧"缺县转电销"规则卡在电销核验的客资恢复为可派发。

背景：2026-09-19 上线的县级派发门槛（S7）会把缺有效县级地区的合格客资
改写为 PENDING_TELESALES_VERIFY / pending_reason=DISTRICT_PENDING_VERIFY，
而回程入口随后被拆除，导致运营无法处置也无法派发（9.22 反馈第 5 条）。
2026-09-23 客户确认口径为"缺县可直派，运营自行担责"，派发与路由入口均已
放开；本脚本按该口径一次性解卡存量数据：

- 目标：状态为 PENDING_TELESALES_VERIFY、pending_reason=DISTRICT_PENDING_VERIFY、
  无进行中派发单（current_assignment_id 为空）、且没有进行中/已提交的
  前置电销任务的客资；
- 动作：按现行入池路由（approved_lead_pool_target）转入 READY_DISPATCH 或
  PUBLIC_POOL，并写入系统审计日志；
- 幂等：解卡后的客资不再满足筛选条件，可安全重复执行。

默认 dry-run 只打印统计；加 --apply 才落库。生产执行示例：
    DATABASE_URL=postgresql://... uv run python scripts/unblock_district_pending_leads.py          # 预览
    DATABASE_URL=postgresql://... uv run python scripts/unblock_district_pending_leads.py --apply  # 执行
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.src.core.database import SessionLocal
from apps.api.src.core.enums import VerificationTaskStatus
from apps.api.src.core.models import Lead, VerificationTask
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.audit import write_audit
from apps.api.src.services.dispatch_v12 import approved_lead_pool_target

PENDING_REASON = "DISTRICT_PENDING_VERIFY"
OPEN_TASK_STATUSES = (
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.IN_PROGRESS.value,
    VerificationTaskStatus.SUBMITTED.value,
)
AUDIT_ACTION = "SYSTEM_DISTRICT_RULE_RETIRED_UNBLOCK"


def collect_targets(db: Session) -> list[Lead]:
    """筛出被 DISTRICT_PENDING_VERIFY 卡住、无进行中任务与派发单的客资；只读。"""

    open_task_lead_ids = set(
        db.scalars(
            select(VerificationTask.lead_id).where(
                VerificationTask.task_type == "PRE_DISPATCH_VERIFY",
                VerificationTask.status.in_(OPEN_TASK_STATUSES),
            )
        )
    )
    candidates = db.scalars(
        select(Lead)
        .where(
            Lead.status == LeadV12Status.PENDING_TELESALES_VERIFY.value,
            Lead.pending_reason == PENDING_REASON,
            Lead.current_assignment_id.is_(None),
            Lead.deleted_at.is_(None),
        )
        .order_by(Lead.created_at.asc(), Lead.id.asc())
    ).all()
    return [lead for lead in candidates if lead.id not in open_task_lead_ids]


def apply_unblock(db: Session, targets: Sequence[Lead]) -> dict[str, object]:
    """把目标客资按现行路由转入派发池/公海池并写审计；flush 但不 commit。"""

    by_status: Counter[str] = Counter()
    for lead in targets:
        if (
            lead.status != LeadV12Status.PENDING_TELESALES_VERIFY.value
            or lead.pending_reason != PENDING_REASON
            or lead.current_assignment_id is not None
        ):
            # 防御：调用方传入过期目标集合时跳过。
            continue
        before = {"status": lead.status, "pending_reason": lead.pending_reason}
        target = approved_lead_pool_target(db, lead)
        lead.status = target.value
        lead.pending_reason = (
            "PUBLIC_POOL_NO_LOCAL_RECEIVER"
            if target is LeadV12Status.PUBLIC_POOL
            else None
        )
        write_audit(
            db,
            principal=None,
            action=AUDIT_ACTION,
            resource_type="lead",
            resource_id=lead.id,
            company_id=lead.supplier_company_id,
            before=before,
            after={"status": lead.status, "pending_reason": lead.pending_reason},
            metadata={"script": "unblock_district_pending_leads", "reason_code": PENDING_REASON},
        )
        by_status[before["status"]] += 1
    db.flush()
    return {"unblocked": len(targets), "by_status": dict(by_status)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正落库；缺省为 dry-run 只预览统计",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        targets = collect_targets(db)
        by_source = Counter(lead.source_kind or "UNKNOWN" for lead in targets)
        print(f"筛选到被 DISTRICT_PENDING_VERIFY 卡住的客资 {len(targets)} 条")
        print(f"  按来源：{dict(by_source)}")
        if not args.apply:
            print("dry-run 结束，未修改任何数据；确认无误后加 --apply 执行。")
            return 0
        summary = apply_unblock(db, targets)
        db.commit()
        print(f"已解卡 {summary['unblocked']} 条并转入派发池/公海池。")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
