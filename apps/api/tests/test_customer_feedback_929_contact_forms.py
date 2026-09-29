from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_supplier_form_accepts_phone_or_customer_wechat() -> None:
    source = (ROOT / "apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")
    assert 'id="supply-customer-wechat"' in source
    payload = source.split("function supplyPayload(){", 1)[1].split(
        "function validateSupplyDraft(", 1
    )[0]
    assert "customer_wechat:document.querySelector('#supply-customer-wechat')" in payload
    validation = source.split("function validateSupplySubmission(payload){", 1)[1].split(
        "function showSupplyErrors(", 1
    )[0]
    assert "!payload.phone&&!payload.customer_wechat" in validation


def test_operations_form_accepts_phone_or_customer_wechat() -> None:
    source = (ROOT / "apps/admin/public/v12-operations.js").read_text(
        encoding="utf-8"
    )
    assert 'id="platform-lead-customer-wechat"' in source
    payload = source.split("async function readPlatformLeadPayload(", 1)[1].split(
        "async function savePlatformLead(", 1
    )[0]
    assert "customer_wechat:document.querySelector('#platform-lead-customer-wechat')" in payload
