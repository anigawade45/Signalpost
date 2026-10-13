"""Tests for deterministic verification engine and the six result states."""

from __future__ import annotations

import pytest
from signalpost.models import (
    AvailabilityStatus,
    Claim,
    CompanyProfile,
    Evidence,
    LegalIdentity,
    SourceType,
)
from signalpost.verification import (
    check_dates_and_publication,
    check_evidence_structure,
    check_financial_requirements,
    check_identity_and_domain_consistency,
    check_organization_number_match,
    verify_company_profile,
    verify_single_claim,
)


def _make_test_identity(org="923609016", name="EQUINOR ASA") -> LegalIdentity:
    return LegalIdentity(
        organization_number=org,
        legal_name=name,
        entity_type="ASA",
        entity_type_description="Allmennaksjeselskap",
        registered_status="active",
    )


def _make_valid_evidence(
    eid="ev-1",
    org="923609016",
    url="https://www.equinor.com/about",
    text="Equinor is an international energy company org 923609016",
) -> Evidence:
    return Evidence(
        id=eid,
        source_url=url,
        retrieved_at="2026-10-13T10:00:00Z",
        supporting_text=text,
        content_sha256="abcd" * 16,
        source_type=SourceType.VERIFIED_COMPANY_WEBSITE,
    )


def test_org_number_matching():
    target_org = "923609016"
    
    # 1. Evidence with matching target org passes
    ev_ok = _make_valid_evidence(org=target_org, text="Official statement for org 923609016.")
    ok, err = check_organization_number_match(target_org, ev_ok)
    assert ok is True
    assert err is None

    # 2. Evidence mentioning conflicting different 9-digit org fails
    ev_conflict = _make_valid_evidence(org="999888777", text="This relates to subsidiary org 999888777.")
    ok, err = check_organization_number_match(target_org, ev_conflict)
    assert ok is False
    assert "mismatched_org_number" in err

    # 3. Evidence without org numbers passes if general text
    ev_no_org = _make_valid_evidence(text="Equinor provides reliable energy globally.")
    ok, err = check_organization_number_match(target_org, ev_no_org)
    assert ok is True


def test_subsidiary_and_parent_leak_marked_ambiguous():
    # If a source refers to Yara India, do not assign its facts to Yara International ASA -> mark ambiguous
    identity = _make_test_identity(org="986228608", name="YARA INTERNATIONAL ASA")
    
    ev_subsidiary = _make_valid_evidence(
        url="https://www.yara.com/india",
        text="Yara India is the leading crop nutrition provider across rural regions.",
    )
    ok, err = check_identity_and_domain_consistency(
        legal_name=identity.legal_name,
        target_org=identity.organization_number,
        evidence=ev_subsidiary,
    )
    assert ok is False
    assert "regional_subsidiary_leak" in err

    # Single claim verification demotes to AMBIGUOUS
    claim = Claim(
        field="business_description",
        value="Yara India is the leading crop nutrition provider across rural regions.",
        availability=AvailabilityStatus.AVAILABLE,
        evidence_ids=[ev_subsidiary.id],
    )
    evidence_map = {ev_subsidiary.id: ev_subsidiary}
    res = verify_single_claim(claim, identity, evidence_map, {claim.field: claim})
    assert res.valid is False
    assert res.verified_availability == AvailabilityStatus.AMBIGUOUS


