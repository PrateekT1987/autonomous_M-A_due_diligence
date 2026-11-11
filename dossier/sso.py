"""OpenID Connect authorization-code flow with PKCE.

Configuration is provider-agnostic: any issuer exposing a standard discovery
document works (Entra ID, Google Workspace, Okta, Auth0, Keycloak). ID tokens
are verified against the issuer's JWKS; the userinfo endpoint is used as a
fallback when a provider returns an opaque access token without an ID token.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import config, db

try:
    import jwt
except Exception:  # pragma: no cover - optional until SSO is configured
    jwt = None  # type: ignore[assignment]

_discovery: dict[str, Any] = {"value": None, "at": 0.0}
_STATE_TTL_SECONDS = 600


class SSOError(RuntimeError):
    pass


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def discovery(force: bool = False) -> dict:
    if not config.OIDC_ENABLED:
        raise SSOError("OIDC is not configured")
    if not force and _discovery["value"] and time.monotonic() - _discovery["at"] < 3600:
        return _discovery["value"]
    url = config.OIDC_ISSUER + "/.well-known/openid-configuration"
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        raise SSOError(f"could not load OIDC discovery document: {exc}") from exc
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not data.get(field):
            raise SSOError(f"discovery document is missing {field}")
    _discovery.update({"value": data, "at": time.monotonic()})
    return data


def begin_login() -> str:
    meta = discovery()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    db.insert(
        "auth_states",
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "redirect_uri": config.OIDC_REDIRECT_URI,
            "created_at": db.now(),
        },
    )
    params = {
        "response_type": "code",
        "client_id": config.OIDC_CLIENT_ID,
        "redirect_uri": config.OIDC_REDIRECT_URI,
        "scope": config.OIDC_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return meta["authorization_endpoint"] + "?" + urlencode(params)


def _consume_state(state: str) -> dict:
    row = db.q1("SELECT * FROM auth_states WHERE state = ?", (state,))
    if not row:
        raise SSOError("unknown or already used login state")
    db.x("DELETE FROM auth_states WHERE state = ?", (state,))
    # Purge abandoned states opportunistically.
    db.x("DELETE FROM auth_states WHERE created_at < ?", (str(_cutoff()),))
    age = time.time() - _parse(row["created_at"])
    if age > _STATE_TTL_SECONDS:
        raise SSOError("login state expired")
    return dict(row)


def _parse(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _cutoff() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=_STATE_TTL_SECONDS)).isoformat(timespec="seconds")


def _verify_id_token(token: str, nonce: str) -> dict:
    if jwt is None:
        raise SSOError("PyJWT is not installed; install requirements.lock")
    meta = discovery()
    try:
        client = jwt.PyJWKClient(meta["jwks_uri"])
        key = client.get_signing_key_from_jwt(token).key
        algorithms = meta.get("id_token_signing_alg_values_supported") or ["RS256"]
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=config.OIDC_CLIENT_ID,
            issuer=config.OIDC_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a login failure
        raise SSOError(f"ID token verification failed: {exc}") from exc
    if nonce and claims.get("nonce") and claims["nonce"] != nonce:
        raise SSOError("ID token nonce mismatch")
    return claims


def finish_login(code: str, state: str) -> dict:
    saved = _consume_state(state)
    if not code:
        raise SSOError("authorization code missing")
    meta = discovery()
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.OIDC_CLIENT_ID,
        "redirect_uri": saved["redirect_uri"],
        "code_verifier": saved["verifier"],
    }
    if config.OIDC_CLIENT_SECRET:
        payload["client_secret"] = config.OIDC_CLIENT_SECRET
    try:
        r = httpx.post(meta["token_endpoint"], data=payload, timeout=20)
        r.raise_for_status()
        tokens = r.json()
    except Exception as exc:  # noqa: BLE001
        raise SSOError(f"token exchange failed: {exc}") from exc

    claims: dict[str, Any] = {}
    if tokens.get("id_token"):
        claims = _verify_id_token(tokens["id_token"], saved["nonce"])
    if not claims.get("email") and meta.get("userinfo_endpoint") and tokens.get("access_token"):
        try:
            r = httpx.get(
                meta["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
                timeout=15,
            )
            r.raise_for_status()
            claims.update(r.json())
        except Exception as exc:  # noqa: BLE001
            raise SSOError(f"userinfo lookup failed: {exc}") from exc
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email or "@" not in email:
        raise SSOError("the identity provider did not return an email address")
    return {
        "email": email,
        "name": claims.get("name") or claims.get("given_name"),
        "tenant": claims.get(config.OIDC_TENANT_CLAIM),
        "role": claims.get("role") or claims.get("dossier_role"),
        "claims": claims,
    }
