from pathlib import Path

import pytest

from apps.api.src.core.models import Region


WORKBENCH = Path("apps/admin/public/v12-operations.js")


def _slice(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def test_service_region_builder_searches_and_adds_standard_regions() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    builder = _slice(
        source,
        "function serviceRegionBuilderMarkup",
        "async function openNewFranchiseCompany",
    )

    assert "搜索服务区域" in builder
    assert "region-search" in builder
    assert "/master-data/regions/search" in builder
    assert "path_label" in builder
    assert "data-add-region" in builder
    assert "searchRequestSequence" in builder
    assert "requestSequence!==searchRequestSequence" in builder


@pytest.mark.parametrize(
    ("keyword", "expected_code", "expected_path"),
    [
        ("仙桃", "429004", "湖北省 · 湖北省直辖县级行政区 · 仙桃市"),
        ("监利", "421088", "湖北省 · 荆州市 · 监利市"),
        ("天门", "429006", "湖北省 · 湖北省直辖县级行政区 · 天门市"),
    ],
)
def test_service_region_search_includes_nationwide_snapshot(
    api_client,
    keyword: str,
    expected_code: str,
    expected_path: str,
) -> None:
    client, _ = api_client

    response = client.get(
        "/api/v1/master-data/regions/search",
        params={"keyword": keyword, "limit": 30},
    )

    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["data"] if row["code"] == expected_code)
    assert item["level"] == "DISTRICT"
    assert item["path_label"] == expected_path


def test_service_region_search_keeps_full_snapshot_path_after_materialization(
    api_client,
) -> None:
    client, factory = api_client
    with factory() as db:
        db.add_all(
            [
                Region(
                    code="420000",
                    name="湖北省直辖县级行政区",
                    level="CITY",
                    aliases=[],
                    active=True,
                ),
                Region(
                    code="429004",
                    name="仙桃市",
                    level="DISTRICT",
                    parent_code="420000",
                    aliases=[],
                    active=True,
                ),
            ]
        )
        db.commit()

    response = client.get(
        "/api/v1/master-data/regions/search",
        params={"keyword": "仙桃", "limit": 30},
    )

    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["data"] if row["code"] == "429004")
    assert item["path_label"] == "湖北省 · 湖北省直辖县级行政区 · 仙桃市"


def test_lead_list_shows_current_franchise_handler() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    review = _slice(source, "async function review()", "function leadDetailBody")

    assert "加盟商跟进人" in review
    assert "franchise_handler_name" in review
    assert "franchise_handler_kind" in review


def test_misdispatch_uses_withdraw_and_redispatch_instead_of_delete() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    review = _slice(source, "async function review()", "function leadDetailBody")

    assert "撤回错派并重新入池" in review
    assert "data-lead-misdispatch-release" in review
    assert "/misdispatch/release-for-redispatch" in source
    assert "data-lead-delete" not in review


def test_test_leads_are_marked_at_creation_and_only_super_admin_can_purge() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    lead_form = _slice(
        source,
        "async function openPlatformLeadForm",
        "async function openQuickDispatchCandidates",
    )
    review = _slice(source, "async function review()", "function leadDetailBody")

    assert 'id="platform-lead-is-test"' in lead_form
    assert "is_test:testToggle.checked" in lead_form
    assert "isSuperAdmin()&&lead.is_test" in review
    assert "data-lead-test-delete" in review
    assert "/test-record" in source
    assert "/test-record/impact" in source
    assert "confirmed_lead_id" in source
    assert "confirmed_customer_name" in source


def test_complete_lead_and_public_pool_exports_are_available() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    public_pool = _slice(source, "async function publicPool()", "async function review()")
    review = _slice(source, "async function review()", "function leadDetailBody")

    assert "导出客资完整信息" in review
    assert "导出当前筛选" in public_pool
    assert "导出全部公海池" in public_pool
    assert "scope:'PUBLIC_POOL'" in public_pool
    assert "customer_source" in public_pool
    assert "duplicate_status" in public_pool
    assert 'id="public-pool-created-from"' in public_pool
    assert 'id="public-pool-created-to"' in public_pool
    assert 'id="public-pool-submitter"' in public_pool
    assert 'id="public-pool-filter-reset"' in public_pool
    assert "publicPoolFilters()" in public_pool
    assert "created_from" in source
    assert "created_to" in source
    assert "submitter_user_id" in source
    assert "下载 Excel" in public_pool
    assert "下载 ZIP" not in public_pool


def test_new_lead_defaults_to_draft_and_can_be_sent_directly_to_telesales() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    lead_form = _slice(
        source,
        "async function openPlatformLeadForm",
        "async function openQuickDispatchCandidates",
    )

    assert 'id="platform-lead-next-action"' in lead_form
    assert '<option value="DRAFT" selected>仅保存草稿</option>' in lead_form
    assert 'value="TELESALES"' in lead_form
    assert "保存并派发电销核验" in lead_form
    assert 'id="platform-lead-telesales-assignee"' in lead_form
    assert 'id="platform-lead-telesales-reason"' in lead_form
    assert "saveAndAssignNewLeadToTelesales" in lead_form
    assert "请填写有效手机号或客户微信号" in lead_form
    assert "/v1.2/platform/leads/pre-dispatch-verification" in lead_form
    assert "idempotency_key" in lead_form
    assert "submit.disabled=true" in lead_form


def test_public_pool_has_single_row_telesales_assignment_for_eligible_sources() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    public_pool = _slice(source, "async function publicPool()", "async function review()")

    assert "publicPoolTelesalesBlockReason" in source
    assert "['PLATFORM_MANUAL','FEISHU_IMPORT']" in source
    assert "data-public-pool-telesales" in public_pool
    assert "批量派发电销" not in public_pool
    assert "assignPublicPoolPreDispatch" in public_pool
    disposition = _slice(source, "function disposePreDispatch", "function companyQueuePager")
    assert "['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(sourceKind)" in disposition
