from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimBody(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_points: int | None = Field(default=None, gt=0, strict=True)
