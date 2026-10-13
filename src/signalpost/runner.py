"""Batch execution engine ensuring exactly one ordered terminal envelope per input."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .output import TerminalEnvelope, format_failed_envelope, format_successful_envelope
from .registry import (
    BlockedError,
    MalformedDataError,
    NotFoundError,
    RegistryClient,
    TransientError,
)
from .research import research_company


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
) -> TerminalEnvelope:
    """Safely process a single organization number, guaranteeing a terminal envelope."""
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    requests_count = 1

    try:
        profile = research_company(org_number, client=client)
        completed_at = datetime.now(timezone.utc).isoformat()
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        return format_successful_envelope(
            profile=profile,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requests=requests_count,
            runtime_ms=elapsed_ms,
        )
    except ValueError as exc:
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

    try:
        for idx, org in enumerate(input_orgs):
            envelope = process_single_company(org, run_id=run_id, client=reg_client)
            results[idx] = envelope
    finally:
        if owned_client:
            reg_client.close()

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

    return final_envelopes
