#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.src.core.database import SessionLocal, init_database
from apps.api.src.services.assignment_timeout_v12 import drain_assignment_timeouts_active
from apps.api.src.services.binding_integrity import audit_primary_binding_integrity
from apps.api.src.services.followup_service import run_followup_overdue
from apps.api.src.services.notification_v12 import drain_due_supplier_reward_settlement_notified
from apps.api.src.services.outbox_worker import process_outbox
from apps.api.src.services.points_service import run_low_points_warnings
from apps.api.src.services.storage_cleanup_worker import process_storage_cleanup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scheduler")
running = True
DEFAULT_HEARTBEAT_FILE = "/tmp/zhongshu-scheduler-heartbeat"
SLOW_JOB_TICKS = 10
HOURLY_JOB_TICKS = 120
DAILY_JOB_TICKS = 24 * 60 * 2


def stop(*_: object) -> None:
    global running
    running = False


def heartbeat_path() -> Path:
    return Path(os.environ.get("SCHEDULER_HEARTBEAT_FILE", DEFAULT_HEARTBEAT_FILE))


def clear_heartbeat(path: Path | None = None) -> None:
    (path or heartbeat_path()).unlink(missing_ok=True)


def publish_heartbeat(path: Path | None = None) -> None:
    target = path or heartbeat_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(f"{time.time()}\n", encoding="ascii")
    temporary.replace(target)


def run_cycle(run_slow_jobs: bool, run_hourly_jobs: bool, run_daily_jobs: bool = False) -> bool:
    with SessionLocal() as db:
        try:
            outbox = process_outbox(db, limit=200, commit_batches=True)
            # N10：outbox 进度先落库——慢/小时任务异常回滚时不得把已发送
            # 状态一并回滚，否则下一轮会向用户重发同一条通知。
            db.commit()
            storage_cleanup = process_storage_cleanup(db, limit=200)
            # 文件清理同样先单独落库：后续慢任务失败不得把
            # 已完成的对象删除重置为待处理。
            db.commit()
            metrics: dict[str, object] = {
                "outbox": outbox,
                "storage_cleanup": storage_cleanup,
            }
            if run_slow_jobs:
                metrics.update(
                    {
                        "timeouts": drain_assignment_timeouts_active(db),
                        "followup_overdue": run_followup_overdue(db),
                        "low_points": run_low_points_warnings(db),
                    }
                )
            if run_hourly_jobs:
                metrics["supplier_rewards"] = drain_due_supplier_reward_settlement_notified(
                    db,
                    batch_size=500,
                    max_batches=20,
                    settled_by=None,
                )
            if run_daily_jobs:
                # N2：绑定一致性日检——error 级违规必须告警，不能只在离线
                # 核查时才被发现；报告只读不改库，失败不阻断调度循环。
                report = audit_primary_binding_integrity(db)
                metrics["binding_integrity"] = {
                    "checked_companies": report.checked_companies,
                    "issue_count": len(report.issues),
                    "valid": report.valid,
                }
                if not report.valid:
                    logger.error(
                        "binding integrity violations detected: %s",
                        [issue["code"] for issue in report.issues],
                    )
            if (
                run_slow_jobs
                or run_hourly_jobs
                or run_daily_jobs
                or outbox.get("sent")
                or outbox.get("failed")
                or storage_cleanup.get("deleted")
                or storage_cleanup.get("failed")
            ):
                logger.info("cycle metrics=%s", metrics)
            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception("scheduler cycle failed")
            return False


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    clear_heartbeat()
    init_database()
    tick = 0
    while running:
        cycle_succeeded = run_cycle(
            run_slow_jobs=tick % SLOW_JOB_TICKS == 0,
            run_hourly_jobs=tick % HOURLY_JOB_TICKS == 0,
            run_daily_jobs=tick % DAILY_JOB_TICKS == 0,
        )
        if cycle_succeeded:
            publish_heartbeat()
        tick += 1
        for _ in range(30):
            if not running:
                break
            time.sleep(1)
    logger.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
