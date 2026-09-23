"""来源渠道选项接口（2026-09-23 反馈 F2）。

- GET  /v1.2/source-channel/options：读取生效的来源选项（公开只读，
  与 /master-data/dictionaries 口径一致）；
- PUT  /v1.2/source-channel/options：整组保存（`source.channel.manage` 权限，
  已授予 OPERATION 角色），版本化落 SystemConfig 并写审计。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..core.auth import require_permissions
from ..core.database import get_db
from ..core.responses import ok
from ..schemas.v12_source_channel import SourceChannelOptionsBody
from ..services.source_channel_options import (
    get_source_channel_options,
    save_source_channel_options,
)

router = APIRouter(prefix="/v1.2/source-channel", tags=["v1.2-source-channel"])


@router.get("/options")
def read_source_channel_options(
    request: Request,
    db: Session = Depends(get_db),
):
    return ok(request, {"items": get_source_channel_options(db)})


@router.put("/options")
def update_source_channel_options(
    body: SourceChannelOptionsBody,
    request: Request,
    principal=Depends(require_permissions("source.channel.manage")),
    db: Session = Depends(get_db),
):
    # 审计由 save_source_channel_options 记录（含整组前后值）。
    items = save_source_channel_options(
        db,
        items=[item.model_dump() for item in body.items],
        principal=principal,
    )
    db.commit()
    return ok(request, {"items": items}, "来源选项已保存")
