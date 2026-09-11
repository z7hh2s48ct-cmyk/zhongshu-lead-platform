from pathlib import Path


WORKBENCH = Path("apps/h5/public/v12-workbench.js")
WORKBENCH_CSS = Path("apps/h5/public/v12-workbench.css")


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
    assert "item.code===selectedCode" in filtering


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
