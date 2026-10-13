"""Tests for the Brønnøysund registry client and normalizer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import httpx
import pytest

from signalpost.registry import (
    BlockedError,
    CompanyRecord,
    MalformedDataError,
    NotFoundError,
    RegistryClient,
    RegistryError,
    TransientError,
    fetch_company_record,
    normalize_registry_response,
    validate_organization_number,
)

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


# ==============================================================================
# Validation Tests
# ==============================================================================

def test_validate_organization_number_valid() -> None:
    assert validate_organization_number("984851006") == "984851006"
    assert validate_organization_number(984851006) == "984851006"
    # Strips surrounding and internal whitespace
    assert validate_organization_number(" 984 851 006 ") == "984851006"


@pytest.mark.parametrize(
    "invalid_input",
    [
        "",
        "12345678",      # 8 digits
        "1234567890",    # 10 digits
        "abcdefghi",     # Letters
        "98485100a",     # Letter at end
        "984-851-006",   # Hyphens
        None,
    ],
)
def test_validate_organization_number_invalid(invalid_input: object) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_organization_number(invalid_input)  # type: ignore[arg-type]
    assert "must be exactly nine digits" in str(exc_info.value)


# ==============================================================================
# Normalization Tests (using saved samples)
# ==============================================================================

def test_normalize_valid_equinor_sample() -> None:
    sample_path = SAMPLES_DIR / "equinor_sample.json"
    raw_data = json.loads(sample_path.read_text(encoding="utf-8"))

    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/984851006",
        retrieved_at="2026-10-13T10:00:00Z",
    )

    assert isinstance(record, CompanyRecord)
    assert record.organization_number == "984851006"
    assert record.legal_name == "EQUINOR ASA"
    assert record.entity_type == "ASA"
    assert record.entity_type_description == "Allment aksjeselskap"
    assert record.registered_status == "active"
    assert record.is_registered is True
    assert record.is_bankrupt is False
    assert record.is_deleted is False
    assert record.industry_code == "06.100"
    assert record.industry_description == "Utvinning av råolje"
    assert record.employee_count == 23000
    assert record.website == "https://www.equinor.com"
    assert record.registry_source_url == "https://data.brreg.no/enhetsregisteret/api/enheter/984851006"
    assert record.retrieved_at == "2026-10-13T10:00:00Z"

    # Business address
    assert record.business_address is not None
    assert record.business_address.city == "STAVANGER"
    assert record.business_address.postal_code == "4035"
    assert record.business_address.street == ["Forusbeen 50"]
    assert record.business_address.country == "Norge"

    # Raw response reference for reproducibility
    assert record.raw_response == raw_data


def test_normalize_missing_fields_sample() -> None:
    """Entities with missing or deleted fields must not crash."""
    sample_path = SAMPLES_DIR / "minimal_company_sample.json"
    raw_data = json.loads(sample_path.read_text(encoding="utf-8"))

    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
    )

    assert record.organization_number == "123456789"
    assert record.legal_name == "MINIMAL ENHET"
    assert record.entity_type is None
    assert record.industry_code is None
    assert record.business_address is None
    assert record.postal_address is None
    assert record.website is None
    assert record.employee_count is None
    assert record.raw_response == raw_data


def test_normalize_deleted_sample() -> None:
    sample_path = SAMPLES_DIR / "deleted_company_sample.json"
    raw_data = json.loads(sample_path.read_text(encoding="utf-8"))

    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/999888777",
    )

    assert record.organization_number == "999888777"
    assert record.is_deleted is True
    assert record.registered_status == "deleted"
    assert record.deleted_date == "2023-01-15"
    assert record.is_bankrupt is True


def test_workforce_scope_distinction() -> None:
    """Ensure employee count preserves its reporting scope (legal_entity vs group) and source."""
    # Norsk Hydro parent company payload: 400 employees, erIKonsern=True
    sample_hydro = {
        "organisasjonsnummer": "914778271",
        "navn": "Norsk Hydro ASA",
        "antallAnsatte": 400,
        "erIKonsern": True,
        "registreringsdatoAntallAnsatteNAVAaregisteret": "2026-09-10",
        "hjelpeenhetskode": {"beskrivelse": "Hovedkontortjenester", "kode": "70.100"},
    }

    record = normalize_registry_response(
        sample_hydro,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/914778271",
    )

    assert record.employee_count == 400
    assert record.employee_count_scope == "legal_entity"
    assert record.employee_count_source == "NAV Aa-registeret"
    assert record.employee_count_date == "2026-09-10"
    assert record.is_part_of_group is True

    # Also verify that the research pipeline turns this into distinct claims and clear evidence
    from signalpost.research import _create_registry_evidence, build_claims_from_record
    claims, _ = build_claims_from_record(record, evidence_id="ev-1")
    claims_map = {c.field: c.value for c in claims}
    assert claims_map["employee_count"] == 400
    assert claims_map["employee_count_scope"] == "legal_entity"
    assert claims_map["is_part_of_group"] is True

    ev = _create_registry_evidence(record, evidence_id="ev-1")
    assert "scope: legal_entity_only, does NOT include subsidiaries" in ev.supporting_text
    assert "corporate group member (erIKonsern: True)" in ev.supporting_text




# ==============================================================================
# Client Error Mapping Tests
# ==============================================================================

def test_unknown_number_maps_404_to_lookup_error() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404
    mock_client.get.return_value = mock_response

    with pytest.raises(LookupError) as exc_info:
        fetch_company_record("999999999", client=mock_client)

    assert issubclass(NotFoundError, LookupError)
    assert issubclass(NotFoundError, RegistryError)
    assert "not found in registry" in str(exc_info.value)


def test_blocked_maps_403_and_429() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 429
    mock_response.text = "Too many requests"
    mock_client.get.return_value = mock_response

    with pytest.raises(BlockedError):
        fetch_company_record("984851006", client=mock_client)


def test_transient_network_timeout() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = httpx.TimeoutException("Read timeout")

    with pytest.raises(TransientError):
        fetch_company_record("984851006", client=mock_client)


def test_transient_500_error() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 503
    mock_response.text = "Service Unavailable"
    mock_client.get.return_value = mock_response

    with pytest.raises(TransientError):
        fetch_company_record("984851006", client=mock_client)


def test_malformed_json_response() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)
    mock_client.get.return_value = mock_response

    with pytest.raises(MalformedDataError):
        fetch_company_record("984851006", client=mock_client)


def test_bad_input_and_404_do_not_crash_safe_usage() -> None:
    """Exit criterion: bad input and 404 do not crash the program when caught safely."""
    results: list[dict[str, str]] = []

    test_inputs = ["invalid", "12345678", "000000000"]
    mock_client = MagicMock(spec=httpx.Client)
    mock_response_404 = MagicMock(spec=httpx.Response)
    mock_response_404.status_code = 404
    mock_client.get.return_value = mock_response_404

    for item in test_inputs:
        try:
            fetch_company_record(item, client=mock_client)
            results.append({"org": item, "status": "ok"})
        except ValueError:
            results.append({"org": item, "status": "bad_format"})
        except LookupError:
            results.append({"org": item, "status": "not_found"})

    assert results == [
        {"org": "invalid", "status": "bad_format"},
        {"org": "12345678", "status": "bad_format"},
        {"org": "000000000", "status": "not_found"},
    ]


# ==============================================================================
# Live Integration Test (Official Public Registry)
# ==============================================================================

def test_live_company_dnb() -> None:
    """Check one real company from the official public registry.

    Confirms returned number and legal name match.
    """
    try:
        record = fetch_company_record("984851006")
    except (TransientError, BlockedError) as exc:
        pytest.skip(f"Live network access unavailable or restricted: {exc}")

    assert record.organization_number == "984851006"
    assert record.legal_name is not None
    assert "DNB" in record.legal_name.upper()
    assert record.entity_type == "ASA"
    assert record.registered_status == "active"
    assert record.is_registered is True
    assert record.is_deleted is False
    assert record.is_bankrupt is False
    assert record.registry_source_url == "https://data.brreg.no/enhetsregisteret/api/enheter/984851006"
    assert record.raw_response is not None


