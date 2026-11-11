"""Document exports: DOCX, print-ready PDF and the Excel diligence workbook.

Citations stay visible in every format. In DOCX/PDF they are rendered as
superscript numbers and expanded into a numbered evidence register at the end,
so a reviewer can trace each claim without the app.
"""
from __future__ import annotations

import io
import re
from datetime import date

from . import db
from .pipeline.write import CITE_RE

EV_RE = CITE_RE
_WS = re.compile(r"[ \t]+")


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\u00a0", " ")).strip()


def load_document(deal_id: str) -> dict:
    row = db.q1("SELECT sections FROM documents WHERE deal_id = ? ORDER BY updated_at DESC LIMIT 1", (deal_id,))
    if not row:
        raise LookupError("No document has been generated for this deal")
    doc = db.loads(row["sections"], None)
    if isinstance(doc, str):
        doc = db.loads(doc, {})
    if not doc:
        raise LookupError("The stored document is empty")
    return doc


def load_context(deal_id: str) -> dict:
    deal = db.q1("SELECT * FROM deals WHERE id = ?", (deal_id,))
    evidence = []
    for row in db.q("SELECT * FROM evidence WHERE deal_id = ? ORDER BY id", (deal_id,)):
        d = dict(row)
        d["tags"] = db.loads(d.get("tags"), [])
        evidence.append(d)
    sources = {r["id"]: dict(r) for r in db.q("SELECT * FROM sources WHERE deal_id = ?", (deal_id,))}
    risks = []
    for row in db.q("SELECT * FROM risks WHERE deal_id = ? ORDER BY id", (deal_id,)):
        d = dict(row)
        for k in ("evidence_ids", "mitigants", "questions"):
            d[k] = db.loads(d.get(k), [])
        risks.append(d)
    summary = db.loads(deal["summary"], {}) if deal else {}
    if isinstance(summary, str):
        summary = db.loads(summary, {})
    return {
        "deal": dict(deal) if deal else {},
        "evidence": evidence,
        "ev": {e["id"]: e for e in evidence},
        "sources": sources,
        "risks": risks,
        "summary": summary,
    }


def citation_map(doc: dict) -> dict[str, int]:
    order: dict[str, int] = {}
    for sec in doc.get("sections", []):
        for block in sec.get("blocks", []):
            for text in _block_texts(block):
                for ref in EV_RE.findall(text):
                    if ref not in order:
                        order[ref] = len(order) + 1
            for ref in str(block.get("ev") or "").replace(" ", "").split(","):
                if EV_RE.fullmatch(ref or "") and ref not in order:
                    order[ref] = len(order) + 1
    return order


def _block_texts(block: dict) -> list[str]:
    out = []
    if block.get("type") == "p":
        out.append(block.get("text", ""))
    elif block.get("type") in ("ul",):
        out.extend(block.get("items", []))
    elif block.get("type") == "callout":
        out.append(block.get("text", ""))
    elif block.get("type") == "risk":
        out.append(block.get("text", ""))
    return out


def _replace_citations(text: str, cmap: dict[str, int]) -> list[tuple[str, str | None]]:
    """Split text into (fragment, ref-number-or-None) runs for rich rendering."""
    parts: list[tuple[str, str | None]] = []
    pos = 0
    for m in EV_RE.finditer(text):
        if m.start() > pos:
            parts.append((text[pos : m.start()], None))
        parts.append(("", str(cmap.get(m.group(1), "?"))))
        pos = m.end()
    if pos < len(text):
        parts.append((text[pos:], None))
    return [(t, r) for t, r in parts if t or r]


def _tokens(text: str, cmap: dict[str, int]) -> list[tuple[str, str | None, bool]]:
    """Like _replace_citations, but also honours **bold** spans."""
    out: list[tuple[str, str | None, bool]] = []
    for fragment, ref in _replace_citations(text, cmap):
        if ref:
            out.append(("", ref, False))
            continue
        for i, chunk in enumerate(fragment.split("**")):
            if chunk:
                out.append((chunk, None, i % 2 == 1))
    return out


