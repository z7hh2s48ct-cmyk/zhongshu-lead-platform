from __future__ import annotations

import json
from pathlib import Path
import subprocess


H5 = Path("apps/h5/public/v12-workbench.js")
ADMIN = Path("apps/admin/public/v12-operations.js")


def _run_js(source: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def test_h5_keeps_customer_and_supply_points_separate() -> None:
    source = H5.read_text(encoding="utf-8")

    assert "客资积分" in source
    assert "供客积分" in source
    assert "point_kind=CUSTOMER" in source
    assert "point_kind=SUPPLY" in source
    assert "客资积分流水" in source
    assert "供客积分流水" in source
    assert "终止合作通用积分" in source


def test_owner_can_review_and_submit_supply_termination() -> None:
    source = H5.read_text(encoding="utf-8")

    assert "termination" in source
    assert "/v1.2/supply-termination/current" in source
    assert "/v1.2/supply-termination" in source
    assert "终止客资合作" in source
    assert "supply.termination.own.read" in source
    assert "can('supply.termination.own.manage')&&state.cooperation_status==='ACTIVE'" in source
    assert "历史终止记录" in source
    assert "清算阻断项" in source
    assert "兑换比例" in source
    assert "线下付款" in source
    assert "不会停用公司或登录账号" in source
    assert "/v1.2/supply-termination/current/cancel" in source
    assert "付款前取消申请" in source
    assert "record_ids" in source
    assert "request.payee_account||request.payee_account_masked" in source
    assert "快照后新增客资积分保留" in source


def test_termination_hides_only_new_supply_entry() -> None:
    source = H5.read_text(encoding="utf-8")

    assert "supplyCooperationAllowsUpload" in source
    assert "TERMINATION_REQUESTED" in source
    assert "TERMINATED" in source
    assert "历史供资记录仍可查看" in source
    assert "FRANCHISE_OWNER:[['home'" in source
    assert "['assignments','hand-claim','接收']" in source
    assert "['followups','clipboard-check','跟进']" in source


def test_supply_write_entry_follows_cooperation_status() -> None:
    source = H5.read_text(encoding="utf-8")
    fragment = source[
        source.index("const TERMINATION_ACTIVE_STATUSES") : source.index(
            "function terminationMoney"
        )
    ]
    result = _run_js(
        fragment
        + """
console.log(JSON.stringify({
  active:supplyCooperationAllowsUpload({cooperation_status:'ACTIVE'}),
  paymentFailed:supplyCooperationAllowsUpload({cooperation_status:'ACTIVE',status:'PAYMENT_FAILED'}),
  reopened:supplyCooperationAllowsUpload({cooperation_status:'ACTIVE',status:'TERMINATED'}),
  pending:supplyCooperationAllowsUpload({cooperation_status:'TERMINATION_PENDING'}),
  terminated:supplyCooperationAllowsUpload({cooperation_status:'TERMINATED'}),
  requested:supplyCooperationAllowsUpload({cooperation_status:'ACTIVE',status:'REQUESTED'})
}));
"""
    )
    assert result == {
        "active": True,
        "paymentFailed": False,
        "reopened": True,
        "pending": False,
        "terminated": False,
        "requested": False,
    }


def test_owner_cancel_entry_stops_after_payment_is_recorded() -> None:
    source = H5.read_text(encoding="utf-8")
    start = source.index("function canCancelSupplyTermination")
    end = source.index("function cancelSupplyTermination", start)
    result = _run_js(
        source[start:end]
        + """
console.log(JSON.stringify({
  requested:canCancelSupplyTermination({status:'REQUESTED'}),
  needMore:canCancelSupplyTermination({status:'NEED_MORE'}),
  approved:canCancelSupplyTermination({status:'APPROVED_PENDING_PAYMENT'}),
  paid:canCancelSupplyTermination({status:'PAID_PENDING_WRITE_OFF'})
}));
"""
    )
    assert result == {
        "requested": True,
        "needMore": True,
        "approved": True,
        "paid": False,
    }


def test_admin_finance_exposes_audited_offline_termination_settlement() -> None:
    source = ADMIN.read_text(encoding="utf-8")

    assert "/v1.2/supply-terminations" in source
    assert "终止客资合作结算" in source
    assert "清算复检" in source
    assert "审核通过" in source
    assert "登记线下付款" in source
    assert "确认付款并核销积分" in source
    assert "恢复客资合作" in source
    assert "更正付款登记" in source
    assert "登记付款失败" in source
    assert "payment_amount_cents" in source
    assert "实际付款金额（元）" in source
    assert "record_ids" in source
    assert "item.payee_account||item.payee_account_masked" in source
    assert "本次冻结快照积分" in source
    assert "/system-configs?domain=supply_termination&status=PUBLISHED" in source
    assert "publish_immediately:true" in source
    assert "cash_cents_per_point" in source
    assert "积分兑换比例（分/积分）" in source
    assert "/payment/fail" in source
    assert "method:'PATCH'" in source
    assert "idempotency_key" in source
    assert "历史终止与付款记录会保留" in source


def test_sensitive_admin_actions_follow_permissions() -> None:
    source = ADMIN.read_text(encoding="utf-8")

    assert "supply.termination.review" in source
    assert "supply.termination.pay" in source
    assert "supply.termination.reopen" in source
    assert "can('supply.termination.pay')" in source
    assert "can('supply.termination.reopen')" in source


def test_operation_can_reach_termination_review_without_finance_access() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    economics = source[
        source.index("async function economics") : source.index("async function finance")
    ]

    assert "economics" in source
    assert "supplyTerminationSection" in economics
    assert "/v1.2/supply-terminations" in economics
    assert "/system-configs?domain=supply_termination&status=PUBLISHED" in economics
    assert "supplyTerminationRateSection" in economics
    assert "finance" not in "OPERATION:['overview','leads','supplements','closed','publicPool','telesales','dispatch','companies','economics']"


def test_manual_adjustment_requires_an_explicit_points_account() -> None:
    source = ADMIN.read_text(encoding="utf-8")
    adjustment = source[
        source.index("function adjustCompanyPoints") : source.index("function reverseLedger")
    ]

    assert 'id="adjust-point-kind"' in adjustment
    assert "客资积分" in adjustment and "供客积分" in adjustment
    assert "point_kind" in adjustment


def test_finance_company_table_shows_both_points_balances() -> None:
    source = ADMIN.read_text(encoding="utf-8")

    assert "customer_points_balance" in source
    assert "supply_points_balance" in source
    assert "客资积分 / 供客积分" in source
