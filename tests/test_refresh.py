"""Tests for snapshot refresh, idempotency, change events, budget tracking, and safe retries."""

from __future__ import annotations

from pathlib import Path
import httpx
import pytest
from signalpost.budget import BudgetLimits, BudgetTracker, execute_with_retry, is_transient_error
from signalpost.models import (
    AvailabilityStatus,
    Claim,
    CompanyProfile,
    Evidence,
    LegalIdentity,
    SourceType,
)
from signalpost.refresh import (
    ChangeEvent,
    SnapshotStore,
    canonicalize_value,
    refresh_and_detect_changes,
)


def _make_base_profile(org="923609016", addr="Forusbeen 50, 4035 STAVANGER") -> CompanyProfile:
    ev = Evidence(
        id="ev-reg-1",
        source_url=f"https://data.brreg.no/api/enheter/{org}",
        retrieved_at="2026-10-13T10:00:00Z",
        supporting_text=f"forretningsadresse: {addr}",
        content_sha256="1234" * 16,
    )
    identity = LegalIdentity(
        organization_number=org,
        legal_name="EQUINOR ASA",
        registered_status="active",
    )
    claims = [
        Claim(field="legal_name", value="EQUINOR ASA", availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev.id]),
        Claim(field="business_address", value=addr, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev.id]),
        Claim(field="employee_count", value=21272, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev.id]),
        Claim(field="official_website", value="https://www.equinor.com", availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev.id]),
    ]
    return CompanyProfile(
        organization_number=org,
        legal_identity=identity,
        claims=claims,
        evidence=[ev],
        retrieved_at="2026-10-13T10:00:00Z",
        requests_count=1,
    )


def test_canonicalize_values():
    """Verify value canonicalization handles whitespace, numbers, and types."""
    assert canonicalize_value("  Forusbeen   50,  4035   STAVANGER  ") == "Forusbeen 50, 4035 STAVANGER"
    assert canonicalize_value(21272.0) == 21272
    assert canonicalize_value(21272) == 21272
    assert canonicalize_value(None) is None
    assert canonicalize_value([" A ", "  B  "]) == ["A", "B"]


def test_idempotency_run_twice_unchanged_sources(tmp_path):
    """Exit criteria: Two identical runs produce no duplicate facts and no false changes."""
    store = SnapshotStore(snapshot_dir=tmp_path)
    profile1 = _make_base_profile()

    # Run 1: First snapshot stored
    p1_out, changes1 = refresh_and_detect_changes(profile1, store=store, detected_at="2026-10-13T10:00:00Z")
    assert changes1 == []
    assert len(p1_out.claims) == 4

    # Run 2: Exact same data
    profile2 = _make_base_profile()
    p2_out, changes2 = refresh_and_detect_changes(profile2, store=store, detected_at="2026-10-13T10:05:00Z")
    
    # Confirm: zero false changes, no duplicate claims or evidence
    assert changes2 == []
    assert len(p2_out.claims) == 4
    assert len(p2_out.evidence) == 1
    assert p2_out.get_claim("business_address").value == "Forusbeen 50, 4035 STAVANGER"


def test_change_event_detection_with_old_and_new_evidence(tmp_path):
    """Exit criteria: A simulated real change is detected once, with both evidence records."""
    store = SnapshotStore(snapshot_dir=tmp_path)
    
    # Run 1: Original address
    profile1 = _make_base_profile(addr="Old Address 1, 4000 STAVANGER")
    refresh_and_detect_changes(profile1, store=store, detected_at="2026-10-13T10:00:00Z")

    # Run 2: Address changed
    profile2 = _make_base_profile(addr="New Corporate HQ, 4035 STAVANGER")
    ev2 = Evidence(
        id="ev-reg-2",
        source_url="https://data.brreg.no/api/enheter/923609016",
        retrieved_at="2026-10-13T11:00:00Z",
        supporting_text="forretningsadresse: New Corporate HQ, 4035 STAVANGER",
        content_sha256="5678" * 16,
    )
    profile2.evidence = [ev2]
    profile2.get_claim("business_address").evidence_ids = [ev2.id]

    p2_out, changes = refresh_and_detect_changes(profile2, store=store, detected_at="2026-10-13T11:00:00Z")

    # Confirm exactly one change event emitted
    assert len(changes) == 1
    ev_change = changes[0]
    assert ev_change.field == "business_address"
    assert ev_change.previous_value == "Old Address 1, 4000 STAVANGER"
    assert ev_change.new_value == "New Corporate HQ, 4035 STAVANGER"
    assert ev_change.change_classification == "updated"
    assert ev_change.previous_evidence is not None
    assert ev_change.previous_evidence["id"] == "ev-reg-1"
    assert ev_change.new_evidence is not None
    assert ev_change.new_evidence["id"] == "ev-reg-2"


