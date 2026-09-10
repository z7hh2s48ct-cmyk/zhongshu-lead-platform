"""Exercise real row-lock contention in a disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from apps.api.src.core.database import Base
from apps.api.src.core.enums import EvidenceType
from apps.api.src.core.models import VerificationTask
from apps.api.src.services.rbac import seed_rbac
from apps.api.src.services.return_v12 import (
    create_or_update_return_draft, prepare_return_evidence_upload, submit_return_request,
)
from apps.api.tests.test_v12_return_workflow import _evidence, _principal, _workflow_setup


@pytest.fixture
def postgres_factory():
    url = os.environ.get("RETURN_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("set RETURN_POSTGRES_TEST_URL to an isolated PostgreSQL database")
    schema = "return_probe_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=4000 -cstatement_timeout=6000"})
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as db:
            seed_rbac(db)
            db.commit()
        yield factory
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.mark.parametrize("first_operation", ["upload", "save", "submit"])
def test_return_mutations_serialize_without_deadlocks_or_duplicate_tasks(postgres_factory, first_operation):
    with postgres_factory() as db:
        setup = _workflow_setup(db)
        owner = _principal(setup["receiver_user"], "return.own.manage")
        assignment_id = setup["assignment"].id
        draft = create_or_update_return_draft(db, assignment_id=assignment_id, principal=owner,
            reason_code="EMPTY_NUMBER", description="客户联系方式确认为空号")
        _evidence(db, draft, owner, EvidenceType.CHAT_SCREENSHOT.value)
        return_id = draft.id
        db.commit()

    first_locked = Event()
    second_attempted = Event()

    def first_writer():
        with postgres_factory() as db:
            connection = db.connection()
            held = False

            def pause_after_first_lock(conn, cursor, statement, parameters, context, executemany):
                nonlocal held
                if "FOR UPDATE" in statement.upper() and not held:
                    held = True
                    assert "FROM assignments" in statement
                    first_locked.set()
                    assert second_attempted.wait(3), "second transaction did not contend"

            event.listen(connection, "after_cursor_execute", pause_after_first_lock)
            if first_operation == "upload":
                from apps.api.src.core.models import ReturnRequest
                prepare_return_evidence_upload(db, request=db.get(ReturnRequest, return_id), principal=owner)
            elif first_operation == "save":
                create_or_update_return_draft(db, assignment_id=assignment_id, principal=owner,
                    reason_code="EMPTY_NUMBER", description="补充客户联系方式为空号的说明")
            else:
                submit_return_request(db, return_id=return_id, principal=owner)
            db.commit()

    def second_writer():
        assert first_locked.wait(3), "first transaction did not acquire its row lock"
        with postgres_factory() as db:
            connection = db.connection()

            def signal_attempt(conn, cursor, statement, parameters, context, executemany):
                if "FOR UPDATE" in statement.upper():
                    second_attempted.set()

            event.listen(connection, "before_cursor_execute", signal_attempt)
            result = submit_return_request(db, return_id=return_id, principal=owner)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_writer)
        second = executor.submit(second_writer)
        first.result(timeout=10)
        result = second.result(timeout=10)
    assert result.request.status == "VERIFYING"
    assert result.idempotent is (first_operation == "submit")
    with postgres_factory() as db:
        assert db.scalar(select(func.count(VerificationTask.id)).where(VerificationTask.return_request_id == return_id)) == 1
