"""OCR and document processing for private data-room ingestion.

This module handles scanned document processing, PDF parsing, and
private-file ingestion with provenance tracking and redaction support.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Optional

from PIL import Image
import pytesseract

from .. import config
from ..obs import METRICS
from ..textutil import safe_json


class OCRError(RuntimeError):
    """OCR failed for a given image or page."""
    pass


class DataRoomSource:
    """Represents a source uploaded to a private data room.

    Attributes:
        id: Source ID in form DR-xxxx
        deal_id: Associated deal
        kind: File type (pdf, docx, xlsx, tiff, jpeg, etc.)
        title: User-provided title
        path: Filesystem path or S3 URI
        provenance: Source of upload (user, email, portal, etc.)
        redacted: Whether PII was redacted
        hash: Content hash for change detection
        metadata: Additional metadata dict
    """
    def __init__(
        self,
        deal_id: str,
        kind: str,
        title: str,
        path: str,
        provenance: str = "user",
        redacted: bool = False,
    ) -> None:
        self.id = f"DR-{hashlib.sha256(path.encode()).hexdigest()[:8].upper()}"
        self.deal_id = deal_id
        self.kind = kind
        self.title = title
        self.path = path
        self.provenance = provenance
        self.redacted = redacted
        self.hash = self._compute_hash()
        self.metadata: dict[str, Any] = {
            "uploaded_at": config.db.now(),
            "uploader": config.auth.principal().label if config.auth.principal().authenticated else "unknown",
            "file_size": Path(path).stat().st_size if Path(path).exists() else 0,
        }

    def _compute_hash(self) -> str:
        h = hashlib.sha256()
        try:
            with open(self.path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
        except FileNotFoundError:
            h.update(b"")
        return h.hexdigest()[:16]

    def extract_text(self) -> str:
        """Extract text from the document using appropriate method."""
        try:
            if self.kind == "pdf":
                return self._extract_pdf_text()
            elif self.kind in ("jpeg", "jpg", "png", "tiff", "tif"):
                return self._extract_image_text()
            elif self.kind in ("docx", "doc"):
                return self._extract_docx_text()
            elif self.kind in ("xlsx", "xls"):
                return self._extract_xlsx_text()
            else:
                # Fallback: read raw bytes
                return self._read_raw()
        except Exception as exc:  # noqa: BLE001 - reported via metrics
            METRICS.inc("dossier_ocr_failures_total", {"kind": self.kind})
            raise OCRError(f"OCR failed for {self.path}: {exc}") from exc

    def _extract_pdf_text(self) -> str:
        """Extract text from PDF using pymupdf (fitz) if available, else OCR."""
        try:
            import fitz  # pymupdf
            doc = fitz.open(self.path)
            texts = []
            for page in doc:
                texts.append(page.get_text("text"))
            return "\n".join(texts)
        except ImportError:
            # Fall back to OCR on rendered page images
            return self._extract_image_text_from_pdf()

    def _extract_image_text_from_pdf(self) -> str:
        """Render PDF pages and OCR each one."""
        try:
            import fitz
            doc = fitz.open(self.path)
            texts = []
            for page_num in range(len(doc)):
                pix = doc[page_num].get_pixmap()
                img_data = pix.pil_image()
                img_bytes = io.BytesIO()
                img_data.save(img_bytes, format="PNG")
                text = pytesseract.image_to_string(io.BytesIO(img_bytes.read()))
                texts.append(text)
            return "\n".join(texts)
        except Exception:  # noqa: BLE001
            raise OCRError("PDF OCR failed")

    def _extract_image_text(self) -> str:
        """OCR a single image file."""
        try:
            img = Image.open(self.path)
            return pytesseract.image_to_string(img)
        except Exception:  # noqa: BLE001
            raise OCRError("Image OCR failed")

    def _extract_docx_text(self) -> str:
        """Extract text from DOCX using python-docx."""
        try:
            from docx import Document
            doc = Document(self.path)
            return "\n".join([p.text for p in doc.paragraphs])
        except ImportError:  # noqa: BLE001
            raise OCRError("python-docx not installed; install requirements.lock")

    def _extract_xlsx_text(self) -> str:
        """Extract text from XLSX using openpyxl."""
        try:
            from openpyxl import load_workbook
            wb = load_workbook(self.path, data_only=True)
            texts = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value:
                            texts.append(str(cell.value))
            return "\n".join(texts)
        except ImportError:  # noqa: BLE001
            raise OCRError("openpyxl not installed; install requirements.lock")

    def _read_raw(self) -> str:
        """Read file as raw text (last resort)."""
        try:
            with open(self.path, "r", errors="ignore") as f:
                return f.read()
        except Exception:  # noqa: BLE001
            return ""

    def to_evidence_records(self, limit: int = 500) -> list[dict[str, Any]]:
        """Convert document content into evidence records for the ledger."""
        text = self.extract_text()
        records = []
        # Split into chunks and create evidence entries
        chunks = text.split("\n\n")
        for i, chunk in enumerate(chunks[:limit]):
            chunk = chunk.strip()
            if not chunk:
                continue
            records.append(
                {
                    "id": f"DR-{self.id}-EV-{i:04d}",
                    "deal_id": self.deal_id,
                    "source_id": self.id,
                    "kind": self.kind,
                    "label": f"Data-room: {self.title}",
                    "value": chunk[:500],  # truncate for ledger
                    "numeric": None,
                    "unit": None,
                    "period": None,
                    "quote": None,
                    "locator": f"data-room:{self.id}",
                    "confidence": 0.7,
                    "tags": '["data-room"]',
                    "created_at": config.db.now(),
                }
            )
        return records
