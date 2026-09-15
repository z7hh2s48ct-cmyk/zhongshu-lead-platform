from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from apps.api.src.core.models import Assignment, Company, Lead, Notification, NotificationOutbox, User, WechatIdentity
from apps.api.src.integrations.wechat import WechatSendResult
from apps.api.src.services.assignment_timeout_v12 import run_assignment_timeouts_v12
from apps.api.src.services.followup_service import run_followup_overdue
from apps.api.src.services.notification_service import create_station_message, enqueue_outbox
from apps.api.src.services.outbox_worker import process_outbox


def seed_assignment(db, *, claimed=False, expired=False):
    now = datetime.now(timezone.utc)
    company = Company(code=f"N-{now.timestamp()}", name="通知验收公司")
    db.add(company)
    db.flush()
    owner = User(display_name="负责人", company_id=company.id)
    employee = User(display_name="处理员工", company_id=company.id)
    db.add_all([owner, employee])
    db.flush()
    company.primary_user_id = owner.id
    lead = Lead(customer_name="测试客资", phone_encrypted="test", phone_hash=f"p-{now.timestamp()}", status="CLAIMED" if claimed else "DISPATCHED", raw_payload={})
    db.add(lead)
    db.flush()
    assignment = Assignment(lead_id=lead.id, company_id=company.id, receiver_company_id=company.id, status="CLAIMED" if claimed else "PENDING_CLAIM", internal_assignee_user_id=employee.id, points_price=100, price_version=1, lead_snapshot={}, assigned_by=owner.id, assigned_at=now-timedelta(hours=25), expires_at=now-timedelta(seconds=1) if expired else now+timedelta(hours=23), first_followup_due_at=now-timedelta(hours=1) if claimed else None)
    db.add(assignment)
    db.flush()
    lead.current_assignment_id = assignment.id
    db.flush()
    return assignment, owner, employee, now


def test_claim_reminder_has_independent_employee_owner_messages(db):
    assignment, owner, employee, now = seed_assignment(db)
    assert run_assignment_timeouts_v12(db, now=now) == {"reminded": 1, "expired": 0}
    messages = db.scalars(select(Notification)).all()
    assert {message.user_id for message in messages} == {owner.id, employee.id}
    events = db.scalars(select(NotificationOutbox)).all()
    assert len(events) == 2
    assert {item.payload["notification_id"] for item in events} == {item.id for item in messages}
    assert all(assignment.id in item.payload["deep_link"] for item in events)
    assert all("截止" in item.body for item in messages)
    assert run_assignment_timeouts_v12(db, now=now) == {"reminded": 0, "expired": 0}


def test_expiry_generates_bound_result_messages(db):
    assignment, owner, employee, now = seed_assignment(db, expired=True)
    assert run_assignment_timeouts_v12(db, now=now)["expired"] == 1
    messages = db.scalars(select(Notification)).all()
    assert {message.user_id for message in messages} == {owner.id, employee.id}
    assert all("回收" in message.body for message in messages)
    assert all(item.payload.get("notification_id") for item in db.scalars(select(NotificationOutbox)).all())


def test_saved_claim_deadline_wins_over_changed_global_config(db, monkeypatch):
    import apps.api.src.services.assignment_timeout_v12 as service
    assignment, _, _, now = seed_assignment(db)
    monkeypatch.setattr(service.settings, "assignment_expire_hours", 12)
    assert run_assignment_timeouts_v12(db, now=now)["expired"] == 0
    assert assignment.status == "PENDING_CLAIM"
    assignment.expires_at = None
    assignment.reminder_sent_at = now
    assert run_assignment_timeouts_v12(db, now=now)["expired"] == 0


def test_timeout_batch_excludes_future_rows_and_limits_due_work(db):
    for _ in range(3):
        seed_assignment(db, expired=True)
    future, _, _, now = seed_assignment(db)
    future.assigned_at = now
    assert run_assignment_timeouts_v12(db, now=now, batch_size=2)["expired"] == 2
    assert run_assignment_timeouts_v12(db, now=now, batch_size=2)["expired"] == 1
    assert future.status == "PENDING_CLAIM"


def test_followup_reminder_has_exact_recipient_and_notification_binding(db):
    assignment, owner, employee, now = seed_assignment(db, claimed=True)
    run_followup_overdue(db, now=now)
    events = db.scalars(select(NotificationOutbox)).all()
    assert {item.payload.get("user_id") for item in events} == {owner.id, employee.id}
    for event in events:
        notification = db.get(Notification, event.payload["notification_id"])
        assert assignment.id in notification.deep_link
        assert notification.user_id == event.payload["user_id"]
    run_followup_overdue(db, now=now)
    assert len(db.scalars(select(NotificationOutbox)).all()) == 2


