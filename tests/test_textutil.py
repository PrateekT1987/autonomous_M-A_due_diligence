from __future__ import annotations

from dossier import db
from dossier.textutil import (
    bps,
    chunk_text,
    dedupe,
    html_to_text,
    money,
    pct,
    sentences,
    split_sections,
)

from .conftest import make_deal


def test_per_deal_ids_do_not_collide():
    make_deal("one")
    make_deal("two")
    a = db.next_id("one", "evidence", "EV", 4)
    b = db.next_id("two", "evidence", "EV", 4)
    assert a == b == "EV-0001"
    db.insert("evidence", {"id": a, "deal_id": "one", "kind": "metric", "label": "x", "created_at": db.now()})
    assert db.next_id("one", "evidence", "EV", 4) == "EV-0002"
    assert db.next_id("two", "evidence", "EV", 4) == "EV-0001"


def test_html_to_text_strips_scripts_and_entities():
    raw = "<html><body><script>x()</script><p>A&nbsp;B &amp; C</p><style>p{}</style></body></html>"
    out = html_to_text(raw)
    assert "A B & C" in out
    assert "x()" not in out
    assert "p{}" not in out


def test_sentence_split_protects_abbreviations():
    text = "Lovesac Co. reported revenue. The U.S. market grew 4.2%. It was founded in 1998."
    parts = sentences(text)
    assert parts[0].startswith("Lovesac Co. reported")
    assert "U.S. market" in parts[1]
    assert parts[-1].startswith("It was founded")


def test_section_split_and_chunking():
    body = "We sell modular sofas direct to consumers. " * 20
    text = f"ITEM 1. BUSINESS\n{body}\nITEM 1A. RISK FACTORS\n{body}\nITEM 7. MANAGEMENT'S DISCUSSION\n{body}"
    sections = split_sections(text, "10-K")
    assert "1" in sections and "1A" in sections
    assert "modular sofas" in sections["1"][0]
    chunks = chunk_text("word " * 500, size=200, overlap=20)
    assert len(chunks) > 2
    assert all(len(c) <= 220 for c in chunks)


def test_formatting_helpers():
    assert money(697_115_000) == "$697.1M"
    assert money(None) == "n/a"
    assert pct(0.564) == "56.4%"
    assert bps(0.008) == "+80 bps"
    first = "The company sells modular sofas directly to consumers across the United States."
    assert dedupe([first, first, "A completely different sentence about supply chain concentration risk."]) == [
        first,
        "A completely different sentence about supply chain concentration risk.",
    ]
