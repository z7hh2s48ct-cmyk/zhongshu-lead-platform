from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.models import Company, Role, User
from apps.api.src.core.models_v12 import CompanyLeadCapability
from apps.api.tests.test_call_h5_v12_contract import _login


ROOT = Path(__file__).resolve().parents[3]
ADMIN_JS = ROOT / "apps" / "admin" / "public" / "v12-operations.js"
ADMIN_HTML = ROOT / "apps" / "admin" / "public" / "v12-operations.html"
SUPPLIER_JS = ROOT / "apps" / "h5" / "public" / "v12-workbench.js"
INSIGHTS = ROOT / "apps" / "api" / "src" / "routers" / "v12_insights.py"


def _slice(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def test_operations_has_a_stable_pending_supplement_queue() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert "supplements:['待补充'" in source
    assert "function supplements()" in source
    assert "S.view==='supplements'" in source
    assert "PRE_DISPATCH_REWORK_REQUIRED" in source


def test_supplier_company_filter_limits_and_searches_submitters() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    insights = INSIGHTS.read_text(encoding="utf-8")

    assert 'id="lead-submitter-search"' in source
    assert "filterLeadSubmitters" in source
    assert "lead-supplier-company-filter" in source
    assert '"company_id"' in insights
    assert '"status"' in insights


def test_active_supplier_accounts_enter_filter_options_before_first_upload(api_client) -> None:
    client, factory = api_client
    with factory() as db:
        company = Company(code="FILTER-911", name="新开通供资公司", status="ACTIVE")
        db.add(company)
        db.flush()
        owner_role = db.scalar(select(Role).where(Role.code == "FRANCHISE_OWNER"))
        assert owner_role is not None
        owner = User(
            username="filter-911-owner",
            display_name="新开通负责人",
            company_id=company.id,
            status="ACTIVE",
        )
        owner.roles.append(owner_role)
        db.add_all(
            [
                owner,
                CompanyLeadCapability(
                    company_id=company.id,
                    capability_code="LEAD_SUPPLIER",
                    active=True,
                    review_status="APPROVED",
                ),
            ]
        )
        db.commit()

    response = client.get(
        "/api/v1/v1.2/reports/leads/filter-options",
        headers=_login(client, "operation", "Operation123!"),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    supplier = next(item for item in data["supplier_companies"] if item["name"] == "新开通供资公司")
    submitter = next(item for item in data["submitters"] if item["name"] == "新开通负责人")
    assert submitter["company_id"] == supplier["id"]
    assert submitter["status"] == "ACTIVE"


def test_operation_owned_supplement_attempts_the_next_transition_after_save() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    save = _slice(source, "async function savePlatformLead", "async function saveAndAssignNewLeadToTelesales")

    assert "forcePublicPool:['PLATFORM_MANUAL','FEISHU_IMPORT'].includes(lead.source_kind)" in source
    assert "/transfer-to-dispatch" in save
    assert "补充完成，客资已进入派发池" in save
    assert "资料仍需补充" in save


def test_return_task_detail_supports_direct_invalid_decision() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    detail = _slice(source, "async function taskDetail", "async function assignTask")

    assert "data-detail-invalid" in detail
    assert "/direct-invalid" in source
    assert "直接判定无效" in detail


def test_updated_admin_bundle_version_and_owner_fallback_copy() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert "v12-operations.js?v=20260913-modal-lifecycle" in html
    assert "有在职员工时选择具体员工；无在职员工时自动派发给负责人" in source


def test_assignment_detail_explains_all_deadline_timestamps() -> None:
    source = SUPPLIER_JS.read_text(encoding="utf-8")
    detail = _slice(source, "async function assignmentDetail", "async function claim")

    assert "['派发时间',fmt(x.assigned_at)]" in detail
    assert "['领取时间',fmt(x.claimed_at)]" in detail
    assert "['过期原因'" in detail


def test_operations_shows_verification_evidence_separately() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    return_detail = _slice(source, "async function returnDetail", "function finalReview")
    task_detail = _slice(source, "async function taskDetail", "async function assignTask")

    assert "电销核验证据" in return_detail
    assert "verification.evidences" in return_detail
    assert "电销核验证据" in task_detail
    assert "info.evidences" in task_detail


def test_operation_can_publish_a_custom_lead_points_price() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert "客资积分价格" in source
    assert "新增价格规则" in source
    assert "领取所需积分 *" in source
    assert "'/points/price-rules'" in source
