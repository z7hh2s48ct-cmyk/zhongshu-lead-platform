"""问题二·权限复核回归：提现审核/付款/失败/核销/更正仅超级管理员可调用。

2026-09-24 资金修复方案要求「读权限与操作权限分开，补上非超管拒绝测试，避免
界面隐藏但 API 可调用」。本用例直接打真实 HTTP 接口，验证：

- OPERATION（持 reward.read，无 ``*``）与 FRANCHISE_OWNER 调用管理操作端点一律 403；
- SUPER_ADMIN 通过权限闸门（对不存在的提现返回 404 而非 403），证明读/操作已分离。
"""
from __future__ import annotations

import pytest


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


# (方法, 路径后缀, 请求体) —— 覆盖全部收紧为 require_permissions("*") 的管理操作端点。
_MANAGE_ACTIONS = [
    ("POST", "review", {"decision": "APPROVE", "review_note": "复核额度与手续费"}),
    ("POST", "record-transfer", {"amount_cents": 10000}),
    ("PATCH", "registration-correction", {"reason": "登记编号填写错误，更正"}),
    ("POST", "fail", {"note": "线下转账失败"}),
    ("POST", "confirm", None),
]


@pytest.mark.parametrize(
    ("username", "password"),
    [("operation", "Operation123!"), ("franchise_demo", "Franchise123!")],
)
def test_non_super_admin_cannot_call_withdrawal_manage_endpoints(
    api_client, username, password
) -> None:
    client, _ = api_client
    headers = _login(client, username, password)

    for method, suffix, body in _MANAGE_ACTIONS:
        url = f"/api/v1/v1.2/admin/supply-withdrawals/does-not-exist/{suffix}"
        response = client.request(method, url, headers=headers, json=body)
        assert response.status_code == 403, (username, suffix, response.text)


def test_super_admin_passes_permission_gate_for_manage_endpoints(api_client) -> None:
    client, _ = api_client
    headers = _login(client, "admin", "Admin123!")

    for method, suffix, body in _MANAGE_ACTIONS:
        url = f"/api/v1/v1.2/admin/supply-withdrawals/does-not-exist/{suffix}"
        response = client.request(method, url, headers=headers, json=body)
        # 超级管理员通过权限闸门：对不存在的提现返回业务 404，而非权限 403。
        assert response.status_code != 403, (suffix, response.text)
        assert response.status_code == 404, (suffix, response.text)
