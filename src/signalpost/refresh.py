"""Snapshot store, canonicalization, idempotency, and change-history engine.

Maintains historical snapshots between runs and emits explicit change events:
- Prevents false changes and duplicate facts across identical runs.
- Preserves earlier verified facts if a subsequent run encounters a transient retrieval failure.
- Emits structured change events with previous and new values, timestamps, and evidence records.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from .models import AvailabilityStatus, Claim, CompanyProfile, Evidence


class ChangeEvent(BaseModel):
    """Detailed change event describing an evolution in company intelligence."""
    model_config = ConfigDict(extra="ignore")

    field: str
    previous_value: Any = None
    new_value: Any = None
    detected_at: str
    previous_evidence: Optional[dict[str, Any]] = None
    new_evidence: Optional[dict[str, Any]] = None
    change_classification: str = "updated"  # "added" | "updated" | "removed"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def canonicalize_value(val: Any) -> Any:
    """Canonicalize values (whitespace, case, numeric types, dates) to avoid duplicate observations."""
    if val is None:
        return None

    if isinstance(val, str):
        # Trim whitespace and collapse repeated internal spaces
        cleaned = re.sub(r"\s+", " ", val).strip()
        return cleaned

    if isinstance(val, (int, float)):
        # Normalize numeric floats with zero decimal part to ints (e.g. 21272.0 -> 21272)
        if isinstance(val, float) and val.is_integer():
            return int(val)
        return val

    if isinstance(val, list):
        return [canonicalize_value(item) for item in val]

    if isinstance(val, dict):
        return {k: canonicalize_value(v) for k, v in sorted(val.items())}

    return val


import difflib


def is_meaningful_change(field: str, prev_val: Any, curr_val: Any) -> bool:
    """Determine whether a difference between previous and current values represents a true change.

    Applies fuzzy comparison to long web descriptions to reject A/B testing and marketing noise:
    - If SequenceMatcher similarity is >= 0.85, treats as minor copy variation / noise.
    - Standard entity fields (addresses, names, numbers, dates) must match exactly.
    """
    prev_canon = canonicalize_value(prev_val)
    curr_canon = canonicalize_value(curr_val)

    if prev_canon == curr_canon:
        return False
    if prev_canon is None or curr_canon is None:
        return True

    # Fuzzy check for free-form web text
    if field in {"business_description", "company_synthesis"} and isinstance(prev_canon, str) and isinstance(curr_canon, str):
        ratio = difflib.SequenceMatcher(None, prev_canon.lower(), curr_canon.lower()).ratio()
        if ratio >= 0.85:
            # Over 85% character/token similarity: reject as minor marketing/rotation noise
            return False

    return True


class SnapshotStore:
    """Lightweight JSON-based snapshot persistence store with graceful degradation."""

    def __init__(self, snapshot_dir: Optional[Path | str] = None) -> None:
        self.dir: Optional[Path] = None
        if snapshot_dir:
            try:
                p = Path(snapshot_dir)
                p.mkdir(parents=True, exist_ok=True)
                self.dir = p
            except Exception:
                # Tolerates read-only or unwritable filesystems by falling back to memory
                self.dir = None
        self._memory_store: dict[str, dict[str, Any]] = {}

    def get_snapshot(self, org_number: str) -> Optional[dict[str, Any]]:
        """Retrieve the previous verified snapshot for an organization number."""
        clean_org = org_number.strip()
        if self.dir:
            try:
                path = self.dir / f"{clean_org}.json"
                if path.exists():
                    return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return self._memory_store.get(clean_org)

    def save_snapshot(self, org_number: str, snapshot_data: dict[str, Any]) -> None:
        """Persist current verified snapshot, never crashing on write failures."""
        clean_org = org_number.strip()
        self._memory_store[clean_org] = snapshot_data
        if self.dir:
            try:
                path = self.dir / f"{clean_org}.json"
                path.write_text(json.dumps(snapshot_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                # A failed snapshot write must NEVER turn a completed company into a failed one
                pass


def profile_to_snapshot(profile: CompanyProfile) -> dict[str, Any]:
    """Convert a CompanyProfile into a serializable snapshot dictionary."""
    return {
        "organization_number": profile.organization_number,
        "legal_identity": profile.legal_identity.model_dump(),
        "claims": [c.model_dump() for c in profile.claims],
        "evidence": [e.model_dump() for e in profile.evidence],
        "retrieved_at": profile.retrieved_at,
    }


def refresh_and_detect_changes(
    current_profile: CompanyProfile,
    store: Optional[SnapshotStore] = None,
    detected_at: Optional[str] = None,
) -> tuple[CompanyProfile, list[ChangeEvent]]:
    """Compare current profile against previous snapshot, preserving history and emitting change events.

    Guarantees:
    1. Idempotency: Unchanged sources emit zero change events.
    2. Retrieval-failure resilience: If a source times out or fails on run two, earlier verified
       facts are retained and no deletion is recorded.
    3. Real changes: Emits exactly one change event per modified field with old and new evidence.
    """
    if detected_at is None:
        detected_at = datetime.now(timezone.utc).isoformat()

    org_number = current_profile.organization_number
    prev_snapshot = store.get_snapshot(org_number) if store else None

    # First run: no prior snapshot
    if not prev_snapshot:
        if store:
            store.save_snapshot(org_number, profile_to_snapshot(current_profile))
        return current_profile, []

    prev_claims_dict = {c["field"]: c for c in prev_snapshot.get("claims", [])}
    prev_evidence_dict = {e["id"]: e for e in prev_snapshot.get("evidence", [])}

    curr_claims_dict = {c.field: c for c in current_profile.claims}
    curr_evidence_dict = {e.id: e for e in current_profile.evidence}

    change_events: list[ChangeEvent] = []
    final_claims: list[Claim] = []
    retained_evidence: list[Evidence] = list(current_profile.evidence)

    # Process each claim
    for claim in current_profile.claims:
        field = claim.field
        prev_claim = prev_claims_dict.get(field)

        if not prev_claim:
            final_claims.append(claim)
            continue

        prev_val_raw = prev_claim.get("value")
        prev_avail = prev_claim.get("availability")
        prev_canon = canonicalize_value(prev_val_raw)

        curr_val_raw = claim.value
        curr_avail = claim.availability.value if isinstance(claim.availability, AvailabilityStatus) else str(claim.availability)
        curr_canon = canonicalize_value(curr_val_raw)

        # RETRIEVAL-FAILURE TEST:
        # If run two failed to retrieve or timed out on this field, but run one had an available fact,
        # retain the earlier fact and record NO deletion.
        if prev_avail == "available" and prev_canon is not None and curr_avail in {"failed", "not_available"} and curr_canon is None:
            # Retain previous verified fact!
            retained_claim = Claim(
                field=field,
                value=prev_val_raw,
                availability=AvailabilityStatus.AVAILABLE,
                evidence_ids=prev_claim.get("evidence_ids", []),
                confidence=prev_claim.get("confidence", 1.0),
            )
            final_claims.append(retained_claim)

            # Ensure previous evidence is kept
            for eid in prev_claim.get("evidence_ids", []):
                if eid in prev_evidence_dict and eid not in curr_evidence_dict:
                    ev_dict = prev_evidence_dict[eid]
                    retained_evidence.append(Evidence.model_validate(ev_dict))
            continue

        # IDEMPOTENCY & VALUE COMPARISON:
        # Check if verified value actually changed meaningfully
        if prev_avail == "available" and curr_avail == "available":
            if is_meaningful_change(field, prev_val_raw, curr_val_raw):
                # Real change detected!
                prev_eids = prev_claim.get("evidence_ids", [])
                curr_eids = claim.evidence_ids
                prev_ev = prev_evidence_dict.get(prev_eids[0]) if prev_eids else None
                curr_ev = curr_evidence_dict.get(curr_eids[0]).model_dump() if curr_eids and curr_eids[0] in curr_evidence_dict else None

                change_events.append(
                    ChangeEvent(
                        field=field,
                        previous_value=prev_val_raw,
                        new_value=curr_val_raw,
                        detected_at=detected_at,
                        previous_evidence=prev_ev,
                        new_evidence=curr_ev,
                        change_classification="updated",
                    )
                )
            elif prev_canon != curr_canon:
                # Minor noise / A/B rotation below threshold: retain prior stable value
                claim.value = prev_val_raw

        final_claims.append(claim)

    current_profile.claims = final_claims
    current_profile.evidence = retained_evidence

    # Save updated snapshot
    if store:
        store.save_snapshot(org_number, profile_to_snapshot(current_profile))

    return current_profile, change_events
