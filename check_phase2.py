"""Phase 2 verification script to test pipeline, batch runner, and real company outputs."""

import json
import os
import subprocess
import sys
from pathlib import Path


# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from signalpost.output import TerminalEnvelope
from signalpost.runner import run_batch


def run_all_pytest():
    print("=" * 60)
    print("Running pytest on Phase 1 & Phase 2 test suites...")
    print("=" * 60)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "tests/test_registry.py",
        "tests/test_output.py",
        "tests/test_batch.py",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError(f"pytest failed with code {res.returncode}")
    print("[PASS] All tests in tests/ passed.")


def test_batch_cli_and_real_companies():
    print("=" * 60)
    print("Running batch on 7 real Norwegian companies...")
    print("=" * 60)

    input_file = Path("data/sample_batch.jsonl")
    output_file = Path("out/phase2_results.jsonl")
    report_file = Path("out/phase2_report.json")

    cmd = [
        sys.executable,
        "-m",
        "signalpost",
        str(input_file),
        str(output_file),
        "--run-id",
        "phase2-verification-001",
        "--report",
        str(report_file),
    ]

    sub_env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent / "src")
    existing_pp = sub_env.get("PYTHONPATH", "")
    sub_env["PYTHONPATH"] = f"{src_dir};{existing_pp}" if existing_pp else src_dir

    res = subprocess.run(cmd, capture_output=True, text=True, env=sub_env)
    print(res.stdout)

    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError(f"CLI batch run failed with code {res.returncode}")


    # Read and inspect every output line
    lines = output_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7, f"Expected 7 output envelopes, got {len(lines)}"

    print("=" * 60)
    print("Inspecting outputs for all 7 companies by hand:")
    print("=" * 60)

    for i, line in enumerate(lines, start=1):
        envelope_data = json.loads(line)
        # Validate against official contract schema
        env = TerminalEnvelope.model_validate(envelope_data)

        # Extract notable claims
        claims_dict = {c.field: (c.value, c.availability) for c in env.claims}
        legal_name_val, _ = claims_dict.get("legal_name", ("N/A", "missing"))
        status_val, _ = claims_dict.get("registered_status", ("N/A", "missing"))
        industry_val, _ = claims_dict.get("industry_description", ("N/A", "missing"))
        website_val, website_avail = claims_dict.get("official_website", ("N/A", "missing"))
        emp_val, emp_avail = claims_dict.get("employee_count", ("N/A", "missing"))
        emp_scope, _ = claims_dict.get("employee_count_scope", ("N/A", "missing"))
        is_group, _ = claims_dict.get("is_part_of_group", ("N/A", "missing"))

        print(f"\n[{i}/7] Org: {env.organisation_number}")
        print(f"     Terminal Status: {env.run.terminal_status} (run_id: {env.run.run_id})")
        print(f"     Legal Name:      {legal_name_val}")
        print(f"     Status:          {status_val}")
        print(f"     Industry:        {industry_val}")
        print(f"     Website:         {website_val} [status: {website_avail}]")
        print(f"     Employees:       {emp_val} [status: {emp_avail}, count_scope: {emp_scope} (excludes subsidiaries)]")
        print(f"     Corporate Group: {is_group} (erIKonsern structural flag; does not alter legal_entity count scope)")
        print(f"     Evidence Source: {env.evidence[0].source_url if env.evidence else 'None'}")
        print(f"     Evidence Text:   {env.evidence[0].claim_span if env.evidence else 'None'}")
        print(f"     Evidence Time:   {env.evidence[0].retrieved_at if env.evidence else 'None'}")


        # Exit criteria checks
        assert env.run.terminal_status == "completed"
        assert len(env.organisation_number) == 9
        assert legal_name_val != "N/A"
        assert len(env.evidence) > 0
        assert env.evidence[0].source_url.startswith("https://data.brreg.no")
        assert env.evidence[0].retrieved_at is not None

        if emp_avail == "available":
            assert emp_scope == "legal_entity", f"Expected legal_entity scope, got {emp_scope}"


        # Verify missing information has explicit status 'not_available' and value None
        for c in env.claims:
            if c.availability == "not_available":
                assert c.value is None, f"Field {c.field} has value {c.value} but status not_available"

    print("\n" + "=" * 60)
    print("Testing bad input containment (invalid number + 404)...")
    print("=" * 60)

    bad_input_path = Path("out/temp_bad_input.jsonl")
    bad_output_path = Path("out/temp_bad_output.jsonl")
    bad_input_path.write_text("invalid-org\n000000000\n984851006\n", encoding="utf-8")

    cmd_bad = [
        sys.executable,
        "-m",
        "signalpost",
        str(bad_input_path),
        str(bad_output_path),
        "--run-id",
        "phase2-bad-test",
    ]
    res_bad = subprocess.run(cmd_bad, capture_output=True, text=True, env=sub_env)
    print(res_bad.stdout)
    assert res_bad.returncode == 0, "CLI should not crash on bad inputs!"

    bad_lines = bad_output_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(bad_lines) == 3

    env1 = TerminalEnvelope.model_validate(json.loads(bad_lines[0]))
    env2 = TerminalEnvelope.model_validate(json.loads(bad_lines[1]))
    env3 = TerminalEnvelope.model_validate(json.loads(bad_lines[2]))

    assert env1.run.terminal_status == "failed"
    assert env1.organisation_number == "invalid-org"
    assert "ValueError" in env1.errors[0]

    assert env2.run.terminal_status == "failed"
    assert env2.organisation_number == "000000000"
    assert "NotFoundError" in env2.errors[0]

    assert env3.run.terminal_status == "completed"
    assert env3.organisation_number == "984851006"

    bad_input_path.unlink(missing_ok=True)
    bad_output_path.unlink(missing_ok=True)

    print("[PASS] Bad inputs produce failed terminal envelopes without program crashes.")


if __name__ == "__main__":
    run_all_pytest()
    test_batch_cli_and_real_companies()
    print("\n" + "=" * 60)
    print("ALL PHASE 2 DELIVERABLES AND EXIT CRITERIA PASSED!")
    print("=" * 60)
