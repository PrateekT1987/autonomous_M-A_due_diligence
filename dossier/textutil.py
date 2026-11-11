"""Text extraction, sectioning and formatting helpers."""
from __future__ import annotations

import html
import json
import re
import nh3
import unicodedata
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

ITEM_RE = re.compile(
    r"(?im)^[ \t]*item[ \t]+(\d{1,2}[A-C]?)[ \t]*[\.\-–—:]?[ \t]*([^\n]{0,140})"
)

JUNK_LINES = {
    "table of contents",
    "index",
    "—",
    "-",
    "page",
}


def html_to_text(raw: str) -> str:
    raw = raw or ""
    if "<" not in raw[:500]:
        return raw
    is_xml = raw.lstrip()[:5].lower().startswith("<?xml")
    soup = BeautifulSoup(raw, "xml" if is_xml else "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    lines: list[str] = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if line.lower() in JUNK_LINES:
            continue
        # page numbers / exhibit refs / XBRL member names
        if re.fullmatch(r"(page\s*)?\d{1,3}", line, re.I):
            continue
        if re.match(r"^[A-Z]{1,8}:[A-Za-z]", line):
            continue
        # reflow lines broken by the HTML renderer: a line that starts with a
        # lowercase word, comma or closing bracket is a continuation.
        if lines and (re.match(r"^[a-z,;\)\]]", line) or (len(line) > 30 and line[:1].islower())):
            lines[-1] = (lines[-1] + " " + line).strip()
        else:
            lines.append(line)
    return "\n".join(lines)


def split_sections(text: str, form: str = "10-K") -> dict[str, tuple[str, int]]:
    """Return {item_key: (body, char_offset)} for the substantive body, not the TOC."""
    matches = list(ITEM_RE.finditer(text))
    if not matches:
        return {}
    segments: list[tuple[str, str, int]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        key = m.group(1).upper().replace(" ", "")
        segments.append((key, body, m.start()))
    # Keep the longest segment per item key (body beats table of contents)
    best: dict[str, tuple[str, int]] = {}
    for key, body, offset in segments:
        if len(body) < 400:
            continue
        if key not in best or len(body) > len(best[key][0]):
            best[key] = (body, offset)
    # Guard against a single swapped/duplicated run
    return best


def paragraphs(text: str) -> list[str]:
    out = []
    for block in re.split(r"\n{1,}", text):
        block = block.strip()
        if len(block) < 80:
            continue
        # split very long blocks on sentence boundaries
        if len(block) > 1800:
            buf = ""
            for s in sentences(block):
                if len(buf) + len(s) > 1200 and buf:
                    out.append(buf.strip())
                    buf = ""
                buf += " " + s
            if len(buf) > 80:
                out.append(buf.strip())
        else:
            out.append(block)
    return out


ABBREVIATIONS = [
    "U.S.", "U.K.", "D.C.", "Mr.", "Mrs.", "Ms.", "Dr.", "Inc.", "Co.", "Corp.",
    "Ltd.", "No.", "vs.", "e.g.", "i.e.", "St.", "Jr.", "Sr.", "approx.", "Est.",
]


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    protected = text
    for abbr in ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", "\u25c6"))
    # single-letter initials ("Shawn D. Nelson")
    protected = re.sub(r"\b([A-Z])\.", lambda m: m.group(1) + "\u25c6", protected)
    parts = re.split(r"(?<=[\.\!\?])\s+(?=[A-Z\(“\"'])", protected)
    return [p.replace("\u25c6", ".").strip() for p in parts if len(p.strip()) > 3]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a)[:400], normalize(b)[:400]).ratio()


def dedupe(items: list[str], threshold: float = 0.86, limit: int | None = None) -> list[str]:
    out: list[str] = []
    for item in items:
        if not item or len(item) < 40:
            continue
        if any(similarity(item, seen) >= threshold for seen in out):
            continue
        out.append(item)
        if limit and len(out) >= limit:
            break
    return out


def sentence_window(text: str, keyword: str, before: int = 2, after: int = 3) -> str:
    sents = sentences(text)
    for i, s in enumerate(sents):
        if keyword.lower() in s.lower():
            return " ".join(sents[max(0, i - before) : i + after])
    return ""


def find_keyword_paragraph(text: str, keywords: list[str], maxlen: int = 1000) -> str:
    paras = paragraphs(text)
    for p in paras:
        low = p.lower()
        if any(k.lower() in low for k in keywords):
            return p[:maxlen]
    return ""


def money(value: float | None, unit: str = "USD", decimals: int = 1) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if unit.upper() in {"USD", "$"}:
        if v >= 1e9:
            return f"{sign}${v/1e9:,.{decimals}f}B"
        if v >= 1e6:
            return f"{sign}${v/1e6:,.{decimals}f}M"
        if v >= 1e3:
            return f"{sign}${v/1e3:,.{decimals}f}K"
        return f"{sign}${v:,.0f}"
    if unit.lower() in {"shares", "count"}:
        return f"{sign}{v:,.0f}"
    return f"{sign}{v:,.{decimals}f}"


def pct(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value*100:,.{decimals}f}%"


def bps(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value*10000:+,.0f} bps"


def growth(curr: float | None, prev: float | None) -> float | None:
    if curr is None or prev in (None, 0):
        return None
    if prev < 0:
        return None
    return (curr - prev) / abs(prev)


def safe_json(text: str, default=None):
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # salvage first object/array
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    return default


def truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[: cut.rfind(" ")] + "…"


def clean_quote(text: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^(?:item\s+\d+[a-c]?[\.\s:]+)", "", text, flags=re.I)
    return truncate(text, limit)


def chunk_text(text: str, size: int = 1100, overlap: int = 120) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(". ", start + int(size * 0.55), end)
            if cut != -1:
                end = cut + 1
        piece = text[start:end].strip()
        if len(piece) > 120:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