def test_retrieval_failure_preserves_earlier_fact_without_deletion(tmp_path):
    """Exit criteria: If a source times out on run two, earlier fact is kept and no deletion is recorded."""
    store = SnapshotStore(snapshot_dir=tmp_path)

    # Run 1: Website successfully verified
    profile1 = _make_base_profile()
    refresh_and_detect_changes(profile1, store=store, detected_at="2026-10-13T10:00:00Z")

    # Run 2: Website retrieval times out (status FAILED, value None)
    profile2 = _make_base_profile()
    web_claim = profile2.get_claim("official_website")
    web_claim.availability = AvailabilityStatus.FAILED
    web_claim.value = None
    web_claim.evidence_ids = []

    p2_out, changes = refresh_and_detect_changes(profile2, store=store, detected_at="2026-10-13T11:00:00Z")

    # Earlier verified fact retained!
    retained_web = p2_out.get_claim("official_website")
    assert retained_web.availability == AvailabilityStatus.AVAILABLE
    assert retained_web.value == "https://www.equinor.com"
    # No false deletion change event recorded!
    assert changes == []


def test_budget_tracker_counters_and_log_visibility():
    """Exit criteria: Budget counters are visible in the run log."""
    tracker = BudgetTracker(limits=BudgetLimits(max_requests_per_company=10, max_total_requests=50))
    tracker.start_company("923609016")

    tracker.record_request(cost_usd=0.00010)
    tracker.record_request(cost_usd=0.00020)
    tracker.record_retry()
    tracker.record_cache_hit()
    tracker.record_completed()

    log_summary = tracker.format_log_summary()
    assert "[Budget]" in log_summary
    assert "Requests: 2" in log_summary
    assert "Retries: 1" in log_summary
    assert "Cache Hits: 1" in log_summary
    assert "Completed: 1" in log_summary
    assert "Cost: $0.00030" in log_summary


def test_budget_safety_valve_stops_optional_research():
    """Verify that optional research stops when approaching limits."""
    tracker = BudgetTracker(limits=BudgetLimits(max_requests_per_company=5))
    tracker.start_company("923609016")

    assert tracker.should_stop_optional_research() is False

    # Simulate 4 requests (80% of limit 5)
    for _ in range(4):
        tracker.record_request()

    assert tracker.should_stop_optional_research() is True


def test_execute_with_retry_only_on_transient_errors():
    """Verify retries only on transient errors (timeouts, 5xx, 429), never on permanent errors."""
    tracker = BudgetTracker()

    # 1. Transient error (HTTP 503) succeeds on 2nd attempt
    attempts = [0]
    def transient_call():
        attempts[0] += 1
        if attempts[0] < 2:
            resp = httpx.Response(503, request=httpx.Request("GET", "https://api.test"))
            raise httpx.HTTPStatusError("Service Unavailable", request=resp.request, response=resp)
        return "success"

    res = execute_with_retry(transient_call, max_retries=2, backoff_factor=0.01, budget_tracker=tracker)
    assert res == "success"
    assert attempts[0] == 2
    assert tracker.retries_count == 1

    # 2. Permanent error (HTTP 404) fails immediately without retry
    attempts_404 = [0]
    def not_found_call():
        attempts_404[0] += 1
        resp = httpx.Response(404, request=httpx.Request("GET", "https://api.test"))
        raise httpx.HTTPStatusError("Not Found", request=resp.request, response=resp)

    with pytest.raises(httpx.HTTPStatusError):
        execute_with_retry(not_found_call, max_retries=2, backoff_factor=0.01, budget_tracker=tracker)
    assert attempts_404[0] == 1  # No retry attempted