def test_generalized_subsidiary_blocking_unseen_company():
    """Verify rules generalize to completely unseen companies and divisions without hardcoding."""
    identity = LegalIdentity(
        organization_number="999111222",
        legal_name="AURORA TECHNOLOGIES ASA",
        entity_type="ASA",
        entity_type_description="Allmennaksjeselskap",
        registered_status="active",
    )

    # 1. Unseen company with a regional subsidiary qualifier (e.g. Aurora Germany)
    ev_germany = _make_valid_evidence(
        org="999111222",
        url="https://www.aurora.com/germany",
        text="Aurora Germany supplies high-precision sensors across DACH region.",
    )
    ok, err = check_identity_and_domain_consistency(identity.legal_name, identity.organization_number, ev_germany)
    assert ok is False
    assert "regional_subsidiary_leak" in err

    # 2. Unseen company with a business division qualifier (e.g. Aurora Renewables)
    ev_division = _make_valid_evidence(
        org="999111222",
        url="https://www.aurora.com/renewables",
        text="Aurora Renewables develops offshore wind energy systems.",
    )
    ok, err = check_identity_and_domain_consistency(identity.legal_name, identity.organization_number, ev_division)
    assert ok is False
    assert "division_subsidiary_leak" in err

    # 3. Dedicated regional URL path leak
    ev_url_leak = _make_valid_evidence(
        org="999111222",
        url="https://www.aurora.com/brasil/about",
        text="Aurora supplies products internationally.",
    )
    ok, err = check_identity_and_domain_consistency(identity.legal_name, identity.organization_number, ev_url_leak)
    assert ok is False
    assert "regional_url_leak" in err


def test_domain_consistency_check():
    identity = _make_test_identity(org="923609016", name="EQUINOR ASA")

    # Mismatched 3rd party domain
    ev_mismatch = _make_valid_evidence(
        url="https://www.totallyunrelatedsite.com/facts",
        text="Equinor is headquartered in Stavanger.",
    )
    ok, err = check_identity_and_domain_consistency(
        legal_name=identity.legal_name,
        target_org=identity.organization_number,
        evidence=ev_mismatch,
        verified_website="https://www.equinor.com",
    )
    assert ok is False
    assert "domain_mismatch" in err


def test_evidence_structure_requirements():
    # Missing source URL
    ev_bad_url = Evidence(
        id="ev-bad",
        source_url="",
        retrieved_at="2026-10-13T10:00:00Z",
        supporting_text="Valid supporting text",
    )
    ok, err = check_evidence_structure(ev_bad_url)
    assert ok is False
    assert err == "missing_or_invalid_source_url"

    # Empty supporting text
    ev_empty_text = Evidence(
        id="ev-empty",
        source_url="https://www.equinor.com",
        retrieved_at="2026-10-13T10:00:00Z",
        supporting_text="   ",
    )
    ok, err = check_evidence_structure(ev_empty_text)
    assert ok is False
    assert err == "empty_supporting_text"


def test_dates_parse_and_publication_dates_never_invented():
    claim_bad_date = Claim(
        field="founding_date",
        value="12th of March 1995",
        availability=AvailabilityStatus.AVAILABLE,
    )
    ev = _make_valid_evidence()
    ok, err = check_dates_and_publication(claim_bad_date, [ev])
    assert ok is False
    assert "invalid_date_format" in err

    claim_ok_date = Claim(
        field="founding_date",
        value="1972-09-18",
        availability=AvailabilityStatus.AVAILABLE,
    )
    ok, err = check_dates_and_publication(claim_ok_date, [ev])
    assert ok is True

    # Bad publication date in evidence
    ev_bad_pub = Evidence(
        id="ev-pub",
        source_url="https://www.equinor.com",
        retrieved_at="2026-10-13T10:00:00Z",
        supporting_text="Equinor",
        publication_date="Winter 2024",
    )
    ok, err = check_dates_and_publication(claim_ok_date, [ev_bad_pub])
    assert ok is False
    assert "unparseable_publication_date" in err


