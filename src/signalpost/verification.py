"""Deterministic verification engine and six-state result classifier.

Ensures that no claim is published without passing strict deterministic checks:
1. Organization number in evidence matches target.
2. Company name and website domain are consistent with registry identity.
3. Source URL and non-empty supporting text are recorded.
4. Dates parse; publication dates are never invented.
5. Financial values keep currency, unit, and reporting period.
6. Sources referring to parent companies, subsidiaries, or similarly named entities
   are classified as ambiguous rather than leaking facts into the target.
7. A failed request is never evidence that a fact does not exist.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional
import urllib.parse
from pydantic import BaseModel, ConfigDict, Field

from .models import AvailabilityStatus, Claim, CompanyProfile, Evidence, LegalIdentity


# Regular expression for Norwegian 9-digit org numbers
ORG_NUMBER_REGEX = re.compile(r"\b(\d{9})\b")

# ISO date format patterns
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YEAR_REGEX = re.compile(r"^\d{4}$")
PERIOD_SPAN_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}\s+(?:to|-)\s+\d{4}-\d{2}-\d{2}$", re.IGNORECASE)


class VerificationResult(BaseModel):
    """Result of deterministic verification on a single claim."""
    model_config = ConfigDict(extra="ignore")

    valid: bool
    field: str
    original_availability: AvailabilityStatus
    verified_availability: AvailabilityStatus
    reason: Optional[str] = None
    note: Optional[str] = None


def check_organization_number_match(
    target_org: str,
    evidence: Evidence,
) -> tuple[bool, Optional[str]]:
    """Verify that any 9-digit org number present in evidence matches the target entity."""
    check_texts = [
        evidence.supporting_text or "",
        evidence.note or "",
        evidence.source_url or "",
    ]
    combined_text = " ".join(check_texts)
    found_orgs = set(ORG_NUMBER_REGEX.findall(combined_text))

    # If evidence explicitly quotes org numbers, target must be among them, or no conflicting org numbers
    conflicting = {org for org in found_orgs if org != target_org}
    if conflicting and target_org not in found_orgs:
        return False, f"mismatched_org_number: found {conflicting} instead of target {target_org}"

    return True, None


REGIONAL_QUALIFIERS = {
    "india", "brasil", "brazil", "china", "uk", "usa", "us", "germany",
    "deutschland", "france", "spain", "sweden", "sverige", "denmark", "danmark",
    "finland", "poland", "japan", "australia", "singapore", "pakistan", "ghana",
    "africa", "asia", "americas", "europe", "nordics", "middle east", "latin america",
}

SUBSIDIARY_DIVISION_WORDS = {
    "rein", "energi", "renewables", "retail", "distribution", "logistics",
    "capital", "finance", "insurance", "ventures", "mobility", "solutions",
    "services", "infra", "digital", "tech", "asset management", "consulting",
}


def check_identity_and_domain_consistency(
    legal_name: Optional[str],
    target_org: str,
    evidence: Evidence,
    verified_website: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Verify that evidence source does not belong to a distinct subsidiary, parent, or third party.

    Applies generalized heuristic and linguistic rules across any target entity:
    1. Detects regional branch qualifiers (<Company> <Region>) not in target's legal name.
    2. Detects distinct business division units (<Company> <Division>) not in target's legal name.
    3. Detects distinct corporate entity names (<Other> AS/Ltd/Inc) in the supporting sentence.
    4. Validates domain consistency against the verified registry identity.
    """
    supp_text = (evidence.supporting_text or "").lower()
    source_url = evidence.source_url or ""
    
    parsed_source = urllib.parse.urlsplit(source_url)
    source_netloc = parsed_source.netloc.lower()

    if legal_name:
        clean_legal = legal_name.lower().strip()
        # Strip organizational form suffix to obtain core stems
        base_name = re.sub(
            r"\b(ASA|AS|ENK|SA|STAT|ANS|DA|NUF|BA|KS|SE|AB|OY|LTD|LLC|INC|GMBH|CORP)\b",
            "",
            clean_legal,
            flags=re.I,
        ).strip()
        tokens = [t for t in re.split(r"\s+", base_name) if len(t) > 2]

        for token in tokens:
            # 1. Generalized Regional Qualifier Rule
            for region in REGIONAL_QUALIFIERS:
                if region not in clean_legal:
                    pattern = rf"\b{re.escape(token)}\s+{re.escape(region)}\b"
                    if re.search(pattern, supp_text):
                        return False, f"regional_subsidiary_leak: evidence qualifies '{token} {region}' but target is '{legal_name}'"

            # 2. Generalized Business Division Rule
            for division in SUBSIDIARY_DIVISION_WORDS:
                if division not in clean_legal:
                    pattern = rf"\b{re.escape(token)}\s+{re.escape(division)}\b"
                    if re.search(pattern, supp_text):
                        return False, f"division_subsidiary_leak: evidence qualifies '{token} {division}' but target is '{legal_name}'"

            # 3. Dedicated regional URL path or subdomain isolation rule
            url_path = parsed_source.path.lower()
            for region in REGIONAL_QUALIFIERS:
                if region not in clean_legal:
                    if f"/{region}/" in url_path or source_netloc.startswith(f"{region}."):
                        return False, f"regional_url_leak: source url is scoped to foreign subsidiary '{region}'"

        # 4. Distinct corporate entity named in text rule
        # Detect if the sentence specifically defines a different entity with corporate suffix
        other_entities = re.findall(r"\b([a-z0-9æøå\-]{3,30}(?:\s+[a-z0-9æøå\-]{3,30})?)\s+(?:as|asa|ab|ltd|llc|inc|gmbh)\b", supp_text)
        for ent in other_entities:
            ent_clean = ent.strip()
            # If named entity is distinct from target's legal name and base stem
            if ent_clean != clean_legal and ent_clean != base_name:
                # If it's a qualified subsidiary (e.g., "acme renewables as" vs "acme asa")
                if any(t in ent_clean for t in tokens) and len(ent_clean.split()) > len(tokens):
                    return False, f"distinct_subsidiary_entity_leak: sentence specifies distinct entity '{ent_clean}'"

    # 5. Domain consistency check for web sources
    if evidence.source_type.value in {"verified_company_website", "other_permitted_public"}:
        if verified_website:
            target_domain = urllib.parse.urlsplit(verified_website).netloc.lower().lstrip("www.")
            source_domain = source_netloc.lstrip("www.")
            if target_domain and source_domain and target_domain != source_domain:
                if not (source_domain.endswith("." + target_domain) or target_domain.endswith("." + source_domain)):
                    return False, f"domain_mismatch: evidence domain '{source_domain}' inconsistent with target '{target_domain}'"

    return True, None


