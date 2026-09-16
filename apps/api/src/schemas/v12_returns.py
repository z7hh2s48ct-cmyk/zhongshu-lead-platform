from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.v12_enums import ReturnReasonCode


VALID_RETURN_REASONS = {item.value for item in ReturnReasonCode}
VALID_CONTACT_RESULTS = {
    "CONNECTED",
    "NO_ANSWER",
    "EMPTY_NUMBER",
    "OUT_OF_SERVICE",
    "WRONG_PERSON",
    "REFUSED",
    "OTHER",
}
VALID_VERIFICATION_CONCLUSIONS = {
    "SUPPORT_RETURN",
    "DOES_NOT_SUPPORT_RETURN",
    "INCONCLUSIVE",
}


class ReturnDraftV12Body(BaseModel):
    reason_code: str = Field(min_length=2, max_length=64)
    description: str = Field(min_length=5, max_length=1000)

    @field_validator("reason_code")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_RETURN_REASONS:
            raise ValueError("退回原因仅支持 V1.2 冻结的四类原因")
        return normalized

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ReturnVerificationAssignBody(BaseModel):
    assignee_user_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=2, max_length=1000)

    @field_validator("assignee_user_id")
    @classmethod
    def strip_assignee(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ReturnVerificationSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_result: str = Field(min_length=2, max_length=64)
    conclusion: str = Field(min_length=2, max_length=64)
    note: str = Field(min_length=2, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("contact_result")
    @classmethod
    def validate_contact_result(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_CONTACT_RESULTS:
            raise ValueError("联系结果无效")
        return normalized

    @field_validator("conclusion")
    @classmethod
    def validate_conclusion(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_VERIFICATION_CONCLUSIONS:
            raise ValueError("核验结论无效")
        return normalized

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("evidence_ids")
    @classmethod
    def normalize_evidence_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item and item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("核验证据不能重复")
        return normalized


class ReturnDirectInvalidBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=2, max_length=1000)

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ReturnRegionRedispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    province_code: str = Field(min_length=1, max_length=32)
    city_code: str = Field(min_length=1, max_length=32)
    district_code: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=2, max_length=1000)

    @field_validator("province_code", "city_code", "district_code", "reason", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ReturnFinalReviewBody(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT|NEED_MORE)$")
    note: str = Field(min_length=2, max_length=1000)

    @field_validator("decision")
    @classmethod
    def normalize_decision(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value
