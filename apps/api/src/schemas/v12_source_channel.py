"""来源渠道选项请求体（2026-09-23 反馈 F2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceChannelOption(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=32)
    enabled: bool = True


class SourceChannelOptionsBody(BaseModel):
    items: list[SourceChannelOption] = Field(min_length=1, max_length=64)
