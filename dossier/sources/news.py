"""News retrieval via Google News RSS (no API key, works headless)."""
from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .. import config
from ..textutil import html_to_text, normalize

CACHE = config.CACHE_DIR / "news"
CACHE.mkdir(parents=True, exist_ok=True)


def _cache(url: str) -> Path:
    return CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".xml")


def _get(url: str, max_age_min: int = 30) -> str | None:
    p = _cache(url)
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_min * 60:
        return p.read_text(encoding="utf-8", errors="ignore")
    for attempt in range(3):
        if attempt:
            time.sleep(min(1.2 * (2 ** (attempt - 1)), 5.0))
        try:
            r = httpx.get(
                url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0 (compatible; DossierResearch/1.0)"},
                follow_redirects=True,
            )
            if r.status_code == 200 and r.text.strip():
                p.write_text(r.text, encoding="utf-8", errors="ignore")
                return r.text
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
        except Exception:  # noqa: BLE001 - retried below
            continue
    return None


def _rss_url(query: str) -> str:
    from urllib.parse import quote_plus

    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )


def search(query: str, limit: int = 15, max_age_min: int = 45) -> list[dict]:
    xml = _get(_rss_url(query), max_age_min)
    if not xml:
        return []
    items: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = item.findtext("description") or ""
        src_el = item.find("{*}source")
        source = (src_el.text if src_el is not None else "") or ""
        if not title:
            continue
        published = None
        if pub:
            try:
                published = parsedate_to_datetime(pub).astimezone(timezone.utc).date().isoformat()
            except Exception:
                published = None
        snippet = html_to_text(desc)
        # Google description is often just a link list; strip the title echo.
        snippet = re.sub(re.escape(title), "", snippet).strip()
        items.append(
            {
                "title": re.sub(r"\s+-\s+[^-]+$", "", title).strip(),
                "url": link,
                "published_at": published,
                "publisher": source.strip(),
                "snippet": snippet[:500],
            }
        )
    # de-dupe within result set
    seen, out = set(), []
    for it in items:
        key = normalize(it["title"])[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def gather(company: str, ticker: str, limit: int = 26) -> list[dict]:
    queries = [
        f'"{company}"',
        f'{ticker} stock',
        f'"{company}" (lawsuit OR investigation OR regulator)',
        f'"{company}" (CEO OR executive OR layoffs OR recall)',
    ]
    seen: set[str] = set()
    articles: list[dict] = []
    for q in queries:
        for item in search(q, limit=12):
            key = normalize(item["title"])[:110]
            if key in seen:
                continue
            seen.add(key)
            item["query"] = q
            articles.append(item)
    # Relevance gate: Google matches "LOVE" to unrelated stories, so require the
    # company name (or an unambiguous name token) in the headline or snippet.
    name_norm = normalize(company)
    tokens = [t for t in name_norm.split() if len(t) > 3]
    filtered = []
    for a in articles:
        hay = normalize(a["title"] + " " + a.get("snippet", ""))
        if name_norm in hay or any(t in hay for t in tokens):
            filtered.append(a)
    filtered.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    return filtered[:limit]
