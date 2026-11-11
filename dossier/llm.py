"""Model access with graceful degradation.

Two backends are supported:

* **Ollama** (default) keeps every prompt and completion on the machine.
* **OpenRouter / OpenAI-compatible** endpoints are used when `.env` sets
  ``LLM_PROVIDER=openrouter`` plus a key. This is what the shipped `.env` does.

Every agent still produces a complete artefact without a model: the
deterministic engine is the floor, the LLM is the polish. If a provider is
unreachable or a call fails, the pipeline falls back instead of failing.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from . import config, obs
from .obs import METRICS, CircuitBreaker, CircuitOpen, digest
from .textutil import safe_json

log = logging.getLogger("dossier.llm")
_BREAKER = CircuitBreaker("llm", threshold=5, cooldown=60.0)


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
    ):
        self.provider = (provider or config.LLM_PROVIDER or "ollama").strip().lower()
        self.enabled = config.LLM_ENABLED
        self.remote = self.provider in config.REMOTE_PROVIDERS
        # Primary/secondary provider chain; empty string means "use deterministic engine"
        self._primary = self.provider or ""
        self._secondary = config.LLM_PROVIDER_SECONDARY.strip().lower() or ""
        # Constructor-provided values take precedence over config
        self._api_key_constructor = api_key  # keep the arg for fallback chain
        self._api_key_constructor_model = model  # keep the arg for fallback chain
        if self.remote:
            self.host = (host or config.OPENROUTER_BASE_URL).rstrip("/")
            # Constructor-provided key takes precedence over config
            self.api_key = api_key if api_key is not None else config.OPENROUTER_API_KEY
            # Constructor-provided model takes precedence over config
            self.model = model or config.OPENROUTER_MODEL
        else:
            self.host = (host or config.OLLAMA_HOST).rstrip("/")
            self.model = model or config.OLLAMA_MODEL
            self.api_key = ""
        self.embed_model = config.OLLAMA_EMBED_MODEL
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._available: bool | None = None
        self._fatal = False
        self.deal_id: str | None = None
        self.run_id: str | None = None
        self.trace_id: str | None = None
        self.purpose = "completion"
        self.budget_calls = int(config.LLM_MAX_CALLS or 0)
        self.budget_tokens = int(config.LLM_MAX_TOKENS or 0)

    def bind(
        self,
        *,
        deal_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        prompt_version: str | None = None,
        temperature: float | None = None,
    ) -> "LLM":
        """Attach run context so every call is recorded against the deal."""
        self.deal_id = deal_id or self.deal_id
        self.run_id = run_id or self.run_id
        self.trace_id = trace_id or self.trace_id
        self._prompt_version = prompt_version or "unknown"
        self._temperature = temperature if temperature is not None else 0.2
        return self

    # ------------------------------------------------------------------ budget
    def _check_budget(self) -> None:
        if self.budget_calls and self.calls >= self.budget_calls:
            raise LLMUnavailable(f"call budget exhausted ({self.calls}/{self.budget_calls})")
        if self.budget_tokens and (self.prompt_tokens + self.completion_tokens) >= self.budget_tokens:
            raise LLMUnavailable(
                f"token budget exhausted ({self.prompt_tokens + self.completion_tokens}/{self.budget_tokens})"
            )

    # ------------------------------------------------------------- availability
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is not None:
            return self._available
        if self.remote:
            # A configured key is enough; an auth failure will disable the
            # provider on the first real call.
            self._available = bool(self.api_key)
            return self._available
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=2.5)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            self._available = any(n.split(":")[0] == self.model.split(":")[0] for n in names)
            if not self._available and names:
                # Prefer the configured model; fall back to any instruct-ish model.
                for n in names:
                    if "embed" not in n:
                        self.model = n
                        self._available = True
                        break
        except Exception:
            self._available = False
        return bool(self._available)

    # --------------------------------------------------------------- completion
    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 700,
        temperature: float = 0.2,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        if not self.enabled:
            raise LLMUnavailable("model access disabled; set LLM_ENABLED=1 in .env")
        self._check_budget()
        self._temperature = temperature
        self._prompt_version = getattr(self, "_prompt_version", "unknown") + f"-{time.time_ns() % 1000000}"

        # Ordered provider chain: primary → secondary → deterministic (Ollama fallback)
        chain: list[str] = []
        if self._primary and self._primary not in {"", "none"}:
            chain.append(self._primary)
        if self._secondary and self._secondary not in {"", "none"}:
            chain.append(self._secondary)
        # Ollama is always available as the fallback (no API key required)
        if config.OLLAMA_HOST:
            chain.append("ollama")

        last_exc: LLMUnavailable | None = None
        for provider_key in chain:
            try:
                _BREAKER.before_call()
            except CircuitOpen as exc:
                last_exc = LLMUnavailable(str(exc))
                obs.alert("circuit_breaker_open", provider=provider_key)
                break  # skip rest of chain

            # Select provider‑specific settings
            prov = provider_key.lower()
            cfg_host = config.OPENROUTER_BASE_URL if prov in config.REMOTE_PROVIDERS else config.OLLAMA_HOST
            # Constructor-provided model takes precedence; fall back to config per provider
            cfg_model = (
                self._api_key_constructor_model
                if self._api_key_constructor_model
                else {"openrouter": config.OPENROUTER_MODEL, "ollama": config.OLLAMA_MODEL}.get(
                    prov, config.OLLAMA_MODEL
                )
            )
            # Constructor-provided API key takes precedence over config
            cfg_api = self._api_key_constructor or config.OPENROUTER_API_KEY if prov in config.REMOTE_PROVIDERS else ""

            # Temporarily swap provider so _record_call captures the right thing
            saved_provider = self.provider
            saved_remote = self.remote
            self.provider = prov
            self.remote = prov in config.REMOTE_PROVIDERS
            self.host = cfg_host.rstrip("/")
            self.model = cfg_model
            self.api_key = cfg_api if cfg_api else ""

            attempt_inner = max(1, int(config.LLM_MAX_RETRIES or 0) + 1)
            for attempt in range(attempt_inner):
                attempt_start = time.monotonic()
                try:
                    if self.remote:
                        text = self._complete_remote(
                            prompt,
                            system=system,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            json_mode=json_mode,
                            timeout=timeout or config.LLM_TIMEOUT,
                        )
                    else:
                        text = self._complete_ollama(
                            prompt,
                            system=system,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            json_mode=json_mode,
                            timeout=timeout or config.LLM_TIMEOUT,
                        )
                    latency_ms = int((time.monotonic() - attempt_start) * 1000)
                    self._record_call(
                        prompt=prompt,
                        response=text,
                        prompt_tokens=self.prompt_tokens
                        - (self.prompt_tokens
                           if False else 0),
                        completion_tokens=0,
                        latency_ms=latency_ms,
                        ok=True,
                    )
                    obs.update_model_metrics(
                        provider=self.provider,
                        model=self.model,
                        purpose=self.purpose,
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=latency_ms,
                        ok=True,
                        trace_id=self.trace_id or obs.current_trace_id(),
                    )
                    _BREAKER.record_success()
                    METRICS.observe("dossier_llm_latency_seconds", latency_ms / 1000, {"purpose": self.purpose})
                    METRICS.inc("dossier_llm_calls_total", {"ok": "true", "purpose": self.purpose})
                    # restore saved provider for subsequent calls
                    self.provider = saved_provider
                    self.remote = saved_remote
                    return text
                except LLMUnavailable as exc:
                    last_exc = exc
                    _BREAKER.record_failure()
                    METRICS.inc("dossier_llm_calls_total", {"ok": "false", "purpose": self.purpose})
                    self._record_call(
                        prompt=prompt,
                        response="",
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=int((time.monotonic() - attempt_start) * 1000),
                        ok=False,
                        error=str(exc)[:400],
                    )
                    # continue to next provider in chain on any failure
            # restore saved provider for next iteration of outer loop
            self.provider = saved_provider
            self.remote = saved_remote

        # All providers exhausted → deterministic fallback (Ollama)
        # This path is reached only when primary+secondary both failed or are disabled.
        # We call ollama directly without the circuit breaker / retry loop so the
        # deterministic engine can always produce an output even when remote hosts are down.
        try:
            text = self._complete_ollama(
                prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=json_mode,
                timeout=timeout or config.LLM_TIMEOUT,
            )
            latency_ms = int((time.monotonic() - time.monotonic()) * 1000)
            # Record a deterministic‑engine call so the audit trail is complete.
            self._record_call(
                prompt=prompt,
                response=text,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=latency_ms,
                ok=True,
            )
            obs.update_model_metrics(
                provider="deterministic",
                model=config.OLLAMA_MODEL,
                purpose=self.purpose,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=latency_ms,
                ok=True,
                trace_id=self.trace_id or obs.current_trace_id(),
            )
            METRICS.inc("dossier_llm_calls_total", {"ok": "true", "purpose": self.purpose})
            return text
        except Exception as exc:  # noqa: BLE001 - deterministic fallback must not raise
            obs.alert("llm_deterministic_failure", error=str(exc)[:300])
            raise LLMUnavailable(f"deterministic fallback failed: {exc}") from exc

    def _complete_ollama(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        timeout: float,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": 8192,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
            },
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = httpx.post(f"{self.host}/api/generate", json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001 - retried by complete()
            raise LLMUnavailable(str(exc)) from exc
        self._record_usage(
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )
        return (data.get("response") or "").strip()

    def _complete_remote(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        timeout: float,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.provider == "openrouter" and config.OPENROUTER_REASONING_EFFORT:
            payload["reasoning"] = {"effort": config.OPENROUTER_REASONING_EFFORT}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": config.OPENROUTER_REFERER,
            "X-Title": config.OPENROUTER_TITLE,
        }
        url = f"{self.host}/chat/completions"
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 400:
                # Not every OpenAI-compatible model supports JSON mode or the
                # reasoning hint; retry once without the optional fields. The
                # prompt already asks for JSON and safe_json() can recover it.
                changed = False
                for optional in ("response_format", "reasoning"):
                    if optional in payload:
                        payload.pop(optional, None)
                        changed = True
                if changed:
                    r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code in (401, 403):
                self._fatal = True
                self._available = False
                raise LLMUnavailable(f"provider rejected credentials ({r.status_code})")
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMUnavailable(f"provider returned no choices: {str(data)[:200]}")
            message = choices[0].get("message") or {}
            text = (message.get("content") or "").strip()
            if not text:
                raise LLMUnavailable("provider returned an empty completion")
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - retried by complete()
            raise LLMUnavailable(str(exc)) from exc
        usage = data.get("usage") or {}
        self._record_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return text

    def _record_usage(self, prompt_tokens: Any, completion_tokens: Any) -> None:
        self.calls += 1
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)

    def _record_call(
        self,
        *,
        prompt: str,
        response: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Persist call metadata for cost review and model governance."""
        try:
            from . import db

            tenant = "default"
            if self.deal_id:
                row = db.q1("SELECT tenant_id FROM deals WHERE id = ?", (self.deal_id,))
                if row and row["tenant_id"]:
                    tenant = row["tenant_id"]
            db.insert(
                "llm_calls",
                {
                    "ts": db.now(),
                    "tenant_id": tenant,
                    "deal_id": self.deal_id,
                    "run_id": self.run_id,
                    "provider": self.provider,
                    "model": self.model,
                    "purpose": self.purpose,
                    "prompt_version": getattr(self, "_prompt_version", "unknown"),
                    "temperature": getattr(self, "_temperature", 0.2),
                    "prompt_tokens": int(prompt_tokens or 0),
                    "completion_tokens": int(completion_tokens or 0),
                    "latency_ms": int(latency_ms or 0),
                    "ok": 1 if ok else 0,
                    "error": error,
                    "request_hash": digest(prompt),
                    "response_hash": digest(response),
                    "trace_id": self.trace_id or obs.current_trace_id(),
                },
            )
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            pass

    def json_complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 700,
        temperature: float = 0.15,
        retries: int = 1,
    ) -> Any:
        last = None
        for _ in range(retries + 1):
            raw = self.complete(
                prompt, system=system, max_tokens=max_tokens, temperature=temperature, json_mode=True
            )
            parsed = safe_json(raw)
            if parsed is not None:
                return parsed
            last = raw
        raise LLMUnavailable(f"model returned unparseable JSON: {str(last)[:200]}")

    # -------------------------------------------------------------- embeddings
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.enabled or not texts:
            return None
        # Prefer local embeddings even when completions are remote: it keeps
        # source text on the machine and avoids paying twice for retrieval.
        vecs = self._embed_ollama(texts)
        if vecs is not None:
            return vecs
        if self.remote and config.OPENROUTER_EMBED_MODEL:
            return self._embed_remote(texts)
        return None

    def _embed_ollama(self, texts: list[str]) -> list[list[float]] | None:
        try:
            r = httpx.post(
                f"{config.OLLAMA_HOST.rstrip('/')}/api/embed",
                json={"model": self.embed_model, "input": texts},
                timeout=90,
            )
            r.raise_for_status()
            emb = r.json().get("embeddings")
            if emb and len(emb) == len(texts):
                return emb
        except Exception as exc:  # noqa: BLE001
            log.info("local embeddings unavailable: %s", exc)
        return None

    def _embed_remote(self, texts: list[str]) -> list[list[float]] | None:
        try:
            r = httpx.post(
                f"{self.host}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": config.OPENROUTER_EMBED_MODEL, "input": texts},
                timeout=90,
            )
            r.raise_for_status()
            data = r.json().get("data") or []
            emb = [d.get("embedding") for d in data]
            if emb and len(emb) == len(texts):
                return emb
        except Exception as exc:  # noqa: BLE001
            log.info("remote embeddings unavailable: %s", exc)
        return None

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "provider": self.provider,
            "local": not self.remote,
            "model": self.model if self.available() else "deterministic",
        }


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)
