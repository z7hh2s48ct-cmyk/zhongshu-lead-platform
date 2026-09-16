import pytest
from sqlalchemy import func, select

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import AssignmentEvent, PointsAccount, PointsLedger, Region
from apps.api.src.services import return_v12 as service
from apps.api.src.services.followup_service import add_followup
from apps.api.src.services.rbac import assign_role
from apps.api.tests.test_v12_return_workflow import _principal, _submit_and_verify, _workflow_setup


def _region_case(db, *, early_reward=False):
    setup = _workflow_setup(db)
    assign_role(db, setup['reviewer'], 'OPERATION')
    if early_reward:
        add_followup(db, assignment=setup['assignment'],
            principal=_principal(setup['receiver_user'], 'followup.own.manage'),
            status='DEAL', note='客户确认有效，提前确认完成', next_followup_at=None)
        db.commit()
        assert setup['reward'].status == 'SETTLED'
    request, _ = _submit_and_verify(db, setup, conclusion='SUPPORT_RETURN')
    request.reason_code = 'OUT_OF_SERVICE_REGION'
    regions = [
        Region(code='410000', name='河南省', level='PROVINCE'),
        Region(code='410100', name='郑州市', level='CITY', parent_code='410000'),
        Region(code='410102', name='中原区', level='DISTRICT', parent_code='410100'),
    ]
    db.add_all([region for region in regions if db.get(Region, region.code) is None])
    db.commit()
    return setup, request, dict(return_id=request.id,
        principal=_principal(setup['reviewer'], 'return.review'),
        province_code='410000', city_code='410100', district_code='410102', reason='已核实实际建房位置在郑州中原区')


def test_region_correction_refunds_claim_snapshot_and_preserves_old_round(db):
    setup, request, args = _region_case(db)
    old_snapshot = dict(setup['assignment'].lead_snapshot)
    setup['assignment'].points_price = 250  # Refund the ledger, never the latest price.
    db.commit()
    result = service.correct_region_and_redispatch(db, **args)
    db.commit()
    assert result.refund_ledger.delta == 100
    assert setup['account'].balance == 1000
    assert request.status == 'APPROVED'
    assert setup['assignment'].status == 'RETURNED'
    assert setup['assignment'].lead_snapshot == old_snapshot
    assert setup['lead'].status == 'READY_DISPATCH'
    assert setup['lead'].current_assignment_id is None
    assert (setup['lead'].province, setup['lead'].city, setup['lead'].district) == ('河南省', '郑州市', '中原区')
    assert setup['reward'].status == 'CANCELLED'
    again = service.correct_region_and_redispatch(db, **args)
    assert again.idempotent
    assert again.refund_ledger.id == result.refund_ledger.id
    assert db.scalar(select(func.count(PointsLedger.id)).where(PointsLedger.business_type == 'V12_RETURN_REFUND')) == 1
    assert db.scalar(select(AssignmentEvent).where(AssignmentEvent.event_type == 'V12_RETURN_REGION_CORRECTED')) is not None


@pytest.mark.parametrize('override,code', [
    ({'city_code': '420100'}, 'RETURN_REGION_INVALID'),
    ({'reason': ' '}, 'RETURN_REGION_REASON_REQUIRED'),
])
def test_region_correction_rejects_invalid_input_without_partial_refund(db, override, code):
    setup, request, args = _region_case(db)
    with pytest.raises(AppError) as error:
        service.correct_region_and_redispatch(db, **dict(args, **override))
    db.rollback()
    assert error.value.code == code
    assert setup['account'].balance == 900
    assert request.status == 'REVIEWING'
    assert setup['lead'].city == '武汉市'


def test_region_correction_is_operations_only(db):
    setup, _, args = _region_case(db)
    args['principal'] = _principal(setup['receiver_user'], 'return.review')
    with pytest.raises(AppError) as error:
        service.correct_region_and_redispatch(db, **args)
    assert error.value.code == 'FORBIDDEN'


def test_region_correction_reverses_early_reward_exactly_once(db):
    setup, request, args = _region_case(db, early_reward=True)
    service.correct_region_and_redispatch(db, **args)
    db.commit()
    service.correct_region_and_redispatch(db, **args)
    assert setup['reward'].status == 'REVERSED'
    supplier = db.scalar(select(PointsAccount).where(PointsAccount.company_id == setup['supplier'].id))
    assert supplier.supply_balance == 0
    assert setup['account'].balance == 1000
    assert db.scalar(select(func.count(PointsLedger.id)).where(
        PointsLedger.business_type == 'V12_SUPPLIER_REWARD_REVERSAL')) == 1


def test_region_correction_rolls_back_refund_when_reward_reversal_fails(db, monkeypatch):
    from apps.api.src.services import supplier_reward_v12

    setup, request, args = _region_case(db, early_reward=True)
    def fail_reversal(*args, **kwargs):
        raise AppError('REWARD_REVERSAL_BLOCKED', '奖励正在结算中', 409)
    monkeypatch.setattr(supplier_reward_v12, 'reverse_supplier_reward', fail_reversal)
    with pytest.raises(AppError):
        service.correct_region_and_redispatch(db, **args)
    db.rollback()
    assert request.status == 'REVIEWING'
    assert setup['account'].balance == 900
    assert setup['lead'].city == '武汉市'
    assert setup['lead'].current_assignment_id == setup['assignment'].id
    assert setup['reward'].status == 'SETTLED'


def test_region_correction_http_and_permissions(api_client):
    from apps.api.src.core.auth import get_current_principal
    from apps.api.src.main import app

    client, factory = api_client
    with factory() as db:
        setup, request, args = _region_case(db)
        return_id = request.id
        owner = _principal(setup['receiver_user'], 'return.own.manage')
    principal = args.pop('principal')
    args.pop('return_id')
    try:
        app.dependency_overrides[get_current_principal] = lambda: owner
        assert client.post(f'/api/v1/v1.2/returns/{return_id}/correct-region-and-redispatch', json=args).status_code == 403
        app.dependency_overrides[get_current_principal] = lambda: principal
        response = client.post(f'/api/v1/v1.2/returns/{return_id}/correct-region-and-redispatch', json=args)
        assert response.status_code == 200, response.text
        assert response.json()['data']['refund_points'] == 100
        assert response.json()['data']['lead_region']['city'] == '郑州市'
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
