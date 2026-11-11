"""Test environment: isolated database and no model, before dossier imports."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="dossier-tests-")
os.environ["DOSSIER_DATA"] = _tmp
os.environ["DOSSIER_DB"] = os.path.join(_tmp, "test.db")
os.environ["LLM_ENABLED"] = "0"
os.environ["SEC_RATE_LIMIT"] = "0"
os.environ["DOSSIER_RUN_MODE"] = "test"

import pytest  # noqa: E402

from dossier import db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    db.init_db()
    for table in (
        "idempotency_keys",
        "document_versions",
        "llm_calls",
        "audit_events",
        "jobs",
        "reviews",
        "run_events",
        "runs",
        "chunks",
        "documents",
        "risks",
        "evidence",
        "sources",
        "deals",
    ):
        db.x(f"DELETE FROM {table}")
    yield


def make_deal(deal_id: str = "acme", name: str = "Acme") -> str:
    now = db.now()
    db.insert(
        "deals",
        {
            "id": deal_id,
            "name": name,
            "company": None,
            "ticker": None,
            "cik": None,
            "deal_type": "sell_side",
            "sector": "",
            "client": "",
            "status": "draft",
            "created_at": now,
            "updated_at": now,
            "config": "{}",
            "summary": "{}",
        },
    )
    return deal_id
