# Signalpost Evaluation & Contract Specification

This document details the official contract, input/output requirements, execution envelopes, constraints, and smoke-test protocols for the **Signalpost Company Intelligence Challenge** on Builderr.

---

## 1. Executive Summary & Challenge Objectives

- **Goal**: Autonomous AI agent that researches Norwegian companies given their 9-digit organisation number (*organisasjonsnummer*).
- **Core Principle**: Evidence-first and auditable. Every extracted fact must be linked to a verifiable source URL, exact text span/selector, and retrieval timestamp.
- **Scoring Rubric (100 Points Total)**:
  - **Recall & Coverage (50 pts)**: 70% company coverage + 30% individual claim recall against cumulative checked reference pool.
  - **Precision & Evidence (30 pts)**: Zero wrong-company matches, verified source citations, immutable content hashes.
  - **Decision-Useful Synthesis (12 pts)**: Supported explanations of business, leadership, changes, and explicit missing fields.
  - **UX & Interaction (8 pts)**: Verifiable, structured outputs accessible across interfaces.
- **Qualification Threshold**: Overall score ≥ 65/100 on an official frozen run. Material wrong-company publication or fabricated financial values disqualifies the run from being official.

---

## 2. Input Specification

- **Input Entity**: Norwegian organisation numbers (exact 9 digits, e.g. `923233580`).
- **Input Format**:
  - A JSONL file or newline-delimited text file containing target organisation numbers.
  - Example line: `{"organisation_number": "923233580"}` or `923233580`.
- **Batch Sizes**:
  - Local Smoke Test: 100 companies selected from the 2025 public universe.
  - Local Scale Test: 1,000+ companies.
  - Official Batch: 1,000 to 1,100 companies supplied dynamically by Builderr after cutoff.
- **Constraint**: The agent must handle unseen companies dynamically; precomputed profile dumps are disallowed.

---

## 3. Terminal Result Envelope (Output Contract)

Every input organisation number must yield **exactly one terminal JSON envelope** in the output JSONL stream. Silent drops or skipped rows score zero.

### Envelope JSON Schema

```json
{
  "organisation_number": "923233580",
  "run": {
    "run_id": "smoke-001",
    "started_at": "2026-10-13T08:00:00Z",
    "completed_at": "2026-10-13T08:00:06Z",
    "terminal_status": "completed"
  },
  "claims": [
    {
      "field": "official_website",
      "value": "https://example.no/",
      "availability": "available",
      "confidence": 0.99,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "jobs",
      "value": null,
      "availability": "not_available",
      "confidence": 1.0,
      "evidence_ids": []
    }
  ],
  "evidence": [
    {
      "id": "ev-1",
      "source_url": "https://example.no/",
      "source_class": "company_owned",
      "retrieved_at": "2026-10-13T08:00:03Z",
      "content_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "claim_span": "Eksempel AS, org.nr. 923 233 580"
    }
  ],
  "changes": [],
  "errors": [],
  "operations": {
    "requests": 4,
    "runtime_ms": 6120,
    "third_party_cost_usd": 0.0
  }
}
```

### Required Fields & Availability States

1. **Mandatory Envelope Sections**:
   - `organisation_number`: 9-digit string.
   - `run`: Execution metadata (`run_id`, `started_at`, `completed_at`, `terminal_status`).
   - `claims`: Fact assertions with explicit availability states.
   - `evidence`: Immutable source snapshots and verification spans.
   - `changes`: Diffs against prior snapshots (for refresh runs).
   - `errors`: Explicit error reports if fetch/parse failed.
   - `operations`: Resource consumption telemetry (`requests`, `runtime_ms`, `third_party_cost_usd`).

2. **Availability States**:
   - `available`: Verified claim found and supported by evidence.
   - `not_available`: Source checked, but field/data does not exist. (Never convert missing to 0).
   - `blocked`: Access denied, CAPTCHA, or prohibited by terms/robots.txt.
   - `not_applicable`: Field not relevant for this company form.
   - `ambiguous`: Candidate found, but identity verification cannot confirm exact entity match.
   - `failed`: Technical/network failure during retrieval.