def register_rows(doc: dict, ctx: dict) -> list[dict]:
    cmap = citation_map(doc)
    rows = []
    for ev_id, n in sorted(cmap.items(), key=lambda kv: kv[1]):
        e = ctx["ev"].get(ev_id) or {}
        src = ctx["sources"].get(e.get("source_id") or "") or {}
        quote = _clean(e.get("quote") or "") or _clean(e.get("value") or "")
        rows.append(
            {
                "n": n,
                "id": ev_id,
                "label": _clean(e.get("label") or ev_id),
                "quote": quote,
                "source": _clean(src.get("title") or e.get("locator") or ""),
                "url": src.get("url") or "",
                "locator": _clean(e.get("locator") or ""),
            }
        )
    return rows


# ------------------------------------------------------------------------- DOCX
def to_docx(deal_id: str) -> tuple[bytes, str]:
    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc_data = load_document(deal_id)
    ctx = load_context(deal_id)
    cmap = citation_map(doc_data)
    company = doc_data.get("company") or ctx["deal"].get("company") or ctx["deal"].get("name", "Company")
    ink = RGBColor(0x1A, 0x1A, 0x1A)
    muted = RGBColor(0x6B, 0x72, 0x80)
    accent = RGBColor(0x0F, 0x76, 0x6E)

    document = Document()
    sec = document.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    for side in ("top_margin", "bottom_margin"):
        setattr(sec, side, Inches(0.9))
    sec.left_margin = sec.right_margin = Inches(1.0)

    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Georgia"
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = ink
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.18
    for name, size in (("Heading 1", 18), ("Heading 2", 13)):
        st = styles[name]
        st.font.name = "Arial"
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = accent if name == "Heading 1" else ink
        st.paragraph_format.space_before = Pt(18 if name == "Heading 1" else 12)
        st.paragraph_format.space_after = Pt(8)

    def footer_number(paragraph):
        run = paragraph.add_run()
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), "PAGE")
        run._r.addnext(fld)

    foot = sec.footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = foot.add_run(f"{company} · Confidential · Dossier   |   ")
    fr.font.size = Pt(8)
    fr.font.color.rgb = muted
    footer_number(foot)

    def para(text: str, *, style: str | None = None, size: float | None = None, italic=False, bold=False,
             color=None, space_after=7, indent=None, align=None) -> None:
        p = document.add_paragraph(style=style)
        if align is not None:
            p.alignment = align
        if space_after is not None:
            p.paragraph_format.space_after = Pt(space_after)
        if indent is not None:
            p.paragraph_format.left_indent = Inches(indent)
        for fragment, ref, is_bold in _tokens(_clean(text), cmap):
            run = p.add_run(fragment)
            if size:
                run.font.size = Pt(size)
            run.italic = italic
            run.bold = bold or is_bold
            if color:
                run.font.color.rgb = color
            if ref:
                sup = p.add_run(ref)
                sup.font.superscript = True
                sup.font.size = Pt(7.5)
                sup.font.color.rgb = accent

    def shade(cell, hex_fill: str) -> None:
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), hex_fill)
        tcPr.append(shd)

    # --- cover
    for _ in range(3):
        document.add_paragraph()
    para("CONFIDENTIAL INFORMATION MEMORANDUM", size=9, bold=True, color=accent, space_after=10)
    t = document.add_paragraph()
    tr = t.add_run(company)
    tr.font.name = "Arial"
    tr.font.size = Pt(30)
    tr.font.bold = True
    tr.font.color.rgb = ink
    para(doc_data.get("subtitle") or "Draft for discussion", size=13, italic=True, color=muted, space_after=4)
    if doc_data.get("ticker"):
        para(doc_data["ticker"], size=10, color=muted)
    document.add_paragraph()
    meta = document.add_table(rows=2, cols=2)
    meta.alignment = WD_TABLE_ALIGNMENT.LEFT
    vals = [
        ("Prepared for", doc_data.get("client") or "Client"),
        ("Prepared by", f"Dossier · {date.today().isoformat()}"),
        ("Transaction", (doc_data.get("deal_type") or "sell-side").replace("_", "-")),
        ("Status", "Draft — not for distribution"),
    ]
    for i, (k, v) in enumerate(vals):
        cell = meta.cell(i % 2, i // 2)
        cell.text = ""
        p = cell.paragraphs[0]
        r = p.add_run(k.upper())
        r.font.size = Pt(7.5)
        r.font.bold = True
        r.font.color.rgb = muted
        p2 = cell.add_paragraph()
        r2 = p2.add_run(str(v))
        r2.font.size = Pt(9.5)
    for _ in range(4):
        document.add_paragraph()
    trust = doc_data.get("trust") or {}
    para(
        "This document was assembled by Dossier from public filings and news. Every factual sentence is "
        f"citation-gated against an evidence ledger ({trust.get('ledger_size', len(ctx['evidence']))} items). "
        "It is a draft for professional review, not valuation, legal, tax or investment advice. Management "
        "information has not been independently verified.",
        size=8.5,
        color=muted,
    )
    document.add_page_break()

    # --- table of contents
    document.add_heading("Contents", level=1)
    for i, s in enumerate(doc_data.get("sections", []), 1):
        para(f"{i:02d}   {s.get('title', '')}", size=10, space_after=4)
    document.add_page_break()

    # --- body
    for si, section in enumerate(doc_data.get("sections", [])):
        document.add_heading(section.get("title", ""), level=1)
        for block in section.get("blocks", []):
            kind = block.get("type")
            if kind == "p":
                para(block.get("text", ""))
            elif kind == "ul":
                if block.get("title"):
                    para(block["title"], bold=True, space_after=3)
                for item in block.get("items", []):
                    p = document.add_paragraph(style="List Bullet")
                    p.paragraph_format.space_after = Pt(3)
                    for fragment, ref, is_bold in _tokens(_clean(item), cmap):
                        run = p.add_run(fragment)
                        run.bold = is_bold
                        if ref:
                            sup = p.add_run(ref)
                            sup.font.superscript = True
                            sup.font.size = Pt(7.5)
                            sup.font.color.rgb = accent
            elif kind == "callout":
                tbl = document.add_table(rows=1, cols=1)
                cell = tbl.cell(0, 0)
                shade(cell, "FEF3C7" if block.get("tone") == "warn" else "EEF6F5")
                cell.text = ""
                p = cell.paragraphs[0]
                r = p.add_run(block.get("title", ""))
                r.bold = True
                r.font.size = Pt(9.5)
                p2 = cell.add_paragraph()
                for fragment, ref, is_bold in _tokens(_clean(block.get("text", "")), cmap):
                    r2 = p2.add_run(fragment)
                    r2.font.size = Pt(9)
                    r2.bold = is_bold
                    if ref:
                        sup = p2.add_run(ref)
                        sup.font.superscript = True
                        sup.font.size = Pt(7.5)
                        sup.font.color.rgb = accent
                document.add_paragraph().paragraph_format.space_after = Pt(4)
            elif kind == "quote":
                para(f"“{_clean(block.get('text', ''))}”", italic=True, indent=0.35, size=10)
            elif kind == "table":
                cols = block.get("columns") or []
                if block.get("title"):
                    para(block["title"], bold=True, size=10, space_after=3)
                tbl = document.add_table(rows=1, cols=len(cols))
                tbl.style = "Light Grid Accent 1"
                for j, c in enumerate(cols):
                    cell = tbl.rows[0].cells[j]
                    cell.text = ""
                    r = cell.paragraphs[0].add_run(_clean(str(c)))
                    r.bold = True
                    r.font.size = Pt(8.5)
                for row in block.get("rows", []):
                    cells = tbl.add_row().cells
                    values = [row.get("label", "")] + list(row.get("values", []))
                    for j, v in enumerate(values[: len(cols)]):
                        cells[j].text = ""
                        p = cells[j].paragraphs[0]
                        r = p.add_run(_clean(str(v)))
                        r.font.size = Pt(8.5)
                        if j == 0:
                            r.bold = True
                if block.get("note"):
                    para(block["note"], size=8, italic=True, color=muted, space_after=8)
                else:
                    document.add_paragraph().paragraph_format.space_after = Pt(4)
            elif kind == "risk":
                tbl = document.add_table(rows=1, cols=1)
                cell = tbl.cell(0, 0)
                shade(cell, "F3F4F6")
                cell.text = ""
                p = cell.paragraphs[0]
                r = p.add_run(f"{block.get('risk_id','')}  ·  {block.get('title','')}")
                r.bold = True
                r.font.size = Pt(10)
                r2 = p.add_run(f"   {block.get('label','')}  {block.get('severity')}×{block.get('likelihood')} = {block.get('score')}/25")
                r2.font.size = Pt(8)
                r2.font.color.rgb = accent
                p2 = cell.add_paragraph()
                for fragment, ref, is_bold in _tokens(_clean(block.get("text", "")), cmap):
                    run = p2.add_run(fragment)
                    run.font.size = Pt(9)
                    run.bold = is_bold
                    if ref:
                        sup = p2.add_run(ref)
                        sup.font.superscript = True
                        sup.font.size = Pt(7)
                        sup.font.color.rgb = accent
                if block.get("mitigant"):
                    p3 = cell.add_paragraph()
                    rr = p3.add_run("Mitigant: ")
                    rr.bold = True
                    rr.font.size = Pt(8.5)
                    rr2 = p3.add_run(_clean(block["mitigant"]))
                    rr2.font.size = Pt(8.5)
                document.add_paragraph().paragraph_format.space_after = Pt(4)
            elif kind == "chart":
                para(block.get("title", ""), bold=True, size=10, space_after=3)
                series = block.get("series") or []
                labels = (series[0].get("labels") if series else []) or []
                if series and labels:
                    tbl = document.add_table(rows=1, cols=1 + len(labels))
                    tbl.style = "Light Grid Accent 1"
                    tbl.rows[0].cells[0].text = ""
                    tbl.rows[0].cells[0].paragraphs[0].add_run("").bold = True
                    for j, lab in enumerate(labels):
                        c = tbl.rows[0].cells[j + 1]
                        c.text = ""
                        r = c.paragraphs[0].add_run(str(lab))
                        r.bold = True
                        r.font.size = Pt(8)
                    for s in series:
                        cells = tbl.add_row().cells
                        cells[0].text = ""
                        cells[0].paragraphs[0].add_run(str(s.get("name", ""))).font.size = Pt(8)
                        for j, v in enumerate((s.get("values") or [])[: len(labels)]):
                            cells[j + 1].text = ""
                            cells[j + 1].paragraphs[0].add_run(
                                "—" if v is None else (f"{v:,.1f}%" if s.get("kind") == "line" else _short_money(v))
                            ).font.size = Pt(8)
                document.add_paragraph().paragraph_format.space_after = Pt(4)

    # --- evidence register
    document.add_page_break()
    document.add_heading("Evidence Register", level=1)
    para(
        "Superscript numbers in the memorandum map to the ledger entries below. Each entry records what was "
        "relied on, where it came from, and the exact locator so a reviewer can check it.",
        size=9,
        color=muted,
    )
    rows = register_rows(doc_data, ctx)
    tbl = document.add_table(rows=1, cols=4)
    tbl.style = "Light Grid Accent 1"
    for j, c in enumerate(("#", "Evidence", "Source", "Locator")):
        cell = tbl.rows[0].cells[j]
        cell.text = ""
        r = cell.paragraphs[0].add_run(c)
        r.bold = True
        r.font.size = Pt(8)
    for row in rows:
        cells = tbl.add_row().cells
        for j, v in enumerate((str(row["n"]), f"{row['id']} · {row['label']}", row["source"], row["locator"])):
            cells[j].text = ""
            p = cells[j].paragraphs[0]
            r = p.add_run(v)
            r.font.size = Pt(7.5)
            if j == 1:
                p2 = cells[j].add_paragraph()
                r2 = p2.add_run(row["quote"][:260])
                r2.font.size = Pt(7)
                r2.italic = True
                r2.font.color.rgb = muted

    buf = io.BytesIO()
    document.save(buf)
    slug = _slug(company)
    return buf.getvalue(), f"{slug}_information_memorandum.docx"


def _short_money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e9:
        return f"${v/1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:,.1f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:,.0f}K"
    return f"${v:,.0f}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "company").lower()).strip("_") or "company"


# -------------------------------------------------------------------------- PDF
def to_pdf(deal_id: str) -> tuple[bytes, str]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    doc_data = load_document(deal_id)
    ctx = load_context(deal_id)
    cmap = citation_map(doc_data)
    company = doc_data.get("company") or ctx["deal"].get("company") or "Company"

    try:
        pdfmetrics.registerFont(TTFont("DossierSerif", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"))
        pdfmetrics.registerFont(TTFont("DossierSerif-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"))
        pdfmetrics.registerFont(TTFont("DossierSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        serif, serif_b, sans = "DossierSerif", "DossierSerif-Bold", "DossierSans"
    except Exception:
        serif, serif_b, sans = "Times-Roman", "Times-Bold", "Helvetica"

    accent = colors.HexColor("#0F766E")
    ink = colors.HexColor("#1A1A1A")
    muted = colors.HexColor("#6B7280")
    base = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=base["BodyText"], fontName=serif, fontSize=9.6, leading=14,
                          textColor=ink, spaceAfter=7, alignment=TA_JUSTIFY)
    small = ParagraphStyle("Small", parent=body, fontSize=7.8, leading=10.5, textColor=muted, spaceAfter=4)
    h1 = ParagraphStyle("H1", parent=base["Heading1"], fontName=sans, fontSize=16, leading=20,
                        textColor=accent, spaceBefore=14, spaceAfter=9)
    h2 = ParagraphStyle("H2", parent=base["Heading2"], fontName=sans, fontSize=10.5, leading=14,
                        textColor=ink, spaceBefore=8, spaceAfter=4)
    quote_s = ParagraphStyle("Quote", parent=body, fontName=serif, fontSize=9.2, leading=13.5,
                             leftIndent=22, rightIndent=14, textColor=muted, spaceAfter=8)
    title_s = ParagraphStyle("TitleX", parent=base["Title"], fontName=sans, fontSize=27, leading=31,
                             textColor=ink, alignment=0, spaceAfter=6)
    sub_s = ParagraphStyle("SubX", parent=body, fontSize=12, textColor=muted, alignment=0)
    kicker = ParagraphStyle("Kicker", parent=body, fontName=sans, fontSize=8.2, textColor=accent,
                            spaceAfter=14, alignment=0)

    def rich(text: str, style=body) -> Paragraph:
        out = []
        for fragment, ref, is_bold in _tokens(_clean(text), cmap):
            frag = f"<b>{_xml(fragment)}</b>" if is_bold else _xml(fragment)
            if ref:
                out.append(f'{frag}<super><font color="#0F766E" size="6.5">{ref}</font></super>')
            else:
                out.append(frag)
        return Paragraph("".join(out), style)

    header = Paragraph(
        f'<font color="#6B7280" size="7.5">{_xml(company)} · Confidential information memorandum · Dossier</font>',
        body,
    )
    footer_txt = "Draft for discussion · Not valuation, legal, tax or investment advice"

    def on_page(canv, doc):
        canv.saveState()
        canv.setStrokeColor(colors.HexColor("#E5E7EB"))
        canv.setLineWidth(0.6)
        canv.line(inch, letter[1] - 0.62 * inch, letter[0] - inch, letter[1] - 0.62 * inch)
        header.wrapOn(canv, letter[0] - 2 * inch, 20)
        header.drawOn(canv, inch, letter[1] - 0.58 * inch)
        canv.setFont(sans, 7.2)
        canv.setFillColor(muted)
        canv.drawString(inch, 0.52 * inch, footer_txt)
        canv.drawRightString(letter[0] - inch, 0.52 * inch, str(canv.getPageNumber()))
        canv.restoreState()

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch,
                            topMargin=0.85 * inch, bottomMargin=0.75 * inch,
                            title=f"{company} — Information Memorandum", author="Dossier")
    story: list = []

    # cover
    story += [
        Spacer(1, 1.5 * inch),
        Paragraph("CONFIDENTIAL INFORMATION MEMORANDUM", kicker),
        Paragraph(_xml(company), title_s),
        Paragraph(_xml(doc_data.get("subtitle") or "Draft for discussion"), sub_s),
        Spacer(1, 0.25 * inch),
        Paragraph(f'<font size="9" color="#6B7280">{_xml(doc_data.get("ticker") or "")}</font>', body),
        Spacer(1, 2.2 * inch),
    ]
    meta_rows = [
        ["Prepared for", doc_data.get("client") or "Client"],
        ["Prepared by", f"Dossier · {date.today().isoformat()}"],
        ["Transaction", (doc_data.get("deal_type") or "sell-side").replace("_", "-")],
        ["Status", "Draft — not for distribution"],
    ]
    meta = Table(
        [[Paragraph(f'<font size="7" color="#6B7280">{k.upper()}</font>', body), Paragraph(_xml(str(v)), body)] for k, v in meta_rows],
        colWidths=[1.1 * inch, 3.6 * inch],
    )
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [meta, Spacer(1, 1.1 * inch)]
    trust = doc_data.get("trust") or {}
    story += [
        Paragraph(
            "This document was assembled by Dossier from public filings and news. Every factual sentence is "
            f"citation-gated against an evidence ledger ({trust.get('ledger_size', len(ctx['evidence']))} items). "
            "It is a draft for professional review, not valuation, legal, tax or investment advice. Management "
            "information has not been independently verified.",
            small,
        ),
        PageBreak(),
    ]

    # contents
    story.append(Paragraph("Contents", h1))
    for i, s in enumerate(doc_data.get("sections", []), 1):
        story.append(Paragraph(f"{i:02d} &nbsp;&nbsp; {_xml(s.get('title',''))}", body))
    story.append(PageBreak())

    def table_flowable(block, max_width=6.5 * inch):
        cols = block.get("columns") or []
        if not cols:
            return None
        data = [[Paragraph(f"<b>{_xml(str(c))}</b>", small) for c in cols]]
        for row in block.get("rows", []):
            values = [row.get("label", "")] + list(row.get("values", []))
            data.append([Paragraph(_xml(str(v)), small) for v in values[: len(cols)]])
        t = Table(data, colWidths=[max_width / len(cols)] * len(cols), repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    for section in doc_data.get("sections", []):
        story.append(Paragraph(_xml(section.get("title", "")), h1))
        for block in section.get("blocks", []):
            kind = block.get("type")
            if kind == "p":
                story.append(rich(block.get("text", "")))
            elif kind == "ul":
                if block.get("title"):
                    story.append(Paragraph(_xml(block["title"]), h2))
                for item in block.get("items", []):
                    story.append(Paragraph(f"•&nbsp;&nbsp;{_inline_cites(item, cmap)}", body))
            elif kind == "callout":
                inner = [
                    Paragraph(f"<b>{_xml(block.get('title',''))}</b>", small),
                    Paragraph(_inline_cites(block.get("text", ""), cmap), small),
                ]
                t = Table([[inner]], colWidths=[6.5 * inch], hAlign="LEFT")
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7" if block.get("tone") == "warn" else "#EEF6F5")),
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]))
                story += [t, Spacer(1, 8)]
            elif kind == "quote":
                story.append(Paragraph(f"“{_xml(_clean(block.get('text','')))}”", quote_s))
            elif kind == "table":
                if block.get("title"):
                    story.append(Paragraph(_xml(block["title"]), h2))
                t = table_flowable(block)
                if t:
                    story.append(t)
                if block.get("note"):
                    story += [Spacer(1, 3), Paragraph(_xml(block["note"]), small), Spacer(1, 6)]
                else:
                    story.append(Spacer(1, 8))
            elif kind == "risk":
                inner = [
                    Paragraph(
                        f"<b>{_xml(block.get('risk_id',''))} · {_xml(block.get('title',''))}</b> "
                        f'<font color="#0F766E" size="7">{_xml(block.get("label", ""))} '
                        f'{block.get("severity")}×{block.get("likelihood")} = {block.get("score")}/25</font>',
                        body,
                    ),
                    rich(block.get("text", ""), small),
                ]
                if block.get("mitigant"):
                    inner.append(Paragraph(f"<b>Mitigant:</b> {_xml(block['mitigant'])}", small))
                t = Table([[inner]], colWidths=[6.5 * inch], hAlign="LEFT")
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]))
                story += [t, Spacer(1, 7)]
            elif kind == "chart":
                if block.get("title"):
                    story.append(Paragraph(_xml(block["title"]), h2))
                series = block.get("series") or []
                labels = (series[0].get("labels") if series else []) or []
                if series and labels:
                    data = [[""] + [str(l) for l in labels]]
                    for s in series:
                        vals = [_short_money(v) if s.get("kind") != "line" else ("—" if v is None else f"{v:.1f}%")
                                for v in (s.get("values") or [])[: len(labels)]]
                        data.append([str(s.get("name", ""))] + vals)
                    t = Table(data, colWidths=[1.4 * inch] + [1.02 * inch] * len(labels), hAlign="LEFT")
                    t.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
                        ("FONTSIZE", (0, 0), (-1, -1), 7),
                        ("FONTNAME", (0, 0), (-1, 0), sans),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]))
                    story += [t, Spacer(1, 8)]

    story.append(PageBreak())
    story.append(Paragraph("Evidence Register", h1))
    story.append(Paragraph(
        "Superscript numbers in the memorandum map to these ledger entries. Each records what was relied on, "
        "where it came from, and the locator needed to check it.",
        small,
    ))
    for row in register_rows(doc_data, ctx):
        story.append(Paragraph(
            f'<super><font color="#0F766E" size="6.5">{row["n"]}</font></super> '
            f'<b>{_xml(row["id"])}</b> · {_xml(row["label"])} — <i>{_xml(row["quote"][:220])}</i><br/>'
            f'<font size="7" color="#6B7280">{_xml(row["source"])}{(" · " + _xml(row["locator"])) if row["locator"] else ""}</font>',
            small,
        ))

    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue(), f"{_slug(company)}_information_memorandum.pdf"


