from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.security import hash_phone, normalize_phone


class LeadReportFilterBody(BaseModel):
    created_from: datetime | None = None
    created_to: datetime | None = None
    source_kind: str | None = Field(default=None, max_length=32)
    supplier_company_id: str | None = Field(default=None, max_length=36)
    submitter_user_id: str | None = Field(default=None, max_length=36)
    phone: str | None = Field(default=None, max_length=32)
    region: str | None = Field(default=None, max_length=64)
    receiver_company_id: str | None = Field(default=None, max_length=36)
    lead_status: str | None = Field(default=None, max_length=32)
    assignment_status: str | None = Field(default=None, max_length=32)
    assigned_by_user_id: str | None = Field(default=None, max_length=36)
    pending_reason: str | None = Field(default=None, max_length=64)

    @field_validator(
        "source_kind",
        "supplier_company_id",
        "submitter_user_id",
        "region",
        "receiver_company_id",
        "lead_status",
        "assignment_status",
        "assigned_by_user_id",
        "pending_reason",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("source_kind", "lead_status", "assignment_status", "pending_reason")
    @classmethod
    def normalize_codes(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @field_validator("phone")
    @classmethod
    def normalize_exact_phone(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = normalize_phone(value)
        if not 7 <= len(normalized) <= 20:
            raise ValueError("请输入有效的完整手机号")
        return normalized

    @field_validator("created_from", "created_to")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("日期时间必须包含时区")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_window(self) -> "LeadReportFilterBody":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("创建时间起始不能晚于结束时间")
        return self

    def filters(self) -> dict[str, str | None]:
        return {
            "created_from": self.created_from.isoformat() if self.created_from else None,
            "created_to": self.created_to.isoformat() if self.created_to else None,
            "source_kind": self.source_kind,
            "supplier_company_id": self.supplier_company_id,
            "submitter_user_id": self.submitter_user_id,
            # 完整手机号只在请求校验期间存在，查询、任务和审计只保存不可逆值。
            "phone_hash": hash_phone(self.phone) if self.phone else None,
            "region": self.region,
            "receiver_company_id": self.receiver_company_id,
            "lead_status": self.lead_status,
            "assignment_status": self.assignment_status,
            "assigned_by_user_id": self.assigned_by_user_id,
            "pending_reason": self.pending_reason,
        }


class LeadReportSearchBody(LeadReportFilterBody):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class LeadExportRequestBody(LeadReportFilterBody):
    idempotency_key: str = Field(min_length=8, max_length=64)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("幂等键不能为空")
        return normalized
