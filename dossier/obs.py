"""Operational plumbing: structured logs, metrics, rate limiting and hashes.

Kept dependency-free so the API, workers and CLI can share it without import
cycles. Prometheus exposition is intentionally simple: counters, gauges and
sum/count histograms, which cover the service-level signals Dossier needs.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

# Correlation id propagated from the HTTP request into queue jobs, stage logs
# and model calls. A worker restores it from the job row.
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("dossier_trace_id", default=None)


def set_trace_id(value: str | None) -> None:
    _trace_id.set(value)


def current_trace_id() -> str | None:
    return _trace_id.get()


class RedactionFilter(logging.Filter):
    """Remove configured secret values from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            for secret in _secrets():
                if secret and secret in message:
                    message = message.replace(secret, "***redacted***")
            record.msg = message
            record.args = ()
            for key, value in list(record.__dict__.items()):
                if isinstance(value, str) and any(s and s in value for s in _secrets()):
                    for secret in _secrets():
                        value = value.replace(secret, "***redacted***")
                    setattr(record, key, value)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return True


def _secrets() -> list[str]:
    try:
        from . import config

        return config.redact_values()
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if current_trace_id():
            payload["trace_id"] = current_trace_id()
        for key in (
            "request_id",
            "trace_id",
            "tenant_id",
            "actor",
            "deal_id",
            "run_id",
            "stage",
            "method",
            "path",
            "status",
            "duration_ms",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactionFilter())
    root.handlers[:] = [handler]
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# --------------------------------------------------------------------- metrics
class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._hist: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    @staticmethod
    def _labels(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))

    def inc(self, name: str, labels: dict[str, Any] | None = None, value: float = 1.0) -> None:
        with self._lock:
            self._counters[(name, self._labels(labels))] += value

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        with self._lock:
            item = self._hist[(name, self._labels(labels))]
            item[0] += value
            item[1] += 1

    def gauge(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._gauges[(name, self._labels(labels))] = float(value)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"{name}{_fmt_labels(labels)} {value:g}")
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_fmt_labels(labels)} {value:g}")
            for (name, labels), (total, count) in sorted(self._hist.items()):
                lines.append(f"{name}_sum{_fmt_labels(labels)} {total:g}")
                lines.append(f"{name}_count{_fmt_labels(labels)} {count:g}")
        return "\n".join(lines) + "\n"


def _fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    escaped = ",".join(f'{k}="{v.replace(chr(34), chr(92) + chr(34))}"' for k, v in labels)
    return "{" + escaped + "}"


METRICS = Metrics()


# ------------------------------------------------------------------ rate limit
class RateLimiter:
    """Token bucket per identity; bounded memory with periodic pruning."""

    def __init__(self, per_minute: int = 240, burst: int = 60) -> None:
        self.rate = max(per_minute, 1) / 60.0
        self.burst = max(burst, 1)
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._last_prune = time.monotonic()

    def allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            if now - self._last_prune > 300:
                self._buckets = {
                    k: v for k, v in self._buckets.items() if now - v[1] < 300
                }
                self._last_prune = now
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, max(0.0, (1.0 - tokens) / self.rate)


# ---------------------------------------------------------------------- hashes
def digest(text: str | bytes | None, limit: int = 2000) -> str:
    if text is None:
        return ""
    if isinstance(text, str):
        text = text[:limit].encode("utf-8", "ignore")
    return hashlib.sha256(text).hexdigest()[:16]


def tail(value: str | None, limit: int = 500) -> str:
    return (value or "")[:limit]


# -------------------------------------------------------------- circuit breaker
class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    """Fail fast after repeated dependency errors, then probe after a cooldown."""

    def __init__(self, name: str, *, threshold: int = 5, cooldown: float = 60.0) -> None:
        self.name = name
        self.threshold = max(1, threshold)
        self.cooldown = max(1.0, cooldown)
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open" and time.monotonic() - self._opened_at >= self.cooldown:
                self._state = "half-open"
            return self._state

    def before_call(self) -> None:
        if self.state == "open":
            METRICS.inc("dossier_circuit_open_total", {"dependency": self.name})
            raise CircuitOpen(f"{self.name} circuit is open; retry in {int(self.cooldown)}s")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                METRICS.inc("dossier_circuit_opened_total", {"dependency": self.name})


# -------------------------------------------------------------------- alerting
def alert(event: str, **fields: Any) -> None:
    """Best-effort webhook for failed runs and server errors (never blocking)."""
    try:
        from . import config

        url = config.ALERT_WEBHOOK_URL
    except Exception:  # noqa: BLE001
        return
    if not url:
        return

    def _send() -> None:
        try:
            import urllib.request

            body = json.dumps(
                {
                    "event": event,
                    "service": "dossier",
                    "trace_id": current_trace_id(),
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    **fields,
                }
            ).encode()
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:  # noqa: BLE001 - alerting is best effort
            pass

    threading.Thread(target=_send, daemon=True, name="dossier-alert").start()


# ---------------------------------------------------------------- observability
_observability_ready = False


def configure_observability(app=None) -> list[str]:
    """Initialise Sentry and OpenTelemetry when configured. Returns warnings."""
    global _observability_ready
    warnings: list[str] = []
    if _observability_ready:
        return warnings
    try:
        from . import config

        if config.SENTRY_DSN:
            try:
                import sentry_sdk

                def _scrub(event, hint):  # noqa: ANN001
                    text = json.dumps(event, default=str)
                    for secret in config.redact_values():
                        text = text.replace(secret, "***redacted***")
                    return json.loads(text)

                sentry_sdk.init(
                    dsn=config.SENTRY_DSN,
                    environment=config.APP_ENV,
                    release="dossier@1.0.0",
                    traces_sample_rate=config.OTEL_TRACES_SAMPLE_RATE,
                    send_default_pii=False,
                    before_send=_scrub,
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Sentry disabled: {exc}")

        if config.OTEL_EXPORTER_OTLP_ENDPOINT:
            try:
                from opentelemetry import trace
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                provider = TracerProvider(
                    resource=Resource.create({"service.name": config.OTEL_SERVICE_NAME})
                )
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=config.OTEL_EXPORTER_OTLP_ENDPOINT))
                )
                trace.set_tracer_provider(provider)
                if app is not None:
                    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                    FastAPIInstrumentor.instrument_app(app)
                try:
                    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

                    HTTPXClientInstrumentor().instrument()
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"OpenTelemetry disabled: {exc}")
    finally:
        _observability_ready = True
    return warnings


@contextmanager
def span(name: str, **attributes: Any):
    """Manual span for pipeline stages; a no-op without an OTel provider."""
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("dossier")
    except Exception:  # noqa: BLE001
        yield None
        return
    with tracer.start_as_current_span(name) as current_span:
        for key, value in attributes.items():
            if value is not None:
                current_span.set_attribute(key, value)
        yield current_span


# --------------------------------------------------------------- model governance metrics
_META: dict[str, Any] = {}


def model_metrics() -> dict[str, Any]:
    """Return the latest model governance snapshot (for /api/metrics or admin UI)."""
    return dict(_META)


def update_model_metrics(
    *,
    provider: str,
    model: str,
    purpose: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    ok: bool,
    trace_id: str,
) -> None:
    """Record a single LLM call's governance metadata."""
    _META.update(
        {
            "last_call_ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "purpose": purpose,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "ok": ok,
            "trace_id": trace_id,
        }
    )


def clear_model_metrics() -> None:
    _META.clear()