def check_evidence_structure(evidence: Evidence) -> tuple[bool, Optional[str]]:
    """Verify that source URL and non-empty supporting text are present."""
    if not evidence.source_url or not evidence.source_url.startswith(("http://", "https://")):
        return False, "missing_or_invalid_source_url"

    if not evidence.supporting_text or not evidence.supporting_text.strip():
        return False, "empty_supporting_text"

    if not evidence.retrieved_at:
        return False, "missing_retrieval_timestamp"

    return True, None


def check_dates_and_publication(
    claim: Claim,
    evidence_items: list[Evidence],
) -> tuple[bool, Optional[str]]:
    """Verify dates parse and publication dates are never invented."""
    # Check claim value if date field
    if claim.field in {"founding_date", "registration_date"} and claim.value:
        str_val = str(claim.value).strip()
        if not (DATE_REGEX.match(str_val) or YEAR_REGEX.match(str_val)):
            return False, f"invalid_date_format: '{str_val}'"

    # Check publication dates in evidence
    for ev in evidence_items:
        if ev.publication_date:
            str_pub = str(ev.publication_date).strip()
            if not (DATE_REGEX.match(str_pub) or YEAR_REGEX.match(str_pub)):
                return False, f"unparseable_publication_date: '{str_pub}'"

    return True, None


def check_financial_requirements(
    claim: Claim,
    all_claims: dict[str, Claim],
    evidence_items: list[Evidence],
) -> tuple[bool, Optional[str]]:
    """Verify financial figures keep currency, unit, and reporting period."""
    if claim.field in {"financial_revenue", "financial_operating_result", "financial_annual_result"}:
        if claim.availability == AvailabilityStatus.AVAILABLE and claim.value is not None:
            # Must have currency
            curr_claim = all_claims.get("financial_currency")
            if not curr_claim or not curr_claim.value:
                return False, "missing_financial_currency"

            # Must have reporting period
            period_claim = all_claims.get("financial_period")
            if not period_claim or not period_claim.value:
                return False, "missing_financial_reporting_period"

            # Value must be numeric
            try:
                float(claim.value)
            except (ValueError, TypeError):
                return False, f"non_numeric_financial_value: '{claim.value}'"

    return True, None


