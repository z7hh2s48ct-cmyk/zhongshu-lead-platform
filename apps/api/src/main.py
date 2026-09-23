from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import urlencode

from anyio.to_thread import current_default_thread_limiter
from fastapi import Cookie, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from jwt import InvalidTokenError
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core.auth import load_current_principal
from .core.config import get_settings
from .core.database import SessionLocal, get_db, init_database
from .core.errors import register_error_handlers
from .core.in_flight import InFlightLimitMiddleware
from .core.legacy_guard import LegacyWriteGuardMiddleware
from .core.logging import configure_logging
from .core.production import validate_production_settings
from .core.request_context import RequestContextMiddleware
from .core.security import decode_access_token
from .routers import (
    admin,
    admin_meta,
    auth,
    claim,
    company_accounts,
    companies,
    dispatch,
    followups,
    invite_preview,
    leads,
    lead_points_v12,
    master_data,
    notifications,
    points,
    supply_termination,
    returns,
    users,
    v12_admin,
    v12_dispatch,
    v12_franchise_export,
    v12_insights,
    v12_lead_supply,
    v12_pre_dispatch,
    v12_public_pool,
    v12_returns,
    v12_rewards,
    v12_source_channel,
    v12_supplier_review,
    supply_withdrawal as supply_withdrawal_router,
    verification,
)
from .services.rbac import require_rbac_sync_complete, seed_rbac
from .services.storage import get_storage

settings = get_settings()
configure_logging()
ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def lifespan(_: FastAPI):
    current_default_thread_limiter().total_tokens = settings.sync_threadpool_tokens
    production = settings.app_env.lower() == "production"
    if production:
        validation = validate_production_settings(settings)
        if not validation.valid:
            raise RuntimeError("生产环境配置校验失败：" + "；".join(validation.errors))
    init_database()
    with SessionLocal() as db:
        if production:
            require_rbac_sync_complete(db, source="app_startup")
        else:
            seed_rbac(db, source="app_startup")
            db.commit()
    yield


app = FastAPI(
    title="合家美宅客资审核、派发与积分管理平台 API",
    version=settings.app_version,
    lifespan=lifespan,
)
app.add_middleware(LegacyWriteGuardMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    InFlightLimitMiddleware,
    limit=settings.max_in_flight_requests,
    queue_timeout_seconds=settings.in_flight_queue_timeout_seconds,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_error_handlers(app)

api_prefix = "/api/v1"
app.include_router(admin.router, prefix=api_prefix)
app.include_router(admin_meta.router, prefix=api_prefix)
app.include_router(auth.router, prefix=api_prefix)
app.include_router(invite_preview.router, prefix=api_prefix)
app.include_router(users.router, prefix=api_prefix)
app.include_router(companies.router, prefix=api_prefix)
app.include_router(company_accounts.router, prefix=api_prefix)
app.include_router(company_accounts.directory_router, prefix=api_prefix)
app.include_router(company_accounts.request_router, prefix=api_prefix)
app.include_router(leads.router, prefix=api_prefix)
app.include_router(lead_points_v12.router, prefix=api_prefix)
app.include_router(verification.router, prefix=api_prefix)
app.include_router(points.router, prefix=api_prefix)
app.include_router(supply_termination.router, prefix=api_prefix)
app.include_router(dispatch.router, prefix=api_prefix)
app.include_router(claim.router, prefix=api_prefix)
app.include_router(followups.router, prefix=api_prefix)
app.include_router(returns.router, prefix=api_prefix)
app.include_router(notifications.router, prefix=api_prefix)
app.include_router(master_data.router, prefix=api_prefix)
app.include_router(v12_admin.router, prefix=api_prefix)
app.include_router(v12_pre_dispatch.router, prefix=api_prefix)
app.include_router(v12_lead_supply.router, prefix=api_prefix)
app.include_router(v12_public_pool.router, prefix=api_prefix)
app.include_router(v12_supplier_review.router, prefix=api_prefix)
app.include_router(v12_dispatch.router, prefix=api_prefix)
app.include_router(v12_returns.router, prefix=api_prefix)
app.include_router(v12_rewards.router, prefix=api_prefix)
app.include_router(v12_franchise_export.router, prefix=api_prefix)
app.include_router(supply_withdrawal_router.router, prefix=api_prefix)
app.include_router(v12_source_channel.router, prefix=api_prefix)
app.include_router(v12_insights.router, prefix=api_prefix)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "zhongshu-lead-platform", "version": settings.app_version, "environment": settings.app_env}


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive", "version": settings.app_version}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    storage = "s3-ready" if settings.object_storage_backend.lower() == "s3" else "local-mounted"
    if settings.object_storage_backend.lower() == "s3":
        get_storage().check_readiness()
    else:
        storage_root = Path(settings.object_storage_dir)
        storage_root.mkdir(parents=True, exist_ok=True)
        if not storage_root.is_dir() or not os.access(storage_root, os.W_OK):
            raise RuntimeError("对象存储目录不可写")
    return {"status": "ready", "database": "ok", "storage": storage, "version": settings.app_version}


