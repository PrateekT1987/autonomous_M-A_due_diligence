"""Authentication, tenancy and role checks.

Three credentials are accepted, in order:

* ``Authorization: Bearer <token>`` / ``X-API-Key`` — the static operator token
  or a named API key. Keys carry a role and tenant.
* a Dossier session cookie created by the OIDC SSO callback.
* ``?token=`` for EventSource and download links, which cannot set headers.

A ``Principal`` is resolved once per request and stored in a context variable,
so every query can be tenant-scoped without threading a parameter through the
agents. Local development with no credentials configured resolves to a single
``default`` tenant with the ``owner`` role.
"""
from __future__ import annotations

import contextvars
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import config, db

READ_ROLES = {"owner", "analyst", "reviewer", "viewer"}
WRITE_ROLES = {"owner", "analyst"}
APPROVE_ROLES = {"owner", "reviewer"}
ADMIN_ROLES = {"owner"}

_ACTION_ROLES = {
    "read": READ_ROLES,
    "write": WRITE_ROLES,
    "approve": APPROVE_ROLES,
    "admin": ADMIN_ROLES,
}


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str = "default"
    role: str = "owner"
    email: str | None = None
    name: str | None = None
    method: str = "anonymous"
    csrf: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.method != "anonymous"

    @property
    def label(self) -> str:
        return self.name or self.email or self.subject


_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "dossier_principal", default=None
)


def auth_enabled() -> bool:
    return bool(config.ACCESS_TOKEN or config.API_KEYS or config.OIDC_ENABLED)


def set_principal(value: Principal | None) -> None:
    _principal.set(value)


def current() -> Principal | None:
    return _principal.get()


def principal() -> Principal:
    """The request principal, or an owner in local unauthenticated mode."""
    value = _principal.get()
    if value is not None:
        return value
    if not auth_enabled():
        return Principal(subject="anonymous", method="anonymous")
    return Principal(subject="", role="viewer", method="anonymous")


def tenant_id() -> str:
    return principal().tenant_id


def allowed(role: str, action: str) -> bool:
    return role in _ACTION_ROLES.get(action, READ_ROLES)


def require(request: Request, action: str = "read") -> Principal:
    who = principal()
    if auth_enabled() and not who.authenticated:
        raise HTTPException(401, "Authentication required")
    if not allowed(who.role, action):
        raise HTTPException(403, f"Role '{who.role}' cannot perform this action")
    return who


# ----------------------------------------------------------------- credentials
def _token_principal(token: str) -> Principal | None:
    if not token:
        return None
    if config.ACCESS_TOKEN and hmac.compare_digest(token, config.ACCESS_TOKEN):
        return Principal(subject="operator", tenant_id="default", role="owner", method="token")
    for label, item in config.API_KEYS.items():
        key = item.get("key") or ""
        if key and hmac.compare_digest(token, key):
            return Principal(
                subject=f"key:{label}",
                tenant_id=item.get("tenant") or "default",
                role=(item.get("role") or "owner").lower(),
                method="api_key",
            )
    return None


def _session_principal(session_id: str) -> Principal | None:
    if not session_id:
        return None
    row = db.q1(
        "SELECT s.id, s.csrf, s.expires_at, u.id AS user_id, u.email, u.name, u.role, u.tenant_id "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (session_id,),
    )
    if not row or str(row["expires_at"]) <= db.now():
        if row:
            db.x("DELETE FROM sessions WHERE id = ?", (session_id,))
        return None
    return Principal(
        subject=str(row["user_id"]),
        tenant_id=str(row["tenant_id"]),
        role=str(row["role"] or "viewer"),
        email=row["email"],
        name=row["name"],
        method="session",
        csrf=str(row["csrf"]),
    )


def resolve(request: Request) -> Principal | None:
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    token = token or request.headers.get("x-api-key") or request.query_params.get("token") or ""
    if token:
        return _token_principal(token)
    cookie = request.cookies.get(config.SESSION_COOKIE) or ""
    return _session_principal(cookie)


# ---------------------------------------------------------------- session CRUD
def create_session(user_id: str, tenant_id_value: str) -> tuple[str, str]:
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    created = db.now()
    expires = db.now().replace("T", "T")  # ISO text, compared lexicographically
    from datetime import datetime, timedelta, timezone

    expires = (datetime.now(timezone.utc) + timedelta(seconds=config.SESSION_TTL_SECONDS)).isoformat(
        timespec="seconds"
    )
    db.insert(
        "sessions",
        {
            "id": sid,
            "user_id": user_id,
            "tenant_id": tenant_id_value,
            "csrf": csrf,
            "created_at": created,
            "expires_at": expires,
        },
    )
    return sid, csrf


def delete_session(session_id: str) -> None:
    if session_id:
        db.x("DELETE FROM sessions WHERE id = ?", (session_id,))


def upsert_user(
    *, email: str, name: str | None, tenant_id_value: str, role: str, provision: bool = True
) -> dict:
    """Find or create a user and its tenant after a successful SSO login."""
    tenant = db.q1("SELECT id FROM tenants WHERE id = ?", (tenant_id_value,))
    if not tenant:
        if not provision:
            raise PermissionError("tenant is not provisioned")
        db.insert("tenants", {"id": tenant_id_value, "name": tenant_id_value, "created_at": db.now()})
    row = db.q1(
        "SELECT * FROM users WHERE tenant_id = ? AND lower(email) = lower(?)",
        (tenant_id_value, email),
    )
    if row:
        db.x(
            "UPDATE users SET name = COALESCE(?, name), last_seen_at = ? WHERE id = ?",
            (name, db.now(), row["id"]),
        )
        return dict(db.q1("SELECT * FROM users WHERE id = ?", (row["id"],)))
    if not provision:
        raise PermissionError("user is not provisioned")
    if not provision:
        raise PermissionError("user is not provisioned")
    user_id = "USR-" + secrets.token_hex(6)
    db.insert(
        "users",
        {
            "id": user_id,
            "tenant_id": tenant_id_value,
            "email": email[:255],
            "name": (name or email.split("@")[0])[:120],
            "role": role,
            "created_at": db.now(),
            "last_seen_at": db.now(),
        },
    )
    return dict(db.q1("SELECT * FROM users WHERE id = ?", (user_id,)))
