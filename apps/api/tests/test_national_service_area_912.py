from __future__ import annotations

from sqlalchemy import func, select

from apps.api.src.core.models import Company, Lead
from apps.api.src.core.models_v12 import CompanyServiceAreaV12
from apps.api.src.services.company_service import materialize_nationwide_regions
from apps.api.src.services.china_regions import region_tree
from apps.api.src.services.dispatch_v12 import has_receiver_coverage


def _login_operation(client) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "operation", "password": "Operation123!"},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _nationwide_city_codes() -> list[str]:
    return [
        city["code"]
        for province in region_tree()["provinces"]
        for city in province["cities"]
    ]


def test_platform_can_create_and_edit_nationwide_service_areas(api_client) -> None:
    client, factory = api_client
    operation = _login_operation(client)
    city_codes = _nationwide_city_codes()

    assert len(city_codes) == 341
    assert {"310000", "440100", "460000"}.issubset(city_codes)

    created = client.post(
        "/api/v1/companies/simple",
        headers=operation,
        json={
            "name": "全国服务加盟商",
            "primary_city_code": "440100",
            "district_codes": [],
            "region_codes": city_codes,
            "serve_all_districts": False,
        },
    )
    assert created.status_code == 200, created.text
    company_id = created.json()["data"]["id"]

    narrowed = client.put(
        f"/api/v1/v1.2/admin/companies/{company_id}/service-areas",
        headers=operation,
        json={"region_codes": ["440100"], "primary_city_code": "440100"},
    )
    assert narrowed.status_code == 200, narrowed.text

    restored = client.put(
        f"/api/v1/v1.2/admin/companies/{company_id}/service-areas",
        headers=operation,
        json={"region_codes": city_codes, "primary_city_code": "440100"},
    )
    assert restored.status_code == 200, restored.text

    with factory() as db:
        company = db.get(Company, company_id)
        assert company is not None
        active_areas = list(
            db.scalars(
                select(CompanyServiceAreaV12).where(
                    CompanyServiceAreaV12.company_id == company_id,
                    CompanyServiceAreaV12.active.is_(True),
                    CompanyServiceAreaV12.review_status == "APPROVED",
                )
            ).all()
        )
        assert len(active_areas) == len(city_codes)
        assert {item.region_code for item in active_areas} == set(city_codes)
        assert [item.region_code for item in active_areas if item.is_primary_city] == [
            "440100"
        ]
        assert db.scalar(
            select(func.count(CompanyServiceAreaV12.id)).where(
                CompanyServiceAreaV12.company_id == company_id,
                CompanyServiceAreaV12.active.is_(True),
            )
        ) == len(city_codes)

        sample_districts = ("440106", "310115", "469001")
        materialize_nationwide_regions(db, list(sample_districts))
        db.flush()
        for region_code in sample_districts:
            assert has_receiver_coverage(db, Lead(region_code=region_code)) is True