def _xml(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_cites(text: str, cmap: dict[str, int]) -> str:
    return "".join(
        (
            f'{_xml(fragment)}<super><font color="#0F766E" size="6.5">{ref}</font></super>'
            if ref
            else (f"<b>{_xml(fragment)}</b>" if is_bold else _xml(fragment))
        )
        for fragment, ref, is_bold in _tokens(_clean(text), cmap)
    )


# ------------------------------------------------------------------------- XLSX
def to_xlsx(deal_id: str) -> tuple[bytes, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table as XLTable
    from openpyxl.worksheet.table import TableStyleInfo

    ctx = load_context(deal_id)
    deal = ctx["deal"]
    summary = ctx["summary"]
    company = deal.get("company") or deal.get("name") or "Company"
    wb = Workbook()

    accent = "0F766E"
    head_fill = PatternFill("solid", fgColor=accent)
    head_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="E5E7EB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def sheet(title: str, headers: list[str], rows: list[list], widths: list[int], freeze="A2") -> None:
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = border
        for r in rows:
            ws.append(["" if v is None else v for v in r])
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border = border
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        ws.freeze_panes = freeze
        if rows:
            ref = f"A1:{get_column_letter(len(headers))}{len(rows)+1}"
            t = XLTable(displayName=re.sub(r"\W", "", title) + "Tbl", ref=ref)
            t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
            ws.add_table(t)

    # Risk register
    risk_rows = []
    for r in ctx["risks"]:
        rating = "Critical" if r["score"] >= 16 else "High" if r["score"] >= 12 else "Moderate" if r["score"] >= 6 else "Low"
        risk_rows.append([
            r["id"], r["category"], r["title"], r["severity"], r["likelihood"], r["score"], rating,
            r.get("description") or "", r.get("exposure") or "",
            "; ".join(r.get("mitigants") or []), "; ".join(r.get("questions") or []),
            ", ".join(r.get("evidence_ids") or []), round(float(r.get("confidence") or 0), 2), r.get("provenance") or "",
        ])
    sheet(
        "Risk register",
        ["ID", "Category", "Risk", "Severity (1-5)", "Likelihood (1-5)", "Score (/25)", "Rating",
         "Rationale", "Exposure", "Mitigants", "Diligence questions", "Evidence", "Confidence", "Provenance"],
        risk_rows,
        [8, 14, 38, 12, 13, 10, 10, 70, 18, 46, 46, 26, 10, 16],
    )
    ws = wb["Risk register"]
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
        for cell in row:
            if cell.value == "Critical":
                cell.font = Font(color="B91C1C", bold=True)
            elif cell.value == "High":
                cell.font = Font(color="C2410C", bold=True)

    # Evidence ledger
    ev_rows = []
    for e in ctx["evidence"]:
        src = ctx["sources"].get(e.get("source_id") or "") or {}
        ev_rows.append([
            e["id"], e["kind"], e["label"], e.get("value") or "", e.get("numeric"), e.get("unit") or "",
            e.get("period") or "", e.get("quote") or "", e.get("locator") or "",
            src.get("title") or "", src.get("url") or "", ", ".join(e.get("tags") or []),
            round(float(e.get("confidence") or 0), 2),
        ])
    sheet(
        "Evidence ledger",
        ["ID", "Kind", "Label", "Value", "Numeric", "Unit", "Period", "Quote", "Locator", "Source", "URL", "Tags", "Confidence"],
        ev_rows,
        [9, 14, 38, 30, 14, 8, 12, 80, 26, 40, 46, 24, 10],
    )

    # Financials
    annual = (summary.get("financials") or {}).get("annual") or []
    fin_rows = []
    keys = [
        ("revenue", "Revenue", "$M"), ("gross_profit", "Gross profit", "$M"),
        ("gross_margin", "Gross margin", "%"), ("operating_income", "Operating income", "$M"),
        ("operating_margin", "Operating margin", "%"), ("net_income", "Net income", "$M"),
        ("net_margin", "Net margin", "%"), ("ocf", "Operating cash flow", "$M"),
        ("capex", "Capital expenditure", "$M"), ("fcf", "Free cash flow", "$M"),
        ("cash", "Cash", "$M"), ("inventory", "Inventory", "$M"),
        ("assets", "Total assets", "$M"), ("liabilities", "Total liabilities", "$M"),
        ("equity", "Shareholders' equity", "$M"), ("debt", "Debt", "$M"),
        ("shares", "Shares outstanding", "M"),
    ]
    for key, label, unit in keys:
        row = [label, unit]
        for c in annual:
            v = c.get(key)
            if v is None:
                row.append(None)
            elif unit == "$M":
                row.append(round(v / 1e6, 2))
            elif unit == "%":
                row.append(round(v * 100, 2))
            else:
                row.append(round(v / 1e6, 3))
        fin_rows.append(row)
    fy_headers = [f"FY{str(c.get('end',''))[:4]}" for c in annual]
    sheet(
        "Financials",
        ["Line item", "Unit"] + fy_headers,
        fin_rows,
        [28, 8] + [14] * max(1, len(annual)),
    )

    # Sources
    src_rows = [
        [s["id"], s.get("kind") or "", s.get("form") or "", s.get("title") or "", s.get("publisher") or "",
         s.get("published_at") or "", s.get("url") or "", s.get("locator") or ""]
        for s in ctx["sources"].values()
    ]
    sheet("Sources", ["ID", "Kind", "Form", "Title", "Publisher", "Published", "URL", "Locator"],
          src_rows, [8, 12, 10, 60, 18, 14, 60, 26])

    # Trust
    doc = load_document(deal_id)
    trust = doc.get("trust") or {}
    stats = doc.get("stats") or {}
    trust_rows = [
        ["Trust score", trust.get("score")],
        ["Citation coverage (%)", trust.get("citation_coverage")],
        ["Claims checked", trust.get("claims")],
        ["Claims sourced", trust.get("sourced_claims")],
        ["Numeric checks", trust.get("numeric_checks")],
        ["Numeric conflicts", len(trust.get("numeric_conflicts") or [])],
        ["Broken citations", len(trust.get("broken_citations") or [])],
        ["Unsupported paragraphs", len(trust.get("unsupported") or [])],
        ["Evidence ledger size", trust.get("ledger_size")],
        ["Draft words", stats.get("words")],
        ["Sections", stats.get("sections")],
        ["Generated", doc.get("date")],
    ]
    sheet("Trust", ["Check", "Value"], trust_rows, [34, 22])

    del wb["Sheet"]
    wb.active = 0
    ws = wb["Risk register"]
    ws.sheet_view.showGridLines = False

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"{_slug(company)}_diligence_workbook.xlsx"
