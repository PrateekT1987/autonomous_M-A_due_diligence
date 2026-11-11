"""Research agent: pulls filings, financial facts and news into an evidence ledger."""
from __future__ import annotations

import hashlib
import re

def evidence_id(deal_id: str, accession: str = "", locator: str = "", value: str = "", period: str = "") -> str:
    """Generate content-addressed evidence ID: EV-{sha256[:10]}}.
    The ID is deterministic based on key facts, so re-running research
    produces the same IDs rather than re-numbering.
    """
    data = f"{deal_id}|{accession}|{locator}|{value}|{period}"
    h = hashlib.sha256(data.encode()).hexdigest()[:10]
    return f"EV-{h.upper()}"
import struct
from typing import Any, Callable

from .. import config, db
from ..llm import LLM
from ..sources import news as news_src
from ..sources import sec
from ..textutil import (
    chunk_text,
    clean_quote,
    dedupe,
    find_keyword_paragraph,
    html_to_text,
    money,
    paragraphs,
    pct,
    sentences,
    truncate,
)

RISK_TOPICS: dict[str, list[str]] = {
    "Financial & liquidity": [
        "liquidity", "going concern", "cash flow", "indebtedness", "credit facility",
        "covenant", "substantial doubt", "working capital", "impairment",
    ],
    "Consumer & demand": [
        "consumer demand", "discretionary", "macroeconomic", "housing market", "inflation",
        "recession", "spending", "seasonal",
    ],
    "Supply chain & tariffs": [
        "tariff", "supply chain", "import", "manufactur", "vendor", "freight", "logistics",
        "shipping", "customs", "duty", "duties", "single source",
    ],
    "Competition & brand": ["compet", "brand", "market share", "pricing pressure", "reputation"],
    "Customer & channel": [
        "customer concentration", "largest customer", "costco", "distributor", "channel partner",
        "top ten customers", "top 10 customers", "wholesale",
    ],
    "Legal & regulatory": [
        "litigation", "lawsuit", "patent infringement", "regulat", "compliance",
        "product liability", "settlement", "consent order", "consumer protection",
    ],
    "Technology & IP": [
        "intellectual property", "trade secret", "trademark", "cybersecurity", "data privacy",
        "information security", "breach", "software",
    ],
    "People & governance": [
        "key person", "human capital", "labor", "employee", "executive", "succession",
        "founder",
    ],
    "Operations & inventory": [
        "inventory", "warehouse", "fulfillment", "obsolescence", "markdown", "shrink",
        "distribution center",
    ],
}

METRIC_LABELS = {
    "revenue": ("Revenue", "USD"),
    "gross_profit": ("Gross profit", "USD"),
    "operating_income": ("Operating income", "USD"),
    "net_income": ("Net income", "USD"),
    "ocf": ("Operating cash flow", "USD"),
    "capex": ("Capital expenditure", "USD"),
    "cash": ("Cash & equivalents", "USD"),
    "inventory": ("Inventory", "USD"),
    "equity": ("Stockholders' equity", "USD"),
    "debt": ("Debt", "USD"),
    "assets": ("Total assets", "USD"),
    "liabilities": ("Total liabilities", "USD"),
    "sga": ("SG&A expense", "USD"),
    "rnd": ("R&D expense", "USD"),
    "shares": ("Shares outstanding", "shares"),
}

GOING_CONCERN = re.compile(
    r"(substantial doubt|going concern|ability to continue as a going concern)", re.I
)
WEAKNESS = re.compile(r"(material weakness|ineffective internal control|restatement)", re.I)


def classify_topic(text: str) -> str | None:
    low = text.lower()
    best, score = None, 0
    for topic, kws in RISK_TOPICS.items():
        s = sum(1 for k in kws if k in low)
        if s > score:
            best, score = topic, s
    return best if score else None


