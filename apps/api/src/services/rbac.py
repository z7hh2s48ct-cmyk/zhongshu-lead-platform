from __future__ import annotations

from dataclasses import dataclass
import logging

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from ..core.models import Permission, Role, RolePermission, User, UserRole
from ..core.role_contract import ACTIVE_BUSINESS_ROLE_CODES, LEGACY_ROLE_CODES


logger = logging.getLogger("zhongshu.rbac")
_RBAC_SYNC_LOCK_KEY = "zhongshu.rbac.fixed-role-matrix"

ROLE_PERMISSION_MATRIX: dict[str, tuple[str, list[str]]] = {
    "SUPER_ADMIN": ("超级管理员", ["*"]),
    "OPERATION": (
        "运营管理员",
        [
            "lead.read",
            "lead.edit",
            "lead.manual.manage",
            "lead.supplier.review",
            "lead.dedup.override",
            "lead.dispatch",
            "lead.dispatch.phone.read",
            "lead.own.delete",
            "lead.phone.export",
            "assignment.read",
            "assignment.release",
            "company.read_eligibility",
            "company.profile.review",
            "company.account.manage",
            "verification.read",
            "return.read",
            "return.review",
            "return.evidence.read",
            "reward.read",
            "dashboard.operation.read",
            "audit.read",
            "report.v12.read",
            "calendar.read",
        ],
    ),
    "TELESALES": (
        "电销人员",
        [
            "verification.task.read",
            "verification.task.start",
            "verification.submit",
            "lead.phone.read",
            "lead.phone.dial",
            "dashboard.telesales.read",
        ],
    ),
    "FRANCHISE_OWNER": (
        "加盟商负责人",
        [
            "h5.home",
            "assignment.own.read",
            "assignment.own.claim",
            "lead.own.phone.read",
            "supplier.lead.manage",
            "supplier.reward.own.read",
            "company.profile.manage",
            "followup.own.manage",
            "return.own.manage",
            "points.own.read",
            "notification.own.read",
        ],
    ),
    "FRANCHISE_EMPLOYEE": (
        "加盟商员工",
        [
            "h5.home",
            "assignment.employee.read",
            "assignment.employee.claim",
            "supplier.lead.manage",
            "followup.own.manage",
            "return.own.manage",
            "notification.own.read",
        ],
    ),
}

SENSITIVE_PERMISSION_CODES = frozenset(
    {
        "*",
        "lead.phone.read",
        "lead.dispatch.phone.read",
        "lead.phone.export",
        "points.recharge",
        "points.reverse",
        "lead.dedup.override",
        "reward.manage",
        "reward.reverse",
        "audit.read",
        "calendar.manage",
        "calendar.import",
    }
)


@dataclass(frozen=True)
class RolePermissionDiff:
    role_code: str
    role_created: bool
    added: tuple[str, ...]
    removed: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "role_code": self.role_code,
            "role_created": self.role_created,
            "added": list(self.added),
            "removed": list(self.removed),
        }


@dataclass(frozen=True)
class RbacSyncResult:
    roles: tuple[RolePermissionDiff, ...]

    @property
    def changed(self) -> bool:
        return bool(self.roles)

    @property
    def added_count(self) -> int:
        return sum(len(role.added) for role in self.roles)

    @property
    def removed_count(self) -> int:
        return sum(len(role.removed) for role in self.roles)

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "added_count": self.added_count,
            "removed_count": self.removed_count,
            "roles": [role.to_dict() for role in self.roles],
        }


class RbacSyncRequiredError(RuntimeError):
    def __init__(self, result: RbacSyncResult) -> None:
        super().__init__(
            "固定 RBAC 权限矩阵尚未同步，"
            "请先执行 scripts/sync_rbac.py 预览并显式应用"
        )
        self.result = result


def _fixed_role_permission_codes(db: Session) -> tuple[set[str], dict[str, set[str]]]:
    fixed_role_codes = set(ROLE_PERMISSION_MATRIX) | set(LEGACY_ROLE_CODES)
    existing_roles = set(
        db.scalars(select(Role.code).where(Role.code.in_(fixed_role_codes))).all()
    )
    permissions_by_role = {role_code: set() for role_code in existing_roles}
    rows = db.execute(
        select(Role.code, Permission.code)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Role.code.in_(fixed_role_codes))
    ).all()
    for role_code, permission_code in rows:
        permissions_by_role[role_code].add(permission_code)
    return existing_roles, permissions_by_role


