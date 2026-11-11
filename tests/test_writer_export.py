from __future__ import annotations

import json

import pytest

from dossier import db, export
from dossier.llm import LLM
from dossier.pipeline.write import WriterAgent

from .conftest import make_deal


def seed_writer_deal(deal_id: str = "writer") -> str:
    make_deal(deal_id)
    summary = {
        "entity": {"name": "Acme Co", "ticker": "ACME", "sic_description": "Retail-Furniture Stores"},
        "financials": {
            "annual": [
                {"end": "2023-01-29", "revenue": 80e6, "operating_income": 8e6, "gross_margin": 0.45},
                {"end": "2024-02-04", "revenue": 90e6, "operating_income": 9e6, "gross_margin": 0.47},
                {"end": "2025-02-02", "revenue": 100e6, "operating_income": 5e6, "gross_margin": 0.5},
            ],
            "latest_quarter": {},
            "meta": {},
        },
    }
    db.x("UPDATE deals SET summary = ? WHERE id = ?", (json.dumps(summary), deal_id))
    now = db.now()
    db.insert(
        "evidence",
        {
            "id": "EV-0001", "deal_id": deal_id, "source_id": None, "kind": "metric",
            "label": "Revenue — FY2025", "value": "$100.0M", "numeric": 100e6, "unit": "USD",
            "period": "FY2025", "quote": None, "locator": "XBRL", "confidence": 0.95,
            "tags": json.dumps(["revenue"]), "created_at": now,
        },
    )
    db.insert(
        "evidence",
        {
            "id": "EV-0002", "deal_id": deal_id, "source_id": None, "kind": "metric",
            "label": "Gross margin — FY2025", "value": "50.0%", "numeric": 0.5, "unit": "ratio",
            "period": "FY2025", "quote": None, "locator": "XBRL", "confidence": 0.95,
            "tags": json.dumps(["gross_margin"]), "created_at": now,
        },
    )
    return deal_id


def writer(deal_id: str) -> WriterAgent:
    deal = dict(db.q1("SELECT * FROM deals WHERE id = ?", (deal_id,)))
    return WriterAgent(deal, "RUN-TEST", LLM())


def test_verify_accepts_sourced_numbers():
    deal_id = seed_writer_deal()
    w = writer(deal_id)
    trust = w._verify(
        [{"key": "financials", "blocks": [{"type": "p", "text": "Revenue was $100.0M at a 50.0% gross margin [[EV-0001]] [[EV-0002]]."}]}]
    )
    assert trust["citation_coverage"] == 100.0
    assert trust["numeric_conflicts"] == []
    assert trust["broken_citations"] == []
    assert trust["score"] >= 95


def test_verify_flags_conflicting_number_and_broken_reference():
    deal_id = seed_writer_deal()
    w = writer(deal_id)
    trust = w._verify(
        [
            {
                "key": "financials",
                "blocks": [
                    {"type": "p", "text": "Revenue was $140.0M [[EV-0001]]."},
                    {"type": "p", "text": "Uncited assertion about the market."},
                    {"type": "p", "text": "A claim citing nothing real [[EV-9999]]."},
                ],
            }
        ]
    )
    assert len(trust["numeric_conflicts"]) == 1
    assert len(trust["broken_citations"]) == 1
    assert trust["citation_coverage"] < 70


def test_citation_map_is_ordered_and_unique():
    doc = {
        "sections": [
            {"blocks": [{"type": "p", "text": "A [[EV-0002]] and [[EV-0001]]."}, {"type": "ul", "items": ["B [[EV-0002]]"]}]}
        ]
    }
    cmap = export.citation_map(doc)
    assert cmap == {"EV-0002": 1, "EV-0001": 2}


def _seed_document(deal_id: str) -> None:
    doc = {
        "title": "Acme Co — Information Memorandum",
        "subtitle": "Confidential Information Memorandum",
        "company": "Acme Co",
        "ticker": "ACME",
        "client": "Test Client",
        "deal_type": "sell_side",
        "date": "2026-01-01",
        "status": "draft",
        "sections": [
            {
                "key": "executive_summary",
                "title": "Executive Summary",
                "blocks": [
                    {"type": "p", "text": "**Highlights.** Revenue was $100.0M [[EV-0001]]."},
                    {"type": "ul", "items": ["A bullet [[EV-0001]]."]},
                    {"type": "callout", "title": "Not a valuation", "text": "Draft only.", "tone": "warn"},
                    {"type": "quote", "text": "We sell sofas.", "source_id": "S-01"},
                    {"type": "table", "title": "Financials", "columns": ["Line", "FY2025"], "rows": [{"label": "Revenue", "values": ["$100.0M"], "ev": "EV-0001"}], "note": "Source: XBRL."},
                    {"type": "chart", "title": "Revenue", "series": [{"name": "Revenue", "kind": "bar", "values": [80e6, 100e6], "labels": ["FY2024", "FY2025"]}]},
                    {"type": "risk", "risk_id": "R-01", "title": "Margin risk", "category": "financial", "severity": 4, "likelihood": 4, "score": 16, "label": "Critical", "text": "Margins compressed [[EV-0002]].", "mitigant": "Cost reset.", "ev": ["EV-0002"]},
                ],
            }
        ],
        "trust": {"score": 97, "citation_coverage": 100.0, "claims": 2, "sourced_claims": 2, "ledger_size": 2},
        "stats": {"words": 20, "sections": 1, "pages_estimate": 1},
    }
    db.insert(
        "sources",
        {"id": "S-01", "deal_id": deal_id, "kind": "sec_filing", "title": "10-K", "content": "text", "meta": "{}", "fetched_at": db.now()},
    )
    db.insert(
        "documents",
        {
            "id": "DOC-01", "deal_id": deal_id, "title": doc["title"], "status": "draft", "version": 1,
            "sections": json.dumps(doc), "trust": json.dumps(doc["trust"]),
            "created_at": db.now(), "updated_at": db.now(),
        },
    )


@pytest.mark.parametrize("fmt,prefix", [("docx", b"PK"), ("pdf", b"%PDF"), ("xlsx", b"PK")])
def test_exports_produce_readable_files(fmt, prefix):
    deal_id = seed_writer_deal()
    _seed_document(deal_id)
    fn = {"docx": export.to_docx, "pdf": export.to_pdf, "xlsx": export.to_xlsx}[fmt]
    data, name = fn(deal_id)
    assert data.startswith(prefix)
    assert name.endswith(f".{fmt}")
    assert len(data) > 5000