def verify_single_claim(
    claim: Claim,
    target_identity: LegalIdentity,
    evidence_map: dict[str, Evidence],
    all_claims: dict[str, Claim],
    verified_website: Optional[str] = None,
) -> VerificationResult:
    """Run all deterministic checks on a single claim before publishing."""
    orig_status = claim.availability

    # If already marked non-available or not applicable or failed, verify semantics
    if orig_status in {AvailabilityStatus.NOT_AVAILABLE, AvailabilityStatus.NOT_APPLICABLE, AvailabilityStatus.BLOCKED, AvailabilityStatus.FAILED}:
        return VerificationResult(
            valid=True,
            field=claim.field,
            original_availability=orig_status,
            verified_availability=orig_status,
        )

    # For available claims, verify evidence links
    if not claim.evidence_ids:
        # Claims with no evidence cannot be published as AVAILABLE
        return VerificationResult(
            valid=False,
            field=claim.field,
            original_availability=orig_status,
            verified_availability=AvailabilityStatus.NOT_AVAILABLE,
            reason="missing_evidence_reference",
        )

    associated_evidence = [evidence_map[eid] for eid in claim.evidence_ids if eid in evidence_map]
    if not associated_evidence:
        return VerificationResult(
            valid=False,
            field=claim.field,
            original_availability=orig_status,
            verified_availability=AvailabilityStatus.NOT_AVAILABLE,
            reason="unresolvable_evidence_ids",
        )

    for ev in associated_evidence:
        # 1. Evidence structure
        ok_struct, err_struct = check_evidence_structure(ev)
        if not ok_struct:
            return VerificationResult(
                valid=False,
                field=claim.field,
                original_availability=orig_status,
                verified_availability=AvailabilityStatus.FAILED,
                reason=err_struct,
            )

        # 2. Org number match
        ok_org, err_org = check_organization_number_match(target_identity.organization_number, ev)
        if not ok_org:
            return VerificationResult(
                valid=False,
                field=claim.field,
                original_availability=orig_status,
                verified_availability=AvailabilityStatus.AMBIGUOUS,
                reason=err_org,
            )

        # 3. Identity and domain consistency (parent/subsidiary check)
        ok_ident, err_ident = check_identity_and_domain_consistency(
            target_identity.legal_name,
            target_identity.organization_number,
            ev,
            verified_website=verified_website,
        )
        if not ok_ident:
            return VerificationResult(
                valid=False,
                field=claim.field,
                original_availability=orig_status,
                verified_availability=AvailabilityStatus.AMBIGUOUS,
                reason=err_ident,
            )

    # 4. Dates and publication
    ok_date, err_date = check_dates_and_publication(claim, associated_evidence)
    if not ok_date:
        return VerificationResult(
            valid=False,
            field=claim.field,
            original_availability=orig_status,
            verified_availability=AvailabilityStatus.FAILED,
            reason=err_date,
        )

    # 5. Financial requirements
    ok_fin, err_fin = check_financial_requirements(claim, all_claims, associated_evidence)
    if not ok_fin:
        return VerificationResult(
            valid=False,
            field=claim.field,
            original_availability=orig_status,
            verified_availability=AvailabilityStatus.FAILED,
            reason=err_fin,
        )

    return VerificationResult(
        valid=True,
        field=claim.field,
        original_availability=orig_status,
        verified_availability=AvailabilityStatus.AVAILABLE,
    )


def verify_company_profile(profile: CompanyProfile) -> tuple[CompanyProfile, list[VerificationResult]]:
    """Run deterministic pre-publishing verification across all claims in a company profile."""
    evidence_map = {ev.id: ev for ev in profile.evidence}
    all_claims = {c.field: c for c in profile.claims}
    
    website_claim = all_claims.get("official_website")
    verified_website = str(website_claim.value) if website_claim and website_claim.value else None

    results: list[VerificationResult] = []
    verified_claims: list[Claim] = []

    for claim in profile.claims:
        res = verify_single_claim(
            claim=claim,
            target_identity=profile.legal_identity,
            evidence_map=evidence_map,
            all_claims=all_claims,
            verified_website=verified_website,
        )
        results.append(res)

        if res.verified_availability != claim.availability:
            # Adjust claim availability and wipe value if demoted to non-available/ambiguous
            new_claim = claim.model_copy()
            new_claim.availability = res.verified_availability
            if res.verified_availability in {AvailabilityStatus.NOT_AVAILABLE, AvailabilityStatus.AMBIGUOUS, AvailabilityStatus.FAILED}:
                new_claim.value = None
            verified_claims.append(new_claim)
        else:
            verified_claims.append(claim)

    profile.claims = verified_claims
    return profile, results
