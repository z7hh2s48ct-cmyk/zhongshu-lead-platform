from __future__ import annotations

import hashlib
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..core.models_v12 import Lead
from ..core.security import fingerprint_phone, hash_phone, normalize_phone


logger = logging.getLogger("zhongshu.phone_uniqueness")


def acquire_phone_identity_lock(db: Session, fingerprint: str) -> None:
    """Share the existing transaction lock with reward-window deduplication."""

    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(
        f"v12-phone-dedup:{fingerprint}".encode("ascii")
    ).digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_id)))


def require_unique_lead_phone(
    db: Session,
    *,
    phone: str,
    exclude_lead_id: str | None = None,
) -> str:
    """Check before assigning a phone, then persist it in this same transaction.

    Callers retain format validation and transaction ownership. Empty drafts do
    not reserve a number. 全平台规范化手机号唯一，包含草稿和历史记录，
    但不包含逻辑删除记录（2026-09-17 确认口径，替代 9.10 D02 的相关部分）：
    同号仅剩已删除记录时允许新录入，旧记录及其历史仍保留可查。
    PostgreSQL uses the application's default READ COMMITTED isolation.
    """

    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return normalized_phone
    fingerprint = fingerprint_phone(normalized_phone)
    statement = select(Lead.id).where(
        Lead.deleted_at.is_(None),
        or_(
            Lead.phone_fingerprint == fingerprint,
            Lead.phone_hash == hash_phone(normalized_phone),
        ),
    )
    if exclude_lead_id is not None:
        statement = statement.where(Lead.id != exclude_lead_id)
    try:
        with db.no_autoflush:
            acquire_phone_identity_lock(db, fingerprint)
            duplicate_id = db.scalar(statement.limit(1))
    except SQLAlchemyError:
        logger.exception(
            "lead phone uniqueness query failed for phone_hash/phone_fingerprint "
            "exclude_lead_id=%s",
            exclude_lead_id,
        )
        raise
    if duplicate_id is not None:
        raise AppError("LEAD_PHONE_DUPLICATE", "该手机号已存在客资，请勿重复录入", 409)
    return normalized_phone
