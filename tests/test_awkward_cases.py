"""Tests for awkward and edge cases:
- Sole proprietorship (ENK)
- Bankrupt or dissolved company
- Company with no industry code or address
- 9-digit number that does not exist in registry (404 -> NOT_FOUND)
- Entity that has moved (differing business and postal address)
- Distinct error codes between invalid format (INVALID_FORMAT) and 404 (NOT_FOUND)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import httpx
import pytest

from signalpost.output import TerminalEnvelope
from signalpost.registry import (
    NotFoundError,
    RegistryClient,
    normalize_registry_response,
)
from signalpost.research import research_company
from signalpost.runner import process_single_company, run_batch

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def test_awkward_sole_proprietorship_enk() -> None:
    """Sole proprietorships (ENK) must be parsed without assuming corporate fields or crashing."""
    raw_data = json.loads((SAMPLES_DIR / "enk_sample.json").read_text(encoding="utf-8"))
    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/933365573",
    )

    assert record.organization_number == "933365573"
    assert record.legal_name == "- ZOTKO .NO"
    assert record.entity_type == "ENK"
    assert record.entity_type_description == "Enkeltpersonforetak"
    assert record.registered_status == "active"
    assert record.is_part_of_group is False
    assert record.employee_count is None
    assert record.website is None

    # Test through research pipeline
    mock_client = MagicMock(spec=RegistryClient)
    mock_client.get_company.return_value = record

    profile = research_company("933365573", client=mock_client)
    claims_by_field = {c.field: c for c in profile.claims}

    assert claims_by_field["entity_type"].value == "ENK"
    assert claims_by_field["employee_count"].availability.value == "not_available"
    assert claims_by_field["employee_count"].value is None
    assert claims_by_field["official_website"].availability.value == "not_available"
    assert claims_by_field["official_website"].value is None


def test_awkward_bankrupt_company() -> None:
    """Bankrupt entities must reflect bankrupt status in legal identity and claims."""
    raw_data = json.loads((SAMPLES_DIR / "bankrupt_sample.json").read_text(encoding="utf-8"))
    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/935567645",
    )

    assert record.organization_number == "935567645"
    assert record.registered_status == "bankrupt"
    assert record.is_bankrupt is True

    mock_client = MagicMock(spec=RegistryClient)
    mock_client.get_company.return_value = record

    profile = research_company("935567645", client=mock_client)
    claims_by_field = {c.field: c for c in profile.claims}

    assert claims_by_field["is_bankrupt"].value is True
    assert claims_by_field["registered_status"].value == "bankrupt"


def test_awkward_dissolved_deleted_company() -> None:
    """Deleted/dissolved entities must reflect deleted status and deletion date."""
    raw_data = json.loads((SAMPLES_DIR / "deleted_company_sample.json").read_text(encoding="utf-8"))
    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/999888777",
    )

    assert record.organization_number == "999888777"
    assert record.registered_status == "deleted"
    assert record.is_deleted is True
    assert record.deleted_date == "2023-01-15"

    mock_client = MagicMock(spec=RegistryClient)
    mock_client.get_company.return_value = record

    profile = research_company("999888777", client=mock_client)
    claims_by_field = {c.field: c for c in profile.claims}

    assert claims_by_field["is_deleted"].value is True
    assert claims_by_field["registered_status"].value == "deleted"


def test_awkward_missing_industry_and_address() -> None:
    """Entities missing industry code and addresses must map to not_available without crashing."""
    raw_data = json.loads((SAMPLES_DIR / "minimal_company_sample.json").read_text(encoding="utf-8"))
    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
    )

    assert record.organization_number == "123456789"
    assert record.industry_code is None
    assert record.business_address is None
    assert record.postal_address is None

    mock_client = MagicMock(spec=RegistryClient)
    mock_client.get_company.return_value = record

    profile = research_company("123456789", client=mock_client)
    claims_by_field = {c.field: c for c in profile.claims}

    assert claims_by_field["industry_code"].availability.value == "not_available"
    assert claims_by_field["industry_code"].value is None
    assert claims_by_field["business_address"].availability.value == "not_available"
    assert claims_by_field["business_address"].value is None
    assert claims_by_field["postal_address"].availability.value == "not_available"
    assert claims_by_field["postal_address"].value is None


def test_awkward_entity_that_has_moved() -> None:
    """Entities that have moved preserve distinct business and postal addresses."""
    raw_data = json.loads((SAMPLES_DIR / "moved_company_sample.json").read_text(encoding="utf-8"))
    record = normalize_registry_response(
        raw_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/921345678",
    )

    assert record.organization_number == "921345678"
    assert record.business_address is not None
    assert "BERGEN" in record.business_address.formatted
    assert record.postal_address is not None
    assert "OSLO" in record.postal_address.formatted

    mock_client = MagicMock(spec=RegistryClient)
    mock_client.get_company.return_value = record

    profile = research_company("921345678", client=mock_client)
    claims_by_field = {c.field: c for c in profile.claims}

    assert claims_by_field["business_address"].value == "Strandgaten 12, 5003 BERGEN"
    assert claims_by_field["postal_address"].value == "Postboks 123 Sentrum, 0150 OSLO"


def test_error_code_distinction_invalid_format_vs_404(tmp_path: Path) -> None:
    """Verify that 9-digit non-existent (404) and invalid format carry distinct error codes."""
    mock_http_client = MagicMock(spec=httpx.Client)

    def mock_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        if "000000000" in url:
            resp.status_code = 404
            resp.text = "Not found"
            return resp
        resp.status_code = 500
        return resp

    mock_http_client.get.side_effect = mock_get
    reg_client = RegistryClient(client=mock_http_client)

    # 1. 9-digit number that passes regex check but returns 404
    env_404 = process_single_company("000000000", run_id="err-test", client=reg_client)
    assert env_404.run.terminal_status == "failed"
    assert len(env_404.errors) == 1
    assert env_404.errors[0]["code"] == "NOT_FOUND"

    # 2. String that fails regex check
    env_format = process_single_company("12345", run_id="err-test", client=reg_client)
    assert env_format.run.terminal_status == "failed"
    assert len(env_format.errors) == 1
    assert env_format.errors[0]["code"] == "INVALID_FORMAT"

    # 3. Verify they are strictly distinct
    assert env_404.errors[0]["code"] != env_format.errors[0]["code"]
