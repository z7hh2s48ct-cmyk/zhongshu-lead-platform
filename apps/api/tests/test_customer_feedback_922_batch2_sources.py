"""2026-09-22/23 客户反馈第二批之三（F1/F2/F3，对应 9.23 第 1 条）：

- (a) 来源渠道（source_channel）加为客资列表筛选维度并在列表展示；
- (b) 字典补齐「直播 / 广告 / 其他」来源选项；
- (c) 客资列表新增关键词搜索，覆盖「具体来源」自由文本；
- 运营可编辑来源选项（SystemConfig 整组存储，source.channel.manage 权限）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import AuditLog, SystemConfig, User
from apps.api.src.services.lead_export_v12 import (
    list_lead_report_rows,
    normalized_lead_report_filters,
)
from apps.api.src.services.source_channel_options import (
    DEFAULT_SOURCE_CHANNEL_OPTIONS,
    get_source_channel_options,
    save_source_channel_options,
)
from apps.api.tests.test_v12_return_workflow import _principal, _workflow_setup


# ---------------------------------------------------------------------------
# (a)+(c)：来源渠道筛选 + 关键词搜索
# ---------------------------------------------------------------------------


def test_normalized_filters_keep_source_channel_and_keyword() -> None:
    values = normalized_lead_report_filters(
        {"source_channel": "douyin", "keyword": " 广告 "}
    )
    assert values["source_channel"] == "DOUYIN"
    assert values["keyword"] == "广告"


def test_lead_report_filters_by_source_channel(db) -> None:
    setup = _workflow_setup(db, suffix="FB923A")
    lead = setup["lead"]
    lead.source_channel = "DOUYIN"
    other = _workflow_setup(db, suffix="FB923B")
    other["lead"].source_channel = "AD"
    db.commit()

    rows, _ = list_lead_report_rows(
        db,
        filters={"source_channel": "DOUYIN"},
        page_no=1,
        page_size=20,
    )
    ids = [row.lead.id for row in rows]
    assert lead.id in ids
    assert other["lead"].id not in ids


def test_lead_report_keyword_search_covers_source_detail(db) -> None:
    setup = _workflow_setup(db, suffix="FB923C")
    lead = setup["lead"]
    lead.source_channel = "OTHER"
    lead.source_detail = "抖音直播投放带来的广告客资"
    db.commit()

    rows, total = list_lead_report_rows(
        db,
        filters={"keyword": "广告"},
        page_no=1,
        page_size=20,
    )
    assert total >= 1
    assert lead.id in [row.lead.id for row in rows]


# ---------------------------------------------------------------------------
# (b)：种子字典补齐直播/广告
# ---------------------------------------------------------------------------


def test_default_options_include_live_and_ad() -> None:
    codes = {item["code"] for item in DEFAULT_SOURCE_CHANNEL_OPTIONS}
    assert {"LIVE", "AD", "OTHER"} <= codes


# ---------------------------------------------------------------------------
# (F2)：运营可编辑来源选项
# ---------------------------------------------------------------------------


def test_options_fall_back_to_defaults_when_unconfigured(db) -> None:
    items = get_source_channel_options(db)
    assert items == DEFAULT_SOURCE_CHANNEL_OPTIONS


def test_operation_can_save_and_read_options(db) -> None:
    operator = User(display_name="来源运营", status="ACTIVE")
    db.add(operator)
    db.flush()
    principal = _principal(operator, "source.channel.manage")

    saved = save_source_channel_options(
        db,
        items=[
            {"code": "DOUYIN", "label": "抖音/信息流", "enabled": True},
            {"code": "TV_AD", "label": "电视广告", "enabled": True},
            {"code": "LIVE", "label": "直播", "enabled": False},
        ],
        principal=principal,
    )
    db.commit()

    assert {item["code"] for item in saved} == {"DOUYIN", "TV_AD", "LIVE"}
    items = get_source_channel_options(db)
    assert {item["code"] for item in items} == {"DOUYIN", "TV_AD", "LIVE"}
    live = next(item for item in items if item["code"] == "LIVE")
    assert live["enabled"] is False
    # 版本化 + 审计
    config = db.scalar(
        select(SystemConfig)
        .where(SystemConfig.domain == "source_channel")
        .order_by(SystemConfig.version.desc())
    )
    assert config.version == 1
    assert config.status == "PUBLISHED"
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "V12_SOURCE_CHANNEL_OPTIONS_UPDATE",
            AuditLog.resource_id == config.id,
        )
    )
    assert audit is not None


def test_options_reject_duplicate_and_bad_codes(db) -> None:
    operator = User(display_name="来源运营二", status="ACTIVE")
    db.add(operator)
    db.flush()
    principal = _principal(operator, "source.channel.manage")

    with pytest.raises(AppError):
        save_source_channel_options(
            db,
            items=[
                {"code": "AD", "label": "广告", "enabled": True},
                {"code": "AD", "label": "重复编码", "enabled": True},
            ],
            principal=principal,
        )
    with pytest.raises(AppError):
        save_source_channel_options(
            db,
            items=[{"code": "带中文!", "label": "非法编码", "enabled": True}],
            principal=principal,
        )


def test_source_channel_options_endpoint_is_public_read(api_client) -> None:
    client, _ = api_client

    response = client.get("/api/v1/v1.2/source-channel/options")

    assert response.status_code == 200, response.text
    codes = {item["code"] for item in response.json()["data"]["items"]}
    assert {"DOUYIN", "LIVE", "AD", "OTHER"} <= codes

    denied = client.put(
        "/api/v1/v1.2/source-channel/options",
        json={"items": [{"code": "AD", "label": "广告", "enabled": True}]},
    )
    assert denied.status_code in (401, 403)
