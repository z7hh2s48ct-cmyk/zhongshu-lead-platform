from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator


class SupplyTerminationCreateBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    payee_name: str = Field(min_length=2, max_length=64)
    payee_account: str = Field(min_length=4, max_length=128)
    payment_method: str = Field(min_length=2, max_length=32)

    @field_validator("reason", "payee_name", "payee_account", "payment_method", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SupplyTerminationReviewBody(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT|NEED_MORE)$")
    note: str = Field(min_length=2, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SupplyTerminationPaymentBody(BaseModel):
    external_reference: str = Field(min_length=3, max_length=128)
    paid_at: datetime
    payment_amount_cents: int = Field(ge=0)
    note: str = Field(min_length=2, max_length=500)
    proof_url: str | None = Field(default=None, max_length=2048)

    @field_validator("external_reference", "note", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("proof_url", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("paid_at")
    @classmethod
    def reject_future_payment_time(cls, value: datetime) -> datetime:
        normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if normalized > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("付款时间不能晚于当前时间")
        return value


class SupplyTerminationConfirmBody(BaseModel):
    confirmed: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128)


class SupplyCooperationReopenBody(BaseModel):
    note: str = Field(min_length=2, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SupplyTerminationNoteBody(BaseModel):
    note: str = Field(min_length=2, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value
