"""Output adapter strictly matching the official Builderr Signalpost result envelope."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from .models import AvailabilityStatus, CompanyProfile


class EnvelopeRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    started_at: str
    completed_at: str
    terminal_status: str  # "completed" | "failed"


class EnvelopeClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    value: Any = None
    availability: str
    confidence: float = 1.0
    evidence_ids: list[str] = Field(default_factory=list)


class EnvelopeEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source_url: str
    source_class: str
    retrieved_at: str
    content_sha256: Optional[str] = None
    claim_span: Optional[str] = None
    note: Optional[str] = None


class EnvelopeError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    message: str


class EnvelopeOperations(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requests: int = 0

    runtime_ms: int = 0
    third_party_cost_usd: float = 0.0


class TerminalEnvelope(BaseModel):
    """The exact official terminal envelope contract required by Builderr."""
    model_config = ConfigDict(extra="ignore")

    organisation_number: str
    run: EnvelopeRun
    claims: list[EnvelopeClaim] = Field(default_factory=list)
    evidence: list[EnvelopeEvidence] = Field(default_factory=list)
    changes: list[Any] = Field(default_factory=list)
    errors: list[Any] = Field(default_factory=list)
    operations: EnvelopeOperations

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def format_successful_envelope(
    profile: CompanyProfile,
    run_id: str,
    started_at: str,
    completed_at: str,
    requests: int = 1,
    runtime_ms: int = 0,
    cost_usd: float = 0.0,
) -> TerminalEnvelope:
    """Transform an internal CompanyProfile into the official terminal envelope."""
    claims = [
        EnvelopeClaim(
            field=c.field,
            value=c.value,
            availability=c.availability.value if isinstance(c.availability, AvailabilityStatus) else str(c.availability),
            confidence=c.confidence if c.confidence is not None else 1.0,
            evidence_ids=c.evidence_ids,
        )
        for c in profile.claims
    ]

    evidence_items = [
        EnvelopeEvidence(
            id=ev.id,
            source_url=ev.source_url,
            source_class=ev.source_type.value if hasattr(ev.source_type, "value") else str(ev.source_type),
            retrieved_at=ev.retrieved_at,
            content_sha256=ev.content_sha256,
            claim_span=ev.supporting_text,
            note=ev.note,
        )
        for ev in profile.evidence
    ]

    return TerminalEnvelope(
        organisation_number=profile.organization_number,
        run=EnvelopeRun(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            terminal_status="completed",
        ),
        claims=claims,
        evidence=evidence_items,
        changes=[],
        errors=[],
        operations=EnvelopeOperations(
            requests=requests,
            runtime_ms=runtime_ms,
            third_party_cost_usd=cost_usd,
        ),
    )


def format_failed_envelope(
    organization_number: str,
    error_message: str,
    run_id: str,
    started_at: str,
    completed_at: str,
    error_code: Optional[str] = None,
    requests: int = 1,
    runtime_ms: int = 0,
    cost_usd: float = 0.0,
) -> TerminalEnvelope:
    """Create a valid terminal envelope representing a failed company research execution."""
    if not error_code:
        if "ValueError" in error_message or "invalid" in error_message.lower():
            error_code = "INVALID_FORMAT"
        elif "NotFoundError" in error_message or "404" in error_message:
            error_code = "NOT_FOUND"
        elif "BlockedError" in error_message or "403" in error_message or "429" in error_message:
            error_code = "BLOCKED"
        elif "TransientError" in error_message or "timeout" in error_message.lower():
            error_code = "TRANSIENT_ERROR"
        elif "MalformedDataError" in error_message:
            error_code = "MALFORMED_DATA"
        else:
            error_code = "GENERIC_ERROR"

    err_item = EnvelopeError(code=error_code, message=error_message)

    return TerminalEnvelope(
        organisation_number=str(organization_number),
        run=EnvelopeRun(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            terminal_status="failed",
        ),
        claims=[],
        evidence=[],
        changes=[],
        errors=[err_item.model_dump()],
        operations=EnvelopeOperations(
            requests=requests,
            runtime_ms=runtime_ms,
            third_party_cost_usd=cost_usd,
        ),
    )

