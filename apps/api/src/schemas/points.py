from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EffectiveWindowMixin(BaseModel):
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self):
        if self.effective_at and self.expires_at and self.effective_at >= self.expires_at:
            raise ValueError("生效时间必须早于失效时间")
        return self


class PointsPackageBody(EffectiveWindowMixin):
    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=128)
    cash_amount_cents: int = Field(ge=0)
    base_points: int = Field(gt=0)
    bonus_points: int = Field(default=0, ge=0)
    level_code: str = Field(default="V1", max_length=32)
    entitlements: dict = Field(default_factory=dict)
    publish: bool = True


class PriceRuleBody(EffectiveWindowMixin):
    region_code: str | None = Field(default=None, max_length=32)
    category_code: str | None = Field(default=None, max_length=64)
    brand_code: str | None = Field(default=None, max_length=64)
    level_code: str | None = Field(default=None, max_length=32)
    points_cost: int = Field(gt=0)
    priority: int = Field(default=100, ge=0, le=10000)
    publish: bool = True


class RechargeBody(BaseModel):
    company_id: str
    package_id: str
    external_reference: str = Field(min_length=3, max_length=128)
    cash_amount_cents: int = Field(ge=0)
    note: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)
    confirmed: bool = False

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ManualAdjustmentBody(BaseModel):
    company_id: str
    delta: int
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)
    point_kind: Literal["CUSTOMER", "SUPPLY"]

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ReverseLedgerBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value
