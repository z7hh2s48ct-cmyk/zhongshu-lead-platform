#!/usr/bin/env python3
"""S7 存量回刷：把滞留在派发池/公海池的缺县客资转回前置电销核验补县。

背景：v1.2.6（0c257aa）上线 S7 之前，初审合格的市级客资会直接进入
待派发（READY_DISPATCH）或公海池（PUBLIC_POOL）；上线后这批存量客资
被派发入口的县级硬门槛（LEAD_DISTRICT_REQUIRED）挡住，而状态机没有
从池内回到电销核验的转换边。客户已确认口径为"未派发转电销补县"，
本脚本按该口径一次性回刷存量数据：

- 目标：状态为 READY_DISPATCH / PUBLIC_POOL、无进行中派发单
  （current_assignment_id 为空）、且缺有效县级地区的客资；
- 动作：状态改为 PENDING_TELESALES_VERIFY，pending_reason 置为
  DISTRICT_PENDING_VERIFY，并写入系统审计日志；
- 已派发（待领取/已领取/跟进中）客资不在范围内，与客户确认的
  "已派发未领取暂不调整；已领取不撤单"一致；
- 幂等：转换后的客资不再满足筛选条件，可安全重复执行。

默认 dry-run 只打印统计；加 --apply 才落库。生产执行示例：
    DATABASE_URL=postgresql://... uv run python scripts/backfill_county_missing_pool_leads.py          # 预览
    DATABASE_URL=postgresql://... uv run python scripts/backfill_county_missing_pool_leads.py --apply  # 执行
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
from apps.api.src.core.models import Lead
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.audit import write_audit
from apps.api.src.services.dispatch_v12 import lead_missing_district_region

PENDING_REASON = "DISTRICT_PENDING_VERIFY"
POOL_STATUSES = (
    LeadV12Status.READY_DISPATCH.value,
    LeadV12Status.PUBLIC_POOL.value,
)
AUDIT_ACTION = "SYSTEM_COUNTY_MISSING_POOL_BACKFILL"


def collect_targets(db: Session) -> list[Lead]:
    """筛出滞留在池内、无派发单且缺县的客资；只读，不修改任何数据。"""

    candidates = db.scalars(
        select(Lead)
        .where(
            Lead.status.in_(POOL_STATUSES),
            Lead.current_assignment_id.is_(None),
            Lead.deleted_at.is_(None),
        )
        .order_by(Lead.created_at.asc(), Lead.id.asc())
    ).all()
    return [lead for lead in candidates if lead_missing_district_region(db, lead)]


def apply_backfill(db: Session, targets: Sequence[Lead]) -> dict[str, object]:
    """把目标客资转入前置电销核验并写审计；flush 但不 commit，由调用方决定提交。"""

    by_status: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    for lead in targets:
        if lead.status not in POOL_STATUSES or lead.current_assignment_id is not None:
            # 防御：调用方传入过期目标集合时跳过，不覆盖新产生的派发单。
            continue
        before = {"status": lead.status, "pending_reason": lead.pending_reason}
        lead.status = LeadV12Status.PENDING_TELESALES_VERIFY.value
        lead.pending_reason = PENDING_REASON
        write_audit(
            db,
            principal=None,
            action=AUDIT_ACTION,
            resource_type="lead",
            resource_id=lead.id,
            company_id=lead.supplier_company_id,
            before=before,
            after={"status": lead.status, "pending_reason": lead.pending_reason},
            metadata={"script": "backfill_county_missing_pool_leads", "reason_code": PENDING_REASON},
        )
        by_status[before["status"]] += 1
        by_source[lead.source_kind or "UNKNOWN"] += 1
    db.flush()
    return {
        "converted": len(targets),
        "by_status": dict(by_status),
        "by_source": dict(by_source),
    }


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
        by_status = Counter(lead.status for lead in targets)
        by_source = Counter(lead.source_kind or "UNKNOWN" for lead in targets)
        print(f"筛选到缺县的池内未派发客资 {len(targets)} 条")
        print(f"  按状态：{dict(by_status)}")
        print(f"  按来源：{dict(by_source)}")
        if not args.apply:
            print("dry-run 结束，未修改任何数据；确认无误后加 --apply 执行。")
            return 0
        summary = apply_backfill(db, targets)
        db.commit()
        print(f"已回刷 {summary['converted']} 条至 PENDING_TELESALES_VERIFY（{PENDING_REASON}）。")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
