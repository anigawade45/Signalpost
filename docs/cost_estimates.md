# Signalpost AI Token & Operating Cost Estimates

This document tracks LLM token budgets, API request overhead, latency profiles, and projected unit economics per researched company.

---

## 1. Unit Economics & Pricing Model

For Phase 3 extraction, Signalpost supports lightweight, high-precision instruction models (e.g., Google Gemini 1.5 Flash, OpenAI GPT-4o-mini, Anthropic Claude 3.5 Haiku) and an offline deterministic fallback.

| Model | Input Cost per 1M Tokens | Output Cost per 1M Tokens | Context Window |
|---|---|---|---|
| **Gemini 1.5 Flash** | $0.075 | $0.300 | 1,000,000 |
| **GPT-4o-mini** | $0.150 | $0.600 | 128,000 |
| **Claude 3.5 Haiku** | $0.250 | $1.250 | 200,000 |
| **Offline Rule-Extractor** | $0.000 | $0.000 | N/A (local zero-cost) |

---

## 2. Per-Company Token Budget Breakdown

For a company with an active website and 3–4 targeted pages (Homepage, About Us, Contact, Careers):

| Pipeline Stage | Tokens In (est.) | Tokens Out (est.) | Cost (Gemini Flash) | Cost (GPT-4o-mini) |
|---|---|---|---|---|
| **Homepage & Identity** | ~1,200 | ~150 | $0.000135 | $0.000270 |
| **About Us (Business Description)** | ~1,000 | ~120 | $0.000111 | $0.000222 |
| **Contact & Careers** | ~800 | ~100 | $0.000090 | $0.000180 |
| **Total per Company** | **~3,000** | **~370** | **~$0.00034** | **~$0.00067** |

---

## 3. Batch Cost Projections

| Run Tier | Target Companies | Estimated In Tokens | Estimated Out Tokens | Est. Total Cost (USD) |
|---|---|---|---|---|
| **Smoke Test** | 100 | 300,000 | 37,000 | ~$0.03 – $0.07 |
| **Scale Validation** | 1,000 | 3,000,000 | 370,000 | ~$0.34 – $0.67 |
| **Official Frozen Run** | 1,100 | 3,300,000 | 407,000 | ~$0.37 – $0.74 |

---

## 4. Latency & Resource Consumption

- **HTTP Fetching**: ~150–400 ms per page using cached HTTP/2 client with connection pooling.
- **HTML Cleaning**: < 10 ms per page with BeautifulSoup.
- **LLM Extraction**: ~400–900 ms per prompt call (asynchronous or concurrent workers).
- **Target Latency per Company**: Under 3.0 seconds end-to-end.
- **Fail-Safe Mode**: If LLM provider returns 429, timeout, or network error, falls back to zero-cost deterministic validation without crashing the runner envelope.

---

## 5. Observed $0.00000 Batch Cost & Provider Modes

In the automated 12-company review and CI test suite, reported AI cost is `$0.00000`:
- **Default Offline Rule-Extractor**: When no remote API key (`GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`) is set in the environment, `LLMClient` automatically selects `provider="offline"`. In offline mode, deterministic extraction executes locally without network egress or API billing.
- **Live LLM Extraction**: Setting `GEMINI_API_KEY` (or `OPENAI_API_KEY`) enables live model calls; token usage is measured per request and billed according to the table above.
- **Reproducibility Guarantee**: Batch runs remain reproducible and zero-cost in unauthenticated test environments while supporting drop-in live model upgrades via environment variables.
