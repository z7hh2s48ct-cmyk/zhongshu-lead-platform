from sqlalchemy import select

import pytest

from apps.api.src.core.auth import Principal
from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Lead, VerificationTask
from apps.api.src.core.security import encrypt_text, hash_phone
from apps.api.src.services.verification_service import (
    claim_task,
    create_tasks,
    publish_template,
    submit_verification,
)
from apps.api.src.services.auth_service import create_internal_user


def principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, display_name="电销", company_id=None, role_codes=frozenset({"TELESALES"}), permission_codes=frozenset({"verification.task.start","verification.submit","lead.phone.read"}), session_version=1)


def test_verification_qualified_flow(db) -> None:
    user = create_internal_user(db, username="tel", password="password1", display_name="电销", role_code="TELESALES")
    lead = Lead(customer_name="张先生", phone_encrypted=encrypt_text("13800138000"), phone_hash=hash_phone("13800138000"), city="上海市", region_code="310100", category_code="OLD_RENOVATION", status="IMPORTED")
    db.add(lead)
    publish_template(db, code="DEFAULT", name="默认核验", schema={"fields":[{"key":"need","required":True}]})
    db.commit()
    tasks = create_tasks(db, lead_ids=[lead.id], assignee_user_id=user.id, assigned_by=user.id, template_code="DEFAULT")
    task = tasks[0]
    claim_task(db, task, principal(user.id))
    submission = submit_verification(db, task, principal(user.id), {"result":"QUALIFIED","answers":{"need":"旧改"},"corrections":{},"note":"确认有效"})
    db.commit()
    assert submission.result == "QUALIFIED"
    assert db.get(Lead, lead.id).status == "QUALIFIED"
    assert db.get(VerificationTask, task.id).status == "SUBMITTED"


def test_verification_can_qualify_lead_without_customer_name(db) -> None:
    user = create_internal_user(
        db,
        username="tel-no-name",
        password="password1",
        display_name="电销",
        role_code="TELESALES",
    )
    lead = Lead(
        customer_name="未填写",
        phone_encrypted=encrypt_text("13800138001"),
        phone_hash=hash_phone("13800138001"),
        city="上海市",
        region_code="310100",
        category_code="OLD_RENOVATION",
        status="IMPORTED",
    )
    db.add(lead)
    publish_template(db, code="NO_NAME", name="姓名选填核验", schema={"fields": []})
    db.commit()
    task = create_tasks(
        db,
        lead_ids=[lead.id],
        assignee_user_id=user.id,
        assigned_by=user.id,
        template_code="NO_NAME",
    )[0]
    claim_task(db, task, principal(user.id))

    submission = submit_verification(
        db,
        task,
        principal(user.id),
        {"result": "QUALIFIED", "answers": {}, "corrections": {}, "note": "姓名未知不影响核验"},
    )

    assert submission.result == "QUALIFIED"
    assert lead.status == "QUALIFIED"


def test_telesales_cannot_start_unassigned_verification_task(db) -> None:
    user = create_internal_user(db, username="tel-unassigned", password="password1", display_name="电销", role_code="TELESALES")
    lead = Lead(customer_name="王先生", phone_encrypted=encrypt_text("13900139001"), phone_hash=hash_phone("13900139001"), city="上海市", region_code="310100", category_code="OLD_RENOVATION", status="IMPORTED")
    db.add(lead)
    publish_template(db, code="UNASSIGNED", name="未派发核验", schema={"fields": []})
    db.commit()
    task = create_tasks(db, lead_ids=[lead.id], assignee_user_id=user.id, assigned_by="operation", template_code="UNASSIGNED")[0]
    task.assignee_user_id = None
    task.status = "PENDING"

    with pytest.raises(AppError) as exc_info:
        claim_task(db, task, principal(user.id))

    assert exc_info.value.code == "VERIFICATION_TASK_NOT_ASSIGNED"
