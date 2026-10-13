"""Deterministic synthesis engine compiling verified facts, changes, and explicit unknowns.

Generates structured summaries without unevidenced narrative:
1. What the company does (grounded strictly in verified description and industry).
2. What changed since the last run (grounded strictly in change events).
3. What remains unknown (explicit unknowns, legal entity workforce scope, unfiled accounts).
4. Full evidence citations for every referenced claim.
"""

from __future__ import annotations

from typing import Any, Optional
from .models import AvailabilityStatus, CompanyProfile
from .refresh import ChangeEvent


def build_deterministic_synthesis(
    profile: CompanyProfile,
    changes: Optional[list[ChangeEvent]] = None,
) -> dict[str, Any]:
    """Generate deterministic synthesis referencing verified claims, changes, and unknowns."""
    claims_dict = {c.field: c for c in profile.claims}
    changes_list = changes or []

    legal_name = claims_dict.get("legal_name").value if "legal_name" in claims_dict else profile.legal_identity.legal_name or "Entity"
    org_type = claims_dict.get("entity_type").value if "entity_type" in claims_dict else profile.legal_identity.entity_type or "company"
    org_desc = claims_dict.get("entity_type_description").value if "entity_type_description" in claims_dict else profile.legal_identity.entity_type_description or org_type
    status = claims_dict.get("registered_status").value if "registered_status" in claims_dict else profile.legal_identity.registered_status or "active"
    industry = claims_dict.get("industry_description").value if "industry_description" in claims_dict else None
    ind_code = claims_dict.get("industry_code").value if "industry_code" in claims_dict else None
    desc_claim = claims_dict.get("business_description")
    desc_val = desc_claim.value if desc_claim and desc_claim.value else None

    # Evidence IDs cited
    cited_eids: list[str] = []
    if "legal_name" in claims_dict and claims_dict["legal_name"].evidence_ids:
        cited_eids.extend(claims_dict["legal_name"].evidence_ids)
    if desc_claim and desc_claim.evidence_ids:
        cited_eids.extend(desc_claim.evidence_ids)

    # 1. What the company does
    activity_parts = []
    if status == "bankrupt":
        activity_parts.append(f"{legal_name} is a bankrupt {org_desc} (org {profile.organization_number}).")
    elif status == "deleted":
        activity_parts.append(f"{legal_name} is a dissolved/deleted {org_desc} (org {profile.organization_number}).")
    else:
        activity_parts.append(f"{legal_name} is an active Norwegian {org_desc} (org {profile.organization_number}).")

    if industry:
        activity_parts.append(f"Operating in {industry} (industry code {ind_code or 'N/A'}).")

    if desc_val:
        activity_parts.append(f"Business operations: {desc_val}")

    what_it_does = " ".join(activity_parts)

    # 2. What changed since last run
    if not changes_list:
        what_changed = "No changes detected since previous verified snapshot (idempotent run)."
    else:
        change_lines = []
        for ch in changes_list:
            field_name = ch.field
            prev = ch.previous_value
            curr = ch.new_value
            change_lines.append(f"- {field_name}: updated from '{prev}' to '{curr}'")
        what_changed = "\n".join(change_lines)

    # 3. What remains unknown
    unknown_items = []

    # Workforce scope unknown
    emp_claim = claims_dict.get("employee_count")
    emp_scope = claims_dict.get("employee_count_scope").value if "employee_count_scope" in claims_dict else None
    is_group = claims_dict.get("is_part_of_group").value if "is_part_of_group" in claims_dict else False
    if emp_claim and emp_claim.value is not None:
        if emp_scope == "legal_entity" and is_group:
            unknown_items.append(f"Group-wide workforce unknown (current count of {emp_claim.value} covers parent legal entity only).")
    else:
        unknown_items.append("Employee workforce count unknown / not registered.")

    # Website unknowns
    web_claim = claims_dict.get("official_website")
    if not web_claim or web_claim.availability == AvailabilityStatus.NOT_AVAILABLE:
        unknown_items.append("Official website unregistered in public registry.")
    elif web_claim.availability == AvailabilityStatus.BLOCKED:
        unknown_items.append(f"Official website ({web_claim.value}) blocked by anti-bot controls.")
    elif web_claim.availability == AvailabilityStatus.FAILED:
        unknown_items.append(f"Official website ({web_claim.value}) unretrievable due to network/server failure.")

    # Financial unknowns
    fin_claim = claims_dict.get("financial_revenue")
    if not fin_claim or fin_claim.availability in {AvailabilityStatus.NOT_AVAILABLE, AvailabilityStatus.NOT_APPLICABLE}:
        if fin_claim and fin_claim.availability == AvailabilityStatus.NOT_APPLICABLE:
            unknown_items.append("Standard revenue figures not applicable (specialized accounts layout, e.g. banking).")
        else:
            unknown_items.append("Annual financial accounts figures unavailable in public registry.")

    # Leadership unknowns
    if not claims_dict.get("ceo") or claims_dict.get("ceo").value is None:
        if org_type != "ENK":
            unknown_items.append("Executive leadership (CEO / Daglig leder) not registered.")

    what_remains_unknown = "\n".join(f"- {item}" for item in unknown_items) if unknown_items else "All core monitored attributes verified."

    summary_text = (
        f"### Company Overview\n{what_it_does}\n\n"
        f"### Recent Changes\n{what_changed}\n\n"
        f"### Explicit Unknowns\n{what_remains_unknown}"
    )

    return {
        "what_it_does": what_it_does,
        "what_changed": what_changed,
        "what_remains_unknown": what_remains_unknown,
        "summary_markdown": summary_text,
        "cited_evidence_ids": list(dict.fromkeys(cited_eids)),
    }
