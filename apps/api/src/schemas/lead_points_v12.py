from __future__ import annotations

from pydantic import BaseModel, Field


class LeadPointsSettingsBody(BaseModel):
    operation_claim_points: int = Field(gt=0, le=2_147_483_647, strict=True)
    supplier_provision_points: int = Field(ge=0, le=2_147_483_647, strict=True)
    expected_version: int = Field(ge=0, strict=True)
