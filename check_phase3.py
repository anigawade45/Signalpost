"""Phase 3 verification script: test discovery, web crawling, AI extraction, and live 12-company review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from signalpost.models import AvailabilityStatus
from signalpost.output import TerminalEnvelope


def run_all_pytest():
    print("=" * 60)
    print("Running pytest on Phase 1, Phase 2, and Phase 3 test suites...")
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
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError(f"pytest failed with code {res.returncode}")
    print("[PASS] All tests across all 7 test suites passed!")


def test_batch_cli_and_live_companies():
    print("=" * 60)
    print("Running batch on 12 real Norwegian companies (Phase 3 Web + AI)...")
    print("=" * 60)

    input_file = Path("data/phase3_batch.jsonl")
    output_file = Path("out/phase3_results.jsonl")
    report_file = Path("out/phase3_report.json")

    cmd = [
        sys.executable,
        "-m",
        "signalpost",
        str(input_file),
        str(output_file),
        "--run-id",
        "phase3-verification-001",
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

    lines = output_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 12, f"Expected 12 output envelopes, got {len(lines)}"

    print("=" * 60)
    print("Manual Hand-Review of all 12 Company Envelopes & Web Claims:")
    print("=" * 60)

    total_runtime_ms = 0
    total_cost_usd = 0.0
    total_requests = 0

    for i, line in enumerate(lines, start=1):
        envelope_data = json.loads(line)
        env = TerminalEnvelope.model_validate(envelope_data)

        total_runtime_ms += env.operations.runtime_ms
        total_cost_usd += env.operations.third_party_cost_usd
        total_requests += env.operations.requests

        claims_dict = {c.field: c for c in env.claims}
        evidence_dict = {e.id: e for e in env.evidence}

        legal_name_val = claims_dict["legal_name"].value if "legal_name" in claims_dict else "N/A"
        status_val = claims_dict["registered_status"].value if "registered_status" in claims_dict else "N/A"
        org_type = claims_dict.get("entity_type")
        org_desc = claims_dict.get("entity_type_description")
        org_form_val = f"{org_type.value} ({org_desc.value})" if org_type and org_desc else (org_type.value if org_type else "N/A")
        founding_date_val = claims_dict["registration_date"].value if "registration_date" in claims_dict else "N/A"
        business_addr = claims_dict.get("business_address")
        postal_addr = claims_dict.get("postal_address")
        addr_val = business_addr.value if business_addr else (postal_addr.value if postal_addr else "N/A")
        ind_code = claims_dict.get("industry_code")
        ind_desc = claims_dict.get("industry_description")
        ind_val = f"{ind_code.value} - {ind_desc.value}" if ind_code and ind_desc else (ind_desc.value if ind_desc else "N/A")
        accounts_val = claims_dict["latest_submitted_accounts"].value if "latest_submitted_accounts" in claims_dict else "N/A"

        website_claim = claims_dict.get("official_website")
        website_val = website_claim.value if website_claim else "N/A"
        website_avail = website_claim.availability if website_claim else "missing"

        emp_claim = claims_dict.get("employee_count")
        emp_val = emp_claim.value if emp_claim else "N/A"
        emp_avail = emp_claim.availability if emp_claim else "missing"
        emp_scope = claims_dict.get("employee_count_scope").value if "employee_count_scope" in claims_dict else "N/A"
        emp_src = claims_dict.get("employee_count_source").value if "employee_count_source" in claims_dict else "N/A"
        is_group = claims_dict.get("is_part_of_group").value if "is_part_of_group" in claims_dict else "N/A"

        # Roles from Brreg
        ceo_claim = claims_dict.get("ceo")
        chair_claim = claims_dict.get("board_chair")
        board_claim = claims_dict.get("board_members")
        ceo_val = ceo_claim.value if ceo_claim and ceo_claim.value else "N/A"
        chair_val = chair_claim.value if chair_claim and chair_claim.value else "N/A"
        board_val = ", ".join(board_claim.value) if board_claim and board_claim.value else "N/A"

        # Annual Accounts figures from Regnskapsregisteret
        rev_claim = claims_dict.get("financial_revenue")
        op_claim = claims_dict.get("financial_operating_result")
        res_claim = claims_dict.get("financial_annual_result")
        curr_claim = claims_dict.get("financial_currency")
        period_claim = claims_dict.get("financial_period")
        curr_val = curr_claim.value if curr_claim and curr_claim.value else "NOK"
        period_val = period_claim.value if period_claim and period_claim.value else "N/A"
        rev_avail = getattr(rev_claim.availability, "value", str(rev_claim.availability)) if rev_claim else "N/A"
        op_avail = getattr(op_claim.availability, "value", str(op_claim.availability)) if op_claim else "N/A"
        rev_val = f"{rev_claim.value:,} {curr_val}" if rev_claim and rev_claim.value is not None else f"[{rev_avail}]"
        op_val = f"{op_claim.value:,} {curr_val}" if op_claim and op_claim.value is not None else f"[{op_avail}]"
        res_val = f"{res_claim.value:,} {curr_val}" if res_claim and res_claim.value is not None else "N/A"

        # Careers & Jobs
        careers_claim = claims_dict.get("careers_url")
        jobs_claim = claims_dict.get("open_job_openings")
        careers_val = careers_claim.value if careers_claim and careers_claim.value else "N/A"
        jobs_val = jobs_claim.value if jobs_claim and jobs_claim.value else "N/A"

        print(f"\n[{i}/12] Org: {env.organisation_number}")
        print(f"      Terminal Status:  {env.run.terminal_status} (run_id: {env.run.run_id})")
        print(f"      Legal Name:       {legal_name_val}")
        print(f"      Org Form:         {org_form_val}")
        print(f"      Registered Status:{status_val}")
        print(f"      Founding Date:    {founding_date_val}")
        print(f"      Registered Address: {addr_val}")
        print(f"      Industry:         {ind_val}")
        print(f"      Employees ({emp_src}): {emp_val} [status: {emp_avail}, scope: {emp_scope}]")
        print(f"      Corporate Group:  {is_group} (erIKonsern structural flag)")
        print(f"      --- Brreg Roles & Leadership ---")
        print(f"      CEO / Daglig Leder:{ceo_val}")
        print(f"      Board Chair:      {chair_val}")
        print(f"      Board Members:    {board_val}")
        print(f"      --- Annual Accounts Financials (Period: {period_val}) ---")
        print(f"      Revenue:          {rev_val}")
        print(f"      Operating Result: {op_val}")
        print(f"      Annual Net Result:{res_val}")
        print(f"      --- Online Presence & Careers ---")
        print(f"      Official Website: {website_val} [status: {website_avail}]")
        print(f"      Careers Page:     {careers_val}")
        print(f"      Job Openings:     {jobs_val}")

        # Web Extracted Claims
        web_claims = [c for c in env.claims if c.field in {
            "business_description", "contact_email", "contact_phone"
        } and (c.availability in {AvailabilityStatus.AVAILABLE, AvailabilityStatus.AVAILABLE.value, "available"}) and c.value is not None]

        if web_claims:
            print(f"      --- Web-Extracted Verified Facts ({len(web_claims)} items) ---")
            for wc in web_claims:
                ev_items = [evidence_dict[eid] for eid in wc.evidence_ids if eid in evidence_dict]
                ev_url = ev_items[0].source_url if ev_items else "N/A"
                ev_span = ev_items[0].claim_span if ev_items else "N/A"
                ev_note = ev_items[0].note if ev_items else "N/A"
                print(f"        * {wc.field}: {wc.value}")
                print(f"          Source URL:     {ev_url}")
                print(f"          Supporting Text:{ev_span}")
                if ev_note and ev_note != "N/A":
                    print(f"          Evidence Note:  {ev_note}")
        else:
            if status_val == "bankrupt":
                reason_expl = "company is registered as bankrupt in Enhetsregisteret; no active web operations verified"
            elif status_val == "deleted":
                reason_expl = "company is deleted/dissolved in the register"
            elif website_avail == "blocked":
                reason_expl = f"official website ({website_val}) access was blocked or denied (HTTP 403 / anti-bot); registry data preserved"
            elif website_avail == "failed":
                reason_expl = f"official website ({website_val}) retrieval failed due to technical/network error; registry data preserved"
            elif website_avail == "not_available":
                reason_expl = "no official website registered in Enhetsregisteret or identified"
            else:
                reason_expl = "official website retrieved, but page content did not yield verified claims matching strict relevance filters"
            print(f"      --- Web-Extracted Facts: None ({reason_expl}) ---")

        print(f"      Telemetry: Requests={env.operations.requests}, Runtime={env.operations.runtime_ms}ms, Cost=${env.operations.third_party_cost_usd:.5f}")

        # Strict checks on envelope
        assert env.run.terminal_status == "completed"
        assert len(env.organisation_number) == 9
        assert len(env.evidence) > 0
        # Every evidence item must have source_url, content_sha256, and claim_span
        for ev in env.evidence:
            assert ev.source_url is not None
            assert ev.content_sha256 is not None
            assert ev.claim_span is not None
            assert ev.retrieved_at is not None

    print("\n" + "=" * 60)
    print("Aggregate Phase 3 Performance & Unit Economics:")
    print("=" * 60)
    avg_runtime_ms = total_runtime_ms / len(lines)
    avg_cost_usd = total_cost_usd / len(lines)
    avg_requests = total_requests / len(lines)

    print(f"Total Companies Processed: {len(lines)}")
    print(f"Total HTTP Requests:       {total_requests} (Avg: {avg_requests:.1f} per company)")
    print(f"Total Runtime:             {total_runtime_ms / 1000:.2f}s (Avg: {avg_runtime_ms / 1000:.2f}s per company)")
    print(f"Total 3rd-Party AI Cost:   ${total_cost_usd:.5f} (Avg: ${avg_cost_usd:.5f} per company)")
    print(f"Projected 1,000 Run Cost:  ${avg_cost_usd * 1000:.3f}")
    print("=" * 60)


def test_bad_input_containment():
    print("\n" + "=" * 60)
    print("Testing bad input containment in Phase 3 (invalid format + 404)...")
    print("=" * 60)

    sub_env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent / "src")
    existing_pp = sub_env.get("PYTHONPATH", "")
    sub_env["PYTHONPATH"] = f"{src_dir};{existing_pp}" if existing_pp else src_dir

    bad_input_path = Path("out/temp_bad_phase3_input.jsonl")
    bad_output_path = Path("out/temp_bad_phase3_output.jsonl")
    bad_input_path.parent.mkdir(parents=True, exist_ok=True)
    bad_input_path.write_text("invalid-org\n000000000\n984851006\n", encoding="utf-8")

    cmd_bad = [
        sys.executable,
        "-m",
        "signalpost",
        str(bad_input_path),
        str(bad_output_path),
        "--run-id",
        "phase3-bad-test",
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
    assert env1.errors[0]["code"] == "INVALID_FORMAT"

    assert env2.run.terminal_status == "failed"
    assert env2.organisation_number == "000000000"
    assert env2.errors[0]["code"] == "NOT_FOUND"

    assert env3.run.terminal_status == "completed"
    assert env3.organisation_number == "984851006"
    print("[PASS] Bad input containment verified: invalid inputs fail gracefully without crashing!")


if __name__ == "__main__":
    run_all_pytest()
    test_batch_cli_and_live_companies()
    test_bad_input_containment()
    print("\n" + "=" * 60)
    print("ALL PHASE 3 DELIVERABLES AND EXIT CRITERIA PASSED!")
    print("=" * 60)

