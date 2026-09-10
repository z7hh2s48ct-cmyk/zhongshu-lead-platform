from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from ..core.auth import require_permissions
from ..core.database import get_db
from ..core.responses import ok
from ..schemas.auth import PasswordResetBody
from ..services.audit import write_audit
from ..services.internal_user_management import (
    create_managed_internal_user,
    delete_test_internal_user,
    generate_initial_password,
    list_internal_users,
    mark_internal_user_as_test,
    reset_internal_password,
    set_internal_user_status,
    update_internal_display_name,
    update_internal_roles,
)

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)


class UserCreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str | None = Field(default=None, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)
    role_codes: list[str] | None = Field(default=None, min_length=1, max_length=1)
    role_code: str | None = None
    company_id: str | None = None
    is_test: bool = False

    @model_validator(mode="after")
    def resolve_role_contract(self) -> "UserCreateBody":
        if self.role_codes is None and self.role_code is None:
            raise ValueError("role_codes is required")
        if self.role_codes is not None and self.role_code is not None:
            raise ValueError("role_codes and role_code cannot both be provided")
        return self

    def resolved_role_codes(self) -> list[str]:
        return self.role_codes or [self.role_code or ""]


class UserRolesBody(BaseModel):
    role_codes: list[str] = Field(min_length=1, max_length=1)


class UserDisplayNameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=64)

    @field_validator("display_name")
    @classmethod
    def reject_blank_or_padded_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("display_name must be non-empty without surrounding spaces")
        return value


class InternalUserDestructiveBody(BaseModel):
    confirm_username: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=2, max_length=500)


def _serialize_user(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "status": user.status,
        "is_test": user.is_test,
        "company_id": user.company_id,
        "roles": sorted(role.code for role in user.roles),
        "session_version": user.session_version,
    }


@router.get("")
def list_users(
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    return ok(request, [_serialize_user(user) for user in list_internal_users(db)])


@router.post("")
def create_user(
    body: UserCreateBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    initial_password = body.password or generate_initial_password(body.username)
    user = create_managed_internal_user(
        db,
        username=body.username,
        password=initial_password,
        display_name=body.display_name,
        role_codes=body.resolved_role_codes(),
        company_id=body.company_id,
        is_test=body.is_test,
    )
    write_audit(
        db,
        principal=principal,
        action="USER_CREATE",
        resource_type="user",
        resource_id=user.id,
        after={
            "username": user.username,
            "display_name": user.display_name,
            "roles": sorted(role.code for role in user.roles),
            "status": user.status,
            "is_test": user.is_test,
        },
        request_id=request.state.request_id,
    )
    db.commit()
    payload = _serialize_user(user)
    if body.password is None:
        payload["initial_password"] = initial_password
    return ok(request, payload)


@router.put("/{user_id}/roles")
def update_roles(
    user_id: str,
    body: UserRolesBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    user, previous_roles, changed = update_internal_roles(
        db,
        user_id=user_id,
        role_codes=body.role_codes,
    )
    if changed:
        write_audit(
            db,
            principal=principal,
            action="USER_ROLES_UPDATE",
            resource_type="user",
            resource_id=user.id,
            before={"roles": previous_roles, "session_version": user.session_version - 1},
            after={
                "roles": sorted(role.code for role in user.roles),
                "session_version": user.session_version,
            },
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(request, _serialize_user(user))


@router.patch("/{user_id}")
def update_display_name(
    user_id: str,
    body: UserDisplayNameBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    user, previous_display_name, changed = update_internal_display_name(
        db,
        user_id=user_id,
        display_name=body.display_name,
    )
    if changed:
        write_audit(
            db,
            principal=principal,
            action="USER_DISPLAY_NAME_UPDATE",
            resource_type="user",
            resource_id=user.id,
            before={"display_name": previous_display_name},
            after={"display_name": user.display_name},
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(request, _serialize_user(user))


@router.post("/{user_id}/mark-test")
def mark_test_user(
    user_id: str,
    body: InternalUserDestructiveBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    user, counts, changed = mark_internal_user_as_test(
        db,
        user_id=user_id,
        confirm_username=body.confirm_username,
    )
    if changed:
        write_audit(
            db,
            principal=principal,
            action="USER_TEST_MARK",
            resource_type="user",
            resource_id=user.id,
            before={"is_test": False},
            after={"is_test": True},
            metadata={"business_reference_counts": counts},
            reason=body.reason,
            request_id=request.state.request_id,
        )
        db.commit()
        logger.warning(
            "internal user marked as test user_id=%s actor_user_id=%s",
            user.id,
            principal.user_id,
        )
    return ok(request, _serialize_user(user))


@router.delete("/{user_id}")
def delete_internal_user(
    user_id: str,
    body: InternalUserDestructiveBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    snapshot = delete_test_internal_user(
        db,
        user_id=user_id,
        confirm_username=body.confirm_username,
    )
    write_audit(
        db,
        principal=principal,
        action="USER_TEST_DELETE",
        resource_type="user",
        resource_id=user_id,
        before=snapshot,
        after={"deleted": True},
        reason=body.reason,
        request_id=request.state.request_id,
    )
    db.commit()
    logger.warning(
        "internal test user deleted user_id=%s actor_user_id=%s",
        user_id,
        principal.user_id,
    )
    return ok(request, {"id": user_id, "deleted": True})


def _change_status(
    *,
    user_id: str,
    status: str,
    request: Request,
    principal,
    db: Session,
):
    user, previous_status, changed = set_internal_user_status(
        db,
        user_id=user_id,
        status=status,
    )
    if changed:
        action = "USER_ENABLE" if status == "ACTIVE" else "USER_DISABLE"
        write_audit(
            db,
            principal=principal,
            action=action,
            resource_type="user",
            resource_id=user.id,
            before={"status": previous_status, "session_version": user.session_version - 1},
            after={"status": user.status, "session_version": user.session_version},
            request_id=request.state.request_id,
        )
        db.commit()
    return ok(request, _serialize_user(user))


@router.post("/{user_id}/disable")
def disable_user(
    user_id: str,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    return _change_status(
        user_id=user_id,
        status="DISABLED",
        request=request,
        principal=principal,
        db=db,
    )


@router.post("/{user_id}/enable")
def enable_user(
    user_id: str,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    return _change_status(
        user_id=user_id,
        status="ACTIVE",
        request=request,
        principal=principal,
        db=db,
    )


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: str,
    body: PasswordResetBody,
    request: Request,
    principal=Depends(require_permissions("*")),
    db: Session = Depends(get_db),
):
    user, previous_session_version = reset_internal_password(
        db,
        user_id=user_id,
        new_password=body.new_password,
    )
    write_audit(
        db,
        principal=principal,
        action="USER_PASSWORD_RESET",
        resource_type="user",
        resource_id=user.id,
        before={"session_version": previous_session_version},
        after={"session_version": user.session_version},
        request_id=request.state.request_id,
    )
    db.commit()
    return ok(request, _serialize_user(user))
