from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.scheduler as scheduler
from apps.api.src.core.database import Base
from apps.api.src.core import auth_models, models  # noqa: F401
from apps.api.src.services.notification_service import enqueue_outbox
from apps.api.src.services.outbox_worker import process_outbox
from apps.api.src.services.storage_cleanup_worker import (
    enqueue_storage_cleanup,
    process_storage_cleanup,
)


@pytest.fixture()
def scheduler_session(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scheduler.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    return factory


def _delivered_outbox(db):
    from apps.api.src.core.models import User, WechatIdentity

    user = User(display_name="N10收件人", status="ACTIVE")
    db.add(user)
    db.flush()
    db.add(WechatIdentity(openid="o-n10-cycle", user_id=user.id))
    item = enqueue_outbox(
        db, event_key="n10:cycle", event_type="ASSIGNMENT_DISPATCHED",
        aggregate_type="assignment", aggregate_id="1", payload={"user_id": user.id},
    )
    db.commit()
    assert process_outbox(db)["sent"] == 1  # dev mock 通道发送成功
    db.commit()
    return item.id


def test_slow_job_failure_does_not_roll_back_outbox_progress(scheduler_session, monkeypatch) -> None:
    """N10：outbox 进度必须先落库——慢任务异常不得回滚已发送状态，
    否则下一轮会向用户重发同一条通知。"""

    from apps.api.src.core.models import NotificationOutbox
    from apps.api.src.integrations import wechat as wechat_module

    monkeypatch.setattr(wechat_module.settings, "wechat_dev_mock", True)

    with scheduler_session() as db:
        item_id = _delivered_outbox(db)

    monkeypatch.setattr(scheduler, "drain_assignment_timeouts_active", lambda db: 0)
    monkeypatch.setattr(scheduler, "run_low_points_warnings", lambda db: 0)
    monkeypatch.setattr(
        scheduler, "run_followup_overdue",
        lambda db: (_ for _ in ()).throw(RuntimeError("slow job boom")),
    )

    assert scheduler.run_cycle(run_slow_jobs=True, run_hourly_jobs=False) is False

    with scheduler_session() as db:
        item = db.get(NotificationOutbox, item_id)
        assert item.status == "SENT", "outbox 已发送状态被慢任务异常回滚"


def test_slow_job_failure_does_not_roll_back_storage_cleanup(
    scheduler_session,
    monkeypatch,
    tmp_path,
) -> None:
    from apps.api.src.core.models import StorageCleanupOutbox
    from apps.api.src.services import storage as storage_module
    from apps.api.src.services.storage import LocalObjectStorage

    storage_root = tmp_path / "private-storage"
    monkeypatch.setattr(storage_module.settings, "object_storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "object_storage_dir", str(storage_root))
    stored = LocalObjectStorage().save(
        b"cleanup",
        prefix="returns/scheduler",
        filename="evidence.bin",
        mime_type="application/octet-stream",
    )
    with scheduler_session() as db:
        item = enqueue_storage_cleanup(
            db,
            event_key="scheduler-storage-cleanup",
            object_key=stored.object_key,
            source_type="return_evidence",
            source_id="scheduler-evidence",
            reason="调度器回归测试",
        )
        db.commit()
        item_id = item.id

    monkeypatch.setattr(scheduler, "drain_assignment_timeouts_active", lambda db: 0)
    monkeypatch.setattr(scheduler, "run_low_points_warnings", lambda db: 0)
    monkeypatch.setattr(
        scheduler,
        "run_followup_overdue",
        lambda db: (_ for _ in ()).throw(RuntimeError("slow job boom")),
    )

    assert scheduler.run_cycle(run_slow_jobs=True, run_hourly_jobs=False) is False

    with scheduler_session() as db:
        item = db.get(StorageCleanupOutbox, item_id)
        assert item is not None and item.status == "DELETED"
    assert not (storage_root / stored.object_key).exists()


def test_storage_cleanup_keeps_retrying_after_five_failures(
    scheduler_session,
    monkeypatch,
    tmp_path,
) -> None:
    from apps.api.src.core.models import StorageCleanupOutbox
    from apps.api.src.services import storage as storage_module
    from apps.api.src.services import storage_cleanup_worker as worker_module

    storage_root = tmp_path / "retry-storage"
    monkeypatch.setattr(storage_module.settings, "object_storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "object_storage_dir", str(storage_root))

    class FlakyStorage:
        def __init__(self) -> None:
            self.calls = 0

        def delete(self, _object_key: str) -> None:
            self.calls += 1
            if self.calls <= 6:
                raise RuntimeError("temporary storage outage")

    storage = FlakyStorage()
    monkeypatch.setattr(worker_module, "get_storage", lambda: storage)
    with scheduler_session() as db:
        item = enqueue_storage_cleanup(
            db,
            event_key="storage-cleanup-retry-forever",
            object_key="returns/retry/evidence.bin",
            source_type="return_evidence",
            source_id="retry-evidence",
            reason="持续重试回归",
        )
        db.commit()
        item_id = item.id

        for expected_attempts in range(1, 7):
            item.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            result = process_storage_cleanup(db)
            db.commit()
            assert result["failed"] == 1
            item = db.get(StorageCleanupOutbox, item_id)
            assert item is not None
            assert item.status == "FAILED"
            assert item.attempts == expected_attempts

        item.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        result = process_storage_cleanup(db)
        db.commit()
        assert result["deleted"] == 1
        assert item.status == "DELETED"


def test_storage_cleanup_refuses_same_bucket_at_changed_s3_endpoint(
    scheduler_session,
    monkeypatch,
) -> None:
    from apps.api.src.core.models import StorageCleanupOutbox
    from apps.api.src.services import storage as storage_module
    from apps.api.src.services import storage_cleanup_worker as worker_module

    monkeypatch.setattr(storage_module.settings, "object_storage_backend", "s3")
    monkeypatch.setattr(storage_module.settings, "s3_bucket", "shared-name")
    monkeypatch.setattr(storage_module.settings, "s3_region", "ap-shanghai")
    monkeypatch.setattr(
        storage_module.settings,
        "s3_endpoint_url",
        "https://old-storage.example.test",
    )

    with scheduler_session() as db:
        item = enqueue_storage_cleanup(
            db,
            event_key="storage-cleanup-endpoint-guard",
            object_key="returns/endpoint-guard/evidence.bin",
            source_type="return_evidence",
            source_id="endpoint-guard-evidence",
            reason="存储目标一致性回归",
        )
        db.commit()
        item_id = item.id

    class RecordingStorage:
        def __init__(self) -> None:
            self.deleted_keys: list[str] = []

        def delete(self, object_key: str) -> None:
            self.deleted_keys.append(object_key)

    storage = RecordingStorage()
    monkeypatch.setattr(worker_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        storage_module.settings,
        "s3_endpoint_url",
        "https://new-storage.example.test",
    )

    with scheduler_session() as db:
        result = process_storage_cleanup(db)
        db.commit()
        item = db.get(StorageCleanupOutbox, item_id)

    assert result["failed"] == 1
    assert storage.deleted_keys == []
    assert item is not None and item.status == "FAILED"
    assert "对象存储目标已变更" in (item.last_error or "")


def test_daily_binding_integrity_violations_raise_alert(scheduler_session, caplog) -> None:
    """N2：binding_integrity 接入 scheduler 日检——违规必须落 error 告警。"""

    import logging as _logging

    from apps.api.src.core.models import Company

    with scheduler_session() as db:
        db.add(
            Company(
                code="N2-DANGLING",
                name="悬空主账号公司",
                status="ACTIVE",
                primary_user_id="ghost-user-id",
            )
        )
        db.commit()

    with caplog.at_level(_logging.ERROR, logger="scheduler"):
        assert scheduler.run_cycle(run_slow_jobs=False, run_hourly_jobs=False, run_daily_jobs=True) is True

    alerts = [r for r in caplog.records if "binding integrity" in r.message]
    assert alerts, "绑定一致性违规必须落 error 级告警"
    assert "DANGLING_PRIMARY" in caplog.text


def test_daily_binding_integrity_runs_once_per_real_day() -> None:
    """scheduler 主循环每 30 秒 tick 一次，日检必须等于 24 小时。"""

    assert scheduler.DAILY_JOB_TICKS == 24 * 60 * 2
