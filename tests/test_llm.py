"""Provider wiring tests: OpenRouter requests and the LLM output gates."""
from __future__ import annotations

import json

from dossier.llm import LLM


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


def test_openrouter_completion_uses_chat_endpoint(monkeypatch):
    monkeypatch.setattr("dossier.config.LLM_ENABLED", True)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(
            {
                "choices": [{"message": {"role": "assistant", "content": "verified"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            }
        )

    monkeypatch.setattr("dossier.llm.httpx.post", fake_post)
    llm = LLM(provider="openrouter", model="test/model", api_key="test-key")

    assert llm.complete("hello", system="be brief", max_tokens=50) == "verified"
    url, kwargs = calls[0]
    assert url.endswith("/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["json"]["model"] == "test/model"
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert kwargs["json"]["reasoning"] == {"effort": "none"}
    assert llm.prompt_tokens == 11
    assert llm.completion_tokens == 3


def test_openrouter_json_mode_retries_without_unsupported_format(monkeypatch):
    monkeypatch.setattr("dossier.config.LLM_ENABLED", True)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(dict(kwargs["json"]))
        if "response_format" in kwargs["json"]:
            return _Resp({"error": "response_format unsupported"}, status=400)
        return _Resp(
            {
                "choices": [{"message": {"content": '{"status": "ok"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        )

    monkeypatch.setattr("dossier.llm.httpx.post", fake_post)
    llm = LLM(provider="openrouter", model="test/model", api_key="test-key")

    assert llm.json_complete("return json") == {"status": "ok"}
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_unavailable_provider_raises_instead_of_faking(monkeypatch):
    llm = LLM(provider="openrouter", model="test/model", api_key="")
    assert llm.available() is False
    assert llm.stats()["model"] == "deterministic"
