from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import EvidenceType, VerificationTaskStatus
from ..core.errors import AppError
from ..core.models import Assignment, ReturnEvidence, ReturnRequest, VerificationTask
from ..core.responses import ok, page
from ..core.v12_enums import ReturnV12Status, VerificationTaskType
from ..schemas.v12_returns import (
    ReturnDirectInvalidBody,
    ReturnDraftV12Body,
    ReturnFinalReviewBody,
    ReturnVerificationAssignBody,
    ReturnVerificationSubmitBody,
)
from ..services.audit import write_audit
from ..services.evidence_file_validation import validate_evidence_file
from ..services.return_v12 import (
    add_return_evidence,
    add_return_verification_evidence,
    assign_return_verification_task,
    claim_return_verification_task,
    create_or_update_return_draft,
    direct_invalid_return,
    final_review_return,
    prepare_return_evidence_upload,
    prepare_return_verification_evidence_upload,
    return_request_list_to_dict,
    return_request_to_dict,
    return_verification_task_list_to_dict,
    return_verification_task_to_dict,
    require_return_verification_task_not_overdue,
    submit_return_request,
    submit_return_verification,
)
from ..services.company_assignment_v12 import require_return_request_access
from ..services.storage import create_file_access_token, decode_file_access_token, get_storage

router = APIRouter(prefix="/v1.2", tags=["v1.2-return-verification"])

_OPEN_RETURN_TASK_STATUSES = (
    VerificationTaskStatus.PENDING.value,
    VerificationTaskStatus.ASSIGNED.value,
    VerificationTaskStatus.IN_PROGRESS.value,
    VerificationTaskStatus.SUBMITTED.value,
)
_OPEN_RETURN_REQUEST_STATUSES = (
    ReturnV12Status.VERIFYING.value,
    ReturnV12Status.REVIEWING.value,
)

IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
AUDIO_MIME = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "application/octet-stream",
}


def _can_read_return(db: Session, principal, item: ReturnRequest) -> bool:
    if principal.can("*") or principal.can("return.read") or principal.can("return.evidence.read"):
        return True
    if principal.has_any_role("TELESALES") and principal.can("verification.task.read"):
        assigned_task = db.scalar(
            select(VerificationTask.id).where(
                VerificationTask.return_request_id == item.id,
                VerificationTask.assignee_user_id == principal.user_id,
                VerificationTask.submitted_at.is_not(None),
            )
        )
        if assigned_task:
            return True
        active_task = db.scalar(
            select(VerificationTask.id).where(
                VerificationTask.return_request_id == item.id,
                VerificationTask.assignee_user_id == principal.user_id,
                VerificationTask.status == VerificationTaskStatus.IN_PROGRESS.value,
            )
        )
        if active_task:
            return True
    if not principal.can("return.own.manage"):
        return False
    assignment = db.get(Assignment, item.assignment_id)
    if assignment is None:
        return False
    try:
        require_return_request_access(
            principal,
            assignment,
            submitted_by=item.submitted_by,
        )
    except AppError:
        return False
    return True


def _validate_evidence_upload(
    *,
    evidence_type: str,
    file: UploadFile,
    content: bytes,
) -> tuple[str, str]:
    normalized_type = evidence_type.strip().upper()
    if normalized_type == EvidenceType.CHAT_SCREENSHOT.value:
        if len(content) > 5 * 1024 * 1024:
            raise AppError("EVIDENCE_IMAGE_INVALID", "截图仅支持 JPG/PNG/WEBP，单张不超过 5MB", 422)
    elif normalized_type == EvidenceType.CALL_RECORDING.value:
        if len(content) > 20 * 1024 * 1024:
            raise AppError("EVIDENCE_AUDIO_INVALID", "录音格式或大小不符合要求，最大 20MB", 422)
    else:
        raise AppError("EVIDENCE_TYPE_INVALID", "证据类型无效", 422)
    mime = validate_evidence_file(
        evidence_type=normalized_type,
        filename=file.filename,
        mime_type=file.content_type,
        content=content,
    )
    return normalized_type, mime


def _attach_evidence_access_tokens(data: dict, principal: CurrentPrincipal) -> dict:
    groups = [
        data.get("evidences", []),
        (data.get("verification") or {}).get("evidences", []),
        (data.get("verification_info") or {}).get("evidences", []),
        (data.get("return_request") or {}).get("available_evidences", []),
    ]
    for group in groups:
        for evidence in group:
            evidence["access_token"] = create_file_access_token(evidence["id"], principal.user_id)
    return data


