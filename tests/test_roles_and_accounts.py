"""Tests for Brreg roles, annual accounts financials, and careers/job-opening discovery."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
import pytest

from signalpost.models import AvailabilityStatus, SourceType
from signalpost.registry import (
    CompanyRecord,
    RegistryClient,
    RolesRecord,
    AnnualAccountsRecord,
    normalize_roles_response,
    normalize_annual_accounts_response,
)
from signalpost.research import research_company
from signalpost.website import CleanedWebPage, discover_target_pages
from signalpost.extraction import extract_claims_from_page, CandidateClaim, validate_candidate_claim
from signalpost.llm import LLMClient, LLMResponse


def test_normalize_roles_extracts_ceo_chair_board_and_discards_birth_dates() -> None:
    raw_roles_data = {
        "rollegrupper": [
            {
                "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {
                            "fodselsdato": "1975-04-12",  # SENSITIVE BIRTH DATE - MUST BE DISCARDED
                            "navn": {"fornavn": "Anders", "etternavn": "Opedal"},
                        },
                    }
                ],
            },
            {
                "type": {"kode": "STYR", "beskrivelse": "Styre"},
                "roller": [
                    {
                        "type": {"kode": "LEDE", "beskrivelse": "Styrets leder"},
                        "person": {
                            "fodselsdato": "1960-08-20",  # MUST BE DISCARDED
                            "navn": {"fornavn": "Jarle", "mellomnavn": "Kjell", "etternavn": "Roth"},
                        },
                    },
                    {
                        "type": {"kode": "MEDL", "beskrivelse": "Styremedlem"},
                        "person": {
                            "fodselsdato": "1980-01-15",  # MUST BE DISCARDED
                            "navn": {"fornavn": "Finn", "mellomnavn": "Bjørn", "etternavn": "Ruyter"},
                        },
                    },
                    {
                        "type": {"kode": "MEDL", "beskrivelse": "Styremedlem"},
                        "person": {
                            "fodselsdato": "1972-11-03",  # MUST BE DISCARDED
                            "navn": {"fornavn": "Anne", "etternavn": "Drinkwater"},
                        },
                    },
                ],
            },
            {
                "type": {"kode": "REVI", "beskrivelse": "Revisor"},
                "roller": [
                    {
                        "type": {"kode": "REVI", "beskrivelse": "Revisor"},
                        "enhet": {"organisasjonsnummer": "980211282", "navn": "ERNST & YOUNG AS"},
                    }
                ],
            },
        ]
    }

    roles = normalize_roles_response(
        raw_roles_data,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016/roller",
        retrieved_at="2026-10-13T10:00:00Z",
    )

    assert roles.ceo == "Anders Opedal"
    assert roles.chair == "Jarle Kjell Roth"
    assert "Finn Bjørn Ruyter" in roles.board_members
    assert "Anne Drinkwater" in roles.board_members
    assert len(roles.board_members) == 2

    # PRIVACY VERIFICATION: Confirm birth dates are completely absent
    dumped_str = json.dumps(roles.model_dump())
    assert "1975-04-12" not in dumped_str
    assert "1960-08-20" not in dumped_str
    assert "1980-01-15" not in dumped_str
    assert "fodselsdato" not in dumped_str


def test_normalize_annual_accounts_extracts_revenue_results_period_currency() -> None:
    raw_accounts = [
        {
            "id": 123456,
            "regnskapstype": "SELSKAP",
            "valuta": "NOK",
            "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {
                        "salgsinntekter": 193619000000.0,
                        "sumDriftsinntekter": 198263000000.0,
                    },
                    "driftsresultat": 9592000000.0,
                },
                "aarsresultat": 2804000000.0,
            },
        }
    ]

    accounts = normalize_annual_accounts_response(
        raw_accounts,
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/914778271",
        retrieved_at="2026-10-13T10:00:00Z",
    )

    assert accounts is not None
    assert accounts.period == "2023-01-01 to 2023-12-31"
    assert accounts.period_start == "2023-01-01"
    assert accounts.period_end == "2023-12-31"
    assert accounts.scope == "company"
    assert accounts.currency == "NOK"
    assert accounts.revenue == 198263000000.0
    assert accounts.operating_result == 9592000000.0
    assert accounts.annual_result == 2804000000.0


def test_research_company_attaches_roles_and_accounts_claims() -> None:
    mock_client = MagicMock(spec=RegistryClient)

    mock_record = CompanyRecord(
        organization_number="914778271",
        legal_name="Norsk Hydro ASA",
        entity_type="ASA",
        entity_type_description="Allmennaksjeselskap",
        registered_status="active",
        founding_date="1905-12-02",
        registration_date="1995-02-19",
        registry_source_url="https://data.brreg.no/enhetsregisteret/api/enheter/914778271",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    mock_client.get_company.return_value = mock_record

    mock_roles = RolesRecord(
        ceo="Eivind Kallevik",
        chair="Rune Bjerke",
        board_members=["Kristin Kragseth", "Espen Gundersen"],
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/914778271/roller",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    mock_client.get_roles.return_value = mock_roles

    mock_accounts = AnnualAccountsRecord(
        period="2025-01-01 to 2025-12-31",
        period_start="2025-01-01",
        period_end="2025-12-31",
        period_year="2025",
        scope="consolidated",
        currency="NOK",
        revenue=213281000000.0,
        operating_result=9592000000.0,
        annual_result=2804000000.0,
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/914778271",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    mock_client.get_annual_accounts.return_value = (mock_accounts, "available")

    profile = research_company("914778271", client=mock_client, enable_web=False)

    claims_by_field = {c.field: c for c in profile.claims}
    assert claims_by_field["founding_date"].value == "1905-12-02"
    assert claims_by_field["registration_date"].value == "1995-02-19"
    assert claims_by_field["ceo"].value == "Eivind Kallevik"
    assert claims_by_field["board_chair"].value == "Rune Bjerke"
    assert "Kristin Kragseth" in claims_by_field["board_members"].value
    assert claims_by_field["financial_revenue"].value == 213281000000.0
    assert claims_by_field["financial_operating_result"].value == 9592000000.0
    assert claims_by_field["financial_annual_result"].value == 2804000000.0
    assert claims_by_field["financial_currency"].value == "NOK"
    assert claims_by_field["financial_period"].value == "2025-01-01 to 2025-12-31"
    assert claims_by_field["financial_scope"].value == "consolidated"

    # Evidence checks: each role gets its own distinct evidence record
    ev_by_id = {e.id: e for e in profile.evidence}
    assert "ev-reg-role-ceo" in ev_by_id
    assert "ev-reg-role-chair" in ev_by_id
    assert "ev-reg-role-board" in ev_by_id
    assert "ev-reg-accounts" in ev_by_id


def test_spot_check_jens_petter_olsen_and_deputy_exclusion() -> None:
    # Test deputy members (VARA) and observers (OBS) are strictly excluded from board_members
    raw_data = {
        "rollegrupper": [
            {
                "type": {"kode": "STYR", "beskrivelse": "Styre"},
                "roller": [
                    {
                        "type": {"kode": "LEDE", "beskrivelse": "Styrets leder"},
                        "person": {"navn": {"fornavn": "Jens", "mellomnavn": "Petter", "etternavn": "Olsen"}},
                    },
                    {
                        "type": {"kode": "MEDL", "beskrivelse": "Styremedlem"},
                        "person": {"navn": {"fornavn": "Gyrid", "etternavn": "Ingerø"}},
                    },
                    {
                        "type": {"kode": "VARA", "beskrivelse": "Varamedlem"},
                        "person": {"navn": {"fornavn": "Per", "mellomnavn": "Morten", "etternavn": "Schou"}},
                    },
                    {
                        "type": {"kode": "OBS", "beskrivelse": "Observatør"},
                        "person": {"navn": {"fornavn": "Astrid", "etternavn": "Observatør"}},
                    },
                ],
            }
        ]
    }
    roles = normalize_roles_response(raw_data, "https://mock/roller", "2026-10-13T10:00:00Z")
    assert roles.chair == "Jens Petter Olsen"
    assert "Gyrid Ingerø" in roles.board_members
    # Varamedlem and observatør must NOT be counted as board members
    assert "Per Morten Schou" not in roles.board_members
    assert "Astrid Observatør" not in roles.board_members
    assert len(roles.board_members) == 1


def test_bank_accounts_mapped_to_not_applicable() -> None:
    mock_client = MagicMock(spec=RegistryClient)
    mock_record = CompanyRecord(
        organization_number="984851006",
        legal_name="DNB BANK ASA",
        entity_type="ASA",
        entity_type_description="Allmennaksjeselskap",
        registered_status="active",
        registry_source_url="https://data.brreg.no/enhetsregisteret/api/enheter/984851006",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    mock_client.get_company.return_value = mock_record
    mock_client.get_roles.return_value = None
    mock_client.get_annual_accounts.return_value = (None, "not_applicable")

    profile = research_company("984851006", client=mock_client, enable_web=False)
    claims_by_field = {c.field: c for c in profile.claims}
    assert claims_by_field["financial_revenue"].availability == AvailabilityStatus.NOT_APPLICABLE
    assert claims_by_field["financial_revenue"].value is None
    ev_ids = [e.id for e in profile.evidence]
    assert "ev-reg-accounts-na" in ev_ids


def test_careers_page_and_job_openings_discovery() -> None:
    # 1. discover_target_pages categorizes careers links
    dummy_page = CleanedWebPage(
        url="https://www.example.no",
        visible_text="Hjemmeside",
        links=[
            "https://www.example.no/om-oss",
            "https://www.example.no/kontakt",
            "https://www.example.no/karriere",
        ],
        content_sha256="abc",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    pages = discover_target_pages("https://www.example.no", dummy_page, max_pages=4)
    assert "https://www.example.no/karriere" in pages

    # 2. open_job_openings claim validation
    page_careers = CleanedWebPage(
        url="https://www.example.no/karriere",
        visible_text="Bli en del av vårt team! Vi har for tiden 14 ledige stillinger i Norge.",
        content_sha256="cde",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    valid_claim = CandidateClaim(
        field="open_job_openings",
        value=14,
        supporting_text="Vi har for tiden 14 ledige stillinger i Norge.",
    )
    is_valid, _ = validate_candidate_claim(valid_claim, page_careers, "Example AS", "123456789")
    assert is_valid is True

    # 3. Invalid negative openings rejected
    invalid_claim = CandidateClaim(
        field="open_job_openings",
        value=-2,
        supporting_text="Vi har for tiden 14 ledige stillinger i Norge.",
    )
    is_valid_inv, reason = validate_candidate_claim(invalid_claim, page_careers, "Example AS", "123456789")
    assert is_valid_inv is False
    assert reason == "invalid_job_openings_count"