def test_evidence_retrieved_at_timestamps_do_not_cause_false_changes(tmp_path):
    """Explicit check: Two runs retrieving the same facts with different timestamps emit 0 changes."""
    store = SnapshotStore(snapshot_dir=tmp_path)

    # Run 1: retrieved at 10:00
    p1 = _make_base_profile()
    p1.retrieved_at = "2026-10-13T10:00:00Z"
    for ev in p1.evidence:
        ev.retrieved_at = "2026-10-13T10:00:00Z"
    _, ch1 = refresh_and_detect_changes(p1, store=store, detected_at="2026-10-13T10:00:00Z")
    assert ch1 == []

    # Run 2: retrieved at 14:00 (4 hours later), but identical claim values
    p2 = _make_base_profile()
    p2.retrieved_at = "2026-10-13T14:00:00Z"
    for ev in p2.evidence:
        ev.retrieved_at = "2026-10-13T14:00:00Z"
    _, ch2 = refresh_and_detect_changes(p2, store=store, detected_at="2026-10-13T14:00:00Z")

    # Confirm 0 false changes emitted despite different retrieved_at timestamps
    assert ch2 == []


def test_web_text_ab_testing_minor_noise_does_not_trigger_false_change(tmp_path):
    """Verify minor marketing copy variation / A/B rotation (>=85% similarity) does not trigger false changes."""
    store = SnapshotStore(snapshot_dir=tmp_path)

    p1 = _make_base_profile()
    desc1 = "Equinor is an international energy company committed to providing reliable energy in a volatile world."
    ev1 = _make_base_profile().evidence[0]
    p1.claims.append(Claim(field="business_description", value=desc1, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev1.id]))
    refresh_and_detect_changes(p1, store=store, detected_at="2026-10-13T10:00:00Z")

    # Run 2: Minor A/B phrasing rotation (over 85% overlap)
    p2 = _make_base_profile()
    desc2 = "Equinor is an international energy company committed to providing reliable energy in an uncertain world."
    p2.claims.append(Claim(field="business_description", value=desc2, availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev1.id]))
    p2_out, ch2 = refresh_and_detect_changes(p2, store=store, detected_at="2026-10-13T14:00:00Z")

    # Treated as minor noise, stable description preserved, 0 false change events emitted!
    assert ch2 == []
    assert p2_out.get_claim("business_description").value == desc1


def test_snapshot_store_tolerates_unwritable_directory():
    """Verify that a snapshot store given an invalid/unwritable directory does not crash."""
    # Point to an impossible / unwritable file path as directory
    invalid_path = Path("out/snapshots_dummy.txt/impossible_sub_dir")
    store = SnapshotStore(snapshot_dir=invalid_path)

    p1 = _make_base_profile()
    # Save and refresh must complete cleanly without throwing OSError or crashing
    p1_out, changes = refresh_and_detect_changes(p1, store=store)
    assert p1_out.organization_number == "923609016"
    assert changes == []


def test_deterministic_synthesis_generation():
    """Verify deterministic synthesis describes activity, changes, and explicit unknowns with citations."""
    from signalpost.synthesis import build_deterministic_synthesis

    profile = _make_base_profile()
    profile.claims.append(Claim(field="employee_count_scope", value="legal_entity", availability=AvailabilityStatus.AVAILABLE))
    profile.claims.append(Claim(field="is_part_of_group", value=True, availability=AvailabilityStatus.AVAILABLE))
    synth = build_deterministic_synthesis(profile, changes=[])

    assert "EQUINOR ASA is an active Norwegian" in synth["what_it_does"]
    assert "No changes detected" in synth["what_changed"]
    # Explicit unknown: group workforce unknown
    assert "Group-wide workforce unknown" in synth["what_remains_unknown"]
    assert len(synth["cited_evidence_ids"]) > 0
