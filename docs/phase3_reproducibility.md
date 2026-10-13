# Signalpost Phase 3 Baseline & Reproducibility Guide

This document establishes the verified baseline for Signalpost Phase 3 (Web Research, AI Fact Extraction, Evidence Hardening, and Outbound IP Enforcement).

---

## 1. Test & Execution Commands

To reproduce the exact verified test suite and 12-company live batch run:

### Full Automated Regression Suite
```bash
pytest -v
```
- **Expected Outcome:** 55 passed in ~2.7s across all 7 test modules:
  - `tests/test_awkward_cases.py` (6 tests)
  - `tests/test_batch.py` (1 test)
  - `tests/test_evidence_quality.py` (16 tests including Phase 3.1 & 3.2 audits and DNS rebinding socket enforcement)
  - `tests/test_extraction.py` (5 tests)
  - `tests/test_output.py` (2 tests)
  - `tests/test_registry.py` (19 tests)
  - `tests/test_web_discovery.py` (6 tests)

### 12-Company Live Verification Batch & Bad-Input Containment
```bash
python check_phase3.py
```
- **Expected Outcome:**
  - 12/12 Norwegian companies completed terminal status with 0 failures.
  - Manual hand-review output demonstrating separation of website status from registered company status.
  - Verification of bad input containment: 1 valid entity completes, 2 invalid inputs fail gracefully without batch crash.

---

## 2. Batch Inputs, Outputs & Telemetry Baseline

| Artifact | Path | Description |
| :--- | :--- | :--- |
| **Input Batch** | `data/phase3_batch.jsonl` | 12 diverse Norwegian entities (ASAs, cooperatives, bankrupt estates, sole proprietorships, public departments) |
| **Output Results** | `out/phase3_results.jsonl` | 12 official Builderr `TerminalEnvelope` JSONL records |
| **Batch Report** | `out/phase3_report.json` | Aggregated run metrics and status counts |

### Baseline Telemetry (12 Companies)
- **Total Completed Envelopes:** 12 / 12 (100%)
- **Total HTTP Requests:** 50 requests (Avg: 4.2 requests/company, budget cap: 6)
- **Total Runtime:** 25.27s (Avg: 2.11s/company)
- **Third-Party AI Cost:** $0.00000 (Offline deterministic extractor; ~$0.0006/company with live models)

---

## 3. Security Safeguards Implemented

### Safeguard 1: Socket-Level Outbound IP Enforcement (Closing DNS Rebinding / TOCTOU)
- **Implementation:** `SafeSSRFTransport` and `SafeSSRFSyncBackend` in `src/signalpost/website.py`.
- **Mechanism:**
  1. Resolves hostname via `socket.getaddrinfo` and asserts that EVERY returned address is globally routable.
  2. Directly connects the TCP socket to the validated IP address, preventing any secondary DNS rebinding.
  3. Verifies actual socket peer address post-connection via `sock.getpeername()`.
  4. Preserves the original target hostname in `httpcore`'s `SyncStream.start_tls`, ensuring TLS SNI and server certificate validation remain 100% intact.
- **Verification:** Tested in `tests/test_evidence_quality.py::test_actual_outbound_ip_enforcement_closes_dns_rebinding`.

### Safeguard 2: Deterministic Verification Separate from LLM Extraction
- **Implementation:** `CandidateClaim` vs `validate_candidate_claim` in `src/signalpost/extraction.py`.
- **Mechanism:**
  - LLMs (or offline extractors) are strictly restricted to proposing candidate claims.
  - Deterministic Python code validates verbatim text match in `page.visible_text`, blocks prompt injection attempts, rejects customer support and regional subsidiary copy, and assigns contact purpose/scope based strictly on textual evidence.
  - Unverified claims are completely excluded from published output envelopes.

### Safeguard 3: Separation of Website Status from Registered Company Status
- **Implementation:** `research_company` in `src/signalpost/research.py`.
- **Statuses:**
  - `available`: Website retrieval and identity verification succeeded.
  - `blocked`: Anti-bot or HTTP 403 challenge encountered; registry data preserved.
  - `failed`: Technical retrieval failure (DNS error, timeout, 404, 5xx); registry data preserved.
  - `not_available`: No website registered or identified; registry data preserved.
- **Independence:** A website retrieval failure or block never implies bankruptcy, liquidation, or deregistration.