def _web_entry_role(db: Session, access_token: str | None) -> str | None:
    """Return the single active business role carried by a web session."""

    if not access_token:
        return None
    try:
        payload = decode_access_token(access_token)
    except InvalidTokenError:
        return None
    principal = load_current_principal(db, payload.get("sub"), payload.get("sv"))
    return next(iter(principal.role_codes)) if principal else None


def _role_entry_target(role_code: str | None, *, surface: str) -> str:
    """Keep every signed-in user on the one workbench allowed by their role."""

    if role_code == "TELESALES":
        return "/h5/call/"
    if role_code in {"FRANCHISE_OWNER", "FRANCHISE_EMPLOYEE"}:
        return "/h5/v12-workbench.html"
    if role_code in {"SUPER_ADMIN", "OPERATION"}:
        return "/h5/admin/" if surface == "mobile" else "/admin/v12-operations.html"
    return "/h5/v12-workbench.html" if surface == "mobile" else "/admin/v12-operations.html"


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_entry(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse(
        url=_role_entry_target(_web_entry_role(db, access_token), surface="desktop"),
        status_code=302,
    )


@app.get("/h5", include_in_schema=False)
@app.get("/h5/", include_in_schema=False)
def h5_entry(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse(
        url=_role_entry_target(_web_entry_role(db, access_token), surface="mobile"),
        status_code=302,
    )


@app.get("/h5/call", include_in_schema=False)
@app.get("/h5/call/", include_in_schema=False)
def h5_call_entry(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    role_code = _web_entry_role(db, access_token)
    if role_code in {None, "TELESALES"}:
        # 未登录时由同一新电销页显示登录表单，避免跳入旧版后台登录壳。
        return RedirectResponse(url="/h5/call/index.html", status_code=302)
    return RedirectResponse(url=_role_entry_target(role_code, surface="mobile"), status_code=302)


@app.get("/call", include_in_schema=False)
@app.get("/call/", include_in_schema=False)
@app.get("/call/{legacy_path:path}", include_in_schema=False)
def legacy_call_entry(legacy_path: str = "") -> RedirectResponse:
    """Keep historical bookmarks working without retaining a second call shell."""

    if legacy_path:
        return RedirectResponse(url="/h5/call/index.html#/invalid-link", status_code=302)
    return RedirectResponse(url="/h5/call/", status_code=302)


@app.get("/admin/legacy", include_in_schema=False)
def admin_legacy_entry() -> RedirectResponse:
    return RedirectResponse(url="/admin/", status_code=302)


@app.get("/h5/legacy", include_in_schema=False)
def h5_legacy_entry() -> RedirectResponse:
    return RedirectResponse(url="/h5/", status_code=302)


@app.get("/admin/index.html", include_in_schema=False)
def admin_index_legacy_entry() -> RedirectResponse:
    """Retire the old hash admin shell without breaking saved bookmarks."""

    return RedirectResponse(url="/admin/", status_code=302)


@app.get("/h5/index.html", include_in_schema=False)
def h5_index_legacy_entry() -> RedirectResponse:
    """Retire the old franchise shell without exposing two active products."""

    return RedirectResponse(url="/h5/", status_code=302)


@app.get("/h5/supplier.html", include_in_schema=False)
def h5_supplier_legacy_entry() -> RedirectResponse:
    """Keep supplier bookmarks on the unified franchise workbench."""

    return RedirectResponse(url="/h5/v12-workbench.html?view=leads&id=supply", status_code=302)


@app.get("/admin/v12-leads.html", include_in_schema=False)
def admin_lead_legacy_entry(request: Request) -> RedirectResponse:
    """Retire the standalone lead page in favour of the platform workbench."""

    query = {"view": "leads"}
    for key in ("id", "status", "source"):
        value = request.query_params.get(key)
        if value:
            query[key] = value
    return RedirectResponse(url=f"/admin/v12-operations.html?{urlencode(query)}", status_code=302)


for route, directory, name in [
    ("/h5/call", ROOT / "apps" / "call-h5" / "public", "h5-call"),
    ("/h5/admin", ROOT / "apps" / "admin" / "public" / "h5", "h5-admin"),
    ("/h5", ROOT / "apps" / "h5" / "public", "h5"),
    ("/admin", ROOT / "apps" / "admin" / "public", "admin"),
]:
    directory.mkdir(parents=True, exist_ok=True)
    app.mount(route, StaticFiles(directory=directory, html=True), name=name)
