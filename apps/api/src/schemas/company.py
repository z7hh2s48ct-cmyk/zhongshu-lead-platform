from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CompanyCreateBody(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9_-]+$")
    name: str = Field(min_length=2, max_length=128)
    owner_name: str | None = Field(default=None, max_length=64)
    contact_phone: str | None = Field(default=None, max_length=32)
    level_code: str = Field(default="V1", max_length=32)
    is_test: bool = False
    region_codes: list[str] = Field(default_factory=list)
    capabilities: list[dict[str, str | None]] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=500)


class CompanySimpleCreateBody(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    owner_name: str | None = Field(default=None, max_length=64)
    contact_phone: str | None = Field(default=None, max_length=32)
    level_code: str = Field(default="V1", max_length=32)
    primary_city_code: str = Field(min_length=1, max_length=32)
    district_codes: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list, max_length=1000)
    serve_all_districts: bool = True
    is_test: bool = False
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("primary_city_code")
    @classmethod
    def clean_primary_city_code(cls, value: str) -> str:
        return value.strip()

    @field_validator("district_codes", "region_codes")
    @classmethod
    def clean_region_codes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(code.strip() for code in value if code.strip()))


class CompanyUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=128)
    owner_name: str | None = Field(default=None, max_length=64)
    contact_phone: str | None = Field(default=None, max_length=32)
    level_code: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, pattern=r"^(ACTIVE|DISABLED|PENDING)$")
    region_codes: list[str] | None = None
    capabilities: list[dict[str, str | None]] | None = None
    notes: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def require_status_change_reason(self) -> "CompanyUpdateBody":
        if self.status is not None and not self.reason:
            raise ValueError("更改加盟商状态必须填写操作原因")
        return self


class CompanyOwnerWechatUnbindBody(BaseModel):
    confirm_name: str = Field(min_length=2, max_length=128)
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("confirm_name", "reason")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("加盟商名称和操作理由首尾不能有空格")
        return value


class CompanyDeleteBody(BaseModel):
    confirm_name: str = Field(min_length=2, max_length=128)
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("confirm_name", "reason")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("加盟商名称和操作理由首尾不能有空格")
        return value


class CompanyMarkTestBody(BaseModel):
    confirm_name: str = Field(min_length=2, max_length=128)
    reason: str = Field(min_length=2, max_length=500)
    confirm_phrase: Literal["永久删除测试数据"]
    scope_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("confirm_name", "reason")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("加盟商名称和操作理由首尾不能有空格")
        return value


class DictionaryItemBody(BaseModel):
    domain: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    sort_order: int = 0
    metadata: dict = Field(default_factory=dict)
    active: bool = True
