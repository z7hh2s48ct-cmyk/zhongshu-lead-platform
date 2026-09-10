"""Real row-lock acceptance; an absent disposable PostgreSQL is a skip, not a pass."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.orm import sessionmaker

from apps.api.src.core.models import NotificationOutbox
from apps.api.src.services.notification_service import enqueue_outbox
from apps.api.src.services.outbox_worker import process_outbox


@pytest.mark.parametrize("commit_batches", [False, True])
def test_postgres_delivery_lease_prevents_second_consumer(monkeypatch, commit_batches):
    import apps.api.src.services.outbox_worker as worker
    url = os.environ.get("V12_E2E_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("requires disposable V12 E2E PostgreSQL database")
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql" or "notification_outbox" not in inspect(engine).get_table_names():
        engine.dispose()
        pytest.skip("requires initialized PostgreSQL schema")
    factory = sessionmaker(engine, expire_on_commit=False)
    event_key = f"concurrency-audit-{uuid4().hex}"
    sending = Event()
    finish = Event()
    deliveries = []
    def capture(db, client, item):
        if item.event_key != event_key:
            return {"success": True}
        deliveries.append(item.id)
        sending.set()
        assert finish.wait(10)
        return {"success": True}
    monkeypatch.setattr(worker, "_send", capture)
    # Isolate candidate discovery without changing production locking behavior.
    original_ready = worker._ready_for_delivery
    monkeypatch.setattr(worker, "_ready_for_delivery", lambda now: original_ready(now) & (NotificationOutbox.event_key == event_key))
    try:
        with factory() as db:
            enqueue_outbox(db, event_key=event_key, event_type="TEST", aggregate_type="test", aggregate_id="1", payload={})
            db.commit()
        def consume():
            with factory() as db:
                result = process_outbox(db, commit_batches=commit_batches)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(consume)
            assert sending.wait(10)
            try:
                second = pool.submit(consume).result(timeout=10)
                assert second["processed"] == 0
            finally:
                finish.set()
            assert first.result(timeout=10)["sent"] == 1
        assert len(deliveries) == 1
        with factory() as db:
            item = db.scalar(select(NotificationOutbox).where(NotificationOutbox.event_key == event_key))
            assert item.status == "SENT" and item.attempts == 1
    finally:
        finish.set()
        with factory() as db:
            db.execute(delete(NotificationOutbox).where(NotificationOutbox.event_key == event_key))
            db.commit()
        engine.dispose()
