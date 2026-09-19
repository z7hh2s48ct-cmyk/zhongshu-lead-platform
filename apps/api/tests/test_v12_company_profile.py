from __future__ import annotations

import pytest

from apps.api.src.core.errors import AppError
from apps.api.src.core.models import Company, Region, User
from apps.api.src.services.company_profile_v12 import (
    replace_service_areas,
    request_capability,
    review_capability,
)


def _identity(db, code: str) -> tuple[Company, User]:
    company = Company(code=code, name=f"测试公司-{code}", status="ACTIVE")
    db.add(company)
    db.flush()
    user = User(display_name="审核员", status="ACTIVE", company_id=company.id)
    db.add(user)
    db.flush()
    return company, user


def test_disabled_approved_capability_can_be_resubmitted(db) -> None:
    company, user = _identity(db, "REAPPLY001")
    item = request_capability(db, company.id, "LEAD_RECEIVER")
    review_capability(
        db,
        company_id=company.id,
        capability_code="LEAD_RECEIVER",
        approve=True,
        reviewed_by=user.id,
    )
    item.active = False
    db.flush()

    resubmitted = request_capability(db, company.id, "LEAD_RECEIVER")
    assert resubmitted.id == item.id
    assert resubmitted.review_status == "PENDING"
    assert resubmitted.active is False
    assert resubmitted.reviewed_by is None


def test_primary_service_area_accepts_district_level(db) -> None:
    """2026-09-19 S12：主要城市支持区县级（区/县/县级市），主体归属可落到县。"""
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(code="420106", name="武昌区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
            Region(code="420000", name="湖北省", level="PROVINCE", aliases=[], active=True),
        ]
    )
    company, _ = _identity(db, "AREA003")
    items = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420106"],
        primary_city_code="420106",
    )
    district_rows = [item for item in items if item.region_code == "420106"]
    assert district_rows and all(item.is_primary_city for item in district_rows)
    # 省级行区仍不能作为主要城市。
    with pytest.raises(AppError) as exc_info:
        replace_service_areas(
            db,
            company_id=company.id,
            region_codes=["420106"],
            primary_city_code="420000",
        )
    assert exc_info.value.code == "PRIMARY_CITY_LEVEL_INVALID"


def test_district_only_service_area_does_not_expand_to_whole_city(db) -> None:
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(code="420106", name="武昌区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
            Region(code="420111", name="洪山区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
        ]
    )
    company, _ = _identity(db, "AREA-DISTRICT-ONLY")

    items = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420106"],
        primary_city_code="420100",
    )

    assert [item.region_code for item in items] == ["420106"]
    assert items[0].is_primary_city is True


def test_service_areas_can_span_multiple_cities(db) -> None:
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(code="430100", name="长沙市", level="CITY", aliases=[], active=True),
            Region(code="430102", name="芙蓉区", level="DISTRICT", parent_code="430100", aliases=[], active=True),
        ]
    )
    company, _ = _identity(db, "AREA004")
    items = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420100", "430102"],
        primary_city_code="420100",
    )

    assert {item.region_code for item in items} == {"420100", "430102"}
    assert next(item for item in items if item.region_code == "420100").is_primary_city


def test_service_area_accepts_township_under_primary_city(db) -> None:
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(code="420106", name="武昌区", level="DISTRICT", parent_code="420100", aliases=[], active=True),
            Region(code="420106001", name="粮道街道", level="TOWNSHIP", parent_code="420106", aliases=[], active=True),
        ]
    )
    company, _ = _identity(db, "AREA-TOWN")

    items = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420100", "420106001"],
        primary_city_code="420100",
    )

    township = next(item for item in items if item.region_code == "420106001")
    assert township.region_level == "TOWNSHIP"
    assert township.review_status == "PENDING"


def test_service_area_accepts_township_in_another_city(db) -> None:
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(code="430100", name="长沙市", level="CITY", aliases=[], active=True),
            Region(code="430102", name="芙蓉区", level="DISTRICT", parent_code="430100", aliases=[], active=True),
            Region(code="430102001", name="定王台街道", level="TOWNSHIP", parent_code="430102", aliases=[], active=True),
        ]
    )
    company, _ = _identity(db, "AREA-CROSS-TOWN")

    items = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420100", "430102001"],
        primary_city_code="420100",
    )

    township = next(item for item in items if item.region_code == "430102001")
    assert township.region_level == "TOWNSHIP"


def test_withdrawn_service_area_request_keeps_its_review_history(db) -> None:
    db.add_all(
        [
            Region(code="420100", name="武汉市", level="CITY", aliases=[], active=True),
            Region(
                code="420106",
                name="武昌区",
                level="DISTRICT",
                parent_code="420100",
                aliases=[],
                active=True,
            ),
        ]
    )
    company, _ = _identity(db, "AREA-HISTORY")
    initial = replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420100", "420106"],
        primary_city_code="420100",
    )
    district = next(item for item in initial if item.region_code == "420106")
    district_id = district.id

    replace_service_areas(
        db,
        company_id=company.id,
        region_codes=["420100"],
        primary_city_code="420100",
    )
    db.flush()

    withdrawn = db.get(type(district), district_id)
    assert withdrawn is not None
    assert withdrawn.active is False
    assert withdrawn.review_status == "REJECTED"
    assert "撤回" in withdrawn.review_note