def preview_rbac_sync(db: Session) -> RbacSyncResult:
    """Return the fixed-role permission diff without changing the database."""

    existing_roles, permissions_by_role = _fixed_role_permission_codes(db)
    diffs: list[RolePermissionDiff] = []
    for role_code, (_, expected_codes) in ROLE_PERMISSION_MATRIX.items():
        expected = set(expected_codes)
        existing = permissions_by_role.get(role_code, set())
        role_created = role_code not in existing_roles
        added = tuple(sorted(expected - existing))
        removed = tuple(sorted(existing - expected))
        if role_created or added or removed:
            diffs.append(
                RolePermissionDiff(
                    role_code=role_code,
                    role_created=role_created,
                    added=added,
                    removed=removed,
                )
            )
    for role_code in sorted(LEGACY_ROLE_CODES.intersection(existing_roles)):
        existing = permissions_by_role.get(role_code, set())
        if existing:
            diffs.append(
                RolePermissionDiff(
                    role_code=role_code,
                    role_created=False,
                    added=(),
                    removed=tuple(sorted(existing)),
                )
            )
    return RbacSyncResult(roles=tuple(diffs))


def require_rbac_sync_complete(
    db: Session,
    *,
    source: str = "production_startup",
) -> RbacSyncResult:
    """Fail closed when production starts before explicit RBAC synchronization."""

    result = preview_rbac_sync(db)
    if result.changed:
        logger.error(
            "fixed RBAC matrix requires explicit synchronization: added=%s removed=%s",
            result.added_count,
            result.removed_count,
            extra={"source": source, "rbac_sync": result.to_dict()},
        )
        raise RbacSyncRequiredError(result)
    return result


def _acquire_rbac_sync_lock(db: Session) -> None:
    # Multiple production instances may start together. Serialize the exact
    # matrix replacement so one transaction cannot race another.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": _RBAC_SYNC_LOCK_KEY},
        )


def seed_rbac(db: Session, *, source: str = "seed_rbac") -> RbacSyncResult:
    """Make every fixed role match the code-defined permission set exactly."""

    _acquire_rbac_sync_lock(db)
    result = preview_rbac_sync(db)
    all_codes = sorted(
        {code for _, codes in ROLE_PERMISSION_MATRIX.values() for code in codes}
    )
    permission_cache = {
        permission.code: permission
        for permission in db.scalars(
            select(Permission).where(Permission.code.in_(all_codes))
        ).all()
    }
    for code in all_codes:
        if code in permission_cache:
            continue
        module = code.split(".", 1)[0] if code != "*" else "system"
        permission = Permission(
            code=code,
            name=code,
            module=module,
            sensitive=code in SENSITIVE_PERMISSION_CODES,
        )
        db.add(permission)
        permission_cache[code] = permission
    db.flush()

    role_cache = {
        role.code: role
        for role in db.scalars(
            select(Role)
            .options(selectinload(Role.permissions))
            .where(Role.code.in_(set(ROLE_PERMISSION_MATRIX)))
        ).all()
    }
    for role_code, (role_name, codes) in ROLE_PERMISSION_MATRIX.items():
        role = role_cache.get(role_code)
        if role is None:
            role = Role(code=role_code, name=role_name, description=role_name)
            db.add(role)
            role_cache[role_code] = role
        role.permissions = [permission_cache[code] for code in codes]
    legacy_roles = db.scalars(
        select(Role)
        .options(selectinload(Role.permissions))
        .where(Role.code.in_(LEGACY_ROLE_CODES))
    ).all()
    for role in legacy_roles:
        role.permissions = []
    db.flush()

    if result.changed:
        log = logger.warning if result.removed_count else logger.info
        log(
            "fixed RBAC matrix synchronized: added=%s removed=%s",
            result.added_count,
            result.removed_count,
            extra={"source": source, "rbac_sync": result.to_dict()},
        )
    return result


def assign_role(db: Session, user: User, role_code: str) -> None:
    if role_code not in ACTIVE_BUSINESS_ROLE_CODES:
        raise ValueError(f"role is not an active business role: {role_code}")
    role = db.scalar(select(Role).where(Role.code == role_code))
    if not role:
        raise ValueError(f"role not seeded: {role_code}")
    existing_role_ids = set(
        db.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id)).all()
    )
    if existing_role_ids and role.id not in existing_role_ids:
        raise ValueError("a user can only have one business role")
    exists = role.id in existing_role_ids
    if not exists:
        db.add(UserRole(user_id=user.id, role_id=role.id))
