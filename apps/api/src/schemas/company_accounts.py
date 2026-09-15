from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CompanyAccountCreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str | None = Field(default=None, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)
    role_code: str = Field(pattern=r"^(FRANCHISE_OWNER|FRANCHISE_EMPLOYEE)$")
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("username", "display_name")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("账号和姓名首尾不能有空格")
        return value


class CompanyAccountReasonBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class CompanyAccountPasswordBody(CompanyAccountReasonBody):
    new_password: str | None = Field(default=None, max_length=128)


class CompanyOwnerCredentialBody(CompanyAccountReasonBody):
    username: str | None = Field(default=None, min_length=2, max_length=64)
    password: str | None = Field(default=None, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class CompanyAccountRequestCreateBody(BaseModel):
    request_type: Literal["CREATE_EMPLOYEE", "DISABLE_EMPLOYEE"]
    username: str | None = Field(default=None, min_length=2, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    target_user_id: str | None = Field(default=None, min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_request_target(self):
        if self.reason != self.reason.strip():
            raise ValueError("申请说明首尾不能有空格")
        if self.request_type == "CREATE_EMPLOYEE":
            if not self.username or not self.display_name:
                raise ValueError("新增员工申请必须填写登录账号和姓名")
            if self.target_user_id:
                raise ValueError("新增员工申请不能指定现有人员")
            if self.username != self.username.strip() or self.display_name != self.display_name.strip():
                raise ValueError("账号和姓名首尾不能有空格")
        elif not self.target_user_id:
            raise ValueError("停用员工申请必须指定员工")
        elif self.username or self.display_name:
            raise ValueError("停用员工申请不能填写新员工信息")
        return self


class CompanyAccountRequestDecisionBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("处理说明首尾不能有空格")
        return value
