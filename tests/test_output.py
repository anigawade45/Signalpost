"""Tests validating that output envelopes strictly match the official Builderr contract schema."""

import json
from pathlib import Path
import pytest

from signalpost.models import AvailabilityStatus, CompanyProfile, Evidence, LegalIdentity, SourceType
from signalpost.output import (
    TerminalEnvelope,
    format_failed_envelope,
    format_successful_envelope,
)
from signalpost.registry import normalize_registry_response
from signalpost.research import build_claims_from_record

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def test_successful_envelope_schema_match() -> None:
    sample_path = SAMPLES_DIR / "equinor_sample.json"
    raw_data = json.loads(sample_path.read_text(encoding="utf-8"))
    record = normalize_registry_response(raw_data, source_url="https://data.brreg.no/enheter/984851006")

    claims, unknowns = build_claims_from_record(record, evidence_id="ev-1")
    evidence = Evidence(
        id="ev-1",
        source_url=record.registry_source_url,
        retrieved_at="2026-10-13T10:00:00Z",
        source_type=SourceType.OFFICIAL_REGISTRY,
        supporting_text="EQUINOR ASA org.nr 984851006",
        note="Consolidated group workforce unknown in registry",
        content_sha256="abc123sha",
    )

    profile = CompanyProfile(
        organization_number="984851006",
        legal_identity=LegalIdentity(
            organization_number="984851006",
            legal_name="EQUINOR ASA",
            entity_type="ASA",
            registered_status="active",
        ),
        claims=claims,
        evidence=[evidence],
        unknowns=unknowns,
        retrieved_at="2026-10-13T10:00:00Z",
    )

    envelope = format_successful_envelope(
        profile=profile,
        run_id="eval-smoke-001",
        started_at="2026-10-13T10:00:00Z",
        completed_at="2026-10-13T10:00:02Z",
        requests=1,
        runtime_ms=1500,
        cost_usd=0.0,
    )

    data = envelope.to_dict()

    # Verify exact top-level keys from official contract
    expected_top_keys = {
        "organisation_number",
        "run",
        "claims",
        "evidence",
        "changes",
        "errors",
        "operations",
    }
    assert set(data.keys()) == expected_top_keys
    assert data["organisation_number"] == "984851006"

    # Verify run envelope
    assert data["run"]["run_id"] == "eval-smoke-001"
    assert data["run"]["terminal_status"] == "completed"
    assert data["run"]["started_at"] == "2026-10-13T10:00:00Z"
    assert data["run"]["completed_at"] == "2026-10-13T10:00:02Z"

    # Verify claims
    assert len(data["claims"]) > 0
    for claim in data["claims"]:
        assert "field" in claim
        assert "value" in claim
        assert "availability" in claim
        assert "confidence" in claim
        assert "evidence_ids" in claim
        assert claim["availability"] in {
            "available",
            "not_available",
            "blocked",
            "not_applicable",
            "ambiguous",
            "failed",
        }
        if claim["availability"] == "not_available":
            assert claim["value"] is None

    # Verify evidence
    assert len(data["evidence"]) == 1
    ev = data["evidence"][0]
    assert ev["id"] == "ev-1"
    assert ev["source_url"] == record.registry_source_url
    assert ev["source_class"] == "official_registry"
    assert ev["claim_span"] == "EQUINOR ASA org.nr 984851006"
    assert ev["note"] == "Consolidated group workforce unknown in registry"
    assert ev["content_sha256"] == "abc123sha"
    assert data["operations"]["requests"] == 1
    assert data["operations"]["runtime_ms"] == 1500
    assert data["operations"]["third_party_cost_usd"] == 0.0

    # Ensure Pydantic validates cleanly
    validated = TerminalEnvelope.model_validate(data)
    assert validated.organisation_number == "984851006"


def test_failed_envelope_schema_match() -> None:
    failed_env = format_failed_envelope(
        organization_number="invalid-123",
        error_message="ValueError: Invalid organization number",
        run_id="eval-smoke-002",
        started_at="2026-10-13T10:00:00Z",
        completed_at="2026-10-13T10:00:01Z",
        requests=1,
        runtime_ms=10,
    )

    data = failed_env.to_dict()
    assert data["organisation_number"] == "invalid-123"
    assert data["errors"] == [
        {"code": "INVALID_FORMAT", "message": "ValueError: Invalid organization number"}
    ]
    assert data["claims"] == []
    assert data["evidence"] == []


    # Validates against contract model
    validated = TerminalEnvelope.model_validate(data)
    assert validated.run.terminal_status == "failed"
