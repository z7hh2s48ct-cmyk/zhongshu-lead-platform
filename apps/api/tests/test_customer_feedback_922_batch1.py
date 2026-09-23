"""2026-09-22/23 客户反馈第一批验收：

- 第 2 条（C1）：加盟商 H5「所在地城市」框改走后端地区搜索，可直搜县级并回填市+县；
- 第 4 条（E1）：电销指派信息并入客资列表状态标签，已分配后按钮改「改派」；
- 第 8 条（G1）：「待终审」状态标签改蓝色（badge 全局 info）。

口径依据：docs/requirements/2026-09-23_922-923反馈需求口径定稿.md。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from apps.api.src.core.models import VerificationTask
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services.lead_export_v12 import (
    lead_report_to_dicts,
    list_lead_report_rows,
)
from apps.api.tests.test_v12_return_workflow import _workflow_setup

ADMIN_JS = Path("apps/admin/public/v12-operations.js")
ADMIN_CSS = Path("apps/admin/public/v12-operations.css")
H5_JS = Path("apps/h5/public/v12-workbench.js")
H5_HTML = Path("apps/h5/public/v12-workbench.html")


# ---------------------------------------------------------------------------
# 第 8 条：「待终审」改蓝
# ---------------------------------------------------------------------------


def test_reviewing_badge_uses_blue_info_class() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    assert "v==='REVIEWING'?'info':'warn'" in source
    css = ADMIN_CSS.read_text(encoding="utf-8")
    assert "--info:#3b6ea5" in css
    assert ".ops-status.info" in css


# ---------------------------------------------------------------------------
# 第 2 条：H5 城市框走后端地区搜索
# ---------------------------------------------------------------------------


def test_h5_city_search_goes_through_backend_region_search() -> None:
    source = H5_JS.read_text(encoding="utf-8")
    assert "function supplyCitySearchOptions" in source
    assert "function renderSupplyCitySearchResults" in source
    assert "function applySupplyDistrictSelection" in source
    assert "DISTRICT:" in source
    assert "搜索城市或区县" in source
    # 旧的纯客户端子串过滤不再直接挂在城市搜索输入上
    assert (
        "document.querySelector('#supply-city-search').oninput=event=>filterSupplyRegionOptions"
        not in source
    )


def test_h5_static_cache_version_bumped_for_region_search() -> None:
    html = H5_HTML.read_text(encoding="utf-8")
    assert "20260923-feedback-922-region-search" in html


def test_region_search_returns_feidong_county(api_client) -> None:
    client, _ = api_client

    response = client.get(
        "/api/v1/master-data/regions/search",
        params={"keyword": "肥东", "limit": 30},
    )

    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["data"] if row["code"] == "340122")
    assert item["level"] == "DISTRICT"
    assert item["path_label"] == "安徽省 · 合肥市 · 肥东县"


# ---------------------------------------------------------------------------
# 第 4 条：电销指派信息并入状态标签
# ---------------------------------------------------------------------------


def test_lead_report_dto_includes_telesales_assignee(db) -> None:
    setup = _workflow_setup(
        db,
        lead_status=LeadV12Status.PENDING_TELESALES_VERIFY.value,
        suffix="FB922A",
    )
    lead = setup["lead"]
    telesales = setup["telesales"]
    operator = setup["operator"]
    db.add(
        VerificationTask(
            lead_id=lead.id,
            task_type="PRE_DISPATCH_VERIFY",
            status="PENDING",
            assignee_user_id=telesales.id,
            assigned_by=operator.id,
            assigned_at=datetime(2026, 9, 22, 14, 20, tzinfo=timezone.utc),
        )
    )
    db.commit()

    rows, _ = list_lead_report_rows(db, filters={}, page_no=1, page_size=20)
    dicts = lead_report_to_dicts(db, rows)
    row = next(item for item in dicts if item["id"] == lead.id)

    assert row["latest_pre_dispatch_assignee_user_id"] == telesales.id
    assert row["latest_pre_dispatch_assignee_name"] == "退回核验电销"
    assert row["latest_pre_dispatch_assigned_at"] is not None


def test_lead_report_dto_without_task_keeps_assignee_none(db) -> None:
    _workflow_setup(db, suffix="FB922B")

    rows, _ = list_lead_report_rows(db, filters={}, page_no=1, page_size=20)
    dicts = lead_report_to_dicts(db, rows)

    assert dicts
    assert all(item["latest_pre_dispatch_assignee_name"] is None for item in dicts)


def test_admin_ui_shows_assignee_in_status_label_and_reassign_button() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    assert "电销 ${esc(lead.latest_pre_dispatch_assignee_name)}" in source
    assert "fmt(lead.latest_pre_dispatch_assigned_at)" in source
    assert "telesalesAssigned?'改派':'分配电销核实'" in source
