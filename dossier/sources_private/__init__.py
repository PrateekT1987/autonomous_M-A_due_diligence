"""Private data-room and regulatory source connectors.

This package provides connectors for private data-room documents,
court dockets, patent filings, UCC/lien searches, and other
non-public regulatory sources used in due diligence.

Example usage:
    from dossier.sources_private import CourtDocketConnector, SourceRegistry
    registry = SourceRegistry()
    registry.register(CourtDocketConnector)
    connector = registry.get("court-docket", "deal-123")
    results = connector.search(party_name="Acme Corp")
    evidence = connector.to_evidence_records(results)
"""
from .court_dockets import CourtDocketConnector  # noqa: F401
from .patents import PatentConnector  # noqa: F401
from .ucc_lien import UCCLienConnector  # noqa: F401
from .regulatory import RegulatoryActionConnector  # noqa: F401
from .source_connector import SourceConnector, SourceRegistry  # noqa: F401
