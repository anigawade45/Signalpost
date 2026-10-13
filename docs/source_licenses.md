# Signalpost Source License and Terms Registry

This document records all public, official, and web sources accessed by Signalpost, their governing data licenses, terms of service, and access parameters.

---

## 1. Official Registers (Brønnøysundregistrene)

### 1.1 Enhetsregisteret (The Central Coordinating Register for Legal Entities)
- **Authority**: Brønnøysundregistrene / Norwegian Ministry of Trade, Industry and Fisheries.
- **Base URLs**:
  - `https://data.brreg.no/enhetsregisteret/api/enheter`
  - `https://data.brreg.no/enhetsregisteret/api/underenheter`
- **License**: [Norwegian Licence for Open Government Data (NLOD) 2.0](https://data.norge.no/nlod/en/2.0) / Creative Commons Attribution 4.0 (CC BY 4.0).
- **Permissions**: Free reuse, modification, and distribution for commercial and non-commercial purposes. Attribution to Brønnøysundregistrene Enhetsregisteret is required.
- **Access Parameters**: Public REST API without authentication. Rate limits respected (bounded concurrency).

### 1.2 Regnskapsregisteret (Register of Company Accounts)
- **Authority**: Brønnøysundregistrene.
- **Base URLs**:
  - `https://data.brreg.no/regnskapsregisteret/regnskap/{org}`
  - `https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}`
- **License**: NLOD 2.0.
- **Permissions**: Free public inspection copy. Annual account PDF retrieval rate-limited to ~30 requests/minute.

### 1.3 NAV Aa-registeret (Arbeidsgiver- og arbeidstakerregisteret)
- **Authority**: The Norwegian Labour and Welfare Administration (NAV).
- **Access Mode**: Sourced indirectly via Enhetsregisteret's `antallAnsatte` field.
- **Scope**: Direct legal-entity employment relationships.

---

## 2. Company Websites (Direct Web Research)

### 2.1 Scope & Legal Basis
- **Purpose**: Factual verification of company operations, business descriptions, official contact information, job vacancies, and publicly announced leadership.
- **Legal Basis**: Nominative fair use of public factual data; informational crawling of openly accessible company websites.
- **Crawler Identity**:
  - `User-Agent: SignalpostResearchAgent/1.0 (+https://builderr.ai; research-audit)`
- **Robots.txt Adherence**:
  - Every candidate website domain's `/robots.txt` is checked before fetching.
  - If a path is disallowed for the user agent or all robots (`*`), the agent immediately refrains from fetching that path and records `blocked`.
- **Exclusion of Protected / Login Areas**:
  - No access to password-protected pages, Intranets, paywalls, or CAPTCHA-gated portals.
  - Private / loopback IP addresses (`127.0.0.1`, `localhost`, `10.0.0.0/8`, `192.168.0.0/16`) are blocked at the DNS/socket level.
- **Data Retention**:
  - Only clean visible text excerpts strictly supporting extracted claims are retained in terminal evidence. Raw HTML is discarded after hashing.

---

## 3. Discovered Candidate Sources & Search APIs

### 3.1 Brave Search API (When Configured)
- **Provider**: Brave Software.
- **Endpoint**: `https://api.search.brave.com/res/v1/web/search`
- **Terms of Service**: [Brave Search API Terms of Service](https://api-dashboard.search.brave.com/terms-of-service).
- **Retention Policy**: Transient in-memory discovery only. Search snippet text and ranking data are discarded; publication requires independent direct fetching and exact-entity verification from the destination company website.

### 3.2 Restricted Platforms (Explicitly Excluded)
In strict compliance with competition rules:
- **LinkedIn**: No unauthorized headless scraping or crawler bypass.
- **Meta / Instagram / Facebook**: No unauthorized scraping.
- **Indeed / Glassdoor**: No unauthorized web extraction.