def test_financial_values_keep_currency_unit_and_period():
    rev_claim = Claim(
        field="financial_revenue",
        value=106462000000.0,
        availability=AvailabilityStatus.AVAILABLE,
    )
    ev = _make_valid_evidence()

    # 1. Missing currency
    all_claims_missing_curr = {
        "financial_revenue": rev_claim,
        "financial_period": Claim(field="financial_period", value="2025-01-01 to 2025-12-31", availability=AvailabilityStatus.AVAILABLE),
    }
    ok, err = check_financial_requirements(rev_claim, all_claims_missing_curr, [ev])
    assert ok is False
    assert err == "missing_financial_currency"

    # 2. Missing reporting period
    all_claims_missing_period = {
        "financial_revenue": rev_claim,
        "financial_currency": Claim(field="financial_currency", value="USD", availability=AvailabilityStatus.AVAILABLE),
    }
    ok, err = check_financial_requirements(rev_claim, all_claims_missing_period, [ev])
    assert ok is False
    assert err == "missing_financial_reporting_period"

    # 3. Complete financial figures pass
    all_claims_complete = {
        "financial_revenue": rev_claim,
        "financial_currency": Claim(field="financial_currency", value="USD", availability=AvailabilityStatus.AVAILABLE),
        "financial_period": Claim(field="financial_period", value="2025-01-01 to 2025-12-31", availability=AvailabilityStatus.AVAILABLE),
    }
    ok, err = check_financial_requirements(rev_claim, all_claims_complete, [ev])
    assert ok is True


def test_all_six_result_states_consistent():
    """Verify all six states behave as strictly specified:
    - available: Information found and verified
    - not_available: Research completed, information not found
    - blocked: A source refused access
    - not_applicable: Field does not apply to this company
    - ambiguous: Entity or claim could not be confidently matched
    - failed: Processing failed (timeout, parse error, crash)
    """
    identity = LegalIdentity(
        organization_number="986228608",
        legal_name="YARA INTERNATIONAL ASA",
        entity_type="ASA",
        entity_type_description="Allmennaksjeselskap",
        registered_status="active",
    )
    ev1 = _make_valid_evidence(
        eid="ev-1",
        org="986228608",
        url="https://data.brreg.no/api/enheter/986228608",
        text="YARA INTERNATIONAL ASA org 986228608",
    )
    ev1.source_type = SourceType.OFFICIAL_REGISTRY
    ev2 = _make_valid_evidence(
        eid="ev-2",
        org="986228608",
        url="https://www.yara.com/india",
        text="Yara India is the regional unit",
    )

    profile = CompanyProfile(
        organization_number=identity.organization_number,
        legal_identity=identity,
        retrieved_at="2026-10-13T10:00:00Z",
        evidence=[ev1, ev2],
        claims=[
            Claim(field="legal_name", value="YARA INTERNATIONAL ASA", availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev1.id]),
            Claim(field="contact_phone", value=None, availability=AvailabilityStatus.NOT_AVAILABLE),
            Claim(field="official_website", value="https://blocked.com", availability=AvailabilityStatus.BLOCKED),
            Claim(field="operating_result", value=None, availability=AvailabilityStatus.NOT_APPLICABLE),
            Claim(field="business_description", value="Yara India operations", availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev2.id]),
            Claim(field="careers_url", value=None, availability=AvailabilityStatus.FAILED),
        ],
    )

    verified_profile, results = verify_company_profile(profile)
    claims_dict = {c.field: c for c in verified_profile.claims}

    assert claims_dict["legal_name"].availability == AvailabilityStatus.AVAILABLE
    assert claims_dict["contact_phone"].availability == AvailabilityStatus.NOT_AVAILABLE
    assert claims_dict["official_website"].availability == AvailabilityStatus.BLOCKED
    assert claims_dict["operating_result"].availability == AvailabilityStatus.NOT_APPLICABLE
    assert claims_dict["business_description"].availability == AvailabilityStatus.AMBIGUOUS
    assert claims_dict["careers_url"].availability == AvailabilityStatus.FAILED


def test_failed_request_is_never_evidence_that_fact_does_not_exist():
    """Rule: A failed request is never evidence that a fact does not exist."""
    claim = Claim(
        field="official_website",
        value=None,
        availability=AvailabilityStatus.FAILED,
    )
    identity = _make_test_identity()
    res = verify_single_claim(claim, identity, {}, {claim.field: claim})
    # Failed status remains FAILED, must NEVER be downgraded to NOT_AVAILABLE
    assert res.verified_availability == AvailabilityStatus.FAILED
    assert res.verified_availability != AvailabilityStatus.NOT_AVAILABLE
