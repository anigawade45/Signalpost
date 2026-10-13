"""Command Line Interface for the Signalpost company intelligence agent."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .runner import run_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signalpost",
        description="Signalpost: Evidence-backed Norwegian company research agent.",
    )
    parser.add_argument(
        "input",
        help="Path to input JSONL or text file containing Norwegian organization numbers.",
    )
    parser.add_argument(
        "output",
        help="Path to output JSONL file to write terminal envelopes.",
    )
    parser.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="Identifier for this evaluation/batch run.",
    )
    parser.add_argument(
        "--report",
        dest="report",
        default=None,
        help="Optional path to write a JSON summary execution report.",
    )
    parser.add_argument(
        "--no-web",
        dest="no_web",
        action="store_true",
        help="Disable web discovery and AI extraction (pure registry mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    print(f"Signalpost Runner: Starting batch from '{args.input}' -> '{args.output}' [run_id: {run_id}]")
    envelopes = run_batch(
        input_path=args.input,
        output_path=args.output,
        run_id=run_id,
        enable_web=not args.no_web,
    )

    completed_count = sum(1 for e in envelopes if e.run.terminal_status == "completed")
    failed_count = sum(1 for e in envelopes if e.run.terminal_status == "failed")
    total_count = len(envelopes)

    print(f"Signalpost Runner: Finished. Processed {total_count} entities (Completed: {completed_count}, Failed: {failed_count}).")

    if args.report:
        report_data = {
            "run_id": run_id,
            "total_count": total_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "output_file": str(args.output),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to '{args.report}'")

    return 0


if __name__ == "__main__":
    sys.exit(main())
