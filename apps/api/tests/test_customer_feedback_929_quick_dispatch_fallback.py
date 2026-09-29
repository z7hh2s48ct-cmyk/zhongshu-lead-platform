from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import Lead, Region
from apps.api.tests.test_pre_dispatch_verification_http import _login


def test_quick_dispatch_offers_public_pool_when_no_receiver() -> None:
    source = Path("apps/admin/public/v12-operations.js").read_text(encoding="utf-8")
    quick_dispatch = source[
        source.index("async function openQuickDispatchCandidates"):
        source.index("async function submitPlatformLead")
    ]
    assert "当前没有符合条件的接收加盟商" in quick_dispatch
    assert "data-quick-dispatch-public-pool" in quick_dispatch
    assert "'/v1.2/platform/leads/quick-dispatch/public-pool'" in quick_dispatch


def test_quick_dispatch_without_receiver_persists_formal_public_pool_lead(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        db.add_all([
            Region(code="530100", name="昆明市", level="CITY", aliases=[], active=True),
            Region(code="530102", name="五华区", level="DISTRICT", parent_code="530100", aliases=[], active=True),
        ])
        db.commit()
    _login(client, "operation", "Operation123!")
    payload = {
        "customer_name": "无人承接的快捷派发客户",
        "phone": "13900139991",
        "province": "云南省",
        "city": "昆明市",
        "district": "五华区",
        "region_code": "530102",
        "source_channel": "OTHER",
        "source_detail": "线下活动",
        "consent_confirmed": True,
    }

    preview = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch/candidates",
        json=payload,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["candidates"] == []
    assert preview.json()["data"]["pool_target"] == "PUBLIC_POOL"

    saved = client.post(
        "/api/v1/v1.2/platform/leads/quick-dispatch/public-pool",
        json=payload,
    )
    assert saved.status_code == 200, saved.text
    lead_id = saved.json()["data"]["lead"]["id"]
    with factory() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.status == "PUBLIC_POOL"
        assert lead.pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"
        assert db.scalar(select(Lead.id).where(Lead.id == lead_id)) == lead_id
