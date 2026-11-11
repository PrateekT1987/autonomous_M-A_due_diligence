"""Source connector framework for private and regulatory data ingestion.

This module defines the abstract base class and registry for all source
connectors, enabling pluggable integration of private data-room documents,
court dockets, patent filings, UCC liens, regulatory actions, and
other non-public sources used in due diligence.

The framework supports:
- Connector registration and discovery
- Provenance tracking and audit
- Content extraction and evidence conversion
- Permission and access control hooks
- Refresh and change-detection semantics
"""
from __future__ import annotations

import abc
import time
from typing import Any, Optional

from .. import config
from ..obs import METRICS


class SourceConnector(abc.ABC):
    """Abstract base class for all private/regulatory source connectors.

    Subclasses must implement:
    - `kind` property for the evidence kind label
    - `provenance` property for the source provenance label
    - `search()` for finding relevant documents
    - `to_evidence_records()` for converting results to evidence records

    Attributes:
        deal_id: Associated deal ID
        provenance: Human-readable source provenance
        connector_type: Type identifier for registry
    """
    connector_type: str = "unknown"

    def __init__(self, deal_id: str, **kwargs: Any) -> None:
        self.deal_id = deal_id
        self.connected_at = config.db.now()
        self.last_queried: Optional[float] = None
        self.query_count = 0

    @property
    @abc.abstractmethod
    def kind(self) -> str:
        """Evidence kind label for this connector type."""
        ...

    @property
    @abc.abstractmethod
    def provenance(self) -> str:
        """Human-readable provenance string."""
        ...

    @abc.abstractmethod
    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Search for relevant documents/records.

        Args:
            **kwargs: Search parameters (varies by connector type)

        Returns:
            List of raw search results
        """
        ...

    @abc.abstractmethod
    def to_evidence_records(
        self, raw_results: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Convert search results into evidence records for the ledger.

        Args:
            raw_results: Optional pre-fetched results; if None, search is performed

        Returns:
            List of evidence record dicts ready for DB insertion
        """
        ...

    def record_provenance(self, action: str, details: dict[str, Any] | None = None) -> None:
        """Record a provenance event in the audit trail.

        Args:
            action: Description of the provenance action
            details: Optional detailed metadata dict
        """
        METRICS.inc("dossier_source_connector_provenance_total", {})
        try:
            from .. import db

            principal_label = (
                config.auth.principal().label
                if config.auth.principal().authenticated
                else "system"
            )
            base_detail = {"connector_type": self.connector_type}
            detail = {**base_detail, **(details or {})}
            db.audit(
                f"source_connector.{action}",
                actor=principal_label,
                deal_id=self.deal_id,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - provenance must not break the operation
            pass

    def rate_limit(self) -> None:
        """Enforce connector-specific rate limiting.

        Override in subclasses for connector-specific throttling.
        Default implements a simple 200ms cooldown.
        """
        delta = time.time() - (self.last_queried or 0)
        if delta < 0.2:
            time.sleep(0.2 - delta)
        self.last_queried = time.time()
        self.query_count += 1


class SourceRegistry:
    """Registry for managing and discovering available source connectors.

    Tracks registered connector types, their instantiation, and
    provides a unified API for source discovery and evidence generation.
    """
    def __init__(self) -> None:
        self._connectors: dict[str, type[SourceConnector]] = {}
        self._instances: dict[str, SourceConnector] = {}

    def register(self, connector_type: type[SourceConnector]) -> None:
        """Register a connector class by its `connector_type` class attribute.

        Args:
            connector_type: The type string from the connector class
        """
        self._connectors[connector_type] = connector_type
        METRICS.inc("dossier_source_connectors_registered_total", {})

    def get(self, connector_type: str, deal_id: str, **kwargs: Any) -> SourceConnector:
        """Get or create a connector instance.

        Args:
            connector_type: The registered connector type string
            deal_id: The deal ID for the connector instance
            **kwargs: Additional constructor arguments

        Returns:
            A connector instance
        """
        key = f"{connector_type}:{deal_id}"
        if key not in self._instances:
            connector_class = self._connectors.get(connector_type)
            if not connector_class:
                raise ValueError(f"Unknown connector type: {connector_type}")
            self._instances[key] = connector_class(deal_id=deal_id, **kwargs)
        return self._instances[key]

    def all_connectors(self, deal_id: str) -> list[SourceConnector]:
        """Get all connector instances for a deal.

        Args:
            deal_id: The deal ID

        Returns:
            List of all connector instances registered for this deal
        """
        instances: list[SourceConnector] = []
        for ctype, _ in self._connectors.items():
            try:
                instances.append(cls(deal_id=deal_id))
            except Exception:  # noqa: BLE001 - non-fatal if constructor fails
                pass
        return instances

    def search_all(self, deal_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Search all registered connectors and combine results.

        Args:
            deal_id: The deal ID
            **kwargs: Search parameters passed to each connector

        Returns:
            Combined list of raw search results from all connectors
        """
        results: list[dict[str, Any]] = []
        for connector in self.all_connectors(deal_id):
            try:
                connector.rate_limit()
                results.extend(connector.search(**kwargs))
            except Exception:  # noqa: BLE001 - non-fatal if individual search fails
                pass
        return results

    def to_evidence_all(self, deal_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Convert all connector results to evidence records.

        Args:
            deal_id: The deal ID
            **kwargs: Search parameters passed to each connector

        Returns:
            Combined list of evidence records ready for DB insertion
        """
        records: list[dict[str, Any]] = []
        for connector in self.all_connectors(deal_id):
            try:
                connector.rate_limit()
                raw = connector.search(**kwargs) if kwargs else []
                records.extend(connector.to_evidence_records(raw))
            except Exception:  # noqa: BLE001 - non-fatal if individual conversion fails
                pass
        return records


# Pre-registered connector types for convenience
DEFAULT_CONNECTOR_TYPES = ["court-docket", "patent", "ucc-lien", "regulatory"]
