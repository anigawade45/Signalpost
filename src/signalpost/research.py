"""Deterministic research pipeline anchoring company intelligence in the Norwegian registry."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any, Optional

import httpx

from .cache import PageCache
from .extraction import extract_claims_from_page
from .llm import LLMClient
from .models import (
    AvailabilityStatus,
    Claim,
    CompanyProfile,
    DEFAULT_SOURCE_HIERARCHY,
    Evidence,
    FINANCIAL_SOURCE_HIERARCHY,
    LegalIdentity,
    SourceType,
)
from .registry import (
    AnnualAccountsRecord,
    CompanyRecord,
    RegistryClient,
    RolesRecord,
    fetch_company_record,
)
from .website import (
    CAREERS_KEYWORDS,
    CleanedWebPage,
    discover_target_pages,
    fetch_page_with_cache,
    verify_website_identity,
)


def _propose_website_candidates(legal_name: Optional[str]) -> list[str]:
    """Propose candidate domains based on company legal name for missing-website records."""
    if not legal_name:
        return []

    clean = legal_name.lower()
    for suffix in [" asa", " as", " enk", " ans", " da", " sa"]:
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]

    tokens = re.findall(r"[a-z0-9]+", clean)
    if not tokens:
        return []

    core = "".join(tokens)
    return [
        f"https://www.{core}.no",
        f"https://www.{core}.com",
        f"https://{core}.no",
        f"https://{core}.com",
    ]


def _create_registry_evidence(record: CompanyRecord, evidence_id: str = "ev-reg-1") -> Evidence:
    """Create an immutable evidence record referencing the official registry call."""
    raw_hash = None
    if record.raw_response:
        raw_bytes = json.dumps(record.raw_response, sort_keys=True, ensure_ascii=False).encode("utf-8")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()

    verbatim_parts = [f'"organisasjonsnummer": "{record.organization_number}"']
    if record.legal_name:
        verbatim_parts.append(f'"navn": "{record.legal_name}"')
    if record.employee_count is not None:
        verbatim_parts.append(f'"antallAnsatte": {record.employee_count}')
    supporting_text = ", ".join(verbatim_parts)

    note_parts = []
    if record.employee_count is not None:
        group_desc = "corporate group member (erIKonsern: True)" if record.is_part_of_group else "independent entity (erIKonsern: False)"
        note_parts.append(
            f"Employee count ({record.employee_count}) represents legal entity {record.organization_number} parent/HQ workforce only "
            f"via NAV Aa-registeret. Never describe as overall company/group size; group-wide headcount is unknown until reported "
            f"in consolidated annual accounts ({group_desc})."
        )
    note = " ".join(note_parts) if note_parts else None

    return Evidence(
        id=evidence_id,
        source_url=record.registry_source_url,
        source_name="Brønnøysundregistrene Enhetsregisteret",
        retrieved_at=record.retrieved_at,
        publication_date=record.registration_date,
        reporting_period=record.latest_submitted_accounts,
        supporting_text=supporting_text,
        note=note,
        source_type=SourceType.OFFICIAL_REGISTRY,
        content_sha256=raw_hash,
    )


def build_claims_from_record(record: CompanyRecord, evidence_id: str) -> tuple[list[Claim], list[str]]:
    """Build normalized claims from registry record.

    Missing fields are explicitly mapped to NOT_AVAILABLE, never 0 or empty string.
    """
    claims: list[Claim] = []
    unknowns: list[str] = []

    # Mapping of field_name -> (value, is_available_check)
    field_data: list[tuple[str, Any]] = [
        ("legal_name", record.legal_name),
        ("entity_type", record.entity_type),
        ("entity_type_description", record.entity_type_description),
        ("registered_status", record.registered_status),
        ("registration_date", record.registration_date),
        ("industry_code", record.industry_code),
        ("industry_description", record.industry_description),
        ("official_website", record.website),
        ("employee_count", record.employee_count),
        ("employee_count_scope", record.employee_count_scope),
        ("employee_count_source", record.employee_count_source),
        ("is_part_of_group", record.is_part_of_group),
        ("latest_submitted_accounts", record.latest_submitted_accounts),
        ("is_registered_in_business_register", record.is_registered_in_business_register),
        ("is_registered_in_vat_register", record.is_registered_in_vat_register),
        ("is_bankrupt", record.is_bankrupt),
        ("is_under_liquidation", record.is_under_liquidation),
        ("is_deleted", record.is_deleted),
    ]

    # Addresses formatted
    business_addr_val = record.business_address.formatted if record.business_address else None
    postal_addr_val = record.postal_address.formatted if record.postal_address else None
    field_data.append(("business_address", business_addr_val))
    field_data.append(("postal_address", postal_addr_val))

    for field_name, value in field_data:
        # Check if field is present and non-empty
        is_missing = value is None or value == ""
        if is_missing:
            claims.append(
                Claim(
                    field=field_name,
                    value=None,  # Never empty string or 0!
                    availability=AvailabilityStatus.NOT_AVAILABLE,
                    evidence_ids=[],
                    confidence=1.0,
                )
            )
            unknowns.append(field_name)
        else:
            claims.append(
                Claim(
                    field=field_name,
                    value=value,
                    availability=AvailabilityStatus.AVAILABLE,
                    evidence_ids=[evidence_id],
                    confidence=1.0,
                )
            )

    return claims, unknowns


def research_company(
    organization_number: str | int,
    client: Optional[RegistryClient] = None,
    cache: Optional[PageCache] = None,
    llm_client: Optional[LLMClient] = None,
    enable_web: bool = True,
    http_client: Optional[httpx.Client] = None,
) -> CompanyProfile:
    """Execute the end-to-end company research pipeline anchoring in registry, discovering websites, and extracting facts.

    Args:
        organization_number: Valid 9-digit Norwegian organization number.
        client: Optional existing RegistryClient.
        cache: Optional PageCache instance.
        llm_client: Optional LLMClient instance.
        enable_web: If True, discovers and scrapes verified websites for AI extraction.
        http_client: Optional shared httpx.Client.

    Returns:
        CompanyProfile: Fully structured profile with claims, evidence, and operations telemetry.
    """
    owned_reg_client = client is None
    reg_client = client or RegistryClient(client=http_client)
    try:
        record = reg_client.get_company(organization_number)
        roles: Optional[RolesRecord] = None
        if hasattr(reg_client, "get_roles"):
            try:
                r_res = reg_client.get_roles(record.organization_number)
                if isinstance(r_res, RolesRecord):
                    roles = r_res
            except Exception:
                roles = None

        accounts: Optional[AnnualAccountsRecord] = None
        acc_status = "not_available"
        if hasattr(reg_client, "get_annual_accounts"):
            try:
                a_res = reg_client.get_annual_accounts(record.organization_number)
                if isinstance(a_res, tuple) and len(a_res) == 2:
                    accounts, acc_status = a_res
                elif isinstance(a_res, AnnualAccountsRecord):
                    accounts, acc_status = a_res, "available"
            except Exception:
                accounts, acc_status = None, "not_available"
    finally:
        if owned_reg_client:
            reg_client.close()

    evidence_item = _create_registry_evidence(record, evidence_id="ev-reg-1")
    claims, unknowns = build_claims_from_record(record, evidence_id=evidence_item.id)
    evidence_list: list[Evidence] = [evidence_item]

    requests_count = 1

    # 1. Add roles claims & evidence
    if roles and (roles.ceo or roles.chair or roles.board_members):
        requests_count += 1
        roles_text = f'"organisasjonsnummer": "{record.organization_number}", "ceo": "{roles.ceo or ""}", "chair": "{roles.chair or ""}", "board_members": {json.dumps(roles.board_members)}'
        ev_roles = Evidence(
            id="ev-reg-roles",
            source_url=roles.source_url,
            source_name="Brønnøysundregistrene Enhetsregisteret (Roller)",
            retrieved_at=roles.retrieved_at,
            supporting_text=roles_text,
            note=f"Official registered leadership and board members for {record.legal_name}. Birth dates discarded.",
            source_type=SourceType.OFFICIAL_REGISTRY,
            content_sha256=hashlib.sha256(roles_text.encode("utf-8")).hexdigest(),
        )
        evidence_list.append(ev_roles)

        if roles.ceo:
            claims.append(Claim(field="ceo", value=roles.ceo, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev_roles.id]))
            claims.append(Claim(field="leadership", value=roles.ceo, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev_roles.id]))
        else:
            claims.append(Claim(field="ceo", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))

        if roles.chair:
            claims.append(Claim(field="board_chair", value=roles.chair, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev_roles.id]))
        else:
            claims.append(Claim(field="board_chair", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))

        if roles.board_members:
            claims.append(Claim(field="board_members", value=roles.board_members, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev_roles.id]))
        else:
            claims.append(Claim(field="board_members", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))
    else:
        claims.append(Claim(field="ceo", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))
        claims.append(Claim(field="board_chair", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))
        claims.append(Claim(field="board_members", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))

    # 2. Add annual accounts claims & evidence
    if accounts and (accounts.revenue is not None or accounts.operating_result is not None or accounts.annual_result is not None):
        requests_count += 1
        acc_text = f'"organisasjonsnummer": "{record.organization_number}", "regnskapsperiode": "{accounts.period}", "valuta": "{accounts.currency}", "sumDriftsinntekter": {accounts.revenue}, "driftsresultat": {accounts.operating_result}, "aarsresultat": {accounts.annual_result}'
        ev_acc = Evidence(
            id="ev-reg-accounts",
            source_url=accounts.source_url,
            source_name=f"Brønnøysundregistrene Regnskapsregisteret ({record.organization_number})",
            retrieved_at=accounts.retrieved_at,
            reporting_period=accounts.period,
            supporting_text=acc_text,
            note=f"Official filed annual accounts for {record.legal_name} ({accounts.period}, currency: {accounts.currency}).",
            source_type=SourceType.OFFICIAL_REPORT,
            content_sha256=hashlib.sha256(acc_text.encode("utf-8")).hexdigest(),
        )
        evidence_list.append(ev_acc)

        claims.append(Claim(field="financial_revenue", value=accounts.revenue, availability=AvailabilityStatus.AVAILABLE if accounts.revenue is not None else AvailabilityStatus.NOT_AVAILABLE, evidence_ids=[ev_acc.id] if accounts.revenue is not None else []))
        claims.append(Claim(field="financial_operating_result", value=accounts.operating_result, availability=AvailabilityStatus.AVAILABLE if accounts.operating_result is not None else AvailabilityStatus.NOT_AVAILABLE, evidence_ids=[ev_acc.id] if accounts.operating_result is not None else []))
        claims.append(Claim(field="financial_annual_result", value=accounts.annual_result, availability=AvailabilityStatus.AVAILABLE if accounts.annual_result is not None else AvailabilityStatus.NOT_AVAILABLE, evidence_ids=[ev_acc.id] if accounts.annual_result is not None else []))
        claims.append(Claim(field="financial_currency", value=accounts.currency, availability=AvailabilityStatus.AVAILABLE if accounts.currency else AvailabilityStatus.NOT_AVAILABLE, evidence_ids=[ev_acc.id] if accounts.currency else []))
        claims.append(Claim(field="financial_period", value=accounts.period, availability=AvailabilityStatus.AVAILABLE if accounts.period else AvailabilityStatus.NOT_AVAILABLE, evidence_ids=[ev_acc.id] if accounts.period else []))
        claims.append(Claim(field="financial_figures", value=accounts.revenue or accounts.operating_result, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev_acc.id]))
    elif acc_status == "failed":
        requests_count += 1
        supp_fail = f"Official Regnskapsregisteret retrieval returned technical or server error (HTTP 500) for {record.organization_number}"
        ev_acc_fail = Evidence(
            id="ev-reg-accounts-failed",
            source_url=f"https://data.brreg.no/regnskapsregisteret/regnskap/{record.organization_number}",
            source_name=f"Brønnøysundregistrene Regnskapsregisteret ({record.organization_number})",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            supporting_text=supp_fail,
            note=f"Annual accounts retrieval failed due to technical server error. Registry data preserved.",
            source_type=SourceType.OFFICIAL_REPORT,
            content_sha256=hashlib.sha256(supp_fail.encode("utf-8")).hexdigest(),
        )
        evidence_list.append(ev_acc_fail)
        claims.append(Claim(field="financial_revenue", value=None, availability=AvailabilityStatus.FAILED, evidence_ids=[ev_acc_fail.id]))
        claims.append(Claim(field="financial_operating_result", value=None, availability=AvailabilityStatus.FAILED, evidence_ids=[ev_acc_fail.id]))
        claims.append(Claim(field="financial_annual_result", value=None, availability=AvailabilityStatus.FAILED, evidence_ids=[ev_acc_fail.id]))
    else:
        claims.append(Claim(field="financial_revenue", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))
        claims.append(Claim(field="financial_operating_result", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))
        claims.append(Claim(field="financial_annual_result", value=None, availability=AvailabilityStatus.NOT_AVAILABLE))

    total_cost_usd = 0.0

    # -------------------------------------------------------------------------
    # Phase 3: Web Discovery, Page Crawling, and AI Fact Extraction
    # -------------------------------------------------------------------------
    if enable_web:
        page_cache = cache or PageCache()
        ai_client = llm_client or LLMClient()

        # Determine candidates: registry website first, else legal name heuristics
        candidate_urls: list[str] = []
        is_registry_site = bool(record.website)
        if record.website:
            candidate_urls.append(record.website)
        else:
            candidate_urls.extend(_propose_website_candidates(record.legal_name))

        city_hint = (record.business_address.city if record.business_address else None) or (
            record.postal_address.city if record.postal_address else None
        )
        address_hint = (record.business_address.formatted if record.business_address else None) or (
            record.postal_address.formatted if record.postal_address else None
        )

        verified_website_url: Optional[str] = None
        verified_pages: list[CleanedWebPage] = []

        for candidate in candidate_urls:
            fetch_res = fetch_page_with_cache(
                candidate, cache=page_cache, client=http_client
            )
            cleaned_homepage, outcome = fetch_res[0], fetch_res[1]
            err_class = getattr(fetch_res, "error_class", None)
            requests_count += 1

            if outcome == AvailabilityStatus.BLOCKED and is_registry_site:
                err_label = err_class or "AccessBlocked"
                supp_text = f"HTTP access blocked or denied (HTTP 403 / anti-bot challenge) for {record.website}"
                ev_blocked = Evidence(
                    id="ev-web-blocked",
                    source_url=record.website,
                    source_name=f"Official Registered Website ({record.website})",
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    supporting_text=supp_text,
                    note=f"Official registered website access was blocked/denied (reason: {err_label}). Registered company status remains {record.registered_status}.",
                    source_type=SourceType.VERIFIED_COMPANY_WEBSITE,
                    content_sha256=hashlib.sha256(supp_text.encode("utf-8")).hexdigest(),
                )
                evidence_list.append(ev_blocked)
                for c in claims:
                    if c.field == "official_website":
                        c.availability = AvailabilityStatus.BLOCKED
                        c.evidence_ids = [ev_blocked.id]
                break

            if is_registry_site and not cleaned_homepage:
                err_label = err_class or "TechnicalError"
                supp_text = f"HTTP retrieval failed for technical or unsuccessful reason ({err_label}) for {record.website}"
                ev_failed = Evidence(
                    id="ev-web-failed",
                    source_url=record.website,
                    source_name=f"Official Registered Website ({record.website})",
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    supporting_text=supp_text,
                    note=f"Official registered website retrieval failed (error: {err_label}). Registered company status remains {record.registered_status}.",
                    source_type=SourceType.VERIFIED_COMPANY_WEBSITE,
                    content_sha256=hashlib.sha256(supp_text.encode("utf-8")).hexdigest(),
                )
                evidence_list.append(ev_failed)
                for c in claims:
                    if c.field == "official_website":
                        c.availability = AvailabilityStatus.FAILED
                        c.evidence_ids = [ev_failed.id]
                break

            if not cleaned_homepage:
                continue

            # Verify that the candidate belongs to the target legal entity
            if is_registry_site:
                # Registry website is the authoritative registered web domain from Enhetsregisteret
                is_verified = True
                reason = "official_registry_website"
            else:
                is_verified, reason = verify_website_identity(
                    pages=[cleaned_homepage],
                    organization_number=record.organization_number,
                    legal_name=record.legal_name or "",
                    city=city_hint,
                    address=address_hint,
                )

                # If homepage alone didn't confirm identity, check candidate About/Contact subpages
                if not is_verified:
                    candidate_subpages = discover_target_pages(candidate, cleaned_homepage, max_pages=2)
                    for sub_url in candidate_subpages:
                        if sub_url == candidate:
                            continue
                        sub_cleaned, sub_out = fetch_page_with_cache(
                            sub_url, cache=page_cache, client=http_client
                        )
                        requests_count += 1
                        if sub_cleaned and sub_out == AvailabilityStatus.AVAILABLE:
                            is_verified, reason = verify_website_identity(
                                pages=[cleaned_homepage, sub_cleaned],
                                organization_number=record.organization_number,
                                legal_name=record.legal_name or "",
                                city=city_hint,
                                address=address_hint,
                            )
                            if is_verified:
                                verified_pages.append(sub_cleaned)
                                break

            if is_verified:
                verified_website_url = cleaned_homepage.url
                verified_pages.append(cleaned_homepage)

                # Discover targeted sub-pages (About, Contact, Careers), capped
                target_subpages = discover_target_pages(
                    homepage_url=verified_website_url,
                    homepage=cleaned_homepage,
                    max_pages=4,
                )

                for subpage_url in target_subpages:
                    if subpage_url == verified_website_url:
                        continue
                    if any(p.url == subpage_url for p in verified_pages):
                        continue
                    cleaned_subpage, sub_outcome = fetch_page_with_cache(
                        subpage_url, cache=page_cache, client=http_client
                    )
                    requests_count += 1
                    if cleaned_subpage and sub_outcome == AvailabilityStatus.AVAILABLE:
                        verified_pages.append(cleaned_subpage)

                break  # Successfully verified official company site

        # If official website verified, update official_website claim and extract facts
        if verified_website_url and verified_pages:
            homepage_page = verified_pages[0]
            web_ev_id = f"ev-web-home"
            homepage_evidence = Evidence(
                id=web_ev_id,
                source_url=verified_website_url,
                source_name=f"Verified Company Website ({verified_website_url})",
                retrieved_at=homepage_page.retrieved_at,
                supporting_text=f'"organisasjonsnummer": "{record.organization_number}", "url": "{verified_website_url}"',
                note=f"Verified official homepage for {record.legal_name} ({record.organization_number})",
                source_type=SourceType.VERIFIED_COMPANY_WEBSITE,
                content_sha256=homepage_page.content_sha256,
            )
            evidence_list.append(homepage_evidence)

            # Update official_website claim
            for c in claims:
                if c.field == "official_website":
                    c.value = verified_website_url
                    c.availability = AvailabilityStatus.AVAILABLE
                    c.evidence_ids = [web_ev_id]
            if "official_website" in unknowns:
                unknowns.remove("official_website")

            # Check if any verified page is a careers page
            careers_page = next((p for p in verified_pages if any(kw in p.url.lower() for kw in CAREERS_KEYWORDS)), None)
            if careers_page:
                ev_careers_id = "ev-web-careers"
                ev_careers = Evidence(
                    id=ev_careers_id,
                    source_url=careers_page.url,
                    source_name=f"Verified Careers Page ({careers_page.url})",
                    retrieved_at=careers_page.retrieved_at,
                    supporting_text=f"Official careers page: {careers_page.url}",
                    note=f"Verified official careers page for {record.legal_name}.",
                    source_type=SourceType.VERIFIED_COMPANY_WEBSITE,
                    content_sha256=careers_page.content_sha256,
                )
                evidence_list.append(ev_careers)
                claims.append(Claim(field="careers_url", value=careers_page.url, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev_careers_id]))

            # Extract facts from each verified page
            for page in verified_pages:
                extracted_claims, extracted_ev, llm_resp = extract_claims_from_page(
                    page=page,
                    legal_name=record.legal_name or "",
                    org_number=record.organization_number,
                    llm_client=ai_client,
                    evidence_index_start=len(evidence_list) + 1,
                )
                total_cost_usd += llm_resp.estimated_cost_usd

                for ex_ev in extracted_ev:
                    evidence_list.append(ex_ev)

                for ex_claim in extracted_claims:
                    # Update or add claim
                    existing = next((c for c in claims if c.field == ex_claim.field), None)
                    if existing:
                        if existing.availability != AvailabilityStatus.AVAILABLE:
                            existing.value = ex_claim.value
                            existing.availability = AvailabilityStatus.AVAILABLE
                            existing.evidence_ids = ex_claim.evidence_ids
                    else:
                        claims.append(ex_claim)

                    if ex_claim.field in unknowns:
                        unknowns.remove(ex_claim.field)

    legal_identity = LegalIdentity(
        organization_number=record.organization_number,
        legal_name=record.legal_name,
        entity_type=record.entity_type,
        entity_type_description=record.entity_type_description,
        registered_status=record.registered_status,
    )

    return CompanyProfile(
        organization_number=record.organization_number,
        legal_identity=legal_identity,
        claims=claims,
        evidence=evidence_list,
        unknowns=unknowns,
        retrieved_at=record.retrieved_at,
        requests_count=requests_count,
        third_party_cost_usd=round(total_cost_usd, 6),
    )
