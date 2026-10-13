"""Phase 1 verification script to test all deliverables and exit criteria."""

import json
import subprocess
import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from signalpost.registry import fetch_company_record, NotFoundError, validate_organization_number

def test_evaluation_contract_doc():
    doc_path = Path("docs/evaluation.md")
    assert doc_path.exists(), "docs/evaluation.md does not exist!"
    content = doc_path.read_text(encoding="utf-8")
    assert len(content) > 500, "docs/evaluation.md is too short!"
    print("[PASS] Evaluation contract written down and comprehensive.")

def test_starter_example():
    print("Running starter kit replay example unchanged...")
    cmd = [
        sys.executable,
        "scripts/run_refresh_replay.py",
        "--manifest",
        "tests/fixtures/refresh-snapshots.json",
        "--output",
        "out/refresh-demo.json"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("Replay STDERR:", res.stderr)
        print("Replay STDOUT:", res.stdout)
        raise RuntimeError(f"Starter example failed with code {res.returncode}")
    print("[PASS] Official starter replay ran successfully unchanged.")

def test_registry_client_live():
    print("Testing live registry call for DNB Bank ASA (org nr 984851006)...")
    try:
        record = fetch_company_record("984851006")
        print(f"  Org number: {record.organization_number}")
        print(f"  Legal name: {record.legal_name}")
        print(f"  Entity type: {record.entity_type} ({record.entity_type_description})")
        print(f"  Status: {record.registered_status}")
        print(f"  City: {record.business_address.city if record.business_address else 'N/A'}")
        print(f"  Website: {record.website}")
        assert record.organization_number == "984851006"
        assert "DNB" in record.legal_name.upper()
        assert record.registered_status == "active", f"Expected active status, got {record.registered_status}"
        print("[PASS] Real company returns matching number, legal name, and active status.")

    except Exception as e:
        print(f"[FAIL] Live network call failed: {e}")
        raise


def test_safe_error_handling():
    print("Testing bad input and 404 error handling...")
    # Bad input
    try:
        fetch_company_record("invalid-org")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        print(f"  Handled bad input cleanly: {e}")

    # 404 unknown company
    try:
        fetch_company_record("000000000")
        assert False, "Should have raised NotFoundError / LookupError"
    except LookupError as e:
        print(f"  Handled 404 cleanly: {e}")
    print("[PASS] Bad input and 404 handled cleanly without program crash.")

def run_pytest_suite():
    print("Running pytest on tests/test_registry.py...")
    cmd = [sys.executable, "-m", "pytest", "-v", "tests/test_registry.py"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError(f"pytest failed with return code {res.returncode}")
    print("[PASS] Pytest passed for registry tests.")

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 1 Exit Criteria Verification")
    print("=" * 60)
    test_evaluation_contract_doc()
    test_starter_example()
    test_safe_error_handling()
    test_registry_client_live()
    try:
        run_pytest_suite()
    except Exception as exc:
        print(f"\n[NOTE] pytest execution failed: {exc}")
        print("Please activate your venv and run:")
        print("  pip install httpx pydantic beautifulsoup4 pytest")
        print("  pytest -v tests/test_registry.py")
        sys.exit(1)

    print("=" * 60)
    print("ALL PHASE 1 DELIVERABLES AND EXIT CRITERIA PASSED!")
    print("=" * 60)