class ResearchAgent:
    def __init__(self, deal: dict, run_id: str, llm: LLM):
        self.deal = deal
        self.deal_id = deal["id"]
        self.run_id = run_id
        self.llm = llm.bind(deal_id=self.deal_id, run_id=run_id)
        self.emitted = 0
        self._seen_quotes: set[str] = set()

    # ------------------------------------------------------------- utilities
    def emit(self, stage: str, message: str, level: str = "info", data: dict | None = None) -> None:
        db.x(
            "INSERT INTO run_events (run_id, deal_id, ts, stage, level, message, data) VALUES (?,?,?,?,?,?,?)",
            (self.run_id, self.deal_id, db.now(), stage, level, message, db.dumps(data or {})),
        )

    def summary(self) -> dict:
        row = db.q1("SELECT summary FROM deals WHERE id = ?", (self.deal_id,))
        return db.loads(row["summary"], {}) if row else {}

    def save_summary(self, summary: dict) -> None:
        db.x(
            "UPDATE deals SET summary = ?, updated_at = ? WHERE id = ?",
            (db.dumps(summary), db.now(), self.deal_id),
        )

    def add_source(self, kind: str, title: str, content: str = "", **kw) -> str:
        sid = db.next_id(self.deal_id, "sources", "S", 2)
        db.insert(
            "sources",
            {
                "id": sid,
                "deal_id": self.deal_id,
                "kind": kind,
                "title": title,
                "publisher": kw.get("publisher"),
                "url": kw.get("url"),
                "published_at": kw.get("published_at"),
                "form": kw.get("form"),
                "accession": kw.get("accession"),
                "locator": kw.get("locator"),
                "content": content[:400000],
                "meta": db.dumps(kw.get("meta") or {}),
                "fetched_at": db.now(),
            },
        )
        return sid

    def add_evidence(
        self,
        kind: str,
        label: str,
        value: str,
        source_id: str | None,
        locator: str,
        *,
        numeric: float | None = None,
        unit: str | None = None,
        period: str | None = None,
        quote: str | None = None,
        confidence: float = 0.85,
        tags: list[str] | None = None,
    ) -> str:
        if quote:
            key = re.sub(r"\W+", " ", quote.lower())[:160]
            if key in self._seen_quotes:
                return ""
            self._seen_quotes.add(key)
        eid = evidence_id(self.deal_id, source_id or "", locator or "", value or "", period or "")
        db.insert(
            "evidence",
            {
                "id": eid,
                "deal_id": self.deal_id,
                "source_id": source_id,
                "kind": kind,
                "label": label[:240],
                "value": value,
                "numeric": numeric,
                "unit": unit,
                "period": period,
                "quote": quote,
                "locator": locator,
                "confidence": confidence,
                "tags": db.dumps(tags or []),
                "created_at": db.now(),
            },
        )
        return eid

    # ------------------------------------------------------------- resolve
    def resolve(self) -> dict:
        cfg = db.loads(self.deal["config"], {})
        ident = cfg.get("identifier") or self.deal.get("ticker") or self.deal.get("name")
        self.emit("resolve", f"Resolving target “{ident}” against SEC EDGAR")
        resolved = sec.resolve(ident)
        if not resolved:
            raise ValueError(f"Could not resolve “{ident}” in SEC EDGAR. Use a ticker or CIK.")
        sub = sec.submissions(resolved["cik_int"])
        entity = {
            **resolved,
            "name": sub.get("name", resolved["name"]),
            "sic": sub.get("sic"),
            "sic_description": sub.get("sicDescription"),
            "state": sub.get("stateOfIncorporation"),
            "fiscal_year_end": sub.get("fiscalYearEnd"),
            "category": sub.get("category"),
            "exchanges": sub.get("exchanges") or [],
            "former_names": [f.get("name") for f in (sub.get("formerNames") or [])][:4],
            "latest_fy": "",
            "latest_period": "",
        }
        tenk = sec.filing_index(sub, ("10-K",), limit=1)
        tenq = sec.filing_index(sub, ("10-Q",), limit=1)
        if tenk:
            entity["latest_fy"] = tenk[0]["report_date"]
        if tenq:
            entity["latest_period"] = tenq[0]["report_date"]
        db.x(
            "UPDATE deals SET company = ?, ticker = ?, cik = ?, sector = COALESCE(NULLIF(sector,''), ?), updated_at = ? WHERE id = ?",
            (entity["name"], entity["ticker"], entity["cik"], entity.get("sic_description") or "", db.now(), self.deal_id),
        )
        summary = self.summary()
        summary["entity"] = entity
        summary["signals"] = []
        self.save_summary(summary)
        self.emit(
            "resolve",
            f"{entity['name']} · {entity['ticker']} · CIK {entity['cik']} · {entity.get('sic_description') or 'n/a'}",
            data={"entity": entity},
        )
        return entity

    # ------------------------------------------------------------- filings
    def ingest_filings(self) -> None:
        entity = self.summary().get("entity") or {}
        cik = entity.get("cik")
        if not cik:
            raise ValueError("target not resolved")
        sub = sec.submissions(cik)
        plan = [
            ("10-K", 1, 0),
            ("10-Q", 3, 0),
            ("DEF 14A", 1, 0),
            ("8-K", 6, 540),
            ("NT 10-K", 2, 900),
            ("NT 10-Q", 2, 900),
            ("25-NSE", 1, 900),
        ]
        seen: set[str] = set()
        sources: dict[str, str] = {}
        summaries = self.summary().get("signals", [])
        for form, limit, since in plan:
            rows = sec.filing_index(sub, (form,), limit=limit, since_days=since or None)
            for row in rows:
                acc = row["accession"]
                if acc in seen:
                    continue
                seen.add(acc)
                url = sec.filing_url(cik, acc, row["primary_doc"])
                title = f"{row['form']} · filed {row['filing_date']}"
                try:
                    raw = sec.fetch(url, max_age_hours=24 * 30)
                except Exception as exc:  # noqa: BLE001
                    self.emit("filings", f"Could not retrieve {row['form']} {row['filing_date']}: {exc}", level="warn")
                    continue
                text = html_to_text(raw)
                sections = sec.sections_from_filing(raw, row["form"]) if row["form"] in ("10-K", "10-Q") else {}
                meta = {
                    "form": row["form"],
                    "filing_date": row["filing_date"],
                    "report_date": row["report_date"],
                    "items": row.get("items"),
                    "sections": {k: len(v[0]) for k, v in sections.items()},
                }
                sid = self.add_source(
                    "sec_filing",
                    title,
                    content=text,
                    publisher="SEC EDGAR",
                    url=url,
                    published_at=row["filing_date"],
                    form=row["form"],
                    accession=acc,
                    locator=f"{row['form']} filed {row['filing_date']}",
                    meta=meta,
                )
                sources[acc] = sid
                self.emit(
                    "filings",
                    f"Retrieved {row['form']} ({row['filing_date']}) · {len(text):,} chars"
                    + (f" · {len(sections)} sections" if sections else ""),
                    data={"source_id": sid, "form": row["form"]},
                )
                # keep parsed section text on the source for later agents
                if sections:
                    db.x("UPDATE sources SET content = ? WHERE id = ?", (text[:400000], sid))
                if row["form"] in ("NT 10-K", "NT 10-Q"):
                    summaries.append(
                        {
                            "type": "late_filing",
                            "text": f"{row['form']} notification filed {row['filing_date']}",
                            "evidence_hint": sid,
                            "severity": 3,
                        }
                    )
                if row["form"] == "25-NSE":
                    summaries.append(
                        {
                            "type": "delisting",
                            "text": f"Form 25-NSE filed {row['filing_date']} (exchange delisting)",
                            "evidence_hint": sid,
                            "severity": 5,
                        }
                    )
        summary = self.summary()
        summary["sources"] = {k: v for k, v in sources.items()}
        summary["signals"] = summaries
        summary["filings"] = [
            {
                "form": r["form"],
                "date": r["filing_date"],
                "report_date": r["report_date"],
                "accession": r["accession"],
                "source_id": sources.get(r["accession"]),
            }
            for r in sec.filing_index(sub, ("10-K", "10-Q", "DEF 14A", "8-K"), limit=12)
        ]
        self.save_summary(summary)

    # ------------------------------------------------------------- financials
    def ingest_financials(self) -> None:
        entity = self.summary().get("entity") or {}
        facts = sec.companyfacts(entity["cik"])
        if not facts.get("facts"):
            raise ValueError("No XBRL company facts available for this registrant")
        annual_series = sec.financial_series(facts, annual=True)
        quarterly_series = sec.financial_series(facts, annual=False)
        cols = sec.annual_columns(annual_series, years=5)
        for col in cols:
            rev = col.get("revenue")
            gp = col.get("gross_profit")
            oi = col.get("operating_income")
            ni = col.get("net_income")
            ocf = col.get("ocf")
            capex = col.get("capex")
            col["gross_margin"] = gp / rev if rev and gp is not None else None
            col["operating_margin"] = oi / rev if rev and oi is not None else None
            col["net_margin"] = ni / rev if rev and ni is not None else None
            col["sga_pct"] = col.get("sga") / rev if rev and col.get("sga") is not None else None
            col["fcf"] = ocf - capex if ocf is not None and capex is not None else None
        for i, col in enumerate(cols):
            col["revenue_growth"] = (
                (col["revenue"] - cols[i - 1]["revenue"]) / cols[i - 1]["revenue"]
                if i > 0 and col.get("revenue") and cols[i - 1].get("revenue")
                else None
            )
            col["inventory_growth"] = (
                (col["inventory"] - cols[i - 1]["inventory"]) / cols[i - 1]["inventory"]
                if i > 0 and col.get("inventory") and cols[i - 1].get("inventory")
                else None
            )

        quarters = sec.annual_columns(quarterly_series, years=2)
        latest_q = quarters[-1] if quarters else None
        prior_q = None
        if latest_q:
            for q in reversed(quarters[:-1]):
                try:
                    gap = abs(
                        (__import__("datetime").date.fromisoformat(latest_q["end"])
                         - __import__("datetime").date.fromisoformat(q["end"])).days
                    )
                except Exception:
                    gap = 0
                if 330 <= gap <= 400:
                    prior_q = q
                    break

        fy = cols[-1] if cols else {}
        prev = cols[-2] if len(cols) > 1 else {}
        meta = {
            "fiscal_year_end": entity.get("fiscal_year_end"),
            "latest_fy": entity.get("latest_fy"),
            "latest_period": entity.get("latest_period"),
            "revenue_cagr_3y": (
                (fy["revenue"] / cols[-4]["revenue"]) ** (1 / 3) - 1
                if len(cols) >= 4 and fy.get("revenue") and cols[-4].get("revenue") and cols[-4]["revenue"] > 0
                else None
            ),
        }
        summary = self.summary()
        summary["financials"] = {
            "annual": cols,
            "quarters": quarters[-6:],
            "latest_quarter": latest_q,
            "prior_year_quarter": prior_q,
            "meta": meta,
            "series_annual": annual_series,
        }
        self.save_summary(summary)
        latest = f"FY ending {fy.get('end')}: revenue {money(fy.get('revenue'))}"
        if fy.get("revenue_growth") is not None:
            latest += f" ({pct(fy['revenue_growth'])} YoY)"
        self.emit("financials", latest, data={"fy": fy.get("end"), "revenue": fy.get("revenue")})
        if latest_q:
            self.emit("financials", f"Latest quarter ending {latest_q.get('end')}: revenue {money(latest_q.get('revenue'))}")

    # ------------------------------------------------------------- news
    def ingest_news(self) -> None:
        entity = self.summary().get("entity") or {}
        company = entity.get("name") or self.deal["name"]
        ticker = entity.get("ticker") or ""
        # "Lovesac Co" → "Lovesac" for a cleaner query
        query_name = re.sub(r"\s+(Co|Inc|Corp|Corporation|Company|Ltd|plc)\.?$", "", company, flags=re.I)
        self.emit("news", f"Scanning news for “{query_name}”")
        articles = news_src.gather(query_name, ticker, limit=28)
        if not articles:
            self.emit("news", "No relevant news returned by the feed", level="warn")
        for a in articles:
            sid = self.add_source(
                "news",
                a["title"],
                content=a.get("snippet", ""),
                publisher=a.get("publisher"),
                url=a.get("url"),
                published_at=a.get("published_at"),
                locator="News article",
                meta={"query": a.get("query")},
            )
            a["source_id"] = sid
        summary = self.summary()
        summary["news"] = articles
        self.save_summary(summary)
        self.emit("news", f"{len(articles)} relevant articles retained", data={"count": len(articles)})

    # ------------------------------------------------------------- evidence
    def build_evidence(self) -> None:
        summary = self.summary()
        entity = summary.get("entity") or {}
        fin = summary.get("financials") or {}
        src_by_acc = summary.get("sources") or {}
        fy_label = self._fy_label(fin)

        # --- entity profile
        self.add_evidence(
            "entity",
            "Entity profile",
            f"{entity.get('sic_description') or 'n/a'} · {entity.get('state') or 'n/a'} · FYE {entity.get('fiscal_year_end') or 'n/a'}",
            None,
            "SEC EDGAR company submissions",
            quote=f"{entity.get('name')} · CIK {entity.get('cik')} · {', '.join(entity.get('exchanges') or []) or 'exchange n/a'}",
            confidence=1.0,
            tags=["entity"],
        )

        # --- XBRL metrics
        cols = fin.get("annual") or []
        if len(cols) >= 4 and cols[-4].get("revenue") and cols[-1].get("revenue") and cols[-4]["revenue"] > 0:
            cagr = (cols[-1]["revenue"] / cols[-4]["revenue"]) ** (1 / 3) - 1
            self.add_evidence(
                "derived",
                f"Revenue CAGR (3-year) — {fy_label or 'latest'}",
                pct(cagr),
                None,
                "Computed from SEC XBRL revenue facts over three fiscal years",
                numeric=cagr,
                unit="ratio",
                period=fy_label or "latest",
                confidence=0.95,
                tags=["derived", "revenue_cagr"],
            )
        for col in cols:
            period = self._period_label(col.get("end"))
            for key in ("revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "sga", "rnd"):
                if col.get(key) is None:
                    continue
                label, unit = METRIC_LABELS[key]
                tag = col.get(key + "_tag") or key
                self.add_evidence(
                    "metric",
                    f"{label} — {period}",
                    money(col[key], unit),
                    src_by_acc.get(self._filing_acc(summary, col.get("end"))),
                    f"SEC XBRL us-gaap:{tag} · period ended {col['end']}",
                    numeric=float(col[key]),
                    unit=unit,
                    period=period,
                    confidence=1.0,
                    tags=["xbrl", key],
                )
            for key in ("cash", "inventory", "equity", "assets", "liabilities", "debt"):
                if col.get(key) is None:
                    continue
                label, unit = METRIC_LABELS[key]
                tag = col.get(key + "_tag") or key
                self.add_evidence(
                    "metric",
                    f"{label} — {period}",
                    money(col[key], unit),
                    src_by_acc.get(self._filing_acc(summary, col.get("end"))),
                    f"SEC XBRL us-gaap:{tag} · as of {col['end']}",
                    numeric=float(col[key]),
                    unit=unit,
                    period=period,
                    confidence=1.0,
                    tags=["xbrl", key],
                )
            # derived
            derived = {
                "gross_margin": ("Gross margin", pct(col.get("gross_margin"))),
                "operating_margin": ("Operating margin", pct(col.get("operating_margin"))),
                "net_margin": ("Net margin", pct(col.get("net_margin"))),
                "revenue_growth": ("Revenue growth (YoY)", pct(col.get("revenue_growth"))),
                "fcf": ("Free cash flow", money(col.get("fcf"))),
                "inventory_growth": ("Inventory growth (YoY)", pct(col.get("inventory_growth"))),
            }
            for key, (label, val) in derived.items():
                if col.get(key) is None:
                    continue
                self.add_evidence(
                    "derived",
                    f"{label} — {period}",
                    val,
                    src_by_acc.get(self._filing_acc(summary, col.get("end"))),
                    f"Computed from SEC XBRL facts · period ended {col['end']}",
                    numeric=float(col[key]),
                    unit="ratio" if key != "fcf" else "USD",
                    period=period,
                    confidence=0.95,
                    tags=["derived", key],
                )

        latest_q = fin.get("latest_quarter")
        if latest_q:
            qlabel = self._period_label(latest_q.get("end"), quarter=True)
            self.add_evidence(
                "metric",
                f"Revenue — {qlabel}",
                money(latest_q.get("revenue")),
                None,
                f"SEC XBRL · quarter ended {latest_q.get('end')}",
                numeric=latest_q.get("revenue"),
                unit="USD",
                period=qlabel,
                confidence=1.0,
                tags=["xbrl", "revenue", "quarter"],
            )
            prior_q = fin.get("prior_year_quarter") or {}
            if latest_q.get("revenue") and prior_q.get("revenue"):
                g = (latest_q["revenue"] - prior_q["revenue"]) / prior_q["revenue"]
                self.add_evidence(
                    "derived",
                    f"Quarterly revenue growth — {qlabel}",
                    pct(g),
                    None,
                    f"Computed from SEC XBRL · quarters ended {latest_q.get('end')} and {prior_q.get('end')}",
                    numeric=g,
                    unit="ratio",
                    period=qlabel,
                    confidence=0.95,
                    tags=["derived", "quarter_growth"],
                )

        # --- narrative quotes from filings
        biz = self._section_text(summary, "10-K", "1")
        risk = self._section_text(summary, "10-K", "1A")
        mdna = self._section_text(summary, "10-K", "7")
        legal = self._section_text(summary, "10-K", "3")
        tenq_mdna = self._section_text(summary, "10-Q", "2", latest=True)
        tenq_legal = self._section_text(summary, "10-Q", "1", latest=True)
        tenq_risk = self._section_text(summary, "10-Q", "1A", latest=True)
        proxy = self._source_text(summary, "DEF 14A")

        self._quote_block(
            summary, biz, "business", "10-K Item 1 — Business",
            ["we are a", "we design", "our products", "we believe", "we offer"],
            limit=3, tags=["business"], first_paragraphs=4,
        )
        self._quote_block(
            summary, biz, "business", "10-K Item 1 — Business",
            ["omni-channel", "showroom", "website", "costco", "distribution"],
            limit=3, tags=["channel"],
        )
        self._quote_block(
            summary, biz, "customers", "10-K Item 1 — Business",
            ["customer concentration", "largest customer", "top 10", "top ten", "accounted for", "no single customer"],
            limit=2, tags=["customer"],
        )
        self._quote_block(
            summary, biz, "supply", "10-K Item 1 — Business",
            ["supplier", "manufactur", "tariff", "import", "vietnam", "china"],
            limit=2, tags=["supply"],
        )
        # employees numeric
        emp = sec.find_employees(biz or "")
        if not emp:
            tenk_full = self._source_text(summary, "10-K")
            emp = sec.find_employees(tenk_full)
        if emp:
            sid = self._source_id_for(summary, "10-K")
            self.add_evidence(
                "metric",
                "Employees",
                f"{emp:,}",
                sid,
                "10-K Item 1 — Business (headcount disclosure)",
                numeric=float(emp),
                unit="count",
                period=fy_label,
                quote=clean_quote(find_keyword_paragraph(biz, ["employees", "associates"], 500)),
                confidence=0.9,
                tags=["employees", "people"],
            )

        self._risk_quotes(summary, risk, "10-K Item 1A — Risk Factors")
        self._risk_quotes(summary, tenq_risk, "10-Q Part II Item 1A — Risk Factors", limit=3)
        self._quote_block(
            summary, mdna, "mdna", "10-K Item 7 — MD&A",
            ["liquidity", "cash flow", "capital resources", "going concern"], limit=3, tags=["liquidity"],
        )
        self._quote_block(
            summary, tenq_mdna, "mdna", "10-Q Item 2 — MD&A",
            ["tariff", "liquidity", "gross margin", "outlook"], limit=3, tags=["liquidity", "quarter"],
        )
        self._quote_block(
            summary, legal, "legal", "10-K Item 3 — Legal Proceedings",
            ["class action", "complaint", "lawsuit", "settled", "damages", "arbitration"],
            limit=3, tags=["legal"], first_paragraphs=0,
            exclude=["we are subject to legal proceedings", "we review any such", "we make a provision"],
        )
        self._quote_block(
            summary, tenq_legal, "legal", "10-Q Part II Item 1 — Legal Proceedings",
            ["class action", "complaint", "lawsuit", "settled", "damages"], limit=2, tags=["legal"],
            exclude=["we are subject to legal proceedings", "we review any such"],
        )
        self._management_quotes(summary, proxy)

        # --- news evidence
        for a in summary.get("news", []):
            self.add_evidence(
                "event",
                truncate(a.get("title", ""), 180),
                a.get("publisher") or "News",
                a.get("source_id"),
                f"{a.get('publisher') or 'News'} · {a.get('published_at') or 'undated'}",
                quote=truncate(a.get("snippet") or a.get("title", ""), 420),
                period=a.get("published_at"),
                confidence=0.7,
                tags=["news", a.get("publisher", "")],
            )

        # --- 8-K material events
        for f in summary.get("filings", []):
            if f.get("form") != "8-K" or not f.get("source_id"):
                continue
            srow = db.q1("SELECT * FROM sources WHERE id = ?", (f["source_id"],))
            if not srow:
                continue
            raw_body = srow["content"] or ""
            m = re.search(r"(?i)item\s+\d\.\d\d", raw_body)
            body = clean_quote(raw_body[m.start():] if m else raw_body, 400)
            self.add_evidence(
                "event",
                f"Form 8-K filed {f['date']}",
                "SEC filing",
                f["source_id"],
                f"8-K · filed {f['date']}",
                quote=body,
                period=f["date"],
                confidence=0.9,
                tags=["8k", "event"],
            )

        # --- internal control / restatement history
        tenk_full = self._source_text(summary, "10-K") or ""
        ctrl_sents = [
            x for x in sentences(re.sub(r"\s+", " ", tenk_full))
            if re.search(r"material weakness|restatement of (our|the) financial statements", x, re.I)
        ]
        for i, q in enumerate(dedupe(ctrl_sents, 0.75, 2)):
            self.add_evidence(
                "controls",
                "Internal controls — material weakness / restatement history" if i == 0 else "Internal controls — remediation",
                truncate(q, 200),
                self._source_id_for(summary, "10-K"),
                "10-K Item 9A — Controls and Procedures" if i == 0 else "10-K Item 1A — Risk Factors",
                quote=clean_quote(q, 800),
                confidence=0.95,
                tags=["controls", "restatement"],
            )

        # --- signals (qualitative flags)
        signals = summary.get("signals") or []
        if risk and GOING_CONCERN.search(risk):
            signals.append(
                {
                    "type": "going_concern",
                    "text": "Going-concern / substantial-doubt language appears in risk factors",
                    "severity": 5,
                }
            )
        controls_text = (risk or "") + (proxy or "")
        if re.search(r"(identified|reported|disclosed)\s+(a\s+)?material weakness|restatement of (our|the) financial statements", controls_text, re.I):
            signals.append(
                {
                    "type": "controls",
                    "text": "Material-weakness or restatement language detected",
                    "severity": 4,
                }
            )
        # de-duplicate accumulated signals
        seen, unique = set(), []
        for sig in signals:
            key = (sig.get("type"), sig.get("text"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(sig)
        summary["signals"] = unique
        summary["evidence_count"] = db.q1(
            "SELECT COUNT(*) c FROM evidence WHERE deal_id = ?", (self.deal_id,)
        )["c"]
        summary["source_count"] = db.q1(
            "SELECT COUNT(*) c FROM sources WHERE deal_id = ?", (self.deal_id,)
        )["c"]
        self.save_summary(summary)
        self.emit(
            "evidence",
            f"Evidence ledger built: {summary['evidence_count']} facts and quotes from {summary['source_count']} sources",
            data={"evidence": summary["evidence_count"], "sources": summary["source_count"]},
        )

    # ------------------------------------------------------------- helpers
    def _fy_label(self, fin: dict) -> str:
        cols = fin.get("annual") or []
        if not cols:
            return ""
        end = cols[-1].get("end", "")
        return f"FY{end[:4]}" if end else ""

    @staticmethod
    def _period_label(end: str | None, quarter: bool = False) -> str:
        if not end:
            return "latest"
        y, m = end[:4], end[5:7]
        if quarter:
            q = {"02": "Q1", "05": "Q2", "08": "Q3", "11": "Q4"}.get(m, f"Q{m}")
            return f"{q} FY{int(y)+1}"
        return f"FY{end[:4]} (ended {end})"

    @staticmethod
    def _filing_acc(summary: dict, end: str | None) -> str | None:
        if not end:
            return None
        for f in summary.get("filings", []):
            if f.get("report_date") == end:
                return f.get("accession")
        return None

    def _source_id_for(self, summary: dict, form: str, latest: bool = True) -> str | None:
        rows = [f for f in summary.get("filings", []) if f.get("form") == form and f.get("source_id")]
        if not rows:
            return None
        rows.sort(key=lambda f: f.get("date") or "", reverse=True)
        return rows[0]["source_id"]

    def _source_text(self, summary: dict, form: str, latest: bool = True) -> str:
        sid = self._source_id_for(summary, form)
        if not sid:
            return ""
        row = db.q1("SELECT content FROM sources WHERE id = ?", (sid,))
        return row["content"] if row else ""

    def _section_text(self, summary: dict, form: str, key: str, latest: bool = False) -> str:
        sid = self._source_id_for(summary, form)
        if not sid:
            return ""
        row = db.q1("SELECT content, meta FROM sources WHERE id = ?", (sid,))
        if not row:
            return ""
        sections = sec.split_sections(row["content"] or "", form)
        item = sections.get(key)
        return item[0] if item else ""

    def _quote_block(
        self,
        summary: dict,
        text: str,
        kind: str,
        locator: str,
        keywords: list[str],
        limit: int = 3,
        tags: list[str] | None = None,
        first_paragraphs: int = 0,
        exclude: list[str] | None = None,
    ) -> None:
        """Sentence-window extraction: filing HTML line-wraps are unreliable, so
        quote complete sentences instead of trusting rendered paragraph breaks."""
        if not text:
            return
        sid = None
        form = locator.split()[0]
        for f in summary.get("filings", []):
            if f.get("form") == form and f.get("source_id"):
                sid = f["source_id"]
                break
        flat = re.sub(r"\s+", " ", text)
        sents = [x.strip() for x in sentences(flat) if 70 < len(x) < 1200]
        picked: list[str] = []

        def add(start_i: int) -> None:
            quote = sents[start_i]
            if exclude and any(x in quote.lower() for x in exclude):
                return
            j = start_i + 1
            while j < len(sents) and len(quote) < 320 and len(quote) + len(sents[j]) < 780:
                quote += " " + sents[j]
                j += 1
            if re.match(r"^[A-Z0-9$\u201c]", quote) and not quote.endswith(":"):
                picked.append(clean_quote(quote, 800))

        if first_paragraphs:
            for i, sent in enumerate(sents):
                if len(picked) >= first_paragraphs:
                    break
                if len(sent) < 140 or not re.match(r"^[A-Z0-9$]", sent):
                    continue
                add(i)
        for i, sent in enumerate(sents):
            if len(picked) >= limit + first_paragraphs:
                break
            low = sent.lower()
            if not any(k.lower() in low for k in keywords):
                continue
            add(i)
        for quote in dedupe(picked, threshold=0.45, limit=limit + first_paragraphs):
            self.add_evidence(
                kind,
                truncate(f"{locator.split('—')[-1].strip()} — {self._label_from_quote(quote)}", 110),
                truncate(quote, 200),
                sid,
                f"{locator}",
                quote=quote,
                confidence=0.88,
                tags=(tags or []) + ["filing_quote"],
            )

    def _management_quotes(self, summary: dict, proxy: str) -> None:
        if not proxy:
            return
        sid = self._source_id_for(summary, "DEF 14A")
        # Locate the executive-officer table, not the director bios.
        m = None
        for cand in re.finditer(r"(?i)executive officers", proxy):
            window = proxy[cand.start(): cand.start() + 500]
            if re.search(r"(?i)(name\s+age\s+position|chief executive officer)", window):
                m = cand
                break
        section = proxy[m.start(): m.start() + 8000] if m else proxy[:8000]
        body = section
        stop = re.search(r"(?i)\b(director nominees|board of directors|corporate governance)\b", body[500:])
        if stop:
            body = body[: 500 + stop.start()]
        names = re.findall(r"(?:Mr\.|Ms\.)\s+([A-Z][a-z]+)", body)
        flat = re.sub(r"\s+", " ", body)
        sents = [x.strip() for x in sentences(flat) if 110 < len(x) < 1000]
        picked = []
        for sent in sents:
            low = sent.lower()
            if any(x in low for x in ("proxy statement", "annual meeting", "proposal", "vote", "compensation committee")):
                continue
            if not any(k in low for k in ("founded the company", "serves as", "has served as", "joined the company", "chief executive officer", "chief financial officer", "president")):
                continue
            if not any(n.lower() in low for n in ["nelson", "fox", "siegner", "mr.", "ms."]):
                continue
            picked.append(clean_quote(sent, 800))
        for quote in dedupe(picked, 0.7, 4):
            self.add_evidence(
                "management",
                f"Management — {self._label_from_quote(quote)}",
                truncate(quote, 200),
                sid,
                "DEF 14A — Executive Officers",
                quote=quote,
                confidence=0.9,
                tags=["management", "executive"],
            )

    def _risk_quotes(self, summary: dict, text: str, locator: str, limit: int = 18) -> None:
        if not text:
            return
        sid = self._source_id_for(summary, "10-K" if "10-K" in locator else "10-Q")
        flat = re.sub(r"\s+", " ", text)
        sents = [x.strip() for x in sentences(flat) if 180 < len(x) < 1200]
        per_topic: dict[str, int] = {}
        count = 0
        for i, sent in enumerate(sents):
            if count >= limit:
                break
            if not re.match(r"^[A-Z0-9$]", sent) or sent.endswith(";"):
                continue
            if "involves a high degree of risk" in sent.lower() or "numerous risks and uncertainties" in sent.lower():
                continue
            topic = classify_topic(sent)
            if not topic or per_topic.get(topic, 0) >= 3:
                continue
            quote = sent
            if i + 1 < len(sents) and len(quote) < 350 and classify_topic(sents[i + 1]) == topic:
                quote += " " + sents[i + 1]
            per_topic[topic] = per_topic.get(topic, 0) + 1
            count += 1
            self.add_evidence(
                "risk_factor",
                f"{topic} — {self._label_from_quote(quote)}",
                truncate(quote, 200),
                sid,
                f"{locator} · disclosure {count}",
                quote=clean_quote(quote, 900),
                confidence=0.9,
                tags=["risk_factor", topic],
            )

    @staticmethod
    def _label_from_quote(quote: str) -> str:
        sents = sentences(quote)
        first = sents[0] if sents else quote
        first = re.sub(r"^[^A-Za-z0-9$]+", "", first)
        return truncate(first, 90)

    # ------------------------------------------------------------- index
    def build_index(self) -> None:
        summary = self.summary()
        db.x("DELETE FROM chunks WHERE deal_id = ?", (self.deal_id,))
        rows = db.q("SELECT id, title, form, content FROM sources WHERE deal_id = ? AND kind = 'sec_filing'", (self.deal_id,))
        total = 0
        for row in rows:
            text = row["content"] or ""
            sections = sec.split_sections(text, row["form"] or "10-K")
            keep = []
            for key, label in (("1", "Business"), ("1A", "Risk Factors"), ("7", "MD&A"), ("3", "Legal Proceedings"), ("2", "MD&A")):
                if key in sections:
                    keep.append((label, sections[key][0]))
            if not keep:
                keep = [("Filing", text[:60000])]
            ordinal = 0
            for label, body in keep:
                for chunk in chunk_text(body, 1100, 120)[:80]:
                    total += 1
                    db.insert(
                        "chunks",
                        {
                            "deal_id": self.deal_id,
                            "source_id": row["id"],
                            "section": label,
                            "ordinal": ordinal,
                            "text": chunk,
                            "embedding": None,
                            "tokens": len(chunk) // 4,
                        },
                    )
                    ordinal += 1
                    if total >= 260:
                        break
        # embeddings (optional; keyword retrieval is the fallback)
        if self.llm.available():
            pending = db.q("SELECT id, text FROM chunks WHERE deal_id = ? AND embedding IS NULL LIMIT 260", (self.deal_id,))
            batch_size = 24
            done = 0
            for i in range(0, len(pending), batch_size):
                batch = pending[i : i + batch_size]
                vecs = self.llm.embed([r["text"] for r in batch])
                if not vecs:
                    break
                for r, vec in zip(batch, vecs):
                    blob = struct.pack(f"<{len(vec)}f", *vec)
                    db.x("UPDATE chunks SET embedding = ? WHERE id = ?", (blob, r["id"]))
                done += len(batch)
                self.emit("index", f"Indexed {min(done, len(pending))}/{len(pending)} passages for retrieval")
            summary["embedded_chunks"] = done
        summary["chunks"] = total
        self.save_summary(summary)
        self.emit("index", f"Retrieval index ready · {total} passages across {len(rows)} filings")

    def run(self) -> None:
        self.resolve()
        self.ingest_filings()
        self.ingest_financials()
        self.ingest_news()
        self.build_evidence()
        self.build_index()
