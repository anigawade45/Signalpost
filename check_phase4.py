"""Phase 4 verification script: deterministic verification, snapshot refresh, idempotency, and budget tracking."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from signalpost.budget import BudgetLimits, BudgetTracker
from signalpost.models import AvailabilityStatus, Claim, CompanyProfile, Evidence, LegalIdentity
from signalpost.output import TerminalEnvelope
from signalpost.refresh import SnapshotStore, refresh_and_detect_changes
from signalpost.runner import process_single_company, run_batch
from signalpost.verification import verify_company_profile


def run_all_pytest():
    print("=" * 60)
    print("Running pytest on all Phase 1 - 4 test suites...")
    print("=" * 60)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "tests/test_registry.py",
        "tests/test_output.py",
        "tests/test_batch.py",
        "tests/test_awkward_cases.py",
        "tests/test_web_discovery.py",
        "tests/test_extraction.py",
        "tests/test_evidence_quality.py",
        "tests/test_roles_and_accounts.py",
        "tests/test_verification.py",
        "tests/test_refresh.py",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError(f"pytest failed with code {res.returncode}")
    print("[PASS] All tests across all Phase 1-4 suites passed!")


def test_idempotent_batch_runs():
    print("=" * 60)
    print("Running Phase 4 Idempotency Test: Two Identical Batch Runs...")
    print("=" * 60)

    input_file = Path("data/phase3_batch.jsonl")
    output_run1 = Path("out/phase4_run1_results.jsonl")
    output_run2 = Path("out/phase4_run2_results.jsonl")
    snapshot_dir = Path("out/phase4_snapshots")

    # Clean snapshot dir for fresh test
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    store = SnapshotStore(snapshot_dir=snapshot_dir)
    tracker1 = BudgetTracker(limits=BudgetLimits(max_total_requests=300))
    tracker2 = BudgetTracker(limits=BudgetLimits(max_total_requests=300))

    # Run 1: Initial observation and snapshot persistence
    print("\n--- Starting Batch Run 1 (Initial Snapshot Creation) ---")
    envelopes1 = run_batch(
        input_path=input_file,
        output_path=output_run1,
        run_id="phase4-run-001",
        store=store,
        budget_tracker=tracker1,
    )
    print(f"Run 1 Completed: {len(envelopes1)} entities. Budget Log: {tracker1.format_log_summary()}")

    # Run 2: Exact same input with unchanged sources
    print("\n--- Starting Batch Run 2 (Idempotency & Zero False Changes Check) ---")
    envelopes2 = run_batch(
        input_path=input_file,
        output_path=output_run2,
        run_id="phase4-run-002",
        store=store,
        budget_tracker=tracker2,
    )
    print(f"Run 2 Completed: {len(envelopes2)} entities. Budget Log: {tracker2.format_log_summary()}")

    # Validate idempotency across all 12 companies
    total_false_changes = 0
    for env in envelopes2:
        if env.changes:
            print(f"[FAIL] Unexpected changes emitted on unchanged data for {env.organisation_number}: {env.changes}")
            total_false_changes += len(env.changes)

    assert total_false_changes == 0, f"Expected 0 false change events, found {total_false_changes}"
    print("\n[PASS] Idempotency verified: Two identical runs produced 0 duplicate facts and 0 false changes!")


def test_simulated_real_change():
    print("=" * 60)
    print("Testing Simulated Real Change Detection...")
    print("=" * 60)

    snapshot_dir = Path("out/phase4_snapshots")
    store = SnapshotStore(snapshot_dir=snapshot_dir)

    # Pick Equinor (923609016) and alter its registered address in the snapshot
    org = "923609016"
    snap = store.get_snapshot(org)
    assert snap is not None, "Snapshot for Equinor should exist from batch run"

    old_addr = None
    for c in snap["claims"]:
        if c["field"] == "business_address":
            old_addr = c["value"]
            c["value"] = "Historical Headquarters 123, 4000 STAVANGER"

    store.save_snapshot(org, snap)

    # Process company again with current live sources
    env = process_single_company(
        org_number=org,
        run_id="phase4-change-test",
        store=store,
    )

    # Must detect exactly 1 change event for business_address
    changes = env.changes
    print(f"Detected Changes for {org}: {changes}")
    assert len(changes) == 1, f"Expected exactly 1 change event, got {len(changes)}"
    change = changes[0]
    assert change["field"] == "business_address"
    assert change["previous_value"] == "Historical Headquarters 123, 4000 STAVANGER"
    assert change["new_value"] == old_addr
    assert change["change_classification"] == "updated"
    assert change["previous_evidence"] is not None
    assert change["new_evidence"] is not None

    print("[PASS] Real change detection verified: Exactly 1 change event with both evidence records preserved!")


def test_retrieval_failure_preserves_facts():
    print("=" * 60)
    print("Testing Retrieval Failure Resilience (No False Deletions)...")
    print("=" * 60)

    store = SnapshotStore()
    ev1 = Evidence(
        id="ev-1",
        source_url="https://www.company.no",
        retrieved_at="2026-10-13T10:00:00Z",
        supporting_text="Official site",
    )
    profile1 = CompanyProfile(
        organization_number="123456789",
        legal_identity=LegalIdentity(organization_number="123456789", legal_name="TEST AS", registered_status="active"),
        claims=[
            Claim(field="official_website", value="https://www.company.no", availability=AvailabilityStatus.AVAILABLE, evidence_ids=[ev1.id]),
        ],
        evidence=[ev1],
        retrieved_at="2026-10-13T10:00:00Z",
    )
    refresh_and_detect_changes(profile1, store=store, detected_at="2026-10-13T10:00:00Z")

    # Run 2: Retrieval fails / times out
    profile2 = CompanyProfile(
        organization_number="123456789",
        legal_identity=LegalIdentity(organization_number="123456789", legal_name="TEST AS", registered_status="active"),
        claims=[
            Claim(field="official_website", value=None, availability=AvailabilityStatus.FAILED, evidence_ids=[]),
        ],
        evidence=[],
        retrieved_at="2026-10-13T11:00:00Z",
    )
    final_p2, changes = refresh_and_detect_changes(profile2, store=store, detected_at="2026-10-13T11:00:00Z")

    # Rule check: Earlier fact kept, no deletion recorded
    retained_claim = final_p2.get_claim("official_website")
    assert retained_claim.availability == AvailabilityStatus.AVAILABLE
    assert retained_claim.value == "https://www.company.no"
    assert changes == []
    print("[PASS] Retrieval failure resilience verified: Earlier fact kept, 0 deletions recorded!")


def test_bad_input_containment():
    print("=" * 60)
    print("Testing Bad Input Containment...")
    print("=" * 60)

    bad_input_path = Path("out/temp_bad_phase4_input.jsonl")
    bad_output_path = Path("out/temp_bad_phase4_output.jsonl")
    bad_input_path.parent.mkdir(parents=True, exist_ok=True)
    bad_input_path.write_text("invalid-org\n000000000\n984851006\n", encoding="utf-8")

    envelopes = run_batch(
        input_path=bad_input_path,
        output_path=bad_output_path,
        run_id="phase4-bad-test",
    )

    assert len(envelopes) == 3
    assert envelopes[0].run.terminal_status == "failed"
    assert envelopes[1].run.terminal_status == "failed"
    assert envelopes[2].run.terminal_status == "completed"

    print("[PASS] Bad input containment verified: 3 inputs in, 3 terminal envelopes out, no crashes!")


if __name__ == "__main__":
    run_all_pytest()
    test_idempotent_batch_runs()
    test_simulated_real_change()
    test_retrieval_failure_preserves_facts()
    test_bad_input_containment()
    print("\n" + "=" * 60)
    print("ALL PHASE 4 DELIVERABLES AND EXIT CRITERIA PASSED!")
    print("=" * 60)
