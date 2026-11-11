"""SEC EDGAR: entity resolution, filing retrieval and XBRL financial facts."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

from .. import config
from ..obs import CircuitBreaker, CircuitOpen
from ..textutil import html_to_text, normalize, split_sections

SEC = "https://www.sec.gov"
DATA = "https://data.sec.gov"
_last_call = 0.0
_BREAKER = CircuitBreaker("sec_edgar", threshold=5, cooldown=90.0)


def _throttle() -> None:
    global _last_call
    delta = time.time() - _last_call
    if delta < config.SEC_RATE_LIMIT_SECONDS:
        time.sleep(config.SEC_RATE_LIMIT_SECONDS - delta)
    _last_call = time.time()


def _cache_path(url: str, suffix: str = ".txt") -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()
    d = config.CACHE_DIR / "sec"
    d.mkdir(parents=True, exist_ok=True)
    return d / (h + suffix)


def fetch(url: str, max_age_hours: float = 24 * 7) -> str:
    p = _cache_path(url)
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_hours * 3600:
        return p.read_text(encoding="utf-8", errors="ignore")
    try:
        _BREAKER.before_call()
    except CircuitOpen as exc:
        raise RuntimeError(f"SEC access temporarily suspended: {exc}") from exc
    headers = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    attempts = 3
    last: Exception | None = None
    with httpx.Client(timeout=40, headers=headers, follow_redirects=True) as client:
        for attempt in range(attempts):
            _throttle()
            try:
                r = client.get(url)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"SEC returned {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                text = r.text
                p.write_text(text, encoding="utf-8", errors="ignore")
                _BREAKER.record_success()
                return text
            except Exception as exc:  # noqa: BLE001 - retried with backoff
                last = exc
                if attempt < attempts - 1:
                    time.sleep(min(1.5 * (2**attempt), 8.0))
    _BREAKER.record_failure()
    raise RuntimeError(f"SEC fetch failed for {url}: {last}") from last


def fetch_json(url: str, max_age_hours: float = 24 * 7) -> Any:
    p = _cache_path(url, ".json")
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_hours * 3600:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    text = fetch(url, max_age_hours=max_age_hours)
    data = json.loads(text)
    p.write_text(json.dumps(data), encoding="utf-8")
    return data


def tickers() -> list[dict]:
    data = fetch_json(f"{SEC}/files/company_tickers.json", max_age_hours=24 * 3)
    return list(data.values())


def resolve(query: str) -> dict | None:
    """Resolve a ticker, CIK or company name to an EDGAR registrant."""
    q = (query or "").strip()
    if not q:
        return None
    rows = tickers()
    if re.fullmatch(r"\d{1,10}", q):
        return _resolve_cik(int(q), rows)
    up = q.upper()
    for r in rows:
        if r["ticker"].upper() == up:
            return _pack(r)
    # exact-ish name, then fuzzy
    best, score = None, 0.0
    nq = normalize(q)
    for r in rows:
        nt = normalize(r["title"])
        s = SequenceMatcher(None, nq, nt).ratio()
        if nq in nt:
            s = max(s, 0.92)
        if s > score:
            best, score = r, s
    if best and score >= 0.74:
        return _pack(best)
    return None


def _pack(r: dict) -> dict:
    cik = str(r["cik_str"]).zfill(10)
    return {"cik": cik, "cik_int": int(r["cik_str"]), "ticker": r["ticker"], "name": r["title"]}


def _resolve_cik(cik: int, rows: list[dict]) -> dict:
    for r in rows:
        if int(r["cik_str"]) == cik:
            return _pack(r)
    sub = submissions(cik)
    return {"cik": str(cik).zfill(10), "cik_int": cik, "ticker": (sub.get("tickers") or [""])[0], "name": sub.get("name", "")}


def submissions(cik: int | str) -> dict:
    c = str(cik).zfill(10)
    return fetch_json(f"{DATA}/submissions/CIK{c}.json", max_age_hours=12)


def companyfacts(cik: int | str) -> dict:
    c = str(cik).zfill(10)
    try:
        return fetch_json(f"{DATA}/api/xbrl/companyfacts/CIK{c}.json", max_age_hours=24 * 3)
    except Exception:
        return {"facts": {}}


def filing_index(sub: dict, forms: tuple[str, ...], limit: int = 3, since_days: int | None = None) -> list[dict]:
    recent = sub.get("filings", {}).get("recent", {})
    out = []
    today = date.today()
    for i in range(len(recent.get("form", []))):
        form = recent["form"][i]
        if form not in forms:
            continue
        fdate = recent["filingDate"][i]
        if since_days:
            try:
                if (today - date.fromisoformat(fdate)).days > since_days:
                    continue
            except ValueError:
                pass
        out.append(
            {
                "form": form,
                "filing_date": fdate,
                "report_date": recent.get("reportDate", [""] * len(recent["form"]))[i],
                "accession": recent["accessionNumber"][i],
                "primary_doc": recent["primaryDocument"][i],
                "description": (recent.get("primaryDocDescription") or [""] * len(recent["form"]))[i],
                "items": (recent.get("items") or [""] * len(recent["form"]))[i],
            }
        )
        if len(out) >= limit:
            break
    return out


def filing_url(cik: int | str, accession: str, doc: str) -> str:
    c = str(cik).lstrip("0")
    return f"{SEC}/Archives/edgar/data/{c}/{accession.replace('-', '')}/{doc}"


def sections_from_filing(html: str, form: str) -> dict[str, tuple[str, int]]:
    text = html_to_text(html)
    return split_sections(text, form)


def latest_period(sub: dict, form: str) -> str:
    rows = filing_index(sub, (form,), limit=1)
    return rows[0]["report_date"] if rows else ""


# ---------------------------------------------------------------- XBRL series

DURATION_TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    "interest": ["InterestExpense", "InterestExpenseNonoperating"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
}
INSTANT_TAGS = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "inventory": ["InventoryNet", "RetailRelatedInventoryMerchandise", "InventoryFinishedGoodsNetOfReserves"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermLineOfCredit", "DebtLongtermAndShorttermCombinedAmount"],
    "shares": ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"],
    "goodwill": ["Goodwill"],
}
DEI_TAGS = {"shares": ["EntityCommonStockSharesOutstanding"]}


def _entries(facts: dict, tag: str) -> list[dict]:
    if "facts" in facts and isinstance(facts["facts"], dict):
        facts = facts["facts"]
    node = facts.get("us-gaap", {}).get(tag) or facts.get("dei", {}).get(tag)
    if not node:
        return []
    units = node.get("units", {})
    unit = "USD" if "USD" in units else ("shares" if "shares" in units else next(iter(units), ""))
    return [dict(e, _unit=unit) for e in units.get(unit, [])]


def series(facts: dict, tags: list[str], annual: bool = True, instant: bool = False) -> list[dict]:
    best: dict[str, dict] = {}
    for tag in tags:
        for e in _entries(facts, tag):
            end = e.get("end")
            if not end:
                continue
            if instant:
                if e.get("start"):
                    continue
            else:
                start = e.get("start")
                if not start:
                    continue
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                except ValueError:
                    continue
                if annual and not (330 <= days <= 400):
                    continue
                if not annual and not (70 <= days <= 110):
                    continue
            try:
                val = float(e["val"])
            except (TypeError, ValueError):
                continue
            prev = best.get(end)
            if prev is None or e.get("filed", "") > prev.get("filed", ""):
                best[end] = {
                    "end": end,
                    "start": e.get("start"),
                    "value": val,
                    "tag": tag,
                    "form": e.get("form"),
                    "fy": e.get("fy"),
                    "fp": e.get("fp"),
                    "filed": e.get("filed"),
                    "unit": e.get("_unit"),
                }
    return sorted(best.values(), key=lambda r: r["end"])


def financial_series(facts: dict, annual: bool = True) -> dict[str, list[dict]]:
    out = {}
    for key, tags in DURATION_TAGS.items():
        s = series(facts, tags, annual=annual, instant=False)
        if s:
            out[key] = s
    for key, tags in INSTANT_TAGS.items():
        s = series(facts, tags, annual=annual, instant=True)
        if s:
            out[key] = s
    return out


def annual_columns(series_map: dict[str, list[dict]], years: int = 4) -> list[dict]:
    """Build fiscal-year columns keyed by period end, newest last."""
    ends: list[str] = []
    for key in ("revenue", "net_income", "operating_income"):
        for row in series_map.get(key, []):
            if row["end"] not in ends:
                ends.append(row["end"])
    ends = sorted(ends)[-years:]
    cols = []
    for end in ends:
        col: dict[str, Any] = {"end": end}
        for key, rows in series_map.items():
            match = [r for r in rows if r["end"] == end]
            if match:
                col[key] = match[0]["value"]
                col[key + "_tag"] = match[0]["tag"]
        cols.append(col)
    return cols


def find_employees(text: str) -> int | None:
    # "917 full-time associates and 965 part-time associates"
    m = re.search(
        r"([\d,]{2,7})\s+full[- ]time\s+(?:associates|employees)[^.]{0,80}?([\d,]{2,7})\s+part[- ]time\s+(?:associates|employees)",
        text,
        re.I,
    )
    if m:
        try:
            return int(m.group(1).replace(",", "")) + int(m.group(2).replace(",", ""))
        except ValueError:
            pass
    patterns = [
        r"(?:approximately|about|roughly)\s+([\d,]{3,7})\s+(?:full[- ]time\s+)?(?:employees|associates|team members)",
        r"([\d,]{3,7})\s+(?:full[- ]time\s+)?(?:employees|associates)",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None
