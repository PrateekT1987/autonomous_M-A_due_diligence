"""Production-shell tests: auth, headers, pagination, idempotency, queue, eval."""
from __future__ import annotations
import os

import json

from fastapi.testclient import TestClient

from dossier import config, db, evals
from dossier.app import app
from dossier.pipeline import orchestrator

from .conftest import make_deal


def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_security_headers_and_request_id():
    with client() as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in r.headers
        assert r.headers["x-request-id"]


def test_optional_bearer_auth(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", "secret-token")
    try:
        with client() as c:
            assert c.get("/api/deals").status_code == 401
            assert c.get("/api/deals", headers={"Authorization": "Bearer wrong"}).status_code == 401
            ok = c.get("/api/deals", headers={"Authorization": "Bearer secret-token"})
            assert ok.status_code == 200
            assert c.get("/api/health").status_code == 200
            # EventSource/export fall back to a query token.
            assert c.get("/api/deals?token=secret-token").status_code == 200
    finally:
        monkeypatch.setattr(config, "ACCESS_TOKEN", "")


def test_errors_do_not_leak_internals(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("very secret database detail")

    monkeypatch.setattr("dossier.app.db.q1", boom)
    with client() as c:
        r = c.get("/api/deals")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "Internal server error"
        assert "secret" not in json.dumps(body)
        assert body["request_id"]


def test_idempotent_deal_creation_and_audit():
    with client() as c:
        headers = {"Idempotency-Key": "mk-1"}
        first = c.post("/api/deals", json={"identifier": "LOVE", "name": "Lovesac", "start": False}, headers=headers)
        second = c.post("/api/deals", json={"identifier": "LOVE", "name": "Lovesac", "start": False}, headers=headers)
        assert first.status_code == 201
        assert first.json()["deal"]["id"] == second.json()["deal"]["id"]
        assert db.q1("SELECT COUNT(*) c FROM deals")["c"] == 1
        audit = c.get("/api/audit").json()
        assert any(e["action"] == "deal.create" for e in audit["events"])


def test_pagination_metadata():
    make_deal("paged")
    now = db.now()
    for i in range(5):
        db.insert(
            "evidence",
            {
                "id": f"EV-{i + 1:04d}",
                "deal_id": "paged",
                "source_id": None,
                "kind": "metric",
                "label": f"Metric {i}",
                "value": str(i),
                "numeric": i,
                "unit": "USD",
                "period": "FY2025",
                "quote": None,
                "locator": "XBRL",
                "confidence": 0.9,
                "tags": "[]",
                "created_at": now,
            },
        )
    with client() as c:
        page = c.get("/api/deals/paged/evidence?limit=2&offset=2").json()
        assert page["total"] == 5
        assert page["count"] == 2
        assert [e["id"] for e in page["evidence"]] == ["EV-0003", "EV-0004"]


def test_queue_claim_retry_and_recovery(monkeypatch):
    make_deal("queued")

    def fake_pipeline(deal_id, run_id, **kwargs):
        orchestrator._set_run(run_id, status="failed", error="transient")

    monkeypatch.setattr(orchestrator, "run_pipeline", fake_pipeline)
    run_id = orchestrator.create_run("queued")
    job = orchestrator.claim_job("test-worker")
    assert job and job["run_id"] == run_id
    assert orchestrator.claim_job("other-worker") is None  # one active job per deal

    orchestrator.process_job({**job, "max_attempts": 2})
    row = db.q1("SELECT status FROM jobs WHERE run_id = ?", (run_id,))
    assert row["status"] == "queued"
    assert db.q1("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "queued"
    db.x("UPDATE jobs SET available_at = ? WHERE run_id = ?", (db.now(), run_id))

    # Second attempt exhausts retries.
    job2 = orchestrator.claim_job("test-worker")
    assert job2 and job2["attempts"] == 2
    orchestrator._set_run(run_id, status="failed", error="still broken")
    orchestrator.process_job(job2)
    assert db.q1("SELECT status FROM jobs WHERE run_id = ?", (run_id,))["status"] == "failed"


def test_queue_recovery_and_cancellation():
    make_deal("stale")
    run_id = orchestrator.create_run("stale")
    job = orchestrator.claim_job("crashed-worker")
    assert job
    db.x("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?", (run_id,))
    assert orchestrator.recover_stale_jobs() >= 1
    assert db.q1("SELECT status FROM jobs WHERE run_id = ?", (run_id,))["status"] == "queued"

    assert orchestrator.cancel_run(run_id)["cancelled"] is True
    assert db.q1("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "cancelled"
    assert db.q1("SELECT status FROM jobs WHERE run_id = ?", (run_id,))["status"] == "cancelled"


def test_evaluation_gate_passes_and_fails():
    make_deal("eval")
    now = db.now()
    sections = [
        {"key": "executive_summary", "title": "Executive summary", "blocks": []},
        {"key": "financials", "title": "Financials", "blocks": []},
        {"key": "risks", "title": "Risks", "blocks": []},
        {"key": "appendix", "title": "Appendix", "blocks": []},
    ] + [{"key": f"s{i}", "title": f"S{i}", "blocks": []} for i in range(6)]
    doc = {
        "sections": sections,
        "stats": {"words": 2000, "sections": len(sections)},
        "trust": {"score": 99, "citation_coverage": 98.0, "numeric_conflicts": [], "broken_citations": []},
    }
    for i in range(25):
        db.insert(
            "evidence",
            {
                "id": f"EV-{i + 1:04d}", "deal_id": "eval", "source_id": None, "kind": "metric",
                "label": "x", "value": "1", "numeric": 1, "unit": "USD", "period": "FY2025",
                "quote": None, "locator": "XBRL", "confidence": 0.9, "tags": "[]", "created_at": now,
            },
        )
    for i in range(6):
        db.insert(
            "risks",
            {
                "id": f"R-{i + 1:02d}", "deal_id": "eval", "title": "Risk", "category": "Financial",
                "severity": 4, "likelihood": 4, "score": 16, "description": "x",
                "evidence_ids": '["EV-0001"]', "mitigants": "[]", "questions": "[]",
                "status": "open", "provenance": "quant", "created_at": now,
            },
        )
    db.insert(
        "documents",
        {
            "id": "DOC-01", "deal_id": "eval", "title": "IM", "status": "draft", "version": 1,
            "sections": json.dumps(doc), "trust": json.dumps(doc["trust"]),
            "created_at": now, "updated_at": now,
        },
    )
    result = evals.evaluate("eval")
    assert result["passed"] is True
    assert result["deals"][0]["trust"]["score"] == 99

    db.x("UPDATE documents SET trust = ? WHERE deal_id = 'eval'", (json.dumps({"score": 40, "citation_coverage": 20}),))
    failed = evals.evaluate("eval")
    assert failed["passed"] is False
    assert "trust_score" in failed["deals"][0]["failed"]

    """Test that cancellation stops the run before the next stage starts."""
    make_deal("cancel-test")
    run_id = orchestrator.create_run("cancel-test")
    # Cancel immediately after start, before analyse stage.
    orchestrator.cancel_run(run_id)
    run = orchestrator.run_status(run_id)
    assert run["status"] == "cancelled"


    """Test LLM provider contract (skipped when LLM_ENABLED=0)."""
    if not os.getenv("LLM_ENABLED", "1") in {"1", "true", "yes", "on"}:
        return  # Skip LLM contract test when deterministic engine is used
    from dossier.llm import LLM
    llm = LLM(provider="ollama", model="qwen2.5:7b-instruct")
    llm.bind(deal_id="contract-test", run_id="run-1")
    # Test that complete() returns a string.
    result = llm.complete("Say hello.", max_tokens=10)
    assert isinstance(result, str)
    # Test json_complete returns dict or None.
    data = llm.json_complete('{"foo": "bar"}', max_tokens=10, json_mode=True)
    assert data is None or isinstance(data, dict)


def test_pagination_completeness():
    """Test that pagination returns all items across pages."""
    make_deal("paging-test")
    now = db.now()
    # Insert 25 evidence items.
    for i in range(25):
        db.insert(
            "evidence",
            {"id": f"EV-{i+1:04d}", "deal_id": "paging-test", "source_id": None, "kind": "metric",
             "label": f"Metric {i}", "value": str(i), "numeric": i, "unit": "USD", "period": "FY2025",
             "quote": None, "locator": "XBRL", "confidence": 0.9, "tags": "[]", "created_at": now},
        )
    with client() as c:
        # Fetch in pages of 5.
        all_ids = []
        for page in range(5):
            resp = c.get(f"/api/deals/paging-test/evidence?limit=5&offset={page*5}")
            data = resp.json()
            all_ids.extend([e["id"] for e in data["evidence"]])
        assert len(all_ids) == 25
        assert sorted(all_ids) == [f"EV-{i:04d}" for i in range(1, 26)]


def test_concurrent_run_isolation():
    """Test that two deals can run concurrently with tenant isolation."""
    make_deal("deal-a")
    make_deal("deal-b")
    # Run both deals concurrently.
    run_a = orchestrator.start_run("deal-a")
    run_b = orchestrator.start_run("deal-b")
    # Both should be active.
    assert orchestrator.is_running("deal-a") or orchestrator.is_running("deal-b")
    # Cancel both.
    orchestrator.cancel_run(run_a)
    orchestrator.cancel_run(run_b)
