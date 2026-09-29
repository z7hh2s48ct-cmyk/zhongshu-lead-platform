from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.models import Lead
from ..core.models_v12 import V12MigrationCheckpoint
from ..core.security import decrypt_text, fingerprint_phone, hash_phone, normalize_phone

settings = get_settings()

PHONE_FINGERPRINT_CHECKPOINT = "t30_phone_fingerprint_backfill_v1"
_MAX_ERROR_SAMPLES = 50


def phone_fingerprint_missing_condition():
    # Earlier drafts could store an encrypted empty string as a missing phone.
    return and_(
        Lead.phone_fingerprint.is_(None),
        Lead.phone_encrypted.is_not(None),
        or_(Lead.phone_hash.is_(None), Lead.phone_hash != hash_phone("")),
    )


@dataclass(frozen=True, slots=True)
class FingerprintBatchResult:
    scanned: int
    updated: int
    errors: int
    last_cursor: str | None
    complete: bool
    checkpoint_status: str
    error_samples: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FingerprintPreviewResult:
    scanned: int
    eligible: int
    errors: int
    truncated: bool
    last_cursor: str | None
    error_samples: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_secret(secret: str | None) -> str:
    resolved = (secret or settings.effective_phone_fingerprint_secret).strip()
    if not resolved:
        raise ValueError("PHONE_FINGERPRINT_SECRET 未配置")
    return resolved


def _phone_for_fingerprint(lead: Lead) -> str:
    decrypted = decrypt_text(lead.phone_encrypted)
    if not decrypted:
        raise ValueError("PHONE_DECRYPT_FAILED")
    normalized = normalize_phone(decrypted)
    if len(normalized) < 7 or len(normalized) > 15:
        raise ValueError("PHONE_FORMAT_INVALID")
    return normalized


def _select_missing_batch(
    db: Session,
    *,
    cursor: str | None,
    batch_size: int,
    lock_rows: bool,
) -> list[Lead]:
    if batch_size < 1 or batch_size > 5000:
        raise ValueError("batch_size 必须在 1 到 5000 之间")
    statement = select(Lead).where(phone_fingerprint_missing_condition())
    if cursor:
        statement = statement.where(Lead.id > cursor)
    statement = statement.order_by(Lead.id).limit(batch_size)
    if lock_rows:
        statement = statement.with_for_update(skip_locked=True)
    return list(db.scalars(statement).all())


def _acquire_migration_lock(db: Session) -> None:
    """Serialize writers on PostgreSQL while keeping SQLite test behavior simple."""

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": PHONE_FINGERPRINT_CHECKPOINT},
        )


def _get_or_create_checkpoint(db: Session, *, reset: bool) -> V12MigrationCheckpoint:
    _acquire_migration_lock(db)
    checkpoint = db.scalar(
        select(V12MigrationCheckpoint)
        .where(V12MigrationCheckpoint.key == PHONE_FINGERPRINT_CHECKPOINT)
        .with_for_update()
    )
    if checkpoint is not None and reset:
        db.delete(checkpoint)
        db.flush()
        checkpoint = None
    if checkpoint is None:
        checkpoint = V12MigrationCheckpoint(
            key=PHONE_FINGERPRINT_CHECKPOINT,
            cursor=None,
            processed_count=0,
            error_count=0,
            status="PENDING",
            metadata_json={"updated_count": 0, "error_samples": []},
        )
        db.add(checkpoint)
        db.flush()
    return checkpoint


