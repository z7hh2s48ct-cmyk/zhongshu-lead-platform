from pathlib import Path


WORKBENCH = Path("apps/h5/public/v12-workbench.js")
WORKBENCH_CSS = Path("apps/h5/public/v12-workbench.css")
WORKBENCH_HTML = Path("apps/h5/public/v12-workbench.html")


def _slice(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def test_supplier_address_keeps_hierarchical_selects_and_adds_keyword_search() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    styles = WORKBENCH_CSS.read_text(encoding="utf-8")
    form = _slice(source, "async function openSupplyForm", "function launchSupplyForm")

    assert 'id="supply-city-search"' in form
    assert 'aria-controls="supply-city"' in form
    assert 'placeholder="搜索省份或城市"' in form
    assert 'id="supply-district-search"' in form
    assert 'aria-controls="supply-district"' in form
    assert 'placeholder="搜索区县"' in form
    assert 'id="supply-city"' in form
    assert 'id="supply-district"' in form
    assert "filterSupplyRegionOptions" in form
    assert "loadSupplyDistricts(event.target.value)" in form
    assert ".wb-region-search" in styles


def test_supplier_region_search_filters_existing_region_tree_options() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    filtering = _slice(
        source,
        "function filterSupplyRegionOptions",
        "function normalizeSupplyPhone",
    )

    assert "item.option_name||item.name" in filtering
    assert ".includes(keyword)" in filtering
    assert "zsSetSafeHtml(select" in filtering
    assert "matchedItems.length===0" in filtering
    assert "未找到匹配地区" in filtering


def test_supplier_region_candidates_show_full_parent_path_and_clear_invalid_children() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    loading = _slice(source, "async function loadSupplyCities", "function normalizeSupplyPhone")
    form = _slice(source, "async function openSupplyForm", "function launchSupplyForm")

    assert "province_name:province.name" in loading
    assert "city_name:city.name" in loading
    assert "option_name:`${city.province_name} · ${city.name} · ${district.name}`" in loading
    assert "bindSupplyRegionEmpty(citySearch)" in form
    assert "bindSupplyRegionEmpty(districtSearch)" in form
    assert "districtSelect.value=''" in form
    assert "townshipSelect.value=''" in form
    assert "districtSearch.value=''" in form
    assert "zsSetSafeHtml(townshipSelect,'<option value=\"\">请先选择区县</option>')" in form
    assert "districts.map(row=>`<option" in form
    assert "row.option_name||row.name" in form
    assert ".wb-region-empty[hidden]" in WORKBENCH_CSS.read_text(encoding="utf-8")


def test_supplier_form_displays_read_only_identity_from_logged_in_user() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    form = _slice(source, "async function openSupplyForm", "function launchSupplyForm")
    identity = _slice(source, "function supplyIdentityView", "async function openSupplyForm")

    assert "S.me?.company_id" in identity
    assert "S.me?.company_name" in identity
    assert "S.me?.display_name" in identity
    assert "未绑定有效加盟商" in identity
    assert "姓名未配置" in identity
    assert "供资加盟商" in identity
    assert "实际录入人" in identity
    assert "+supplyIdentityView()" in form
    assert 'aria-readonly="true"' in identity
    assert "<input" not in identity


def test_supplier_workbench_assets_are_cache_busted_for_feedback_911() -> None:
    html = WORKBENCH_HTML.read_text(encoding="utf-8")

    assert "v12-workbench.css?v=20260911-feedback-911" in html
    assert "v12-workbench.js?v=20260913-modal-lifecycle" in html


def test_supplier_customer_name_is_optional_and_blank_is_submitted_unchanged() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    form = _slice(source, "async function openSupplyForm", "function launchSupplyForm")
    payload = _slice(source, "function supplyPayload", "function validateSupplyDraft")
    validation = _slice(
        source,
        "function validateSupplySubmission",
        "function showSupplyErrors",
    )

    assert '<label for="supply-name">客户姓名</label>' in form
    name_input = form.split('id="supply-name"', 1)[1].split(">", 1)[0]
    assert "required" not in name_input
    assert "customer_name:document.querySelector('#supply-name')?.value.trim()||''" in payload
    assert "errors.customer_name" not in validation
