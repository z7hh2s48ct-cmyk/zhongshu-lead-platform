from __future__ import annotations

from pathlib import Path

from apps.api.src.main import app


def test_release_quality_documents_exist():
    required = [
        "docs/quality/TEST_REPORT.md",
        "docs/quality/SECURITY_AUDIT.md",
        "docs/traceability/IMPLEMENTATION_MATRIX.md",
        "docs/release/RELEASE_NOTES_V1.2.5.md",
        "docs/release/RELEASE_NOTES_V1.2.4.md",
        "docs/release/RELEASE_NOTES_V1.2.2.md",
        "docs/release/RELEASE_NOTES_V1.2.1.md",
        "docs/release/RELEASE_NOTES_V1.2.0.md",
        "docs/reviews/INDEX_V1.2.md",
        "docs/reviews/53-v1.2-sprint6-comprehensive-final-review.md",
        "docs/runbooks/DEPLOYMENT.md",
        "docs/runbooks/BACKUP_RESTORE.md",
        "docs/runbooks/SECURITY_CHECKLIST.md",
        "docs/runbooks/PRODUCTION_CHECKLIST_V1.2.md",
        "docs/runbooks/V1.2_INITIALIZATION_SOP.md",
        "docs/runbooks/V1.2_MIGRATION_RUNBOOK.md",
        "docs/runbooks/V1.2_UAT.md",
        "docs/runbooks/V1.2_GO_NO_GO.md",
        "docs/runbooks/V1.2_ROLLBACK.md",
        "docs/runbooks/V1.2_POST_LAUNCH.md",
        "docs/runbooks/WECHAT_GATE0.md",
        "docs/source/PRD_V1.0_执行版.pdf",
        "docs/source/详细开发计划_V1.0.xlsx",
    ]
    for path in required:
        assert Path(path).is_file(), path


def test_live_openapi_contains_v12_contract_and_no_online_payment():
    paths = app.openapi()["paths"]
    legacy_required = {
        "/api/v1/leads/feishu/sync",
        "/api/v1/verification/tasks/{task_id}/submit",
        "/api/v1/dispatch/leads/{lead_id}",
        "/api/v1/claims/assignments/{assignment_id}",
        "/api/v1/points/recharge",
        "/api/v1/followups/assignments/{assignment_id}",
        "/api/v1/returns/{return_id}/review",
    }
    v12_required = {
        "/api/v1/v1.2/reports/overview",
        "/api/v1/v1.2/reports/own",
        "/api/v1/v1.2/audit-events",
        "/api/v1/v1.2/trace/{business_id}",
    }
    assert legacy_required <= set(paths)
    assert v12_required <= set(paths)
    online_payment_paths = [
        path for path in paths
        if "/supply-terminations/" not in path
        and ("payment" in path.lower() or "wechat-pay" in path.lower())
    ]
    assert online_payment_paths == []
