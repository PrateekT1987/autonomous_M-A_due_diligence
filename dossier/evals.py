"""Quality gates for generated memoranda.

The evaluation is deterministic and reads only what is stored in the database:
citation coverage, numeric consistency, required sections, ledger size and
whether every risk block is backed by evidence. It is deliberately strict enough
to catch prompt or pipeline regressions without requiring a model call.
"""
from __future__ import annotations

import json
from typing import Any

from . import db

REQUIRED_SECTIONS = ("executive_summary", "financials", "risks", "appendix")
MIN_SECTIONS = 10
MIN_RISKS = 5
MIN_EVIDENCE = 20


def _load(value: Any, default):
    if isinstance(value, (dict, list)):
        return value
    return db.loads(value, default)


def evaluate(
    deal_id: str | None = None,
    *,
    min_trust: int = 95,
    min_coverage: float = 90.0,
    max_conflicts: int = 0,
    min_words: int = 1500,
) -> dict:
    sql = "SELECT id, name FROM deals"
    params: list[Any] = []
    if deal_id:
        sql += " WHERE id = ?"
        params.append(deal_id)
    deals = db.q(sql, params)
    results: list[dict] = []
    for deal in deals:
        results.append(
            _evaluate_deal(
                deal["id"],
                deal["name"],
                min_trust=min_trust,
                min_coverage=min_coverage,
                max_conflicts=max_conflicts,
                min_words=min_words,
            )
        )
    passed = bool(results) and all(r["passed"] for r in results)
    return {
        "passed": passed,
        "thresholds": {
            "min_trust": min_trust,
            "min_coverage": min_coverage,
            "max_conflicts": max_conflicts,
            "min_words": min_words,
        },
        "deals": results,
    }


def _evaluate_deal(
    deal_id: str,
    name: str,
    *,
    min_trust: int,
    min_coverage: float,
    max_conflicts: int,
    min_words: int,
) -> dict:
    checks: list[dict] = []

    def check(key: str, ok: bool, detail: Any) -> None:
        checks.append({"check": key, "ok": bool(ok), "detail": detail})

    row = db.q1("SELECT * FROM documents WHERE deal_id = ? ORDER BY updated_at DESC LIMIT 1", (deal_id,))
    doc = _load(row["sections"], {}) if row else {}
    trust = _load(row["trust"], {}) if row else {}
    trust = trust or doc.get("trust") or {}
    stats = doc.get("stats") or {}
    sections = doc.get("sections") or []
    keys = {s.get("key") for s in sections}
    evidence = db.q1("SELECT COUNT(*) c FROM evidence WHERE deal_id = ?", (deal_id,))["c"]
    risks = db.q1("SELECT COUNT(*) c FROM risks WHERE deal_id = ?", (deal_id,))["c"]
    unsourced = db.q1(
        "SELECT COUNT(*) c FROM risks WHERE deal_id = ? AND (evidence_ids IS NULL OR evidence_ids IN ('', '[]'))",
        (deal_id,),
    )["c"]

    check("document_exists", bool(row), "draft generated" if row else "no document")
    check("words", int(stats.get("words") or 0) >= min_words, int(stats.get("words") or 0))
    check("sections", len(sections) >= MIN_SECTIONS, len(sections))
    check(
        "required_sections",
        all(k in keys for k in REQUIRED_SECTIONS),
        sorted(k for k in REQUIRED_SECTIONS if k not in keys) or "all present",
    )
    check("trust_score", int(trust.get("score") or 0) >= min_trust, trust.get("score"))
    check(
        "citation_coverage",
        float(trust.get("citation_coverage") or 0) >= min_coverage,
        trust.get("citation_coverage"),
    )
    check(
        "numeric_conflicts",
        len(trust.get("numeric_conflicts") or []) <= max_conflicts,
        len(trust.get("numeric_conflicts") or []),
    )
    check("broken_citations", not trust.get("broken_citations"), len(trust.get("broken_citations") or []))
    check("evidence_ledger", evidence >= MIN_EVIDENCE, evidence)
    check("risk_count", risks >= MIN_RISKS, risks)
    check("risks_have_evidence", unsourced == 0, unsourced)

    failed = [c for c in checks if not c["ok"]]
    return {
        "deal_id": deal_id,
        "name": name,
        "passed": not failed,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "stats": stats,
        "trust": {
            "score": trust.get("score"),
            "citation_coverage": trust.get("citation_coverage"),
            "numeric_conflicts": len(trust.get("numeric_conflicts") or []),
            "broken_citations": len(trust.get("broken_citations") or []),
        },
    }


def render(result: dict) -> str:
    lines = [
        f"Quality evaluation · {'PASS' if result['passed'] else 'FAIL'}",
        json.dumps(result["thresholds"]),
    ]
    for deal in result["deals"]:
        lines.append("")
        lines.append(f"[{'PASS' if deal['passed'] else 'FAIL'}] {deal['deal_id']} · {deal['name']}")
        for c in deal["checks"]:
            mark = "ok  " if c["ok"] else "FAIL"
            lines.append(f"  {mark} {c['check']}: {c['detail']}")
    return "\n".join(lines)
