from sqlalchemy import select

from apps.api.src.core.models import PointsAccount
from apps.api.src.services.dispatch_v12 import evaluate_candidate, list_candidates
from apps.api.src.services.lead_points_v12 import update_lead_points_settings
from apps.api.tests.test_v12_dispatch_claim import dispatch_setup  # noqa: F401


def test_candidate_availability_subtracts_frozen_points_at_current_price(
    db, dispatch_setup
) -> None:
    _, _, receiver, receiver_user, lead = dispatch_setup
    update_lead_points_settings(
        db,
        operation_claim_points=150,
        supplier_provision_points=30,
        expected_version=0,
        updated_by=receiver_user.id,
    )
    account = db.scalar(
        select(PointsAccount).where(PointsAccount.company_id == receiver.id)
    )
    assert account is not None
    account.frozen_customer_points = 1000
    db.flush()

    single = evaluate_candidate(db, lead=lead, company=receiver)
    batch = next(
        item for item in list_candidates(db, lead=lead) if item.company_id == receiver.id
    )

    for candidate in (single, batch):
        assert candidate.points_price == 150
        assert candidate.points_balance == 1000
        assert candidate.points_reserved == 0
        assert candidate.points_available == 0
        assert candidate.eligible is False
        assert "POINTS_INSUFFICIENT" in candidate.exclusion_reasons
