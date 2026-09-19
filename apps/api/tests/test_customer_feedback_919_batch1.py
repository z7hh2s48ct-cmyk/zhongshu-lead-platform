"""2026-09-19 客户反馈第一批的服务端验收测试。

覆盖：S13 修改实际区域并重新派发（快照校验、搜索数据源、承接去向规则）、
S11 退回记录关键字搜索、S12 主要城市支持区县级。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Region
from apps.api.src.core.models_v12 import CompanyLeadCapability, CompanyServiceAreaV12
from apps.api.src.core.state_machine_v12 import LEAD_TRANSITIONS
from apps.api.src.core.v12_enums import LeadV12Status
from apps.api.src.services import return_v12 as service
from apps.api.src.services.china_regions import match_province_city_district
from apps.api.src.services.rbac import assign_role
from apps.api.tests.test_v12_return_workflow import _principal, _submit_and_verify, _workflow_setup


# ---------------------------------------------------------------------------
# S13：省市区县校验以静态快照为唯一权威源
# ---------------------------------------------------------------------------


def test_match_province_city_district_accepts_hanchuan_under_xiaogan():
    matched = match_province_city_district('420000', '420900', '420984')
    assert matched is not None
    province, city, district = matched
    assert (province['name'], city['name'], district['name']) == ('湖北省', '孝感市', '汉川市')


def test_match_province_city_district_rejects_mismatched_chains():
    assert match_province_city_district('410000', '420900', '420984') is None
    assert match_province_city_district('420000', '410100', '410102') is None
    assert match_province_city_district('420000', '420900', '410102') is None
    assert match_province_city_district('999999', '420900', '420984') is None


def test_state_machine_allows_closed_lead_to_public_pool_only():
    assert LeadV12Status.PUBLIC_POOL in LEAD_TRANSITIONS[LeadV12Status.CLOSED]
    assert LeadV12Status.DISPATCHED not in LEAD_TRANSITIONS[LeadV12Status.CLOSED]


def _redispatch_case(db, *, cover_target=False):
    setup = _workflow_setup(db)
    assign_role(db, setup['reviewer'], 'OPERATION')
    request, _ = _submit_and_verify(db, setup, conclusion='SUPPORT_RETURN')
    request.reason_code = 'OUT_OF_SERVICE_REGION'
    if cover_target:
        db.add(
            CompanyLeadCapability(
                company_id=setup['receiver'].id,
                capability_code='LEAD_RECEIVER',
                review_status='APPROVED',
            )
        )
        db.add(
            CompanyServiceAreaV12(
                company_id=setup['receiver'].id,
                region_code='410102',
                region_level='DISTRICT',
                review_status='APPROVED',
            )
        )
    db.commit()
    args = dict(
        return_id=request.id,
        principal=_principal(setup['reviewer'], 'return.review'),
        province_code='410000', city_code='410100', district_code='410102',
        reason='已核实实际建房位置在郑州中原区',
    )
    return setup, request, args


def test_region_redispatch_without_coverage_routes_to_public_pool(db):
    setup, request, args = _redispatch_case(db)
    result = service.correct_region_and_redispatch(db, **args)
    db.commit()
    assert result.pool_target == 'PUBLIC_POOL'
    assert setup['lead'].status == 'PUBLIC_POOL'
    assert setup['lead'].pending_reason == 'PUBLIC_POOL_NO_LOCAL_RECEIVER'
    assert (setup['lead'].province, setup['lead'].city, setup['lead'].district) == ('河南省', '郑州市', '中原区')
    assert setup['lead'].region_code == '410102'


def test_region_redispatch_with_receiver_coverage_returns_to_dispatch_pool(db):
    setup, request, args = _redispatch_case(db, cover_target=True)
    result = service.correct_region_and_redispatch(db, **args)
    db.commit()
    assert result.pool_target == 'READY_DISPATCH'
    assert setup['lead'].status == 'READY_DISPATCH'
    assert setup['lead'].pending_reason == 'RETURN_REGION_CORRECTED'
    assert (setup['lead'].province, setup['lead'].city, setup['lead'].district) == ('河南省', '郑州市', '中原区')


def test_region_redispatch_platform_source_without_coverage_stays_dispatchable(db):
    setup, request, args = _redispatch_case(db)
    setup["lead"].source_kind = "PLATFORM_MANUAL"
    setup["lead"].source_type = "PLATFORM_MANUAL"
    db.commit()
    result = service.correct_region_and_redispatch(db, **args)
    db.commit()
    # 公海池只承载供资来源客资；平台来源无承接时保持待派发并标记原因。
    assert result.pool_target == "READY_DISPATCH"
    assert setup["lead"].status == "READY_DISPATCH"
    assert setup["lead"].pending_reason == "PUBLIC_POOL_NO_LOCAL_RECEIVER"


def test_region_redispatch_idempotent_replay_keeps_pool_target(db):
    setup, request, args = _redispatch_case(db)
    service.correct_region_and_redispatch(db, **args)
    db.commit()
    again = service.correct_region_and_redispatch(db, **args)
    assert again.idempotent
    assert again.pool_target == 'PUBLIC_POOL'


# ---------------------------------------------------------------------------
# S12：主要城市支持区县级（区/县/县级市）
# ---------------------------------------------------------------------------


def test_primary_city_validation_accepts_district_level(db):
    from apps.api.src.services.company_profile_v12 import _validate_region_hierarchy

    regions_rows = [
        Region(code='410100', name='郑州市', level='CITY', parent_code='410000'),
        Region(code='410102', name='中原区', level='DISTRICT', parent_code='410100'),
    ]
    db.add_all(regions_rows)
    db.commit()
    regions = {row.code: row for row in regions_rows}
    # 区县可作为主要城市，不抛错即通过。
    _validate_region_hierarchy(db, regions, '410102')
    with pytest.raises(AppError) as error:
        _validate_region_hierarchy(db, regions, '410000')
    assert error.value.code == 'PRIMARY_CITY_LEVEL_INVALID'


# ---------------------------------------------------------------------------
# S11：退回记录关键字搜索（编号 / 客户姓名 / 手机号）
# ---------------------------------------------------------------------------


def test_returns_keyword_search(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup = _workflow_setup(db)
        assign_role(db, setup['reviewer'], 'OPERATION')
        request, _ = _submit_and_verify(db, setup, conclusion='SUPPORT_RETURN')
        db.commit()
        return_id = request.id
        principal = _principal(setup['reviewer'], 'return.read')
    compact_id = return_id.replace('-', '')
    app.dependency_overrides[get_current_principal] = lambda: principal
    try:
        matched = client.get(
            '/api/v1/v1.2/returns', params={'keyword': compact_id[-8:]}
        )
        assert matched.status_code == 200, matched.text
        ids = [item['id'] for item in matched.json()['data']['items']]
        assert return_id in ids

        by_phone = client.get('/api/v1/v1.2/returns', params={'keyword': '13800138301'})
        assert return_id in [item['id'] for item in by_phone.json()['data']['items']]

        missed = client.get('/api/v1/v1.2/returns', params={'keyword': '绝无仅有的关键字'})
        assert all(item['id'] != return_id for item in missed.json()['data']['items'])
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
