from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class LeadDraftBody(BaseModel):
    customer_name: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    province: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=64)
    district: str | None = Field(default=None, max_length=64)
    region_code: str | None = Field(default=None, max_length=32)
    category_code: str | None = Field(default=None, max_length=64)
    brand_code: str | None = Field(default=None, max_length=64)
    source_channel: str | None = Field(default=None, max_length=64)
    source_detail: str | None = Field(default=None, max_length=128)
    need_summary: str | None = Field(default=None, max_length=2000)
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    acquisition_cost_cents: int | None = Field(default=None, ge=0)
    consent_confirmed: bool = False

    @model_validator(mode="after")
    def validate_budget(self) -> "LeadDraftBody":
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("预算上限不能低于预算下限")
        return self


class PlatformLeadDraftBody(LeadDraftBody):
    is_test: bool = False


class PlatformLeadPreDispatchBody(PlatformLeadDraftBody):
    assignee_user_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=64)

    @field_validator("assignee_user_id", "reason", "idempotency_key")
    @classmethod
    def normalize_pre_dispatch_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必填字段不能为空")
        return normalized


class LeadDraftUpdateBody(BaseModel):
    customer_name: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    province: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=64)
    district: str | None = Field(default=None, max_length=64)
    region_code: str | None = Field(default=None, max_length=32)
    category_code: str | None = Field(default=None, max_length=64)
    brand_code: str | None = Field(default=None, max_length=64)
    source_channel: str | None = Field(default=None, max_length=64)
    source_detail: str | None = Field(default=None, max_length=128)
    need_summary: str | None = Field(default=None, max_length=2000)
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    acquisition_cost_cents: int | None = Field(default=None, ge=0)
    consent_confirmed: bool | None = None


class LeadQuickDispatchBody(PlatformLeadDraftBody):
    company_id: str = Field(min_length=1, max_length=36)
    employee_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=64)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("company_id", "employee_user_id", "idempotency_key")
    @classmethod
    def normalize_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("必填字段不能为空")
        return normalized

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


class LeadCorrectionBody(LeadDraftUpdateBody):
    reason: str | None = Field(default=None, max_length=1000)
    expected_snapshot_version: int | None = Field(default=None, ge=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


class LeadCorrectionRecheckBody(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)
    expected_snapshot_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 5:
            raise ValueError("重新检查原因至少 5 个字符")
        return normalized


class LeadCorrectionRedispatchBody(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)
    expected_snapshot_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 5:
            raise ValueError("解除派发原因至少 5 个字符")
        return normalized


class TestLeadDeleteBody(BaseModel):
    confirmed_lead_id: str = Field(min_length=1, max_length=36)
    confirmed_customer_name: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=5, max_length=1000)

    @field_validator("confirmed_lead_id", "confirmed_customer_name", "reason")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必填字段不能为空")
        return normalized


class LeadDeleteBody(BaseModel):
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_delete_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("删除原因至少 2 个字符")
        return normalized


class SupplierReviewBody(BaseModel):
    decision: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=1000)
    assignee_user_id: str | None = Field(default=None, max_length=36)
    pre_dispatch_reason: str | None = Field(default=None, max_length=500)
    template_code: str = Field(default="PRE_DISPATCH", min_length=2, max_length=64)

    @field_validator("decision")
    @classmethod
    def normalize_decision(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {
            "QUALIFIED",
            "INFO_INCOMPLETE",
            "DUPLICATE",
            "INVALID",
            "APPROVE",
            "REJECT",
        }:
            raise ValueError("初审结论无效")
        return normalized

    @field_validator("note", "assignee_user_id", "pre_dispatch_reason")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("template_code")
    @classmethod
    def normalize_template_code(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_telesales_assignment_before_supplier_dispatch(self) -> "SupplierReviewBody":
        if self.decision == "INFO_INCOMPLETE":
            if not self.note:
                raise ValueError("派发电销核实前必须填写初审说明")
            if not self.assignee_user_id or not self.pre_dispatch_reason:
                raise ValueError("加盟商客资派送前必须指定电销人员和核验重点")
        elif self.decision in {"DUPLICATE", "INVALID", "REJECT"} and not self.note:
            raise ValueError("重复或无效结论必须填写初审说明")
        return self


class PreDispatchAssignBody(BaseModel):
    assignee_user_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=500)
    template_code: str = Field(default="PRE_DISPATCH", min_length=2, max_length=64)

    @field_validator("assignee_user_id", "reason", "template_code")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return value.strip()


class PreDispatchSubmitBody(BaseModel):
    contact_result: str = Field(min_length=1, max_length=64)
    conclusion: str = Field(
        pattern=r"^(QUALIFIED|INFO_INCOMPLETE|UNVERIFIABLE|INVALID|DUPLICATE)$"
    )
    note: str = Field(min_length=1, max_length=1000)

    @field_validator("contact_result", "conclusion")
    @classmethod
    def normalize_submission_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("note")
    @classmethod
    def normalize_submission_note(cls, value: str) -> str:
        return value.strip()


class PreDispatchDispositionBody(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE_POOL|RETURN_REWORK|DUPLICATE|CLOSE)$")
    note: str = Field(min_length=1, max_length=1000)

    @field_validator("decision")
    @classmethod
    def normalize_disposition_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("note")
    @classmethod
    def normalize_disposition_note(cls, value: str) -> str:
        return value.strip()


class DedupOverrideBody(BaseModel):
    event_id: str | None = None
    reason: str = Field(min_length=5, max_length=1000)


class CapabilityRequestBody(BaseModel):
    capability_code: str = Field(pattern=r"^(LEAD_SUPPLIER|LEAD_RECEIVER)$")

    @field_validator("capability_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.upper()


class CapabilityReviewBody(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("decision")
    @classmethod
    def normalize_decision(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def require_rejection_note(self) -> "CapabilityReviewBody":
        if self.decision == "REJECT" and not (self.note and self.note.strip()):
            raise ValueError("驳回公司能力申请时必须填写原因")
        return self


class CompanyCapabilityConfigureBody(BaseModel):
    """Platform-side capability switch; companies do not apply for it themselves."""

    active: bool
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


class CompanyProfileBulkApproveBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ServiceAreaReplaceBody(BaseModel):
    region_codes: list[str] = Field(min_length=1, max_length=1000)
    primary_city_code: str = Field(min_length=1, max_length=32)

    @field_validator("region_codes")
    @classmethod
    def normalize_region_codes(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))

    @field_validator("primary_city_code")
    @classmethod
    def normalize_primary_city(cls, value: str) -> str:
        return value.strip()


class ServiceAreaReviewBody(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("decision")
    @classmethod
    def normalize_decision(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def require_rejection_note(self) -> "ServiceAreaReviewBody":
        if self.decision == "REJECT" and not (self.note and self.note.strip()):
            raise ValueError("驳回服务区域申请时必须填写原因")
        return self
