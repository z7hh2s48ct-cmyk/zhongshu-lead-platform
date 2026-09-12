from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..core.auth import require_roles
from ..core.database import get_db
from ..core.responses import ok
from ..schemas.lead_points_v12 import LeadPointsSettingsBody
from ..services.audit import write_audit
from ..services.lead_points_v12 import get_lead_points_settings, update_lead_points_settings

router = APIRouter(prefix="/v1.2/admin", tags=["v1.2-lead-points-settings"])


@router.get("/lead-points-settings")
def read_lead_points_settings(
    request: Request,
    principal=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    return ok(request, get_lead_points_settings(db).to_dict())


@router.put("/lead-points-settings")
def save_lead_points_settings(
    body: LeadPointsSettingsBody,
    request: Request,
    principal=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    before = get_lead_points_settings(db).to_dict()
    settings = update_lead_points_settings(
        db,
        operation_claim_points=body.operation_claim_points,
        supplier_provision_points=body.supplier_provision_points,
        expected_version=body.expected_version,
        updated_by=principal.user_id,
    )
    after = settings.to_dict()
    write_audit(
        db,
        principal=principal,
        action="V12_LEAD_POINTS_SETTINGS_UPDATE",
        resource_type="lead_points_settings",
        resource_id=settings.config_id,
        before=before,
        after=after,
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, after, "客资积分设置已更新")
