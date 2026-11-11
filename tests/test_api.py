from __future__ import annotations

from fastapi.testclient import TestClient

from dossier.app import app


def client() -> TestClient:
    return TestClient(app)


def test_health_and_empty_deals():
    with client() as c:
        h = c.get("/api/health")
        assert h.status_code == 200
        assert h.json()["app"] == "Dossier"
        d = c.get("/api/deals")
        assert d.status_code == 200
        assert d.json()["deals"] == []


def test_create_deal_without_starting():
    with client() as c:
        r = c.post("/api/deals", json={"identifier": "LOVE", "name": "Lovesac", "client": "Acme", "start": False})
        assert r.status_code == 201
        body = r.json()
        deal_id = body["deal"]["id"]
        assert body["run_id"] is None
        assert body["deal"]["status"] == "draft"
        detail = c.get(f"/api/deals/{deal_id}")
        assert detail.status_code == 200
        assert detail.json()["name"] == "Lovesac"
        assert c.get(f"/api/deals/{deal_id}/document").status_code == 404
        assert c.get(f"/api/deals/{deal_id}/export/pdf").status_code == 404
        assert c.get(f"/api/deals/{deal_id}/risks").json()["risks"] == []


def test_validation_and_missing_deal():
    with client() as c:
        assert c.post("/api/deals", json={}).status_code == 422
        assert c.get("/api/deals/nope").status_code == 404
        assert c.get("/api/runs/nope").status_code == 404


def test_demo_creates_one_reusable_deal():
    with client() as c:
        first = c.post("/api/demo", json={"start": False}).json()
        second = c.post("/api/demo", json={"start": False}).json()
        assert first["deal_id"] == second["deal_id"]
        assert first["run_id"] is None


def test_export_formats_are_validated():
    with client() as c:
        c.post("/api/deals", json={"identifier": "X", "start": False})
        c.post("/api/demo", json={"start": False})
        r = c.get("/api/deals/lovesac/export/bogus")
        assert r.status_code == 422
