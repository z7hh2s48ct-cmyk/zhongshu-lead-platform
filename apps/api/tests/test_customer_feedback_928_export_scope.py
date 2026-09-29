"""2026-09-28 反馈第 4 条：加盟商导出"新领取客资不出现"排查的行为锁定。

线上版本已确认与 9.22/9.23 分支一致（PR #3 已部署），导出实现无版本偏差。
本测试锁定导出的范围语义，作为客户确认口径后决策的基线：

- 导出语义 = "已领取"：CLAIMED / FOLLOWING / COMPLETED；
- 已派未领取（PENDING_CLAIM）与退回单步提交后（RETURN_PENDING）不在导出内
  ——这正是"9/28 导出与 9/24 内容一致、新客资缺失"的两个候选数据状态；
- 若客户口径改为"派给我司的全部导出"，按口径定稿第四节调整状态集后，
  本测试同步修改作为验收门禁。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from apps.api.src.core.enums import AssignmentStatus
from apps.api.src.core.models import Assignment
from apps.api.tests.test_customer_feedback_919_batch3 import (
    HEADERS,
    _owner_export_headers,
    _parse_csv,
    _principal,
    _workflow_setup,
)

WORKBENCH_JS = Path("apps/h5/public/v12-workbench.js")
WORKBENCH_CSS = Path("apps/h5/public/v12-workbench.css")
WORKBENCH_HTML = Path("apps/h5/public/v12-workbench.html")


def _get_assignment(db, setup) -> Assignment:
    assignment = db.scalar(
        select(Assignment).where(Assignment.id == setup["assignment"].id)
    )
    assert assignment is not None
    return assignment


def test_pending_claim_lead_is_excluded_from_export(api_client) -> None:
    """锁定现状：已派未领取（PENDING_CLAIM）不出现在导出 CSV 中。"""

    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        principal = _principal(
            setup["receiver_user"], "assignment.own.read", "lead.own.phone.read"
        )
        assignment = _get_assignment(db, setup)
        assignment.status = AssignmentStatus.PENDING_CLAIM.value
        db.commit()

    response = _owner_export_headers(client, lambda: principal)
    assert response.status_code == 200, response.text
    rows = _parse_csv(response.text)
    assert rows[0] == HEADERS
    assert len(rows) == 1  # 仅有表头：已派未领取不导出。


def test_return_pending_claimed_lead_disappears_from_export(api_client) -> None:
    """锁定现状：领取后提交退回（RETURN_PENDING）的客资从导出中消失。"""

    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        principal = _principal(
            setup["receiver_user"], "assignment.own.read", "lead.own.phone.read"
        )
        assert _get_assignment(db, setup).status == AssignmentStatus.CLAIMED.value

    response = _owner_export_headers(client, lambda: principal)
    rows = _parse_csv(response.text)
    assert len(rows) == 2  # CLAIMED 状态正常导出（表头 + 1 行）。

    with factory() as db:
        assignment = _get_assignment(db, setup)
        assignment.status = AssignmentStatus.RETURN_PENDING.value
        assignment.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

    response_after = _owner_export_headers(client, lambda: principal)
    rows_after = _parse_csv(response_after.text)
    assert len(rows_after) == 1  # 提交退回后即从导出消失。


def test_export_hint_mentions_return_pending_exclusion(api_client) -> None:
    """口径定稿第 4 条方案 A：导出按钮旁必须说明"退回处理中不包含在导出内"。

    背景：H5 跟进列表含 RETURN_PENDING，而导出排除之；客户在页面上看到
    "已领取"却导不出，需要在导出入口处就地说明差异，避免反复反馈。
    """

    source = WORKBENCH_JS.read_text(encoding="utf-8")
    styles = WORKBENCH_CSS.read_text(encoding="utf-8")
    html = WORKBENCH_HTML.read_text(encoding="utf-8")

    start = source.index("const exportLink=")
    snippet = source[start : source.index("shell(`<section", start)]
    assert "退回处理中的客资不包含在导出内" in snippet
    # 说明与导出按钮同容器，保证视觉上"按钮旁"。
    assert 'class="wb-export-wrap"' in snippet
    # 样式与缓存版本同步。
    assert ".wb-export-wrap" in styles
    assert "v12-workbench.js?v=20260930-feedback-929" in html
    assert "v12-workbench.css?v=20260930-feedback-929" in html
