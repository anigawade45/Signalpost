"""Core domain models, availability enums, evidence, and claim structures."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class AvailabilityStatus(str, Enum):
    """The six required official availability statuses."""
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class SourceType(str, Enum):
    """Source hierarchy categories from most authoritative to candidate discovery."""
    OFFICIAL_REGISTRY = "official_registry"
    OFFICIAL_REPORT = "official_report"
    VERIFIED_COMPANY_WEBSITE = "verified_company_website"
    OTHER_PERMITTED_PUBLIC = "other_permitted_public"
    DISCOVERY_CANDIDATE = "discovery_candidate"


# Source rank: higher integer = higher trust by default
DEFAULT_SOURCE_HIERARCHY: dict[SourceType, int] = {
    SourceType.OFFICIAL_REGISTRY: 100,
    SourceType.OFFICIAL_REPORT: 90,
    SourceType.VERIFIED_COMPANY_WEBSITE: 80,
    SourceType.OTHER_PERMITTED_PUBLIC: 60,
    SourceType.DISCOVERY_CANDIDATE: 20,
}

# Special rule: For financial facts, official annual accounts report outranks basic registry snapshot
FINANCIAL_SOURCE_HIERARCHY: dict[SourceType, int] = {
    SourceType.OFFICIAL_REPORT: 100,
    SourceType.OFFICIAL_REGISTRY: 80,
    SourceType.VERIFIED_COMPANY_WEBSITE: 50,
    SourceType.OTHER_PERMITTED_PUBLIC: 40,
    SourceType.DISCOVERY_CANDIDATE: 10,
}


class Evidence(BaseModel):
    """Evidence backing a published claim with complete provenance and hash."""
    model_config = ConfigDict(extra="ignore")

    id: str
    source_url: str
    source_name: str = "Brønnøysundregistrene Enhetsregisteret"
    retrieved_at: str
    publication_date: Optional[str] = None
    reporting_period: Optional[str] = None
    supporting_text: Optional[str] = None
    note: Optional[str] = None  # Explanatory sentence or domain interpretation note
    source_type: SourceType = SourceType.OFFICIAL_REGISTRY
    content_sha256: Optional[str] = None



class Claim(BaseModel):
    """Claim representing an asserted company fact or explicit missing-data marker."""
    model_config = ConfigDict(extra="ignore")

    field: str
    value: Any = None
    availability: AvailabilityStatus
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Optional[float] = 1.0


class LegalIdentity(BaseModel):
    """Core legal identity of an entity."""
    model_config = ConfigDict(extra="ignore")

    organization_number: str
    legal_name: Optional[str] = None
    entity_type: Optional[str] = None
    entity_type_description: Optional[str] = None
    registered_status: Optional[str] = None


class CompanyProfile(BaseModel):
    """Internal normalized company profile with claims, evidence, and explicit unknowns."""
    model_config = ConfigDict(extra="ignore")

    organization_number: str
    legal_identity: LegalIdentity
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    retrieved_at: str
    requests_count: int = 1
    third_party_cost_usd: float = 0.0

    def get_claim(self, field_name: str) -> Optional[Claim]:
        for c in self.claims:
            if c.field == field_name:
                return c
        return None
