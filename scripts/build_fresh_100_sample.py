"""Build a diverse sample of ~100 fresh Norwegian entities from Brreg's public search API.

Queries a mix of:
- AS (Aksjeselskap, small and large)
- ASA (Allmennaksjeselskap)
- ENK (Enkeltpersonforetak)
- SA (Samvirkeforetak)
- STAT (Staten)
- STI (Stiftelser / foundations)
- Bankrupt entities (konkurs=true)
- Mix of entities with and without registered websites
"""

from __future__ import annotations

import json
from pathlib import Path
import httpx

BRREG_SEARCH_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"

CATEGORIES = [
    {"organisasjonsform": "AS", "size": 25, "desc": "Aksjeselskap (Private Limited)"},
    {"organisasjonsform": "ASA", "size": 15, "desc": "Allmennaksjeselskap (Public)"},
    {"organisasjonsform": "ENK", "size": 15, "desc": "Enkeltpersonforetak (Sole Proprietorship)"},
    {"organisasjonsform": "SA", "size": 10, "desc": "Samvirkeforetak (Cooperatives)"},
    {"organisasjonsform": "STAT", "size": 10, "desc": "Staten (State Entities)"},
    {"organisasjonsform": "STI", "size": 10, "desc": "Stiftelser (Foundations)"},
    {"konkurs": "true", "size": 15, "desc": "Konkurs (Bankrupt Entities)"},
]

EXCLUDED_ORGS = {
    "984851006", "923609016", "914778271", "986228608", "910747711", "982463718",
    "912660680", "933365573", "935567645", "966833300", "981078365", "933835081",
}


def fetch_fresh_sample(target_count: int = 100) -> list[dict[str, str]]:
    client = httpx.Client(timeout=15.0, headers={"User-Agent": "signalpost-sampler/0.1.0"})
    seen_orgs = set(EXCLUDED_ORGS)
    results: list[dict[str, str]] = []

    print("Fetching fresh diverse sample from Brreg API...")

    for cat in CATEGORIES:
        params = {"size": cat["size"] * 2, "page": 0}
        if "organisasjonsform" in cat:
            params["organisasjonsform"] = cat["organisasjonsform"]
        if "konkurs" in cat:
            params["konkurs"] = cat["konkurs"]

        try:
            resp = client.get(BRREG_SEARCH_URL, params=params)
            if resp.status_code == 200:
                data = resp.json()
                embedded = data.get("_embedded", {})
                enheter = embedded.get("enheter", [])
                added_for_cat = 0
                for e in enheter:
                    org = e.get("organisasjonsnummer")
                    name = e.get("navn")
                    if org and org not in seen_orgs and len(org) == 9:
                        seen_orgs.add(org)
                        results.append({
                            "organisation_number": org,
                            "legal_name": name,
                            "org_form": e.get("organisasjonsform", {}).get("kode", ""),
                            "is_bankrupt": e.get("konkurs", False),
                            "website": e.get("hjemmeside"),
                        })
                        added_for_cat += 1
                        if added_for_cat >= cat["size"]:
                            break
                print(f"  Added {added_for_cat} entities for {cat['desc']}")
            else:
                print(f"  Failed for {cat['desc']}: status {resp.status_code}")
        except Exception as exc:
            print(f"  Error fetching {cat['desc']}: {exc}")

    client.close()
    return results[:target_count]


def main():
    sample = fetch_fresh_sample(target_count=100)
    out_path = Path("data/fresh_100_sample.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        for item in sample:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(sample)} fresh sample entities to {out_path}")


if __name__ == "__main__":
    main()