---

## 4. CLI Arguments & Execution Interface

The agent provides standardized entry points:

### 1. Refresh Replay (Zero-Network Smoke Test)
```bash
python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```
- Validates diff logic, idempotency, and evidence completeness without network calls.

### 2. Batch Execution Command
```bash
python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100
```
- `--organisations`: Input JSONL or text batch file.
- `--bulk`: Optional local Brønnøysund bulk dump for offline entity seeding.
- `--profiles-output`: Internal normalized company profiles JSONL.
- `--output`: Official terminal envelopes JSONL.
- `--report`: Aggregate run metrics JSON.
- `--run-id`: Identifier for the batch run.
- `--expected-count`: Expected entity count; ensures exact matching count with zero silent drops.

---

## 5. Time, Request & Resource Limits

- **HTTP Timeout**: Maximum 15 seconds per external request.
- **Rate Limiting & Concurrency**: Controlled request rates to `data.brreg.no` and company domains to prevent blocking.
- **Fail-Safe Envelope**: If an entity times out or fails, emit a terminal envelope with `terminal_status: "failed"` and error details rather than dropping the entity.
- **Metrics Tracked**:
  - `requests`: Total HTTP requests executed.
  - `runtime_ms`: p50 and p95 latency per company.
  - `third_party_cost_usd`: External API charges incurred.

---

## 6. Model, API & Source Restrictions

1. **Permitted Sources**:
   - Brønnøysundregistrene Enhetsregisteret & Regnskapsregisteret (authoritative anchor).
   - Verified official company websites and feeds.
   - Public pages adhering to `robots.txt` and terms of service.
2. **Restricted Platforms**:
   - LinkedIn, Meta, Indeed, Glassdoor must **not** be scraped using unauthorized headless crawlers.
   - Only official APIs or licensed data feeds are permitted for restricted networks. If inaccessible, mark as `blocked` or `not_available`.
3. **LLM Usage Rules**:
   - LLMs may summarize supported claims or propose site candidates.
   - LLMs **must never** invent financial figures or override deterministic entity verification.
   - API secrets must only be passed via documented environment variables (`.env`).

---

## 7. 100-Company Smoke Test Format

To qualify a submission:
1. Sample 100 companies from `signalpost-universe.jsonl.gz`.
2. Run batch execution producing:
   - `out/smoke-envelopes.jsonl` (exactly 100 JSON envelopes).
   - `out/smoke-report.json` (report detailing precision, recall, request counts, runtime, costs).
3. Verify:
   - 100 inputs in -> 100 envelopes out.
   - Zero missing envelopes.
   - All claims have evidence IDs and valid availability states.

---

## 8. Data-Quality & Reporting Scope Rules

### Employee Counts & Workforce Semantics
- **Source & Scope**: `antallAnsatte` in Enhetsregisteret originates from **NAV Aa-registeret** (Arbeidsgiver- og arbeidstakerregisteret). It strictly counts active employment relationships registered directly under that single 9-digit legal entity (`juridisk enhet`), partitioned across its Norwegian subunits (`underenheter`).
- **Legal Entity vs. Corporate Group**:
  - For parent/holding companies in a corporate group (`erIKonsern: true`), this figure only counts headquarters or holding company staff (e.g., Norsk Hydro ASA: 400; Telenor ASA: 374; Orkla ASA: 72).
  - It does **not** include employees of subsidiaries (e.g. Hydro Aluminium AS, Telenor Norge AS) or overseas personnel (~30,000+ worldwide).
- **Signalpost Contract Preservation**:
  - All registry employee claims must preserve reporting scope (`employee_count_scope: "legal_entity"`).
  - Sourcing must record `NAV Aa-registeret via Enhetsregisteret` with effective dates.
  - Entities that belong to a corporate group must preserve `is_part_of_group: true`.
  - Group-wide consolidated workforce numbers (from annual account PDFs / financial notes) must be published under a distinct scope or claim rather than conflated with the legal entity's registry count.