def backfill_phone_fingerprints_batch(
    db: Session,
    *,
    batch_size: int = 500,
    secret: str | None = None,
    reset: bool = False,
) -> FingerprintBatchResult:
    """Backfill one bounded, resumable batch without committing the transaction.

    The caller owns commit/rollback. Plaintext phone values are never returned or
    stored in checkpoint metadata. Failed rows are recorded by business ID and
    can be retried by explicitly resetting the checkpoint after remediation.
    """

    resolved_secret = _resolve_secret(secret)
    checkpoint = _get_or_create_checkpoint(db, reset=reset)

    if checkpoint.status == "COMPLETED":
        missing = db.scalar(select(func.count()).select_from(Lead).where(
            phone_fingerprint_missing_condition()
        )) or 0
        if missing == 0:
            return FingerprintBatchResult(
                scanned=0,
                updated=0,
                errors=0,
                last_cursor=checkpoint.cursor,
                complete=True,
                checkpoint_status=checkpoint.status,
            )
        # A later data import can introduce new historical rows. A previously
        # clean completion safely starts a new pass over only missing rows.
        checkpoint.cursor = None
        checkpoint.status = "RUNNING"

    rows = _select_missing_batch(
        db,
        cursor=checkpoint.cursor,
        batch_size=batch_size,
        lock_rows=True,
    )
    if not rows:
        status = "COMPLETED_WITH_ERRORS" if checkpoint.error_count else "COMPLETED"
        checkpoint.status = status
        metadata = dict(checkpoint.metadata_json or {})
        metadata["completed_at"] = _utcnow().isoformat()
        checkpoint.metadata_json = metadata
        db.flush()
        return FingerprintBatchResult(
            scanned=0,
            updated=0,
            errors=0,
            last_cursor=checkpoint.cursor,
            complete=True,
            checkpoint_status=status,
        )

    checkpoint.status = "RUNNING"
    updated = 0
    errors = 0
    samples: list[dict[str, str]] = []
    for lead in rows:
        try:
            normalized = _phone_for_fingerprint(lead)
            lead.phone_fingerprint = fingerprint_phone(normalized, secret=resolved_secret)
            updated += 1
        except ValueError as exc:
            errors += 1
            if len(samples) < _MAX_ERROR_SAMPLES:
                samples.append({"lead_id": lead.id, "reason": str(exc)})

    last_cursor = rows[-1].id
    checkpoint.cursor = last_cursor
    checkpoint.processed_count += len(rows)
    checkpoint.error_count += errors
    metadata = dict(checkpoint.metadata_json or {})
    metadata["updated_count"] = int(metadata.get("updated_count", 0)) + updated
    previous_samples = list(metadata.get("error_samples") or [])
    metadata["error_samples"] = (previous_samples + samples)[:_MAX_ERROR_SAMPLES]
    metadata["last_batch_at"] = _utcnow().isoformat()
    metadata["batch_size"] = batch_size
    checkpoint.metadata_json = metadata

    has_more = db.scalar(
        select(func.count()).select_from(Lead).where(
            phone_fingerprint_missing_condition(),
            Lead.id > last_cursor,
        )
    ) or 0
    complete = has_more == 0
    if complete:
        checkpoint.status = "COMPLETED_WITH_ERRORS" if checkpoint.error_count else "COMPLETED"
        metadata = dict(checkpoint.metadata_json or {})
        metadata["completed_at"] = _utcnow().isoformat()
        checkpoint.metadata_json = metadata
    db.flush()
    return FingerprintBatchResult(
        scanned=len(rows),
        updated=updated,
        errors=errors,
        last_cursor=last_cursor,
        complete=complete,
        checkpoint_status=checkpoint.status,
        error_samples=tuple(samples),
    )


def preview_phone_fingerprint_backfill(
    db: Session,
    *,
    batch_size: int = 500,
    max_batches: int = 20,
    secret: str | None = None,
) -> FingerprintPreviewResult:
    """Read-only scan of rows requiring T30 backfill without row locks."""

    resolved_secret = _resolve_secret(secret)
    if max_batches < 1 or max_batches > 10000:
        raise ValueError("max_batches 必须在 1 到 10000 之间")
    cursor: str | None = None
    scanned = eligible = errors = 0
    samples: list[dict[str, str]] = []
    truncated = False
    for _ in range(max_batches):
        rows = _select_missing_batch(
            db,
            cursor=cursor,
            batch_size=batch_size,
            lock_rows=False,
        )
        if not rows:
            break
        for lead in rows:
            scanned += 1
            try:
                normalized = _phone_for_fingerprint(lead)
                fingerprint_phone(normalized, secret=resolved_secret)
                eligible += 1
            except ValueError as exc:
                errors += 1
                if len(samples) < _MAX_ERROR_SAMPLES:
                    samples.append({"lead_id": lead.id, "reason": str(exc)})
        cursor = rows[-1].id
        if len(rows) < batch_size:
            break
    else:
        truncated = bool(
            db.scalar(
                select(func.count()).select_from(Lead).where(
                    phone_fingerprint_missing_condition(),
                    Lead.id > (cursor or ""),
                )
            )
        )
    return FingerprintPreviewResult(
        scanned=scanned,
        eligible=eligible,
        errors=errors,
        truncated=truncated,
        last_cursor=cursor,
        error_samples=tuple(samples),
    )