@router.post("/returns/assignments/{assignment_id}/draft")
def save_return_draft(
    assignment_id: str,
    body: ReturnDraftV12Body,
    request: Request,
    principal=Depends(require_permissions("return.own.manage")),
    db: Session = Depends(get_db),
):
    item = create_or_update_return_draft(
        db,
        assignment_id=assignment_id,
        principal=principal,
        reason_code=body.reason_code,
        description=body.description,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_DRAFT_SAVE",
        resource_type="return_request",
        resource_id=item.id,
        company_id=item.company_id,
        after={"reason_code": item.reason_code, "status": item.status},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, return_request_to_dict(db, item, include_evidence=True), "退回草稿已保存")


@router.post("/returns/{return_id}/evidence")
def upload_return_evidence(
    return_id: str,
    request: Request,
    principal=Depends(require_permissions("return.own.manage")),
    db: Session = Depends(get_db),
    evidence_type: str = Form(...),
    duration_seconds: int | None = Form(default=None),
    file: UploadFile = File(...),
):
    item = db.get(ReturnRequest, return_id)
    if item is None:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    assignment = db.get(Assignment, item.assignment_id)
    if assignment is None:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发单不存在", 404)
    require_return_request_access(
        principal,
        assignment,
        submitted_by=item.submitted_by,
    )
    content = file.file.read()
    normalized_type, mime = _validate_evidence_upload(
        evidence_type=evidence_type,
        file=file,
        content=content,
    )

    item = prepare_return_evidence_upload(
        db,
        request=item,
        principal=principal,
    )
    stored = get_storage().save(
        content,
        prefix=f"evidence/v1.2/{datetime.utcnow():%Y/%m}/{item.id}",
        filename=file.filename or "evidence.bin",
        mime_type=mime,
    )
    evidence = add_return_evidence(
        db,
        request=item,
        principal=principal,
        evidence_type=normalized_type,
        object_key=stored.object_key,
        original_name=file.filename or "evidence.bin",
        mime_type=stored.mime_type,
        file_size=stored.size,
        sha256=stored.sha256,
        duration_seconds=duration_seconds,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_EVIDENCE_UPLOAD",
        resource_type="return_evidence",
        resource_id=evidence.id,
        company_id=item.company_id,
        after={"type": evidence.evidence_type, "size": evidence.file_size, "sha256": evidence.sha256},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        {
            "id": evidence.id,
            "type": evidence.evidence_type,
            "size": evidence.file_size,
            "sha256": evidence.sha256,
        },
        "证据已上传",
    )