def test_deleted_lead_is_skipped_by_assignment_and_followup_jobs(db):
    pending, _, _, pending_now = seed_assignment(db, expired=True)
    following, _, _, follow_now = seed_assignment(db, claimed=True)
    pending_lead = db.get(Lead, pending.lead_id)
    following_lead = db.get(Lead, following.lead_id)
    pending_lead.deleted_at = pending_now
    following_lead.deleted_at = follow_now
    db.flush()

    assert run_assignment_timeouts_v12(db, now=pending_now) == {
        "reminded": 0,
        "expired": 0,
    }
    assert run_followup_overdue(db, now=follow_now) == {"overdue": 0, "notified": 0}
    assert pending.status == "PENDING_CLAIM"
    assert following.status == "CLAIMED"
    assert db.scalars(select(NotificationOutbox)).all() == []


def test_legacy_followup_events_resolve_by_own_link_not_latest_scene(db, monkeypatch):
    import apps.api.src.services.outbox_worker as worker
    assignment, owner, _, _ = seed_assignment(db, claimed=True)
    db.add(WechatIdentity(openid="notify-test-owner", user_id=owner.id))
    sent = []
    monkeypatch.setattr(worker.WechatOfficialAccountClient, "send_scene_message", lambda self, **kw: (sent.append(kw) or WechatSendResult(success=True, message_id="test")))
    for i in (1, 2):
        link = f"/h5/v12-workbench.html?view=assignments&id=test-{i}"
        create_station_message(db, user_id=None, company_id=assignment.company_id, scene="FOLLOWUP_OVERDUE", title=f"提醒{i}", body=f"客资{i}", deep_link=link)
        enqueue_outbox(db, event_key=f"legacy-{i}", event_type="FOLLOWUP_OVERDUE", aggregate_type="assignment", aggregate_id=f"test-{i}", payload={"company_id": assignment.company_id, "deep_link": link})
    process_outbox(db)
    assert [item["title"] for item in sent] == ["提醒1", "提醒2"]
    assert sent[0]["url"].endswith("id=test-1")
    assert sent[1]["url"].endswith("id=test-2")


def test_expired_processing_lease_is_reclaimed(db, monkeypatch):
    import apps.api.src.services.outbox_worker as worker
    enqueue_outbox(db, event_key="expired-lease", event_type="TEST", aggregate_type="test", aggregate_id="1", payload={})
    item = db.scalar(select(NotificationOutbox).where(NotificationOutbox.event_key == "expired-lease"))
    item.status = "PROCESSING"
    item.next_attempt_at = datetime.now(timezone.utc)-timedelta(minutes=1)
    monkeypatch.setattr(worker, "_send", lambda *args: {"success": True})
    assert process_outbox(db)["sent"] == 1
    assert item.status == "SENT"


def test_live_processing_lease_is_not_stolen(db, monkeypatch):
    import apps.api.src.services.outbox_worker as worker
    item = enqueue_outbox(db, event_key="live-lease", event_type="TEST", aggregate_type="test", aggregate_id="1", payload={})
    item.status = "PROCESSING"
    item.next_attempt_at = datetime.now(timezone.utc)+timedelta(minutes=1)
    monkeypatch.setattr(worker, "_send", lambda *args: (_ for _ in ()).throw(AssertionError("must not send")))
    assert process_outbox(db)["processed"] == 0
    assert item.status == "PROCESSING"


def test_job_delivery_commits_lease_before_send_and_success_afterward(db, monkeypatch):
    import apps.api.src.services.outbox_worker as worker
    item = enqueue_outbox(db, event_key="job-lease", event_type="TEST", aggregate_type="test", aggregate_id="1", payload={})
    def capture(session, client, message):
        from sqlalchemy.orm import Session
        with Session(db.get_bind()) as observer:
            persisted = observer.get(NotificationOutbox, item.id)
            assert persisted.status == "PROCESSING"
            assert persisted.next_attempt_at is not None
        return {"success": True}
    monkeypatch.setattr(worker, "_send", capture)
    assert process_outbox(db, commit_batches=True)["sent"] == 1
    db.expire_all()
    assert db.get(NotificationOutbox, item.id).status == "SENT"


def test_active_delivery_cannot_be_manually_retried(api_client):
    from test_auth_company import _admin_headers
    client, factory = api_client
    headers = _admin_headers(client)
    with factory() as db:
        item = enqueue_outbox(db, event_key="manual-live-lease", event_type="TEST", aggregate_type="test", aggregate_id="1", payload={})
        item.status = "PROCESSING"
        item.next_attempt_at = datetime.now(timezone.utc)+timedelta(minutes=1)
        db.commit()
        item_id = item.id
    response = client.post(f"/api/v1/notifications/outbox/{item_id}/retry", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "OUTBOX_NOT_RETRYABLE"
