from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any, Optional

from .. import config
from ..obs import METRICS
from ..textutil import safe_json

connector_type = "court-docket"

class CourtDocketConnector:
    def __init__(self, deal_id: str, jurisdiction: str = "federal",
                 pacer_account: str | None = None) -> None:
        self.deal_id = deal_id
        self.jurisdiction = jurisdiction
        self.pacer_account = pacer_account
        self.connector_type = "court-docket"
        self._last_request = 0.0
        self._pacer_rate = 0.1

    def search_by_party(self, party_name: str,
                case_range: str | None = None) -> list[dict[str, Any]]:
        return []

    def search_by_case_number(self, case_number: str) -> Optional[dict[str, Any]]:
        return None

    def get_filing_history(self, case_number: str) -> list[dict[str, Any]]:
        return []

    def _rate_limit(self) -> None:
        delta = time.time() - self._last_request
        if delta < self._pacer_rate:
            time.sleep(self._pacer_rate - delta)
        self._last_request = time.time()
        self.query_count += 1

    def to_evidence_records(self, case_data: dict[str, Any] | None = None
                 ) -> list[dict[str, Any]]:
        records = []
        if case_data:
            records.append(
                {
                    "id": f"CT-hash here",
                    "deal_id": case_data.get("case_id", ""),
                    "source_id": f"court:{self.jurisdiction}",
                    "kind": self.kind,
                    "label": f"Court docket:",
                    "value": case_data.get("description", ""),
                    "numeric": None,
                    "unit": None,
                    "period": case_data.get("filing_date"),
                    "quote": None,
                    "locator": case_data.get("docket_url", ""),
                    "confidence": 0.8,
                    "tags": "["court-docket"]",
                    "created_at": "config.db.now()",
                }
            )
        return records

class CourtDocketError(RuntimeError):
    pass