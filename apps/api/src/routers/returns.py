from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.enums import EvidenceType
from ..core.errors import AppError
from ..core.models import Assignment, Lead, ReturnEvidence, ReturnRequest
from ..core.responses import ok, page
from ..schemas.returns import ReturnDraftBody, ReturnReviewBody
from ..services.audit import write_audit
from ..services.evidence_file_validation import validate_evidence_file
from ..services.lead_deletion_v12 import require_lead_not_deleted
from ..services.return_service import add_evidence, create_or_update_return, return_to_dict, review_return, submit_return
from ..services.storage import create_file_access_token, decode_file_access_token, get_storage

router = APIRouter(prefix="/returns", tags=["returns"])

IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
AUDIO_MIME = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a", "audio/aac", "application/octet-stream"}


@router.post("/assignments/{assignment_id}/draft")
def draft_return(assignment_id: str, body: ReturnDraftBody, request: Request, principal=Depends(require_permissions("return.own.manage")), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise AppError("ASSIGNMENT_NOT_FOUND", "派发订单不存在", 404)
    item = create_or_update_return(db, assignment=assignment, principal=principal, reason_code=body.reason_code, description=body.description)
    write_audit(db, principal=principal, action="RETURN_DRAFT_SAVE", resource_type="return_request", resource_id=item.id, company_id=item.company_id, after={"reason": item.reason_code}, request_id=request.state.request_id)
    db.commit()
    return ok(request, return_to_dict(db, item, include_evidence=True))


@router.post("/{return_id}/evidence")
async def upload_evidence(
    return_id: str,
    request: Request,
    principal=Depends(require_permissions("return.own.manage")),
    db: Session = Depends(get_db),
    evidence_type: str = Form(...),
    duration_seconds: int | None = Form(default=None),
    file: UploadFile = File(...),
):
    item = db.get(ReturnRequest, return_id)
    if not item or item.company_id != principal.company_id:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    require_lead_not_deleted(db.get(Lead, item.lead_id))
    if evidence_type not in {EvidenceType.CHAT_SCREENSHOT, EvidenceType.CALL_RECORDING}:
        raise AppError("EVIDENCE_TYPE_INVALID", "证据类型无效", 422)
    content = await file.read()
    if evidence_type == EvidenceType.CHAT_SCREENSHOT:
        if len(content) > 5 * 1024 * 1024:
            raise AppError("EVIDENCE_IMAGE_INVALID", "截图仅支持 JPG/PNG/WEBP，单张不超过5MB", 422)
    else:
        if len(content) > 20 * 1024 * 1024:
            raise AppError("EVIDENCE_AUDIO_INVALID", "录音格式或大小不符合要求（最大20MB）", 422)
    mime = validate_evidence_file(
        evidence_type=evidence_type,
        filename=file.filename,
        mime_type=file.content_type,
        content=content,
    )
    storage = get_storage()
    now = datetime.utcnow()
    stored = storage.save(content, prefix=f"evidence/{now:%Y/%m}/{item.id}", filename=file.filename or "evidence.bin", mime_type=mime)
    evidence = add_evidence(db, request=item, evidence_type=evidence_type, object_key=stored.object_key, original_name=file.filename or "evidence.bin", mime_type=stored.mime_type, file_size=stored.size, sha256=stored.sha256, duration_seconds=duration_seconds, uploaded_by=principal.user_id)
    write_audit(db, principal=principal, action="RETURN_EVIDENCE_UPLOAD", resource_type="return_evidence", resource_id=evidence.id, company_id=item.company_id, after={"type": evidence.evidence_type, "size": evidence.file_size, "sha256": evidence.sha256}, request_id=request.state.request_id)
    db.commit()
    return ok(request, {"id": evidence.id, "type": evidence.evidence_type, "size": evidence.file_size})


@router.post("/{return_id}/submit")
def submit(return_id: str, request: Request, principal=Depends(require_permissions("return.own.manage")), db: Session = Depends(get_db)):
    item = db.get(ReturnRequest, return_id)
    if not item:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    submit_return(db, item, principal)
    write_audit(db, principal=principal, action="RETURN_SUBMIT", resource_type="return_request", resource_id=item.id, company_id=item.company_id, after={"status": item.status}, request_id=request.state.request_id)
    db.commit()
    return ok(request, return_to_dict(db, item, include_evidence=True), "退回申请已提交")


@router.get("")
def list_returns(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    stmt = select(ReturnRequest)
    count_stmt = select(func.count(ReturnRequest.id))
    if principal.has_any_role("FRANCHISE_OWNER"):
        live_leads = select(Lead.id).where(Lead.deleted_at.is_(None))
        stmt = stmt.where(
            ReturnRequest.company_id == principal.company_id,
            ReturnRequest.lead_id.in_(live_leads),
        )
        count_stmt = count_stmt.where(
            ReturnRequest.company_id == principal.company_id,
            ReturnRequest.lead_id.in_(live_leads),
        )
    elif not (principal.can("return.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看退回申请", 403)
    elif company_id:
        stmt = stmt.where(ReturnRequest.company_id == company_id)
        count_stmt = count_stmt.where(ReturnRequest.company_id == company_id)
    if status:
        stmt = stmt.where(ReturnRequest.status == status)
        count_stmt = count_stmt.where(ReturnRequest.status == status)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(ReturnRequest.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([return_to_dict(db, x) for x in items], total, page_no, page_size))


@router.get("/{return_id}")
def get_return(return_id: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    item = db.get(ReturnRequest, return_id)
    if not item:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    if principal.has_any_role("FRANCHISE_OWNER") and item.company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权查看退回申请", 403)
    if principal.has_any_role("FRANCHISE_OWNER"):
        lead = db.get(Lead, item.lead_id)
        if lead is None or lead.deleted_at is not None:
            raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    elif not (principal.can("return.read") or principal.can("return.evidence.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权查看退回申请", 403)
    data = return_to_dict(db, item, include_evidence=True)
    for evidence in data.get("evidences", []):
        evidence["access_token"] = create_file_access_token(evidence["id"], principal.user_id)
    return ok(request, data)


@router.post("/{return_id}/review")
def review(return_id: str, body: ReturnReviewBody, request: Request, principal=Depends(require_permissions("return.review")), db: Session = Depends(get_db)):
    item = db.get(ReturnRequest, return_id)
    if not item:
        raise AppError("RETURN_NOT_FOUND", "退回申请不存在", 404)
    ledger = review_return(db, request=item, principal=principal, decision=body.decision, note=body.note)
    write_audit(db, principal=principal, action="RETURN_REVIEW", resource_type="return_request", resource_id=item.id, company_id=item.company_id, after={"decision": body.decision, "status": item.status, "refund_ledger_id": ledger.id if ledger else None}, request_id=request.state.request_id)
    db.commit()
    return ok(request, return_to_dict(db, item, include_evidence=True), "审核完成")


@router.get("/evidence/{evidence_id}/download")
def download_evidence(evidence_id: str, token: str, request: Request, principal: CurrentPrincipal, db: Session = Depends(get_db)):
    payload = decode_file_access_token(token)
    if payload.get("sub") != evidence_id or payload.get("uid") != principal.user_id:
        raise AppError("FILE_TOKEN_MISMATCH", "文件访问凭据不匹配", 403)
    evidence = db.get(ReturnEvidence, evidence_id)
    if not evidence:
        raise AppError("FILE_NOT_FOUND", "证据文件不存在", 404)
    return_request = db.get(ReturnRequest, evidence.return_request_id)
    if principal.has_any_role("FRANCHISE_OWNER") and return_request and return_request.company_id != principal.company_id:
        raise AppError("FORBIDDEN", "无权访问证据文件", 403)
    if principal.has_any_role("FRANCHISE_OWNER"):
        lead = db.get(Lead, return_request.lead_id) if return_request else None
        if lead is None or lead.deleted_at is not None:
            raise AppError("FILE_NOT_FOUND", "证据文件不存在", 404)
    elif not (principal.can("return.evidence.read") or principal.can("*")):
        raise AppError("FORBIDDEN", "无权访问证据文件", 403)
    content = get_storage().read(evidence.object_key)
    write_audit(db, principal=principal, action="RETURN_EVIDENCE_READ", resource_type="return_evidence", resource_id=evidence.id, company_id=return_request.company_id if return_request else None, request_id=request.state.request_id)
    db.commit()
    return Response(content=content, media_type=evidence.mime_type, headers={"Content-Disposition": f'inline; filename="{evidence.original_name}"'})
