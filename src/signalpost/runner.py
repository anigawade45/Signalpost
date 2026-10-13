"""Batch execution engine ensuring exactly one ordered terminal envelope per input."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .budget import BudgetTracker
from .output import TerminalEnvelope, format_failed_envelope, format_successful_envelope
from .refresh import SnapshotStore, refresh_and_detect_changes
from .registry import (
    BlockedError,
    MalformedDataError,
    NotFoundError,
    RegistryClient,
    TransientError,
)
from .research import research_company
from .verification import verify_company_profile


def parse_input_file(file_path: Path | str) -> list[str]:
    """Parse inputs from a JSONL or plain text file preserving original order.

    Supports:
    - Lines with plain 9-digit strings
    - JSON lines with 'organisation_number' or 'organization_number'
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    inputs: list[str] = []
    for line_num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue

        if clean.startswith("{") and clean.endswith("}"):
            try:
                data = json.loads(clean)
                org = str(data.get("organisation_number") or data.get("organization_number") or "").strip()
                inputs.append(org)
            except json.JSONDecodeError:
                inputs.append(clean)
        else:
            inputs.append(clean)

    return inputs


def process_single_company(
    org_number: str,
    run_id: str,
    client: Optional[RegistryClient] = None,
    cache: Optional[Any] = None,
    llm_client: Optional[Any] = None,
    enable_web: bool = True,
    store: Optional[SnapshotStore] = None,
    budget_tracker: Optional[BudgetTracker] = None,
) -> TerminalEnvelope:
    """Safely process a single organization number, guaranteeing a terminal envelope."""
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    requests_count = 1

    if budget_tracker:
        budget_tracker.start_company(org_number)

    effective_enable_web = enable_web
    if budget_tracker and budget_tracker.should_stop_optional_research():
        effective_enable_web = False

    try:
        profile = research_company(
            org_number,
            client=client,
            cache=cache,
            llm_client=llm_client,
            enable_web=effective_enable_web,
        )

        # 1. Deterministic verification before publishing
        verified_profile, _ = verify_company_profile(profile)

        # 2. Refresh comparison and change event emission
        completed_at = datetime.now(timezone.utc).isoformat()
        final_profile, changes = refresh_and_detect_changes(
            current_profile=verified_profile,
            store=store,
            detected_at=completed_at,
        )

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        if budget_tracker:
            for _ in range(final_profile.requests_count):
                budget_tracker.record_request(cost_usd=final_profile.third_party_cost_usd / max(final_profile.requests_count, 1))
            budget_tracker.record_completed()

        return format_successful_envelope(
            profile=final_profile,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=final_profile.requests_count,
            runtime_ms=elapsed_ms,
            cost_usd=final_profile.third_party_cost_usd,
            changes=changes,
        )
    except ValueError as exc:
        if budget_tracker:
            budget_tracker.record_failed()
        completed_at = datetime.now(timezone.utc).isoformat()
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return format_failed_envelope(
            organization_number=org_number,
            error_message=f"ValueError: {exc}",
            error_code="INVALID_FORMAT",
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=requests_count,
            runtime_ms=elapsed_ms,
        )
    except NotFoundError as exc:
        if budget_tracker:
            budget_tracker.record_failed()
        completed_at = datetime.now(timezone.utc).isoformat()
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return format_failed_envelope(
            organization_number=org_number,
            error_message=f"NotFoundError: {exc}",
            error_code="NOT_FOUND",
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=requests_count,
            runtime_ms=elapsed_ms,
        )
    except BlockedError as exc:
        if budget_tracker:
            budget_tracker.record_failed()
        completed_at = datetime.now(timezone.utc).isoformat()
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return format_failed_envelope(
            organization_number=org_number,
            error_message=f"BlockedError: {exc}",
            error_code="BLOCKED",
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=requests_count,
            runtime_ms=elapsed_ms,
        )
    except TransientError as exc:
        if budget_tracker:
            budget_tracker.record_failed()
        completed_at = datetime.now(timezone.utc).isoformat()
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return format_failed_envelope(
            organization_number=org_number,
            error_message=f"TransientError: {exc}",
            error_code="TRANSIENT_ERROR",
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=requests_count,
            runtime_ms=elapsed_ms,
        )
    except Exception as exc:
        if budget_tracker:
            budget_tracker.record_failed()
        completed_at = datetime.now(timezone.utc).isoformat()
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return format_failed_envelope(
            organization_number=org_number,
            error_message=f"{type(exc).__name__}: {str(exc)}",
            error_code="GENERIC_ERROR",
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=requests_count,
            runtime_ms=elapsed_ms,
        )



def run_batch(
    input_path: Path | str,
    output_path: Path | str,
    run_id: Optional[str] = None,
    client: Optional[RegistryClient] = None,
    cache: Optional[Any] = None,
    llm_client: Optional[Any] = None,
    enable_web: bool = True,
    store: Optional[SnapshotStore] = None,
    budget_tracker: Optional[BudgetTracker] = None,
    generate_reports: bool = False,
    reports_dir: Optional[Path | str] = None,
) -> list[TerminalEnvelope]:
    """Execute batch run over an input file, allocating result slots and ensuring 1-to-1 output."""
    if run_id is None:
        run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    input_orgs = parse_input_file(input_path)
    total_count = len(input_orgs)

    # Pre-allocate one result slot per input to guarantee order and count
    results: list[Optional[TerminalEnvelope]] = [None] * total_count

    owned_client = client is None
    reg_client = client or RegistryClient()
    active_tracker = budget_tracker or BudgetTracker()
    active_store = store or SnapshotStore()

    try:
        for idx, org in enumerate(input_orgs):
            envelope = process_single_company(
                org,
                run_id=run_id,
                client=reg_client,
                cache=cache,
                llm_client=llm_client,
                enable_web=enable_web,
                store=active_store,
                budget_tracker=active_tracker,
            )
            results[idx] = envelope
    finally:
        if owned_client:
            reg_client.close()

    # Print visible budget counters in run log
    print(active_tracker.format_log_summary())

    # Verify all slots filled
    final_envelopes: list[TerminalEnvelope] = [
        res if res is not None else format_failed_envelope(
            organization_number=input_orgs[i],
            error_message="Unexpected internal slot unallocated",
            run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        for i, res in enumerate(results)
    ]

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as f:
        for env in final_envelopes:
            f.write(json.dumps(env.to_dict(), ensure_ascii=False) + "\n")

    if generate_reports or reports_dir:
        from .reporting import write_batch_inspection_artifacts
        write_batch_inspection_artifacts(
            envelopes=final_envelopes,
            reports_dir=reports_dir or "out/reports",
            budget_tracker=active_tracker,
        )

    return final_envelopes
