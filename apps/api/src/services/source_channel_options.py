"""来源渠道选项（2026-09-23 反馈 F2）：运营可编辑的来源选项。

- 权威源：SystemConfig(domain='source_channel', key='options')，整组数组存储，
  天然版本化 + 审计；未配置时回落到内置默认（与 bootstrap 种子一致）。
- 编辑权：`source.channel.manage` 权限（授予 OPERATION 角色）；
- 只做启用/停用与改名/新增（软下线），历史客资引用不受停用影响。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import SystemConfig
from apps.api.src.services.audit import write_audit

CONFIG_DOMAIN = "source_channel"
CONFIG_KEY = "options"

DEFAULT_SOURCE_CHANNEL_OPTIONS: list[dict[str, Any]] = [
    {"code": "DOUYIN", "label": "抖音/信息流", "enabled": True},
    {"code": "WECHAT_VIDEO", "label": "视频号", "enabled": True},
    {"code": "XIAOHONGSHU", "label": "小红书", "enabled": True},
    {"code": "LIVE", "label": "直播", "enabled": True},
    {"code": "AD", "label": "广告", "enabled": True},
    {"code": "MANUAL", "label": "人工录入", "enabled": True},
    {"code": "OTHER", "label": "其他", "enabled": True},
]


def _normalize_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise AppError(
            "SOURCE_CHANNEL_OPTIONS_INVALID",
            "来源选项必须是非空数组",
            422,
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise AppError(
                "SOURCE_CHANNEL_OPTIONS_INVALID",
                "来源选项每项必须是对象",
                422,
            )
        code = str(entry.get("code") or "").strip().upper()
        label = str(entry.get("label") or "").strip()
        enabled = bool(entry.get("enabled", True))
        if not code or len(code) > 32 or not code.replace("_", "").isalnum():
            raise AppError(
                "SOURCE_CHANNEL_OPTIONS_INVALID",
                "来源选项编码必须是 1-32 位字母/数字/下划线",
                422,
                {"code": code},
            )
        if not label or len(label) > 32:
            raise AppError(
                "SOURCE_CHANNEL_OPTIONS_INVALID",
                "来源选项名称必填且不超过 32 个字",
                422,
                {"label": label},
            )
        if code in seen:
            raise AppError(
                "SOURCE_CHANNEL_OPTIONS_INVALID",
                "来源选项编码重复",
                422,
                {"code": code},
            )
        seen.add(code)
        normalized.append({"code": code, "label": label, "enabled": enabled})
    return normalized


def get_source_channel_options(db: Session) -> list[dict[str, Any]]:
    """已发布的来源选项；未配置时返回内置默认。"""

    config = db.scalar(
        select(SystemConfig)
        .where(
            SystemConfig.domain == CONFIG_DOMAIN,
            SystemConfig.key == CONFIG_KEY,
            SystemConfig.status == "PUBLISHED",
        )
        .order_by(SystemConfig.version.desc())
        .limit(1)
    )
    if config is None:
        return [dict(item) for item in DEFAULT_SOURCE_CHANNEL_OPTIONS]
    items = config.value_json.get("items")
    if not isinstance(items, list) or not items:
        return [dict(item) for item in DEFAULT_SOURCE_CHANNEL_OPTIONS]
    return [dict(item) for item in items]


def save_source_channel_options(
    db: Session,
    *,
    items: list[dict[str, Any]],
    principal,
) -> list[dict[str, Any]]:
    normalized = _normalize_items(items)
    latest_version = db.scalar(
        select(SystemConfig.version)
        .where(SystemConfig.domain == CONFIG_DOMAIN, SystemConfig.key == CONFIG_KEY)
        .order_by(SystemConfig.version.desc())
        .limit(1)
    )
    config = SystemConfig(
        domain=CONFIG_DOMAIN,
        key=CONFIG_KEY,
        value_json={"items": normalized},
        version=(latest_version or 0) + 1,
        status="PUBLISHED",
        effective_at=datetime.now(timezone.utc),
        published_by=principal.user_id if principal else None,
    )
    db.add(config)
    db.flush()
    write_audit(
        db,
        principal=principal,
        action="V12_SOURCE_CHANNEL_OPTIONS_UPDATE",
        resource_type="system_config",
        resource_id=config.id,
        after={"items": normalized, "version": config.version},
        reason="运营编辑来源渠道选项",
    )
    return normalized
