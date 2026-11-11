"""Runtime configuration for Dossier.

A local `.env` file is loaded before the constants below are read, so the
provider configured there (for example OpenRouter) is used by the CLI, the API
server and the evaluation scripts alike. Real environment variables always win.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader; does not override variables already in the environment."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DOSSIER_DATA", ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DEAL_DIR = DATA_DIR / "deals"
DB_PATH = Path(os.getenv("DOSSIER_DB", DATA_DIR / "dossier.db"))

# PostgreSQL is the production database. SQLite remains the default so the
# local demo and tests keep working without any external service.
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DOSSIER_DATABASE_URL") or "").strip()
ALLOW_SQLITE = (os.getenv("DOSSIER_ALLOW_SQLITE") or "1").strip().lower() not in {"0", "false", "no", "off"}

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "Dossier Research (due diligence platform) research@dossier.local"
)
SEC_RATE_LIMIT_SECONDS = float(os.getenv("SEC_RATE_LIMIT", "0.12"))

# ---------------------------------------------------------------- model access
# `ollama` keeps everything on the machine. `openrouter` (or any OpenAI-compatible
# endpoint) is used when credentials are supplied in .env.
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
# Primary and secondary providers for fallback (comma-separated, e.g. "openrouter,ollama").
# The pipeline will try primary first, then secondary, then deterministic engine.
LLM_PROVIDER_SECONDARY = (os.getenv("LLM_PROVIDER_SECONDARY") or "").strip().lower()
LLM_ENABLED = (os.getenv("LLM_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "240"))

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_BASE_URL = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
# `MODEL` is the conventional name used in .env for OpenRouter model ids.
OPENROUTER_MODEL = (
    os.getenv("OPENROUTER_MODEL") or os.getenv("MODEL") or "nvidia/nemotron-3-ultra-550b-a55b:free"
).strip()
OPENROUTER_EMBED_MODEL = (os.getenv("OPENROUTER_EMBED_MODEL") or "").strip()
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "http://localhost:8000")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "Dossier")
# Reasoning models can burn an entire completion budget before answering.
# "none" keeps latency and token use low for structured JSON rewrites; set to
# "low"/"medium"/"high" if you want the model to deliberate, or "" to omit.
OPENROUTER_REASONING_EFFORT = (os.getenv("OPENROUTER_REASONING_EFFORT") or "none").strip().lower()

REMOTE_PROVIDERS = {"openrouter", "openai", "openai-compatible", "custom"}
LLM_IS_REMOTE = LLM_PROVIDER in REMOTE_PROVIDERS


def resolve_polish(value: bool | None = None) -> bool:
    """Decide whether to use the model for prose polish.

    Explicit CLI/API values win. Otherwise `LLM_POLISH` sets the default, and
    "auto" enables polish when a remote provider is configured. The local 7B
    model remains opt-in because of its latency.
    """
    if value is not None:
        return bool(value)
    setting = (os.getenv("LLM_POLISH") or "auto").strip().lower()
    if setting in {"1", "true", "yes", "on"}:
        return True
    if setting in {"0", "false", "no", "off"}:
        return False
    return bool(LLM_ENABLED and LLM_IS_REMOTE and OPENROUTER_API_KEY)


APP_NAME = "Dossier"
APP_TAGLINE = "Autonomous due diligence for boutique M&A advisors"

# ------------------------------------------------------------- production ops
APP_ENV = (os.getenv("DOSSIER_ENV") or "development").strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
LOG_FORMAT = (os.getenv("LOG_FORMAT") or "json" if IS_PRODUCTION else os.getenv("LOG_FORMAT") or "text").lower()

# Optional access control. Leave empty for the local demo; set one or both in
# any shared deployment. Multiple keys are comma-separated as label:key[:role[:tenant]].
ACCESS_TOKEN = (os.getenv("DOSSIER_ACCESS_TOKEN") or "").strip()
_raw_keys = (os.getenv("DOSSIER_API_KEYS") or "").strip()
API_KEYS: dict[str, dict] = {}
for _item in _raw_keys.split(","):
    parts = [p.strip() for p in _item.split(":")]
    if len(parts) >= 2 and parts[1]:
        API_KEYS[parts[0] or "api"] = {
            "key": parts[1],
            "role": (parts[2] if len(parts) > 2 and parts[2] else "owner"),
            "tenant": (parts[3] if len(parts) > 3 and parts[3] else "default"),
        }
AUTH_REQUIRED = bool(ACCESS_TOKEN or API_KEYS)

# Generic OpenID Connect SSO. When configured, /auth/login starts the
# authorization-code + PKCE flow and /auth/callback creates a Dossier session.
OIDC_ISSUER = (os.getenv("OIDC_ISSUER") or "").strip().rstrip("/")
OIDC_CLIENT_ID = (os.getenv("OIDC_CLIENT_ID") or "").strip()
OIDC_CLIENT_SECRET = (os.getenv("OIDC_CLIENT_SECRET") or "").strip()
OIDC_REDIRECT_URI = (os.getenv("OIDC_REDIRECT_URI") or "").strip()
OIDC_SCOPES = (os.getenv("OIDC_SCOPES") or "openid email profile").strip()
OIDC_BUTTON_LABEL = (os.getenv("OIDC_BUTTON_LABEL") or "Sign in with SSO").strip()
OIDC_TENANT_CLAIM = (os.getenv("OIDC_TENANT_CLAIM") or "tenant").strip()
OIDC_DEFAULT_TENANT = (os.getenv("OIDC_DEFAULT_TENANT") or "default").strip()
OIDC_DEFAULT_ROLE = (os.getenv("OIDC_DEFAULT_ROLE") or "analyst").strip().lower()
OIDC_AUTO_PROVISION = (os.getenv("OIDC_AUTO_PROVISION") or "1").strip().lower() not in {"0", "false", "no", "off"}
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_REDIRECT_URI)
OIDC_POST_LOGIN = os.getenv("OIDC_POST_LOGIN", "/")

SESSION_COOKIE = (os.getenv("DOSSIER_SESSION_COOKIE") or "dossier_session").strip()
SESSION_TTL_SECONDS = int(os.getenv("DOSSIER_SESSION_TTL", "43200"))

CORS_ORIGINS = [o.strip() for o in (os.getenv("DOSSIER_CORS_ORIGINS") or "").split(",") if o.strip()]
RATE_LIMIT_PER_MINUTE = int(os.getenv("DOSSIER_RATE_LIMIT", "240"))
RATE_LIMIT_BURST = int(os.getenv("DOSSIER_RATE_BURST", "60"))
MAX_BODY_BYTES = int(os.getenv("DOSSIER_MAX_BODY_BYTES", "2000000"))
REQUEST_ID_HEADER = "X-Request-ID"

# embedded = in-process worker (single container); worker = separate `dossier worker` process.
RUN_MODE = (os.getenv("DOSSIER_RUN_MODE") or "embedded").strip().lower()
WORKER_POLL_SECONDS = float(os.getenv("DOSSIER_WORKER_POLL", "1.0"))
JOB_STALE_SECONDS = int(os.getenv("DOSSIER_JOB_STALE", "900"))
JOB_MAX_ATTEMPTS = int(os.getenv("DOSSIER_JOB_ATTEMPTS", "2"))

# Model guardrails per run.
LLM_MAX_CALLS = int(os.getenv("LLM_MAX_CALLS", "12"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "30000"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))

# ------------------------------------------------------- per-deal limits
# Enforced by the API; set to 0 or None to disable limiting.
SOURCE_LIMIT_PER_DEAL = int(os.getenv("DOSSIER_SOURCE_LIMIT_PER_DEAL", "500"))
EVIDENCE_LIMIT_PER_DEAL = int(os.getenv("DOSSIER_EVIDENCE_LIMIT_PER_DEAL", "2000"))
RISK_LIMIT_PER_DEAL = int(os.getenv("DOSSIER_RISK_LIMIT_PER_DEAL", "200"))
EXPORT_MAX_BYTES = int(os.getenv("DOSSIER_EXPORT_MAX_BYTES", "500000"))  # 500 KB
# Observability
SENTRY_DSN = (os.getenv("SENTRY_DSN") or "").strip()
OTEL_EXPORTER_OTLP_ENDPOINT = (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
OTEL_SERVICE_NAME = (os.getenv("OTEL_SERVICE_NAME") or "dossier").strip()
OTEL_TRACES_SAMPLE_RATE = float(os.getenv("OTEL_TRACES_SAMPLE_RATE", "0.1"))
ALERT_WEBHOOK_URL = (os.getenv("DOSSIER_ALERT_WEBHOOK_URL") or "").strip()
TRUST_PROXY = (os.getenv("DOSSIER_TRUST_PROXY") or "0").strip().lower() not in {"0", "false", "no", "off"}

for _p in (DATA_DIR, CACHE_DIR, DEAL_DIR):
    _p.mkdir(parents=True, exist_ok=True)


def redact_values() -> list[str]:
    """Secret values that must never appear in logs or error responses."""
    values = [ACCESS_TOKEN, OIDC_CLIENT_SECRET, OPENROUTER_API_KEY, OIDC_CLIENT_ID]
    values += [item.get("key", "") for item in API_KEYS.values()]
    return [v for v in values if v and len(v) >= 6]


def validate() -> list[str]:
    """Fail-fast configuration check. Returns warnings; raises in production.

    Production refuses to start without access control, a PostgreSQL database
    and a real SEC contact address. Those are P0 controls, so a misconfigured
    deployment fails loudly instead of silently exposing a public demo.
    """
    warnings: list[str] = []
    errors: list[str] = []
    if not (AUTH_REQUIRED or OIDC_ENABLED):
        errors.append("no access control: set DOSSIER_ACCESS_TOKEN, DOSSIER_API_KEYS or OIDC_*")
    if not DATABASE_URL and not ALLOW_SQLITE:
        errors.append("DATABASE_URL is required (SQLite fallback is disabled)")
    if DATABASE_URL and not DATABASE_URL.startswith(("postgres://", "postgresql://")):
        warnings.append("DATABASE_URL is not a PostgreSQL URL")
    if "research@dossier.local" in SEC_USER_AGENT:
        errors.append("SEC_USER_AGENT must identify a monitored contact address")
    if OIDC_ENABLED:
        if not OIDC_REDIRECT_URI.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            errors.append("OIDC_REDIRECT_URI must be HTTPS (localhost is allowed for development)")
        if OIDC_DEFAULT_ROLE not in {"owner", "analyst", "reviewer", "viewer"}:
            errors.append("OIDC_DEFAULT_ROLE must be owner, analyst, reviewer or viewer")
    if IS_PRODUCTION and not DATABASE_URL:
        errors.append("production requires PostgreSQL via DATABASE_URL")
    if IS_PRODUCTION and not (SENTRY_DSN or OTEL_EXPORTER_OTLP_ENDPOINT):
        warnings.append("no error tracking or tracing exporter configured (SENTRY_DSN / OTEL_EXPORTER_OTLP_ENDPOINT)")
    if IS_PRODUCTION and not OIDC_ENABLED:
        warnings.append("SSO is not configured; only static bearer/API keys are available")
    if not IS_PRODUCTION:
        return warnings
    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))
    return warnings
