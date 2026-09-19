"""中国大陆省、市、区县三级地区快照。

数据快照来自开源项目 xihan123/gb2260 的 2025 年聚合结果。该项目基于
GB/T 2260 等公开资料整理，并非国家统计局官方实时接口。数据使用 CC0-1.0
许可；升级快照前需要重新执行地区联动回归测试。
"""

from __future__ import annotations

import base64
import gzip
import json
from functools import lru_cache
from typing import Any


SNAPSHOT_YEAR = 2025
SOURCE_URL = "https://github.com/xihan123/gb2260"
_REGION_TREE_GZIP_BASE64 = (
    "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"
    "iYdnDITJ9eZlYyey+fP/Ac/kk2vB8QEA"
)


@lru_cache(maxsize=1)
def region_tree() -> dict[str, Any]:
    """返回前端地区组件使用的只读省市区县树。"""

    provinces = json.loads(
        gzip.decompress(base64.b64decode(_REGION_TREE_GZIP_BASE64)).decode("utf-8")
    )
    return {
        "year": SNAPSHOT_YEAR,
        "scope": "中国大陆省市区县",
        "source": {
            "name": "xihan123/gb2260",
            "url": SOURCE_URL,
            "license": "CC0-1.0",
        },
        "provinces": provinces,
    }


@lru_cache(maxsize=1)
def city_index() -> dict[str, dict[str, Any]]:
    """按地区编码索引所有可选城市，供创建加盟商时复用同一份数据。"""

    return {
        city["code"]: city
        for province in region_tree()["provinces"]
        for city in province["cities"]
    }


def city_by_code(code: str) -> dict[str, Any] | None:
    return city_index().get(code)


@lru_cache(maxsize=1)
def region_index() -> dict[str, dict[str, str | None]]:
    """Return canonical province/city context for a nationwide region code."""

    items: dict[str, dict[str, str | None]] = {}
    for province in region_tree()["provinces"]:
        for city in province["cities"]:
            items[city["code"]] = {
                "province_code": province["code"],
                "province_name": province["name"],
                "city_code": city["code"],
                "city_name": city["name"],
                "district_code": None,
                "district_name": None,
            }
            for district in city["districts"]:
                items[district["code"]] = {
                    "province_code": province["code"],
                    "province_name": province["name"],
                    "city_code": city["code"],
                    "city_name": city["name"],
                    "district_code": district["code"],
                    "district_name": district["name"],
                }
    return items


def region_by_code(code: str) -> dict[str, str | None] | None:
    """Resolve a nationwide component code to canonical province/city names."""

    return region_index().get(code)


@lru_cache(maxsize=1)
def _searchable_regions() -> tuple[dict[str, Any], ...]:
    """Flatten the nationwide city/district snapshot for keyword lookup."""

    items: list[dict[str, Any]] = []
    for province in region_tree()["provinces"]:
        province_path = [
            {"code": province["code"], "name": province["name"], "level": "PROVINCE"}
        ]
        for city in province["cities"]:
            city_path = [
                *province_path,
                {"code": city["code"], "name": city["name"], "level": "CITY"},
            ]
            items.append(
                {
                    "code": city["code"],
                    "name": city["name"],
                    "level": "CITY",
                    "parent_code": province["code"],
                    "path": city_path,
                    "path_codes": [entry["code"] for entry in city_path],
                    "path_label": " · ".join(entry["name"] for entry in city_path),
                }
            )
            for district in city["districts"]:
                district_path = [
                    *city_path,
                    {
                        "code": district["code"],
                        "name": district["name"],
                        "level": "DISTRICT",
                    },
                ]
                items.append(
                    {
                        "code": district["code"],
                        "name": district["name"],
                        "level": "DISTRICT",
                        "parent_code": city["code"],
                        "path": district_path,
                        "path_codes": [entry["code"] for entry in district_path],
                        "path_label": " · ".join(
                            entry["name"] for entry in district_path
                        ),
                    }
                )
    return tuple(items)


def search_region_tree(keyword: str, *, limit: int) -> list[dict[str, Any]]:
    """Search nationwide city/district names without requiring DB materialization."""

    normalized = keyword.strip().casefold()
    if not normalized:
        return []
    matches = [
        item
        for item in _searchable_regions()
        if normalized in str(item["name"]).casefold()
    ]
    matches.sort(
        key=lambda item: (
            str(item["name"]).casefold() != normalized,
            not str(item["name"]).casefold().startswith(normalized),
            len(str(item["name"])),
            str(item["level"]),
            str(item["code"]),
        )
    )
    return [dict(item) for item in matches[:limit]]


def district_by_code(city: dict[str, Any], code: str) -> dict[str, str] | None:
    return next(
        (district for district in city["districts"] if district["code"] == code),
        None,
    )


@lru_cache(maxsize=1)
def province_index() -> dict[str, dict[str, Any]]:
    """按编码索引省级行区，与前端 region-tree 共用同一份快照。"""

    return {province["code"]: province for province in region_tree()["provinces"]}


def match_province_city_district(
    province_code: str,
    city_code: str,
    district_code: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """省/市/区县三级编码逐级隶属时返回快照节点，否则返回 None。

    这是平台地区唯一权威校验源：前端下拉与后端校验共用静态快照，
    不再读数据库 regions 表（该表仅为按需物化的服务区域行，缺省级行）。
    """

    province = province_index().get(province_code)
    if province is None:
        return None
    city = next(
        (item for item in province["cities"] if item["code"] == city_code),
        None,
    )
    if city is None:
        return None
    district = next(
        (item for item in city["districts"] if item["code"] == district_code),
        None,
    )
    if district is None:
        return None
    return province, city, district