@router.post("/returns/{return_id}/submit")
def submit_return(
    return_id: str,
    request: Request,
    principal=Depends(require_permissions("return.own.manage")),
    db: Session = Depends(get_db),
):
    result = submit_return_request(db, return_id=return_id, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_SUBMIT",
        resource_type="return_request",
        resource_id=result.request.id,
        company_id=result.request.company_id,
        after={
            "status": result.request.status,
            "verification_task_id": result.task.id if result.task else None,
            "expired": result.expired,
            "idempotent": result.idempotent,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    if result.expired:
        raise AppError("RETURN_WINDOW_EXPIRED", "已超过领取后 48 小时退回申诉期", 409)
    return ok(
        request,
        return_request_to_dict(db, result.request, include_evidence=True),
        "退回申请已提交，等待后置电销核验" if not result.idempotent else "退回申请已提交",
    )


@router.get("/returns")
def list_returns_v12(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    filters = []
    can_read_all = principal.can("*") or principal.can("return.read")
    if can_read_all:
        if company_id:
            filters.append(ReturnRequest.company_id == company_id)
    elif principal.has_any_role("FRANCHISE_OWNER") and principal.can("return.own.manage") and principal.company_id:
        filters.append(ReturnRequest.company_id == principal.company_id)
    elif principal.has_any_role("FRANCHISE_EMPLOYEE") and principal.can("return.own.manage") and principal.company_id:
        filters.extend(
            [
                ReturnRequest.company_id == principal.company_id,
                or_(
                    ReturnRequest.submitted_by == principal.user_id,
                    ReturnRequest.assignment_id.in_(
                        select(Assignment.id).where(
                            Assignment.internal_assignee_user_id == principal.user_id
                        )
                    ),
                ),
            ]
        )
    else:
        raise AppError("FORBIDDEN", "无权查看退回申请", 403)
    normalized_status = status.strip().upper() if status else None
    if normalized_status:
        filters.append(ReturnRequest.status == normalized_status)
    elif can_read_all:
        # 运营默认队列只是已正式提交的申请；草稿仍留在
        # 发起人侧继续补材料，管理员可通过 status=DRAFT 显式查阅。
        filters.append(ReturnRequest.submitted_at.is_not(None))
    total = db.scalar(select(func.count(ReturnRequest.id)).where(*filters)) or 0
    items = db.scalars(
        select(ReturnRequest)
        .where(*filters)
        .order_by(ReturnRequest.created_at.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    return ok(
        request,
        page(
            return_request_list_to_dict(
                db,
                list(items),
                include_phone=principal.can("*") or principal.can("lead.phone.read"),
            ),
            int(total),
            page_no,
            page_size,
        ),
    )


@router.get("/returns/{return_id}")
def return_detail_v12(
    return_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    item = db.get(ReturnRequest, return_id)
    if item is None:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    if not _can_read_return(db, principal, item):
        raise AppError("FORBIDDEN", "无权查看退回申请", 403)
    data = return_request_to_dict(
        db,
        item,
        include_evidence=True,
        include_phone=principal.can("*") or principal.can("lead.phone.read"),
    )
    return ok(request, _attach_evidence_access_tokens(data, principal))


@router.get("/return-evidences/{evidence_id}/download")
def download_return_evidence_v12(
    evidence_id: str,
    token: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    payload = decode_file_access_token(token)
    if payload.get("sub") != evidence_id or payload.get("uid") != principal.user_id:
        raise AppError("FILE_TOKEN_MISMATCH", "文件访问凭据不匹配", 403)
    evidence = db.get(ReturnEvidence, evidence_id)
    if evidence is None:
        raise AppError("FILE_NOT_FOUND", "证据文件不存在", 404)
    item = db.get(ReturnRequest, evidence.return_request_id)
    if item is None or not _can_read_return(db, principal, item):
        raise AppError("FORBIDDEN", "无权访问证据文件", 403)
    content = get_storage().read(evidence.object_key)
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_EVIDENCE_READ",
        resource_type="return_evidence",
        resource_id=evidence.id,
        company_id=item.company_id,
        request_id=request.state.request_id,
    )
    db.commit()
    safe_name = "".join(char for char in evidence.original_name if ord(char) >= 32 and ord(char) != 127)
    encoded_name = quote(safe_name or "evidence", safe="")
    return Response(
        content=content,
        media_type=evidence.mime_type,
        headers={"Content-Disposition": f"inline; filename=\"evidence\"; filename*=UTF-8''{encoded_name}"},
    )


@router.get("/return-verifications/tasks")
def list_return_verification_tasks(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    mine: bool = Query(default=False),
    submitted_history: bool = False,
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    if not (
        principal.can("verification.read")
        or principal.can("verification.task.read")
        or principal.can("*")
    ):
        raise AppError("FORBIDDEN", "无权查看退回核验任务", 403)
    filters = [VerificationTask.task_type == VerificationTaskType.RETURN_VERIFY.value]
    if mine or principal.has_any_role("TELESALES"):
        filters.append(VerificationTask.assignee_user_id == principal.user_id)
    if submitted_history:
        filters.append(VerificationTask.submitted_at.is_not(None))
    elif status:
        filters.append(VerificationTask.status == status.strip().upper())
    else:
        filters.append(VerificationTask.status.in_(_OPEN_RETURN_TASK_STATUSES))
        filters.append(
            VerificationTask.return_request_id.in_(
                select(ReturnRequest.id).where(ReturnRequest.status.in_(_OPEN_RETURN_REQUEST_STATUSES))
            )
        )
    total = db.scalar(select(func.count(VerificationTask.id)).where(*filters)) or 0
    order_by = (
        (
            VerificationTask.submitted_at.desc(),
            VerificationTask.created_at.desc(),
            VerificationTask.id.desc(),
        )
        if submitted_history
        else (VerificationTask.created_at.desc(), VerificationTask.id.desc())
    )
    tasks = db.scalars(
        select(VerificationTask)
        .where(*filters)
        .order_by(*order_by)
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).all()
    return ok(
        request,
        page(
            return_verification_task_list_to_dict(
                db,
                tasks,
                principal,
                include_phone=(
                    submitted_history and principal.has_any_role("TELESALES")
                ) or (
                    principal.has_any_role("OPERATION", "SUPER_ADMIN")
                    and (principal.can("*") or principal.can("lead.phone.read"))
                ),
            ),
            int(total),
            page_no,
            page_size,
        ),
    )


@router.get("/return-verifications/tasks/{task_id}")
def return_verification_task_detail(
    task_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    task = db.get(VerificationTask, task_id)
    if task is None or task.task_type != VerificationTaskType.RETURN_VERIFY.value:
        raise AppError("RETURN_VERIFY_TASK_NOT_FOUND", "退回核验任务不存在", 404)
    if principal.has_any_role("TELESALES") and task.assignee_user_id != principal.user_id:
        raise AppError("FORBIDDEN", "无权查看该退回核验任务", 403)
    if not (
        principal.can("verification.read")
        or principal.can("verification.task.read")
        or principal.can("*")
    ):
        raise AppError("FORBIDDEN", "无权查看退回核验任务", 403)
    data = return_verification_task_to_dict(
        db,
        task,
        principal,
        include_phone=(
            principal.has_any_role("OPERATION", "SUPER_ADMIN")
            or (
                task.assignee_user_id == principal.user_id
                and (
                    task.status == VerificationTaskStatus.IN_PROGRESS.value
                    or task.submitted_at is not None
                )
            )
        ),
        include_verification_info=True,
    )
    return ok(request, _attach_evidence_access_tokens(data, principal))


@router.post("/return-verifications/tasks/{task_id}/assign")
def assign_return_verification(
    task_id: str,
    body: ReturnVerificationAssignBody,
    request: Request,
    principal=Depends(require_permissions("verification.read")),
    db: Session = Depends(get_db),
):
    assignment = assign_return_verification_task(
        db,
        task_id=task_id,
        assignee_user_id=body.assignee_user_id,
        assigned_by=principal.user_id,
        reason=body.reason,
    )
    task = assignment.task
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_VERIFY_ASSIGN",
        resource_type="verification_task",
        resource_id=task.id,
        before=assignment.before,
        after=assignment.after,
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, return_verification_task_to_dict(db, task, principal), "任务已分配")


@router.post("/return-verifications/tasks/{task_id}/start")
@router.post("/return-verifications/tasks/{task_id}/claim", include_in_schema=False)
def start_return_verification(
    task_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
):
    if not principal.has_any_role("TELESALES") or not principal.can(
        "verification.task.start"
    ):
        raise AppError("FORBIDDEN", "仅被派发任务的电销人员可以开始核验", 403)
    task = claim_return_verification_task(db, task_id=task_id, principal=principal)
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_VERIFY_START",
        resource_type="verification_task",
        resource_id=task.id,
        after={"assignee_user_id": task.assignee_user_id, "status": task.status},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        return_verification_task_to_dict(db, task, principal, include_phone=True),
        "任务已开始核验",
    )


@router.post("/return-verifications/tasks/{task_id}/dial")
def dial_return_verification(
    task_id: str,
    request: Request,
    principal=Depends(require_permissions("lead.phone.dial")),
    db: Session = Depends(get_db),
):
    task = db.get(VerificationTask, task_id)
    if (
        task is None
        or task.task_type != VerificationTaskType.RETURN_VERIFY.value
        or task.assignee_user_id != principal.user_id
        or task.status != VerificationTaskStatus.IN_PROGRESS.value
    ):
        raise AppError("FORBIDDEN", "无权拨打该退回核验电话", 403)
    require_return_verification_task_not_overdue(task)
    data = return_verification_task_to_dict(db, task, principal, include_phone=True)
    phone = data["lead"]["phone"]
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_VERIFY_DIAL",
        resource_type="verification_task",
        resource_id=task.id,
        metadata={"return_request_id": task.return_request_id},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"phone": phone, "tel_url": f"tel:{phone}"})


@router.post("/return-verifications/tasks/{task_id}/evidence")
def upload_return_verification_evidence(
    task_id: str,
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    evidence_type: str = Form(...),
    duration_seconds: int | None = Form(default=None),
    file: UploadFile = File(...),
):
    if not principal.has_any_role("TELESALES") or not principal.can("verification.submit"):
        raise AppError("FORBIDDEN", "仅任务本人可上传退回核验证据", 403)
    task, return_request = prepare_return_verification_evidence_upload(
        db,
        task_id=task_id,
        principal=principal,
    )
    content = file.file.read()
    normalized_type, mime = _validate_evidence_upload(
        evidence_type=evidence_type,
        file=file,
        content=content,
    )
    stored = get_storage().save(
        content,
        prefix=f"evidence/v1.2/verification/{datetime.utcnow():%Y/%m}/{return_request.id}",
        filename=file.filename or "evidence.bin",
        mime_type=mime,
    )
    evidence = add_return_verification_evidence(
        db,
        task_id=task.id,
        principal=principal,
        evidence_type=normalized_type,
        object_key=stored.object_key,
        original_name=file.filename or "evidence.bin",
        mime_type=stored.mime_type,
        file_size=stored.size,
        sha256=stored.sha256,
        duration_seconds=duration_seconds,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_VERIFY_EVIDENCE_UPLOAD",
        resource_type="return_evidence",
        resource_id=evidence.id,
        company_id=return_request.company_id,
        after={
            "verification_task_id": task.id,
            "type": evidence.evidence_type,
            "size": evidence.file_size,
            "sha256": evidence.sha256,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        {
            "id": evidence.id,
            "type": evidence.evidence_type,
            "original_name": evidence.original_name,
            "mime_type": evidence.mime_type,
            "file_size": evidence.file_size,
            "duration_seconds": evidence.duration_seconds,
            "created_at": evidence.created_at.isoformat(),
            "access_token": create_file_access_token(evidence.id, principal.user_id),
        },
        "核验证据已上传",
    )


@router.post("/return-verifications/tasks/{task_id}/submit")
def submit_return_verification_result(
    task_id: str,
    body: ReturnVerificationSubmitBody,
    request: Request,
    principal=Depends(require_permissions("verification.submit")),
    db: Session = Depends(get_db),
):
    task = submit_return_verification(
        db,
        task_id=task_id,
        principal=principal,
        contact_result=body.contact_result,
        conclusion=body.conclusion,
        note=body.note,
        evidence_ids=body.evidence_ids,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_VERIFY_SUBMIT",
        resource_type="verification_task",
        resource_id=task.id,
        after={
            "contact_result": task.contact_result,
            "conclusion": task.verification_conclusion,
            "status": task.status,
            "evidence_ids": body.evidence_ids,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, return_verification_task_to_dict(db, task, principal), "事实核验已提交")


@router.post("/returns/{return_id}/direct-invalid")
def directly_confirm_return_invalid(
    return_id: str,
    body: ReturnDirectInvalidBody,
    request: Request,
    principal=Depends(require_permissions("return.review")),
    db: Session = Depends(get_db),
):
    result = direct_invalid_return(
        db,
        return_id=return_id,
        principal=principal,
        note=body.note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_DIRECT_INVALID",
        resource_type="return_request",
        resource_id=result.request.id,
        company_id=result.request.company_id,
        after={
            "status": result.request.status,
            "refund_ledger_id": result.refund_ledger.id if result.refund_ledger else None,
            "idempotent": result.idempotent,
        },
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        return_request_to_dict(db, result.request, include_evidence=True),
        "已由运营确认客资无效",
    )


@router.post("/returns/{return_id}/final-review")
def final_review_return_request(
    return_id: str,
    body: ReturnFinalReviewBody,
    request: Request,
    principal=Depends(require_permissions("return.review")),
    db: Session = Depends(get_db),
):
    result = final_review_return(
        db,
        return_id=return_id,
        principal=principal,
        decision=body.decision,
        note=body.note,
    )
    write_audit(
        db,
        principal=principal,
        action="V12_RETURN_FINAL_REVIEW",
        resource_type="return_request",
        resource_id=result.request.id,
        company_id=result.request.company_id,
        after={
            "decision": body.decision,
            "status": result.request.status,
            "refund_ledger_id": result.refund_ledger.id if result.refund_ledger else None,
            "idempotent": result.idempotent,
        },
        reason=body.note,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(
        request,
        return_request_to_dict(db, result.request, include_evidence=True),
        "退回终审已完成",
    )
