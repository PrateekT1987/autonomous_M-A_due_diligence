"""FastAPI application and JSON API for Dossier.

Production behaviour is configuration-driven: request IDs, structured access
logs, security headers, optional bearer/API-key access control, per-identity
rate limits, safe error responses and an append-only audit trail. None of it is
required for the local demo, which continues to run unauthenticated.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, export, obs, sso
from .llm import LLM
from .obs import METRICS, RateLimiter
from .pipeline import orchestrator

APP_VERSION = "1.0.0"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STARTED_AT = time.time()

obs.configure_logging(config.LOG_LEVEL, config.LOG_FORMAT)
log = logging.getLogger("dossier.api")
limiter = RateLimiter(config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_BURST)

app = FastAPI(
    title=config.APP_NAME,
    version=APP_VERSION,
    docs_url=None if config.IS_PRODUCTION else "/api/docs",
    redoc_url=None,
    openapi_url=None if config.IS_PRODUCTION else "/openapi.json",
)

PUBLIC_API = {"/api/health", "/api/ready", "/api/me"}
PUBLIC_PATHS = PUBLIC_API | {"/auth/login", "/auth/callback", "/auth/logout"}


# ---------------------------------------------------------------- middleware
def _auth_required() -> bool:
    return auth.auth_enabled()


def _protected(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    return path.startswith("/api/") or path in {"/openapi.json", "/api/docs", "/docs"}


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or ""
    if not origin:
        return False
    try:
        return urlparse(origin).netloc == (request.headers.get("host") or "")
    except ValueError:
        return False


def _csrf_ok(request: Request, who: auth.Principal) -> bool:
    """Session cookies need a CSRF token or a same-origin Origin header."""
    if who.method != "session":
        return True
    supplied = request.headers.get("x-csrf-token") or ""
    if who.csrf and hmac.compare_digest(supplied, who.csrf):
        return True
    return _same_origin(request)


@app.middleware("http")
async def _middleware(request: Request, call_next):
    started = time.monotonic()
    request_id = (request.headers.get(config.REQUEST_ID_HEADER) or uuid.uuid4().hex[:16])[:64]
    trace_id = request_id
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    obs.set_trace_id(trace_id)
    who = auth.resolve(request)
    auth.set_principal(who)
    actor = who.label if who else None
    request.state.actor = actor
    client_ip = request.client.host if request.client else None
    path = request.url.path

    def secure(response: Response) -> Response:
        response.headers[config.REQUEST_ID_HEADER] = request_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; font-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if config.IS_PRODUCTION:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > config.MAX_BODY_BYTES:
                METRICS.inc("dossier_body_rejected_total")
                return secure(
                    JSONResponse(
                        status_code=413,
                        content={"error": "Request body too large", "request_id": request_id},
                    )
                )
        except ValueError:
            return secure(
                JSONResponse(status_code=400, content={"error": "Invalid Content-Length", "request_id": request_id})
            )

    if _protected(path) and _auth_required() and not who:
        METRICS.inc("dossier_auth_failures_total")
        response = JSONResponse(
            status_code=401,
            content={"error": "Authentication required", "request_id": request_id},
            headers={"WWW-Authenticate": "Bearer"},
        )
        return secure(response)

    unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
    if unsafe and path.startswith("/api/") and who and not _csrf_ok(request, who):
        METRICS.inc("dossier_csrf_failures_total")
        return secure(
            JSONResponse(
                status_code=403,
                content={"error": "CSRF validation failed", "request_id": request_id},
            )
        )
    if unsafe and path == "/auth/logout" and who and not _csrf_ok(request, who):
        return secure(JSONResponse(status_code=403, content={"error": "CSRF validation failed"}))

    if path.startswith("/api/") and path not in PUBLIC_API:
        allowed, retry_after = limiter.allow((who.subject if who else "") or client_ip or "anonymous")
        if not allowed:
            METRICS.inc("dossier_rate_limited_total")
            response = JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded", "request_id": request_id},
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
            return secure(response)

    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        log.exception(
            "Unhandled request failure",
            extra={"request_id": request_id, "actor": actor, "method": request.method, "path": path},
        )
        METRICS.inc("dossier_http_errors_total", {"path": path})
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except Exception:  # noqa: BLE001
            pass
        obs.alert(
            "http_500",
            request_id=request_id,
            method=request.method,
            path=path,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        response = JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "request_id": request_id},
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    route = request.scope.get("route")
    label = getattr(route, "path", path)
    METRICS.inc("dossier_http_requests_total", {"method": request.method, "path": label, "status": response.status_code})
    METRICS.observe("dossier_http_request_seconds", duration_ms / 1000, {"path": label})
    log.info(
        "%s %s %s",
        request.method,
        path,
        response.status_code,
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": who.tenant_id if who else None,
            "actor": actor,
            "method": request.method,
            "path": label,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return secure(response)


# CORS is added last so preflight requests are answered before auth/rate limits.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "X-CSRF-Token",
            "X-Request-ID",
            "Idempotency-Key",
        ],
        expose_headers=[config.REQUEST_ID_HEADER, "X-Trace-ID"],
    )


@app.on_event("startup")
def _startup() -> None:
    warnings = config.validate()
    for warning in warnings:
        log.warning("configuration: %s", warning)
    for warning in obs.configure_observability(app):
        log.warning("observability: %s", warning)
    db.init_db()
    orchestrator.start_worker()
    log.info(
        "Dossier started",
        extra={"stage": "startup", "deal_id": None},
    )


@app.on_event("shutdown")
def _shutdown() -> None:
    orchestrator.stop_worker()


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:16])
    log.exception("Unhandled exception", extra={"request_id": request_id})
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass
    obs.alert("unhandled_exception", request_id=request_id, error=f"{type(exc).__name__}: {exc}"[:300])
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "request_id": request_id},
    )


# ------------------------------------------------------------------ helpers
def _actor(request: Request) -> str:
    return getattr(request.state, "actor", None) or "anonymous"


def _audit(request: Request, action: str, *, deal_id: str | None = None, run_id: str | None = None, detail: dict | None = None) -> None:
    who = auth.principal()
    db.audit(
        action,
        actor=_actor(request),
        tenant_id=who.tenant_id,
        deal_id=deal_id,
        run_id=run_id,
        request_id=getattr(request.state, "request_id", None),
        trace_id=getattr(request.state, "trace_id", None),
        ip=request.client.host if request.client else None,
        detail=detail,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, str):
        return value
    return value


def _deal(deal_id: str) -> dict:
    row = db.q1(
        "SELECT * FROM deals WHERE id = ? AND tenant_id = ?",
        (deal_id, auth.tenant_id()),
    )
    if not row:
        raise HTTPException(404, f"Deal {deal_id} not found")
    d = dict(row)
    d["config"] = db.loads(d.get("config"), {})
    d["summary"] = db.loads(d.get("summary"), {})
    if isinstance(d["summary"], str):
        d["summary"] = db.loads(d["summary"], {})
    return d


def _run(run_id: str) -> dict:
    row = db.q1(
        "SELECT r.* FROM runs r JOIN deals d ON d.id = r.deal_id "
        "WHERE r.id = ? AND d.tenant_id = ?",
        (run_id, auth.tenant_id()),
    )
    if not row:
        raise HTTPException(404, "Run not found")
    return dict(row)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "deal"


def _unique_deal_id(base: str) -> str:
    candidate = _slug(base)
    existing = {r["id"] for r in db.q("SELECT id FROM deals")}
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


def _deal_overview(d: dict) -> dict:
    deal_id = d["id"]
    counts = {
        "sources": db.q1("SELECT COUNT(*) c FROM sources WHERE deal_id = ?", (deal_id,))["c"],
        "evidence": db.q1("SELECT COUNT(*) c FROM evidence WHERE deal_id = ?", (deal_id,))["c"],
        "risks": db.q1("SELECT COUNT(*) c FROM risks WHERE deal_id = ?", (deal_id,))["c"],
        "critical_risks": db.q1(
            "SELECT COUNT(*) c FROM risks WHERE deal_id = ? AND score >= 16", (deal_id,)
        )["c"],
        "chunks": db.q1("SELECT COUNT(*) c FROM chunks WHERE deal_id = ?", (deal_id,))["c"],
        "documents": db.q1("SELECT COUNT(*) c FROM documents WHERE deal_id = ?", (deal_id,))["c"],
    }
    doc_row = db.q1(
        "SELECT trust, updated_at, sections FROM documents WHERE deal_id = ? ORDER BY updated_at DESC LIMIT 1",
        (deal_id,),
    )
    trust = db.loads(doc_row["trust"], {}) if doc_row else {}
    if isinstance(trust, str):
        trust = db.loads(trust, {})
    stats = {}
    if doc_row:
        doc = db.loads(doc_row["sections"], {})
        if isinstance(doc, str):
            doc = db.loads(doc, {})
        stats = doc.get("stats") or {}
    run = orchestrator.latest_run(deal_id)
    if run:
        run["stats"] = db.loads(run.get("stats"), {})
    base = {
        "id": deal_id,
        "name": d.get("name"),
        "company": d.get("company") or d.get("name"),
        "ticker": d.get("ticker"),
        "cik": d.get("cik"),
        "sector": d.get("sector"),
        "client": d.get("client"),
        "deal_type": d.get("deal_type"),
        "status": d.get("status"),
        "archived_at": d.get("archived_at"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "counts": counts,
        "trust": trust,
        "stats": stats,
        "latest_run": run,
        "running": orchestrator.is_running(deal_id),
    }
    return base


def _idempotent(request: Request, scope: str) -> dict | None:
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None
    scoped = f"{auth.tenant_id()}:{scope}"
    row = db.q1("SELECT response FROM idempotency_keys WHERE key = ? AND scope = ?", (key, scoped))
    if not row:
        return None
    try:
        return json.loads(row["response"])
    except ValueError:
        return None


def _remember(request: Request, scope: str, deal_id: str | None, payload: dict) -> None:
    key = request.headers.get("Idempotency-Key")
    if not key:
        return
    scoped = f"{auth.tenant_id()}:{scope}"
    try:
        db.insert(
            "idempotency_keys",
            {
                "key": key,
                "scope": scoped,
                "deal_id": deal_id,
                "response": db.dumps(payload),
                "created_at": db.now(),
            },
        )
    except Exception:  # noqa: BLE001 - duplicate keys return the first response
        pass


def _page(limit: int, offset: int, maximum: int = 500) -> tuple[int, int]:
    return max(1, min(int(limit or 1), maximum)), max(0, int(offset or 0))


# ------------------------------------------------------------------- identity
def _tenant_slug(value: str | None) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-")[:64]
    return slug or config.OIDC_DEFAULT_TENANT


@app.get("/api/me")
def me():
    who = auth.principal()
    return {
        "authenticated": who.authenticated,
        "auth_required": auth.auth_enabled(),
        "sso_enabled": config.OIDC_ENABLED,
        "sso_label": config.OIDC_BUTTON_LABEL,
        "user": (
            {
                "subject": who.subject,
                "name": who.label,
                "email": who.email,
                "role": who.role,
                "tenant": who.tenant_id,
                "method": who.method,
            }
            if who.authenticated
            else None
        ),
        "role": who.role,
        "tenant": who.tenant_id,
        "csrf": who.csrf,
    }


@app.get("/auth/login")
def auth_login():
    if not config.OIDC_ENABLED:
        raise HTTPException(404, "SSO is not configured")
    try:
        return RedirectResponse(sso.begin_login(), status_code=302)
    except sso.SSOError as exc:
        log.warning("SSO login failed to start: %s", exc)
        return RedirectResponse("/?login_error=" + str(exc)[:120].replace(" ", "+"), status_code=302)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if not config.OIDC_ENABLED:
        raise HTTPException(404, "SSO is not configured")
    try:
        info = sso.finish_login(code, state)
    except sso.SSOError as exc:
        log.warning("SSO callback rejected: %s", exc)
        return RedirectResponse("/?login_error=" + str(exc)[:120].replace(" ", "+"), status_code=302)
    tenant = _tenant_slug(info.get("tenant"))
    role = (info.get("role") or config.OIDC_DEFAULT_ROLE).lower()
    if role not in auth.READ_ROLES:
        role = config.OIDC_DEFAULT_ROLE
    try:
        user = auth.upsert_user(
            email=info["email"],
            name=info.get("name"),
            tenant_id_value=tenant,
            role=role,
            provision=config.OIDC_AUTO_PROVISION,
        )
    except PermissionError:
        return RedirectResponse("/?login_error=Account+not+provisioned", status_code=302)
    sid, _csrf = auth.create_session(user["id"], user["tenant_id"])
    response = RedirectResponse(config.OIDC_POST_LOGIN, status_code=302)
    response.set_cookie(
        config.SESSION_COOKIE,
        sid,
        max_age=config.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=config.IS_PRODUCTION or request.url.scheme == "https",
        path="/",
    )
    db.audit("auth.login", actor=info["email"], tenant_id=tenant, ip=request.client.host if request.client else None)
    return response


@app.post("/auth/logout")
def auth_logout(request: Request):
    sid = request.cookies.get(config.SESSION_COOKIE) or ""
    auth.delete_session(sid)
    response = JSONResponse({"logged_out": True})
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return response


# ------------------------------------------------------------------- health
@app.get("/api/health")
def health():
    llm = LLM()
    available = llm.available()
    queue = orchestrator.queue_stats()
    return {
        "app": config.APP_NAME,
        "version": APP_VERSION,
        "tagline": config.APP_TAGLINE,
        "env": config.APP_ENV,
        "llm": {
            "available": available,
            "provider": llm.provider,
            "model": llm.model if available else None,
            "local": not llm.remote,
            "polish": config.resolve_polish(),
        },
        "database": "postgresql" if db.IS_POSTGRES else "sqlite",
        "queue": queue,
        "auth_required": _auth_required(),
        "sso_enabled": config.OIDC_ENABLED,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "time": db.now(),
    }


@app.get("/api/ready")
def ready():
    checks = {"database": False, "queue": False}
    try:
        db.q1("SELECT 1")
        checks["database"] = True
        stats = orchestrator.queue_stats()
        # A queued backlog is fine; readiness only needs a live database and a
        # configured worker mode.
        checks["queue"] = stats["mode"] in ("embedded", "worker", "test", "auto", "dev", "")
    except Exception:  # noqa: BLE001
        stats = {}
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ready": ok, "checks": checks, "queue": stats, "time": db.now()},
    )


@app.get("/api/metrics")
def metrics(request: Request):
    auth.require(request, "admin")
    queue = orchestrator.queue_stats()
    METRICS.gauge("dossier_queue_depth", queue.get("queued", 0) + queue.get("running", 0))
    METRICS.gauge(
        "dossier_deals_total",
        db.q1("SELECT COUNT(*) c FROM deals WHERE tenant_id = ?", (auth.tenant_id(),))["c"],
    )
    METRICS.gauge("dossier_jobs_failed", queue.get("failed", 0))
    return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4")


# -------------------------------------------------------------------- deals
@app.get("/api/deals")
def list_deals(include_archived: bool = False, limit: int = 100, offset: int = 0):
    limit, offset = _page(limit, offset)
    where = "WHERE tenant_id = ?" + ("" if include_archived else " AND archived_at IS NULL")
    params: list[Any] = [auth.tenant_id()]
    total = db.q1(f"SELECT COUNT(*) c FROM deals {where}", params)["c"]
    rows = db.q(
        f"SELECT * FROM deals {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {"deals": [_deal_overview(dict(r)) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.post("/api/deals", status_code=201)
def create_deal(request: Request, payload: dict = Body(...)):
    auth.require(request, "write")
    cached = _idempotent(request, "deals.create")
    if cached:
        return cached
    identifier = (payload.get("identifier") or payload.get("ticker") or payload.get("name") or "").strip()
    if not identifier:
        raise HTTPException(422, "A company name, ticker or CIK is required")
    if len(identifier) > 80 or re.search(r"https?://|[\x00-\x1f\x7f]", identifier):
        raise HTTPException(422, "Identifier must be a company name, ticker or CIK (not a URL)")
    name = (payload.get("name") or identifier).strip()[:120]
    deal_id = _unique_deal_id(name if name != identifier else identifier)
    now = db.now()
    cfg = {
        "identifier": identifier[:80],
        "sector_hint": (payload.get("sector") or "")[:80],
        "notes": (payload.get("notes") or "")[:2000],
    }
    db.insert(
        "deals",
        {
            "id": deal_id,
            "tenant_id": auth.tenant_id(),
            "name": name,
            "company": None,
            "ticker": None,
            "cik": None,
            "deal_type": payload.get("deal_type") or "sell_side",
            "sector": (payload.get("sector") or "")[:80],
            "client": (payload.get("client") or "")[:120],
            "status": "draft",
            "created_at": now,
            "updated_at": now,
            "config": db.dumps(cfg),
            "summary": "{}",
        },
    )
    deal = _deal(deal_id)
    run_id = None
    if payload.get("start", True):
        try:
            run_id = orchestrator.start_run(deal_id, polish=payload.get("polish"))
        except orchestrator.RunBusy:
            run_id = orchestrator.active_run(deal_id)
    result = {
        "deal": _deal_overview({**deal, "config": db.dumps(deal["config"]), "summary": db.dumps(deal["summary"])}),
        "run_id": run_id,
    }
    _audit(request, "deal.create", deal_id=deal_id, run_id=run_id, detail={"identifier": identifier[:80]})
    _remember(request, "deals.create", deal_id, result)
    return result


@app.get("/api/deals/{deal_id}")
def get_deal(deal_id: str):
    d = _deal(deal_id)
    out = _deal_overview({**d, "config": db.dumps(d["config"]), "summary": db.dumps(d["summary"])})
    out["config"] = d["config"]
    out["summary"] = d["summary"]
    return out


@app.delete("/api/deals/{deal_id}")
def delete_deal(deal_id: str, request: Request):
    auth.require(request, "admin")
    _deal(deal_id)
    if orchestrator.is_running(deal_id):
        raise HTTPException(409, "Cannot delete a deal while a run is in progress")
    # Emit audit events for all data before removal; these are immutable history.
    for action, detail in [
        ("document_versions.delete", {"deal_id": deal_id}),
        ("idempotency_keys.delete", {"deal_id": deal_id}),
        ("jobs.delete", {"deal_id": deal_id}),
        ("reviews.delete", {"deal_id": deal_id}),
        ("run_events.delete", {"deal_id": deal_id}),
        ("runs.delete", {"deal_id": deal_id}),
        ("chunks.delete", {"deal_id": deal_id}),
        ("documents.delete", {"deal_id": deal_id}),
        ("risks.delete", {"deal_id": deal_id}),
        ("evidence.delete", {"deal_id": deal_id}),
        ("sources.delete", {"deal_id": deal_id}),
    ]:
        _audit(request, action, deal_id=deal_id, detail=detail)
    # Soft delete: mark the deal as deleted and retain all audit data for SOC 2/retention.
    # The actual row deletion is handled by a separate purge job (not implemented here).
    db.x(
        "UPDATE deals SET status = 'deleted', updated_at = ? WHERE id = ? AND tenant_id = ?",
        (db.now(), deal_id, auth.tenant_id()),
    )
    _audit(request, "deal.soft_delete", deal_id=deal_id)
    return {"deleted": deal_id, "status": "soft_deleted", "retention_hold": True}


# --------------------------------------------------------------------- runs
@app.post("/api/deals/{deal_id}/run")
def start_run(deal_id: str, request: Request, payload: dict = Body(default={})):
    auth.require(request, "write")
    _deal(deal_id)
    cached = _idempotent(request, f"run.{deal_id}")
    if cached:
        return cached
    try:
        run_id = orchestrator.start_run(deal_id, polish=payload.get("polish"))
    except orchestrator.RunBusy:
        run_id = orchestrator.active_run(deal_id)
        raise HTTPException(409, f"A run is already in progress ({run_id})")
    result = {"run_id": run_id, "deal_id": deal_id}
    _audit(request, "run.start", deal_id=deal_id, run_id=run_id, detail={"polish": payload.get("polish")})
    _remember(request, f"run.{deal_id}", deal_id, result)
    return result


@app.get("/api/deals/{deal_id}/run")
def current_run(deal_id: str):
    _deal(deal_id)
    run = orchestrator.latest_run(deal_id)
    if not run:
        return {"run": None, "running": False}
    run["stats"] = db.loads(run.get("stats"), {})
    return {"run": run, "running": orchestrator.is_running(deal_id)}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = _run(run_id)
    run["stats"] = db.loads(run.get("stats"), {})
    return run


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request):
    auth.require(request, "write")
    _run(run_id)
    try:
        result = orchestrator.cancel_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, "Run not found") from exc
    run = orchestrator.run_status(run_id) or {}
    _audit(request, "run.cancel", deal_id=run.get("deal_id"), run_id=run_id, detail=result)
    return result


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0, limit: int = 500):
    _run(run_id)
    limit, _ = _page(limit, 0, 1000)
    rows = db.q(
        "SELECT id, ts, stage, level, message, data, trace_id FROM run_events "
        "WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after, limit),
    )
    return {
        "events": [
            {**dict(r), "data": db.loads(r["data"], {})} for r in rows
        ]
    }


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str, request: Request, after: int = 0):
    """Server-sent events: incremental run log, then a terminal `state` event."""
    run = _run(run_id)
    obs.set_trace_id(run.get("trace_id") or run_id)

    async def gen():
        last = after
        idle = 0.0
        yield f"event: open\ndata: {json.dumps({'run_id': run_id})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            rows = db.q(
                "SELECT id, ts, stage, level, message, data FROM run_events WHERE run_id = ? AND id > ? ORDER BY id LIMIT 200",
                (run_id, last),
            )
            for r in rows:
                last = r["id"]
                payload = {"id": r["id"], "ts": r["ts"], "stage": r["stage"], "level": r["level"], "message": r["message"], "data": db.loads(r["data"], {})}
                yield f"id: {last}\nevent: log\ndata: {json.dumps(payload)}\n\n"
            state = orchestrator.run_status(run_id)
            if not rows:
                idle += 0.5
            else:
                idle = 0
            if state and state["status"] in ("complete", "failed", "cancelled"):
                yield f"event: state\ndata: {json.dumps(state)}\n\n"
                break
            if idle > 15:
                yield f"event: heartbeat\ndata: {json.dumps({'ts': db.now()})}\n\n"
                idle = 0
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ sources
@app.get("/api/deals/{deal_id}/sources")
def list_sources(
    deal_id: str,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    _deal(deal_id)
    limit, offset = _page(limit, offset)
    where = "WHERE deal_id = ?"
    params: list[Any] = [deal_id]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if q:
        where += " AND (title LIKE ? OR content LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    # Enforce per-deal source limit
    if config.SOURCE_LIMIT_PER_DEAL:
        total = db.q1(f"SELECT COUNT(*) c FROM sources {where}", params)["c"]
        if total > config.SOURCE_LIMIT_PER_DEAL:
            raise HTTPException(
                400,
                f"Source limit exceeded: {total} sources (max {config.SOURCE_LIMIT_PER_DEAL})",
            )
    else:
        total = db.q1(f"SELECT COUNT(*) c FROM sources {where}", params)["c"]
    rows = db.q(
        f"SELECT id, kind, title, publisher, url, published_at, form, accession, locator, meta, fetched_at "
        f"FROM sources {where} ORDER BY fetched_at DESC, id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = db.loads(d.get("meta"), {})
        out.append(d)
    kinds = [r["kind"] for r in db.q("SELECT DISTINCT kind FROM sources WHERE deal_id = ?", (deal_id,))]
    return {"sources": out, "kinds": sorted(kinds), "count": len(out), "total": total, "limit": limit, "offset": offset}


@app.get("/api/deals/{deal_id}/sources/{source_id}")
def get_source(deal_id: str, source_id: str, excerpt: int = 4000):
    _deal(deal_id)
    row = db.q1("SELECT * FROM sources WHERE deal_id = ? AND id = ?", (deal_id, source_id))
    if not row:
        raise HTTPException(404, "Source not found")
    d = dict(row)
    d["meta"] = db.loads(d.get("meta"), {})
    content = d.pop("content", "") or ""
    # Filing cover pages are dense inline-XBRL data. Prefer the section-aware
    # chunks (Business, Risk Factors, MD&A) for a readable preview.
    chunks = db.q(
        "SELECT section, text FROM chunks WHERE deal_id = ? AND source_id = ? ORDER BY ordinal LIMIT 3",
        (deal_id, source_id),
    )
    if chunks:
        parts = []
        for c in chunks:
            label = c["section"] or "Filing"
            parts.append(f"[{label}]\n{c['text']}")
        preview = "\n\n".join(parts)
    else:
        preview = content
    d["excerpt"] = preview[: max(0, min(excerpt, 40000))]
    d["length"] = len(content)
    d["evidence"] = [
        {**dict(r), "tags": db.loads(r["tags"], [])}
        for r in db.q("SELECT * FROM evidence WHERE deal_id = ? AND source_id = ? ORDER BY id", (deal_id, source_id))
    ]
    return d


# ----------------------------------------------------------------- evidence
@app.get("/api/deals/{deal_id}/evidence")
def list_evidence(
    deal_id: str,
    kind: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    limit: int = 400,
    offset: int = 0,
):
    _deal(deal_id)
    limit, offset = _page(limit, offset, 1000)
    where = "WHERE deal_id = ?"
    params: list[Any] = [deal_id]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if q:
        where += " AND (label LIKE ? OR value LIKE ? OR quote LIKE ?)"
        params += [f"%{q}%"] * 3
    if tag:
        where += " AND tags LIKE ?"
        params.append(f'%"{tag}"%')
    # Enforce per-deal evidence limit
    if config.EVIDENCE_LIMIT_PER_DEAL:
        total = db.q1(f"SELECT COUNT(*) c FROM evidence {where}", params)["c"]
        if total > config.EVIDENCE_LIMIT_PER_DEAL:
            raise HTTPException(
                400,
                f"Evidence limit exceeded: {total} items (max {config.EVIDENCE_LIMIT_PER_DEAL})",
            )
    else:
        total = db.q1(f"SELECT COUNT(*) c FROM evidence {where}", params)["c"]
    rows = db.q(
        f"SELECT * FROM evidence {where} ORDER BY id LIMIT ? OFFSET ?", (*params, limit, offset)
    )
    sources = {r["id"]: dict(r) for r in db.q("SELECT id, title, publisher, url, form FROM sources WHERE deal_id = ?", (deal_id,))}
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = db.loads(d.get("tags"), [])
        src = sources.get(d.get("source_id") or "") or {}
        d["source"] = {"id": src.get("id"), "title": src.get("title"), "publisher": src.get("publisher"), "url": src.get("url"), "form": src.get("form")}
        out.append(d)
    kinds = [r["kind"] for r in db.q("SELECT DISTINCT kind FROM evidence WHERE deal_id = ? ORDER BY kind", (deal_id,))]
    return {"evidence": out, "kinds": kinds, "count": len(out), "total": total, "limit": limit, "offset": offset}


@app.get("/api/deals/{deal_id}/evidence/{ev_id}")
def get_evidence(deal_id: str, ev_id: str):
    _deal(deal_id)
    row = db.q1("SELECT * FROM evidence WHERE deal_id = ? AND id = ?", (deal_id, ev_id))
    if not row:
        raise HTTPException(404, "Evidence item not found")
    d = dict(row)
    d["tags"] = db.loads(d.get("tags"), [])
    if d.get("source_id"):
        s = db.q1(
            "SELECT id, kind, title, publisher, url, form, locator, published_at, content FROM sources WHERE deal_id = ? AND id = ?",
            (deal_id, d["source_id"]),
        )
        if s:
            src = dict(s)
            src["excerpt"] = (src.pop("content", "") or "")[:4000]
            d["source"] = src
    d["risks"] = [
        {"id": r["id"], "title": r["title"], "score": r["score"]}
        for r in db.q("SELECT * FROM risks WHERE deal_id = ? AND evidence_ids LIKE ?", (deal_id, f'%"{ev_id}"%'))
    ]
    return d


@app.get("/api/deals/{deal_id}/search")
def search_passages(deal_id: str, q: str = Query(..., min_length=2), limit: int = 20):
    _deal(deal_id)
    terms = [t for t in re.findall(r"\w{3,}", q)][:6]
    if not terms:
        return {"results": []}
    where = " OR ".join("text LIKE ?" for _ in terms)
    rows = db.q(
        f"SELECT id, source_id, section, ordinal, text FROM chunks WHERE deal_id = ? AND ({where}) LIMIT 200",
        (deal_id, *[f"%{t}%" for t in terms]),
    )
    scored = []
    for r in rows:
        text = r["text"]
        score = sum(text.lower().count(t.lower()) for t in terms)
        scored.append((score, dict(r)))
    scored.sort(key=lambda x: -x[0])
    out = []
    sources = {r["id"]: dict(r) for r in db.q("SELECT id, title, form, url FROM sources WHERE deal_id = ?", (deal_id,))}
    for score, r in scored[: max(1, min(limit, 100))]:
        out.append({**r, "score": score, "source": sources.get(r["source_id"])})
    return {"results": out, "query": q}


# -------------------------------------------------------------------- risks
@app.get("/api/deals/{deal_id}/risks")
def list_risks(deal_id: str, limit: int = 200, offset: int = 0):
    _deal(deal_id)
    limit, offset = _page(limit, offset)
    total = db.q1("SELECT COUNT(*) c FROM risks WHERE deal_id = ?", (deal_id,))["c"]
    if config.RISK_LIMIT_PER_DEAL and total > config.RISK_LIMIT_PER_DEAL:
        raise HTTPException(
            400,
            f"Risk limit exceeded: {total} risks (max {config.RISK_LIMIT_PER_DEAL})",
        )
    risks = []
    for r in db.q("SELECT * FROM risks WHERE deal_id = ? ORDER BY score DESC, id LIMIT ? OFFSET ?", (deal_id, limit, offset)):
        d = dict(r)
        for k in ("evidence_ids", "mitigants", "questions"):
            d[k] = db.loads(d.get(k), [])
        risks.append(d)
    summary = db.q1("SELECT summary FROM deals WHERE id = ?", (deal_id,))["summary"]
    analysis = (db.loads(summary, {}) or {}).get("risks") or {}
    return {"risks": risks, "summary": analysis, "total": total, "limit": limit, "offset": offset}


@app.patch("/api/deals/{deal_id}/risks/{risk_id}")
def update_risk(deal_id: str, risk_id: str, request: Request, payload: dict = Body(...)):
    auth.require(request, "write")
    _deal(deal_id)
    row = db.q1("SELECT * FROM risks WHERE deal_id = ? AND id = ?", (deal_id, risk_id))
    if not row:
        raise HTTPException(404, "Risk not found")
    fields: dict[str, Any] = {}
    if "severity" in payload:
        fields["severity"] = max(1, min(5, int(payload["severity"])))
    if "likelihood" in payload:
        fields["likelihood"] = max(1, min(5, int(payload["likelihood"])))
    if fields:
        sev = fields.get("severity", row["severity"])
        lik = fields.get("likelihood", row["likelihood"])
        fields["score"] = sev * lik
    if "status" in payload and payload["status"] in ("open", "mitigating", "accepted", "closed"):
        fields["status"] = payload["status"]
    if not fields:
        raise HTTPException(422, "Nothing to update")
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.x(f"UPDATE risks SET {sets} WHERE deal_id = ? AND id = ?", (*fields.values(), deal_id, risk_id))
    db.insert(
        "reviews",
        {
            "deal_id": deal_id,
            "doc_id": None,
            "action": "risk_updated",
            "actor": _actor(request),
            "note": f"{risk_id}: {json.dumps(fields)}",
            "ts": db.now(),
        },
    )
    _audit(request, "risk.update", deal_id=deal_id, detail={"risk_id": risk_id, **fields})
    d = dict(db.q1("SELECT * FROM risks WHERE deal_id = ? AND id = ?", (deal_id, risk_id)))
    for k in ("evidence_ids", "mitigants", "questions"):
        d[k] = db.loads(d.get(k), [])
    return d


# ----------------------------------------------------------------- document
@app.get("/api/deals/{deal_id}/document")
def get_document(deal_id: str):
    _deal(deal_id)
    row = db.q1("SELECT * FROM documents WHERE deal_id = ? ORDER BY updated_at DESC LIMIT 1", (deal_id,))
    if not row:
        raise HTTPException(404, "No document generated yet")
    doc = db.loads(row["sections"], {})
    if isinstance(doc, str):
        doc = db.loads(doc, {})
    trust = db.loads(row["trust"], {})
    if isinstance(trust, str):
        trust = db.loads(trust, {})
    doc["trust"] = trust or doc.get("trust") or {}
    doc["doc_id"] = row["id"]
    doc["version"] = row["version"]
    doc["updated_at"] = row["updated_at"]
    doc["status"] = row["status"]
    return doc


@app.get("/api/deals/{deal_id}/documents")
def list_document_versions(deal_id: str, limit: int = 50, offset: int = 0):
    _deal(deal_id)
    limit, offset = _page(limit, offset)
    total = db.q1("SELECT COUNT(*) c FROM document_versions WHERE deal_id = ?", (deal_id,))["c"]
    rows = db.q(
        "SELECT id, deal_id, doc_id, version, run_id, status, created_at FROM document_versions "
        "WHERE deal_id = ? ORDER BY version DESC, id DESC LIMIT ? OFFSET ?",
        (deal_id, limit, offset),
    )
    return {"versions": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/api/deals/{deal_id}/documents/{version}")
def get_document_version(deal_id: str, version: int):
    _deal(deal_id)
    row = db.q1(
        "SELECT * FROM document_versions WHERE deal_id = ? AND version = ? ORDER BY id DESC LIMIT 1",
        (deal_id, version),
    )
    if not row:
        raise HTTPException(404, "Document version not found")
    doc = db.loads(row["sections"], {})
    if isinstance(doc, str):
        doc = db.loads(doc, {})
    trust = db.loads(row["trust"], {})
    if isinstance(trust, str):
        trust = db.loads(trust, {})
    doc["trust"] = trust or doc.get("trust") or {}
    doc["doc_id"] = row["doc_id"]
    doc["version"] = row["version"]
    doc["created_at"] = row["created_at"]
    doc["status"] = row["status"]
    return doc


@app.post("/api/deals/{deal_id}/document/approve")
def approve_document(deal_id: str, request: Request, payload: dict = Body(default={})):
    who = auth.require(request, "approve")
    _deal(deal_id)
    row = db.q1("SELECT id, version, sections, trust, reviewer_comments, change_summary FROM documents WHERE deal_id = ? ORDER BY updated_at DESC LIMIT 1", (deal_id,))
    if not row:
        raise HTTPException(404, "No document generated yet")
    # Store previous version's sections for diff purposes
    prev_ver = db.q1(
        "SELECT version, sections, reviewer_comments, change_summary FROM document_versions WHERE deal_id = ? AND doc_id = ? ORDER BY version DESC LIMIT 1",
        (deal_id, row["id"]),
    )
    if prev_ver and prev_ver["version"] >= row["version"]:
        # Save the current version's data as "previous" before upgrading
        db.x(
            "INSERT INTO document_versions (deal_id, doc_id, version, run_id, status, sections, trust, reviewer_comments, change_summary, previous_sections) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deal_id,
                row["id"],
                prev_ver["version"] if prev_ver else row["version"],
                prev_ver["run_id"] if prev_ver else None,
                prev_ver["status"] if prev_ver else "draft",
                prev_ver["sections"] if prev_ver else row["sections"],
                prev_ver["trust"] if prev_ver else row["trust"],
                prev_ver["reviewer_comments"] if prev_ver else row["reviewer_comments"],
                prev_ver["change_summary"] if prev_ver else "",
                row["sections"],  # current sections become previous for next diff
            ),
        )
    actor = who.label[:80]
    note = (payload.get("note") or "").strip()[:500]
    change_summary = (payload.get("change_summary") or "").strip()[:500]
    # Update document status
    db.x("UPDATE documents SET status = 'approved', updated_at = ? WHERE deal_id = ? AND id = ?", (db.now(), deal_id, row["id"]))
    # Update deal status
    db.x(
        "UPDATE deals SET status = 'approved', updated_at = ? WHERE id = ? AND tenant_id = ?",
        (db.now(), deal_id, auth.tenant_id()),
    )
    # Insert review record with full actor info
    db.insert(
        "reviews",
        {
            "deal_id": deal_id,
            "doc_id": row["id"],
            "action": "approved",
            "actor": actor,
            "note": note,
            "change_summary": change_summary,
            "ts": db.now(),
        },
    )
    # Update document version with reviewer comments
    db.x(
        "UPDATE document_versions SET reviewer_comments = ?, change_summary = ? WHERE deal_id = ? AND doc_id = ? AND version = ?",
        (note, change_summary, deal_id, row["id"], row["version"]),
    )
    # Record audit event with full detail
    _audit(
        request,
        "document.approve",
        deal_id=deal_id,
        detail={
            "doc_id": row["id"],
            "version": row["version"],
            "actor": actor,
            "note": note,
            "change_summary": change_summary,
        },
    )
    return {
        "approved": True,
        "doc_id": row["id"],
        "version": row["version"],
        "actor": actor,
        "note": note,
        "change_summary": change_summary,
    }


@app.get("/api/deals/{deal_id}/reviews")
def list_reviews(deal_id: str, limit: int = 200, offset: int = 0):
    _deal(deal_id)
    limit, offset = _page(limit, offset)
    total = db.q1("SELECT COUNT(*) c FROM reviews WHERE deal_id = ?", (deal_id,))["c"]
    rows = db.q(
        "SELECT id, doc_id, action, actor, note, change_summary, ts FROM reviews WHERE deal_id = ? ORDER BY ts DESC LIMIT ? OFFSET ?", (deal_id, limit, offset)
    )
    return {"reviews": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# -------------------------------------------------------------------- audit
@app.get("/api/audit")
def list_audit(
    request: Request,
    deal_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    auth.require(request, "admin")
    limit, offset = _page(limit, offset, 500)
    where = "WHERE tenant_id = ?"
    params: list[Any] = [auth.tenant_id()]
    if deal_id:
        where += " AND deal_id = ?"
        params.append(deal_id)
    if action:
        where += " AND action = ?"
        params.append(action)
    total = db.q1(f"SELECT COUNT(*) c FROM audit_events {where}", params)["c"]
    rows = db.q(
        f"SELECT * FROM audit_events {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "events": [{**dict(r), "detail": db.loads(r["detail"], {})} for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/deals/{deal_id}/llm-calls")
def list_llm_calls(deal_id: str, limit: int = 100, offset: int = 0):
    _deal(deal_id)
    limit, offset = _page(limit, offset)
    total = db.q1("SELECT COUNT(*) c FROM llm_calls WHERE deal_id = ?", (deal_id,))["c"]
    rows = db.q(
        "SELECT id, ts, run_id, provider, model, purpose, prompt_tokens, completion_tokens, latency_ms, ok, error, trace_id "
        "FROM llm_calls WHERE deal_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (deal_id, limit, offset),
    )
    return {"calls": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# ------------------------------------------------------------------ exports
@app.get("/api/deals/{deal_id}/export/{fmt}")
def export_document(deal_id: str, fmt: str, request: Request):
    _deal(deal_id)
    fmt = fmt.lower()
    try:
        if fmt == "docx":
            data, name = export.to_docx(deal_id)
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif fmt == "pdf":
            data, name = export.to_pdf(deal_id)
            media = "application/pdf"
        elif fmt in ("xlsx", "excel"):
            data, name = export.to_xlsx(deal_id)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            name = name.replace(".xlsx", ".xlsx")
        else:
            raise HTTPException(422, "Format must be docx, pdf or xlsx")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    headers = {
        "Content-Disposition": f'attachment; filename="{name}"',
        "Cache-Control": "no-store",
    }
    _audit(request, "document.export", deal_id=deal_id, detail={"format": fmt, "bytes": len(data)})
    METRICS.inc("dossier_exports_total", {"format": fmt})
    return Response(content=data, media_type=media, headers=headers)


# --------------------------------------------------------------------- demo
DEMO = {
    "id": "lovesac",
    "name": "Lovesac",
    "identifier": "LOVE",
    "client": "Confidential — demonstration",
    "deal_type": "sell_side",
}


@app.post("/api/demo")
def load_demo(request: Request, payload: dict = Body(default={})):
    """Create (or reuse) the Lovesac demonstration deal and optionally run it."""
    auth.require(request, "write")
    existing = db.q1(
        "SELECT id FROM deals WHERE id = ? AND tenant_id = ?",
        (DEMO["id"], auth.tenant_id()),
    )
    if existing:
        deal_id = existing["id"]
    else:
        now = db.now()
        deal_id = _unique_deal_id(DEMO["id"])
        db.insert(
            "deals",
            {
                "id": deal_id,
                "tenant_id": auth.tenant_id(),
                "name": DEMO["name"],
                "company": None,
                "ticker": None,
                "cik": None,
                "deal_type": DEMO["deal_type"],
                "sector": "Consumer / furniture",
                "client": DEMO["client"],
                "status": "draft",
                "created_at": now,
                "updated_at": now,
                "config": db.dumps({"identifier": DEMO["identifier"], "demo": True}),
                "summary": "{}",
            },
        )
    run_id = None
    if payload.get("start") and not orchestrator.is_running(deal_id):
        run_id = orchestrator.start_run(deal_id, polish=payload.get("polish"))
    _audit(request, "demo.load", deal_id=deal_id, run_id=run_id)
    return {"deal_id": deal_id, "run_id": run_id}


# -------------------------------------------------------------------- static
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets", check_dir=False), name="assets")


@app.get("/")
def index():
    if not (WEB_DIR / "index.html").exists():
        return JSONResponse({"app": config.APP_NAME, "message": "Web UI not installed", "docs": "/api/docs"})
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    p = WEB_DIR / "assets" / "favicon.svg"
    if p.exists():
        return FileResponse(p, media_type="image/svg+xml")
    return Response(status_code=204)
