from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class ManualDispatchBody(BaseModel):
    company_id: str = Field(min_length=1, max_length=36)
    employee_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    return_receiver_override: bool = False
    return_receiver_override_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("company_id", "employee_user_id", "idempotency_key")
    @classmethod
    def strip_required(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("note", "return_receiver_override_reason")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def require_override_reason(self):
        if self.return_receiver_override and not self.return_receiver_override_reason:
            raise ValueError("再次派发给原领取公司必须填写例外原因")
        return self


class ClaimBody(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class RefuseAssignmentBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("拒绝领取必须填写原因")
        return cleaned


class InternalAssignmentBody(BaseModel):
    employee_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("employee_user_id", "reason")
    @classmethod
    def strip_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()
