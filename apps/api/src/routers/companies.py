from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..core.auth import CurrentPrincipal, require_permissions
from ..core.database import get_db
from ..core.errors import AppError
from ..core.models import Assignment, Company, Lead, ReturnRequest
from ..core.responses import ok, page
from ..core.v12_enums import LeadSourceKind
from ..schemas.company import (
    CompanyCreateBody,
    CompanyDeleteBody,
    CompanyMarkTestBody,
    CompanyOwnerWechatUnbindBody,
    CompanySimpleCreateBody,
    CompanyUpdateBody,
)
from ..services.audit import write_audit
from ..services.company_service import (
    company_to_dict,
    create_company,
    create_simple_company,
    delete_test_company,
    mark_company_as_test,
    preview_test_company_purge,
    unbind_company_owner_wechat,
    update_company,
)

router = APIRouter(prefix="/companies", tags=["companies"])
logger = logging.getLogger("zhongshu.companies")


@router.get("")
def list_companies(
    request: Request,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page_no: int = Query(default=1, alias="page", ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    if not (
        principal.can("company.read")
        or principal.can("company.account.manage")
        or principal.can("*")
    ):
        raise AppError("FORBIDDEN", "无权查看加盟商公司列表", 403)
    stmt = select(Company).options(selectinload(Company.service_regions), selectinload(Company.capabilities), selectinload(Company.points_account))
    count_stmt = select(func.count(Company.id))
    if keyword:
        stmt = stmt.where(Company.name.contains(keyword) | Company.code.contains(keyword))
        count_stmt = count_stmt.where(Company.name.contains(keyword) | Company.code.contains(keyword))
    if status:
        stmt = stmt.where(Company.status == status)
        count_stmt = count_stmt.where(Company.status == status)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(Company.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    include_finance = (
        principal.can("points.read")
        or principal.can("company.account.manage")
        or principal.can("*")
    )
    include_assignment_summary = principal.can("assignment.read") or principal.can("*")
    summaries: dict[str, dict[str, object]] = {}
    provided_summaries: dict[str, dict[str, object]] = {}
    refusal_counts: dict[str, int] = {}
    return_counts: dict[str, dict[str, int]] = {}
    if include_assignment_summary and items:
        company_ids = [company.id for company in items]
        counts = db.execute(
            select(Assignment.company_id, Assignment.status, func.count(Assignment.id))
            .where(Assignment.company_id.in_(company_ids))
            .group_by(Assignment.company_id, Assignment.status)
        ).all()
        for company_id, assignment_status, count in counts:
            summary = summaries.setdefault(company_id, {"total": 0, "by_status": {}})
            summary["total"] = int(summary["total"]) + int(count)
            summary["by_status"][str(assignment_status)] = int(count)
        provided_counts = db.execute(
            select(Lead.supplier_company_id, Lead.status, func.count(Lead.id))
            .where(
                Lead.supplier_company_id.in_(company_ids),
                Lead.source_kind == LeadSourceKind.SUPPLIER_H5.value,
                Lead.deleted_at.is_(None),
            )
            .group_by(Lead.supplier_company_id, Lead.status)
        ).all()
        for company_id, lead_status, count in provided_counts:
            if not company_id:
                continue
            summary = provided_summaries.setdefault(company_id, {"total": 0, "by_status": {}})
            summary["total"] = int(summary["total"]) + int(count)
            summary["by_status"][str(lead_status)] = int(count)
        refusal_counts = {
            str(company_id): int(count)
            for company_id, count in db.execute(
                select(Assignment.company_id, func.count(Assignment.id))
                .where(
                    Assignment.company_id.in_(company_ids),
                    Assignment.status == "RELEASED",
                    Assignment.release_reason == "REFUSED_CLAIM",
                )
                .group_by(Assignment.company_id)
            ).all()
        }
        return_rows = db.execute(
            select(ReturnRequest.company_id, ReturnRequest.status, func.count(ReturnRequest.id))
            .where(ReturnRequest.company_id.in_(company_ids))
            .group_by(ReturnRequest.company_id, ReturnRequest.status)
        ).all()
        for company_id, status_value, count in return_rows:
            return_counts.setdefault(company_id, {})[str(status_value)] = int(count)

    payload = []
    for company in items:
        item = company_to_dict(company, include_finance=include_finance)
        if include_assignment_summary:
            received = summaries.get(
                company.id,
                {"total": 0, "by_status": {}},
            )
            provided = provided_summaries.get(company.id, {"total": 0, "by_status": {}})
            returns_by_status = return_counts.get(company.id, {})
            item["assignment_summary"] = received
            item["provided"] = provided
            item["received"] = received
            item["exception_breakdown"] = {
                "refused_claim": refusal_counts.get(company.id, 0),
                "return_requested": sum(
                    int(returns_by_status.get(status_value, 0))
                    for status_value in (
                        "SUBMITTED",
                        "VERIFYING",
                        "REVIEWING",
                        "NEED_MORE_EVIDENCE",
                        "APPROVED",
                        "REJECTED",
                    )
                ),
                "confirmed_invalid": int(returns_by_status.get("APPROVED", 0)),
            }
        payload.append(item)
    return ok(request, page(payload, total, page_no, page_size))


@router.post("")
def create_company_endpoint(
    body: CompanyCreateBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = create_company(db, body)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_CREATE",
        resource_type="company",
        resource_id=company.id,
        company_id=company.id,
        after={"code": company.code, "name": company.name, "is_test": company.is_test},
        request_id=request.state.request_id,
    )
    db.commit()
    db.refresh(company)
    return ok(
        request,
        {
            "id": company.id,
            "code": company.code,
            "name": company.name,
            "is_test": company.is_test,
        },
        "创建成功",
    )


@router.post("/simple")
def create_simple_company_endpoint(
    body: CompanySimpleCreateBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company, readiness = create_simple_company(db, body, approved_by=principal.user_id)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_SIMPLE_CREATE",
        resource_type="company",
        resource_id=company.id,
        company_id=company.id,
        after={
            "code": company.code,
            "name": company.name,
            "primary_city_code": body.primary_city_code,
            "district_codes": body.district_codes,
            "region_codes": body.region_codes,
            "serve_all_districts": body.serve_all_districts,
            "readiness": readiness,
            "is_test": company.is_test,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    db.refresh(company)
    return ok(
        request,
        {
            "id": company.id,
            "code": company.code,
            "name": company.name,
            "readiness": readiness,
            "is_test": company.is_test,
        },
        "创建成功",
    )


@router.patch("/{company_id}")
def update_company_endpoint(
    company_id: str,
    body: CompanyUpdateBody,
    request: Request,
    principal=Depends(require_permissions("company.profile.review")),
    db: Session = Depends(get_db),
):
    company = db.scalar(select(Company).options(selectinload(Company.members), selectinload(Company.service_regions), selectinload(Company.capabilities)).where(Company.id == company_id))
    if not company:
        raise AppError("COMPANY_NOT_FOUND", "加盟商公司不存在", 404)
    before = {
        "name": company.name,
        "owner_name": company.owner_name,
        "status": company.status,
        "level_code": company.level_code,
        "notes": company.notes,
    }
    update_company(db, company, body)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_UPDATE",
        resource_type="company",
        resource_id=company.id,
        company_id=company.id,
        before=before,
        after={
            "name": company.name,
            "owner_name": company.owner_name,
            "status": company.status,
            "level_code": company.level_code,
            "notes": company.notes,
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, {"id": company.id}, "更新成功")


@router.post("/{company_id}/wechat-binding/unbind")
def unbind_company_owner_wechat_endpoint(
    company_id: str,
    body: CompanyOwnerWechatUnbindBody,
    request: Request,
    principal=Depends(require_permissions("company.account.manage")),
    db: Session = Depends(get_db),
):
    result = unbind_company_owner_wechat(db, company_id, confirm_name=body.confirm_name)
    write_audit(
        db,
        principal=principal,
        action="COMPANY_WECHAT_UNBIND",
        resource_type="company",
        resource_id=str(result["company_id"]),
        company_id=str(result["company_id"]),
        before=result["before"],
        after=result["after"],
        metadata={
            "reason": body.reason,
            "revoked_invite_count": result["revoked_invite_count"],
            "cancelled_invite_delivery_count": result["cancelled_invite_delivery_count"],
        },
        request_id=request.state.request_id,
    )
    db.commit()
    logger.warning(
        "company_owner_wechat_unbound",
        extra={
            "company_id": result["company_id"],
            "actor_user_id": principal.user_id,
            "unbound_user_id": result["unbound_user_id"],
        },
    )
    return ok(
        request,
        {
            "id": result["company_id"],
            "unbound_user_id": result["unbound_user_id"],
            "revoked_invite_count": result["revoked_invite_count"],
            "cancelled_invite_delivery_count": result["cancelled_invite_delivery_count"],
        },
        "负责人微信已解绑",
    )


@router.get("/{company_id}/purge-preview")
def preview_test_company_purge_endpoint(
    company_id: str,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    preview = preview_test_company_purge(db, company_id)
    return ok(request, preview)


@router.post("/{company_id}/mark-test")
def mark_company_as_test_endpoint(
    company_id: str,
    body: CompanyMarkTestBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    result = mark_company_as_test(
        db,
        company_id,
        confirm_name=body.confirm_name,
        confirm_phrase=body.confirm_phrase,
        scope_token=body.scope_token,
    )
    write_audit(
        db,
        principal=principal,
        action="COMPANY_TEST_MARK",
        resource_type="company",
        resource_id=str(result["company_id"]),
        company_id=str(result["company_id"]),
        before=result["before"],
        after=result["after"],
        metadata={"purge_preview": result["preview"]},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    logger.warning(
        "company_marked_as_test",
        extra={"company_id": result["company_id"], "actor_user_id": principal.user_id},
    )
    return ok(request, {"id": result["company_id"], "is_test": True}, "已标记为测试主体")


@router.delete("/{company_id}")
def delete_test_company_endpoint(
    company_id: str,
    body: CompanyDeleteBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    snapshot = delete_test_company(
        db,
        company_id,
        confirm_name=body.confirm_name,
    )
    write_audit(
        db,
        principal=principal,
        action="COMPANY_TEST_DELETE",
        resource_type="company",
        resource_id=str(snapshot["id"]),
        company_id=str(snapshot["id"]),
        before=snapshot,
        after={"deleted": True},
        metadata={
            "detached_user_ids": snapshot["detached_user_ids"],
            "purged": snapshot["purged"],
        },
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    logger.warning(
        "test_company_deleted",
        extra={
            "company_id": snapshot["id"],
            "actor_user_id": principal.user_id,
            "detached_user_count": len(snapshot["detached_user_ids"]),
            "purged": snapshot["purged"],
        },
    )
    return ok(
        request,
        {
            "id": snapshot["id"],
            "detached_user_count": len(snapshot["detached_user_ids"]),
            "purged": snapshot["purged"],
        },
        "测试加盟商已删除",
    )
