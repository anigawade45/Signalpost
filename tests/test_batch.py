"""Tests for batch runner: slot pre-allocation, error containment, and one-result-per-input."""

import json
from pathlib import Path
from unittest.mock import MagicMock
import httpx
import pytest

from signalpost.output import TerminalEnvelope
from signalpost.registry import RegistryClient
from signalpost.runner import parse_input_file, run_batch

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def test_batch_runner_three_inputs_in_three_out(tmp_path: Path) -> None:
    """Test 3 inputs in -> 3 results out, including one invalid number, with error containment."""
    input_file = tmp_path / "test_inputs.jsonl"
    output_file = tmp_path / "test_envelopes.jsonl"

    # 3 inputs: 1 valid, 1 unknown (404), 1 invalid format
    input_file.write_text(
        '{"organisation_number": "984851006"}\n'
        '{"organisation_number": "000000000"}\n'
        'invalid-org-number\n',
        encoding="utf-8",
    )

    # Mock client to avoid unnecessary network latency in unit tests
    mock_http_client = MagicMock(spec=httpx.Client)

    def mock_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        if "984851006" in url:
            resp.status_code = 200
            sample_data = json.loads((SAMPLES_DIR / "equinor_sample.json").read_text(encoding="utf-8"))
            resp.json.return_value = sample_data
            return resp
        elif "000000000" in url:
            resp.status_code = 404
            resp.text = "Not found"
            return resp
        else:
            resp.status_code = 500
            resp.text = "Server error"
            return resp

    mock_http_client.get.side_effect = mock_get
    mock_reg_client = RegistryClient(client=mock_http_client)

    # Run batch
    envelopes = run_batch(
        input_path=input_file,
        output_path=output_file,
        run_id="test-batch-001",
        client=mock_reg_client,
    )

    # 1. Exact count check: 3 in, 3 out
    assert len(envelopes) == 3

    # 2. Order preservation check
    assert envelopes[0].organisation_number == "984851006"
    assert envelopes[1].organisation_number == "000000000"
    assert envelopes[2].organisation_number == "invalid-org-number"

    # 3. Status checks
    # First: completed
    assert envelopes[0].run.terminal_status == "completed"
    assert len(envelopes[0].claims) > 0
    assert len(envelopes[0].evidence) == 1
    assert envelopes[0].errors == []

    # Second: 404 mapped to failed result with NOT_FOUND code, NOT crash
    assert envelopes[1].run.terminal_status == "failed"
    assert len(envelopes[1].errors) == 1
    assert envelopes[1].errors[0]["code"] == "NOT_FOUND"
    assert "NotFoundError" in envelopes[1].errors[0]["message"]

    # Third: invalid format mapped to failed result with INVALID_FORMAT code, NOT crash
    assert envelopes[2].run.terminal_status == "failed"
    assert len(envelopes[2].errors) == 1
    assert envelopes[2].errors[0]["code"] == "INVALID_FORMAT"
    assert "ValueError" in envelopes[2].errors[0]["message"]


    # 4. Verify output file contains exactly 3 valid JSON lines
    lines = output_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        data = json.loads(line)
        # Every line validates against TerminalEnvelope schema
        validated = TerminalEnvelope.model_validate(data)
        assert validated.run.run_id == "test-batch-001"
