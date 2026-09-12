from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..core.enums import ConfigStatus
from ..core.errors import AppError
from ..core.models import SystemConfig
from ..core.v12_enums import LeadSourceKind

LEAD_POINTS_DOMAIN = "lead_points"
LEAD_POINTS_KEY = "global"


@dataclass(frozen=True, slots=True)
class LeadPointsSettings:
    configured: bool
    operation_claim_points: int | None
    supplier_provision_points: int | None
    version: int
    config_id: str | None = None
    effective_at: datetime | None = None

    def to_dict(self) -> dict[str, int | bool | None]:
        return {
            "configured": self.configured,
            "operation_claim_points": self.operation_claim_points,
            "supplier_provision_points": self.supplier_provision_points,
            "version": self.version,
        }


def _latest_settings_config(db: Session, *, lock: bool = False) -> SystemConfig | None:
    stmt = (
        select(SystemConfig)
        .where(
            SystemConfig.domain == LEAD_POINTS_DOMAIN,
            SystemConfig.key == LEAD_POINTS_KEY,
            SystemConfig.status == ConfigStatus.PUBLISHED.value,
        )
        .order_by(SystemConfig.version.desc())
        .limit(1)
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def get_lead_points_settings(db: Session) -> LeadPointsSettings:
    item = _latest_settings_config(db)
    if item is None:
        return LeadPointsSettings(False, None, None, 0)
    return lead_points_settings_from_config(item)


def lead_points_settings_from_config(item: SystemConfig) -> LeadPointsSettings:
    values = dict(item.value_json or {})
    return LeadPointsSettings(
        configured=True,
        operation_claim_points=int(values["operation_claim_points"]),
        supplier_provision_points=int(values["supplier_provision_points"]),
        version=int(item.version),
        config_id=item.id,
        effective_at=item.effective_at,
    )


def operation_claim_points_for_lead(
    db: Session,
    *,
    source_kind: str | None,
    source_type: str | None = None,
    settings: LeadPointsSettings | None = None,
) -> tuple[int, int] | None:
    source = source_kind or source_type
    if source not in {
        LeadSourceKind.PLATFORM_MANUAL.value,
        LeadSourceKind.FEISHU_IMPORT.value,
    }:
        return None
    settings = settings or get_lead_points_settings(db)
    if not settings.configured or settings.operation_claim_points is None:
        return None
    return settings.operation_claim_points, settings.version


def update_lead_points_settings(
    db: Session,
    *,
    operation_claim_points: int,
    supplier_provision_points: int,
    expected_version: int,
    updated_by: str,
) -> LeadPointsSettings:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(71912026)))
    elif db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. A no-op write serializes both first-time and
        # subsequent setting changes before the expected-version check.
        db.execute(
            update(SystemConfig)
            .where(
                SystemConfig.domain == LEAD_POINTS_DOMAIN,
                SystemConfig.key == LEAD_POINTS_KEY,
            )
            .values(version=SystemConfig.version)
            .execution_options(synchronize_session=False)
        )
    current = _latest_settings_config(db, lock=True)
    current_version = int(current.version) if current else 0
    if expected_version != current_version:
        raise AppError(
            "LEAD_POINTS_SETTINGS_VERSION_CONFLICT",
            "积分设置已被其他管理员更新，请刷新后重试",
            409,
            {"expected_version": expected_version, "current_version": current_version},
        )
    if current is not None:
        current.status = ConfigStatus.RETIRED.value
    item = SystemConfig(
        domain=LEAD_POINTS_DOMAIN,
        key=LEAD_POINTS_KEY,
        value_json={
            "operation_claim_points": operation_claim_points,
            "supplier_provision_points": supplier_provision_points,
        },
        version=current_version + 1,
        status=ConfigStatus.PUBLISHED.value,
        effective_at=datetime.now(timezone.utc),
        published_by=updated_by,
    )
    db.add(item)
    db.flush()
    return get_lead_points_settings(db)
