"""Deterministic research pipeline anchoring company intelligence in the Norwegian registry."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

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
from .registry import CompanyRecord, RegistryClient, fetch_company_record


def _create_registry_evidence(record: CompanyRecord, evidence_id: str = "ev-reg-1") -> Evidence:
    """Create an immutable evidence record referencing the official registry call."""
    raw_hash = None
    if record.raw_response:
        raw_bytes = json.dumps(record.raw_response, sort_keys=True, ensure_ascii=False).encode("utf-8")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()

    supporting_parts = [f"Organisasjonsnummer: {record.organization_number}"]
    if record.legal_name:
        supporting_parts.append(f"Navn: {record.legal_name}")
    if record.entity_type:
        supporting_parts.append(f"Form: {record.entity_type}")
    if record.employee_count is not None:
        group_desc = "corporate group member (erIKonsern: True)" if record.is_part_of_group else "independent entity (erIKonsern: False)"
        supporting_parts.append(
            f"Ansatte: {record.employee_count} direct employees under legal entity {record.organization_number} "
            f"(source: {record.employee_count_source or 'NAV Aa-registeret'}; scope: legal_entity_only, does NOT include subsidiaries or group-wide workforce; corporate structure: {group_desc})"
        )


    return Evidence(
        id=evidence_id,
        source_url=record.registry_source_url,
        source_name="Brønnøysundregistrene Enhetsregisteret",
        retrieved_at=record.retrieved_at,
        publication_date=record.registration_date,
        reporting_period=record.latest_submitted_accounts,
        supporting_text="; ".join(supporting_parts),
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
) -> CompanyProfile:
    """Execute the deterministic registry-anchored research pipeline for a single company.

    Args:
        organization_number: Valid 9-digit Norwegian organization number.
        client: Optional existing RegistryClient.

    Returns:
        CompanyProfile: Fully structured profile with claims and evidence.
    """
    if client is not None:
        record = client.get_company(organization_number)
    else:
        record = fetch_company_record(organization_number)

    evidence_item = _create_registry_evidence(record, evidence_id="ev-reg-1")
    claims, unknowns = build_claims_from_record(record, evidence_id=evidence_item.id)

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
        evidence=[evidence_item],
        unknowns=unknowns,
        retrieved_at=record.retrieved_at,
    )
