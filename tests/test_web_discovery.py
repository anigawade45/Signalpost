"""Tests for web discovery, canonical URL caching, robots.txt, and identity verification."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import httpx
import pytest

from signalpost.cache import PageCache, classify_http_outcome, normalize_url
from signalpost.models import AvailabilityStatus
from signalpost.website import (
    CleanedWebPage,
    assert_public_url,
    clean_html,
    discover_target_pages,
    fetch_page_with_cache,
    verify_website_identity,
)


def test_normalize_url_deduplication() -> None:
    # Strips fragments and utm tracking parameters
    u1 = "https://www.example.no/about/?utm_source=google&utm_medium=cpc#team"
    u2 = "https://www.example.no/about?utm_campaign=winter"
    assert normalize_url(u1) == "https://www.example.no/about"
    assert normalize_url(u2) == "https://www.example.no/about"

    # Lowercases scheme and domain, standardizes empty path
    assert normalize_url("HTTP://Example.NO") == "http://example.no/"


def test_assert_public_url_blocks_private_and_localhost() -> None:
    with pytest.raises(ValueError) as exc:
        assert_public_url("http://localhost:8000/api")
    assert "prohibited" in str(exc.value).lower()

    with pytest.raises(ValueError):
        assert_public_url("http://127.0.0.1:8080")

    with pytest.raises(ValueError):
        assert_public_url("http://192.168.1.1/admin")


def test_classify_http_outcomes() -> None:
    # 200 normal -> AVAILABLE
    assert classify_http_outcome(200, "<html>Welcome to our store</html>") == AvailabilityStatus.AVAILABLE

    # 200 with Cloudflare challenge -> BLOCKED
    cf_html = "<html><head><title>Attention Required! | Cloudflare</title></head><body>Please enable cookies</body></html>"
    assert classify_http_outcome(200, cf_html) == AvailabilityStatus.BLOCKED

    # 403 / 429 -> BLOCKED
    assert classify_http_outcome(403) == AvailabilityStatus.BLOCKED
    assert classify_http_outcome(429) == AvailabilityStatus.BLOCKED

    # 404 / 410 -> NOT_AVAILABLE
    assert classify_http_outcome(404) == AvailabilityStatus.NOT_AVAILABLE
    assert classify_http_outcome(410) == AvailabilityStatus.NOT_AVAILABLE

    # 408 / 500 / 503 / 0 -> FAILED (transient, NOT not_available)
    assert classify_http_outcome(500) == AvailabilityStatus.FAILED
    assert classify_http_outcome(503) == AvailabilityStatus.FAILED
    assert classify_http_outcome(0) == AvailabilityStatus.FAILED


def test_clean_html_strips_scripts_and_extracts_links() -> None:
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Norge Tech AS - Ledende softwareløsninger</title>
        <meta name="description" content="Norge Tech utvikler skytjenester for offentlig og privat sektor.">
        <style>body { font-family: sans-serif; }</style>
        <script>console.log("tracker");</script>
      </head>
      <body>
        <nav><a href="/home">Hjem</a></nav>
        <main>
          <h1>Velkommen til Norge Tech AS</h1>
          <p>Vi leverer innovative programmeringstjenester over hele Norden.</p>
          <a href="/om-oss">Les mer om oss</a>
          <a href="/kontakt">Kontakt oss</a>
          <a href="/karriere">Ledige stillinger</a>
          <a href="https://external.com/tracking">Partner</a>
        </main>
        <footer>
          <p>Org.nr: 987 654 321 | Oslo, Norge</p>
        </footer>
      </body>
    </html>
    """

    cleaned = clean_html(
        html=html,
        base_url="https://www.norgetech.no",
        content_sha256="hash123",
        retrieved_at="2026-10-13T10:00:00Z",
    )

    assert cleaned.title == "Norge Tech AS - Ledende softwareløsninger"
    assert "Norge Tech utvikler skytjenester" in (cleaned.meta_description or "")
    assert "console.log" not in cleaned.visible_text
    assert "font-family" not in cleaned.visible_text
    assert "Velkommen til Norge Tech AS" in cleaned.visible_text
    assert "Vi leverer innovative programmeringstjenester" in cleaned.visible_text

    # Internal links discovered, external filtered
    assert "https://www.norgetech.no/om-oss" in cleaned.links
    assert "https://www.norgetech.no/kontakt" in cleaned.links
    assert "https://www.norgetech.no/karriere" in cleaned.links
    assert not any("external.com" in link for link in cleaned.links)


def test_discover_target_pages() -> None:
    dummy_page = CleanedWebPage(
        url="https://www.norgetech.no",
        visible_text="Hjemmeside",
        links=[
            "https://www.norgetech.no/om-oss",
            "https://www.norgetech.no/kontakt-oss",
            "https://www.norgetech.no/karriere",
            "https://www.norgetech.no/produkter",
            "https://www.norgetech.no/blogg",
        ],
        content_sha256="abc",
        retrieved_at="2026-10-13T10:00:00Z",
    )

    targets = discover_target_pages("https://www.norgetech.no", dummy_page, max_pages=4)
    assert len(targets) <= 4
    assert "https://www.norgetech.no/" in targets
    assert "https://www.norgetech.no/om-oss" in targets
    assert "https://www.norgetech.no/kontakt-oss" in targets
    assert "https://www.norgetech.no/karriere" in targets


def test_verify_website_identity_scenarios() -> None:
    # 1. Page with exact 9-digit org number in footer
    page_correct = CleanedWebPage(
        url="https://www.norgetech.no",
        visible_text="Norge Tech AS. Org.nr 987 654 321. Alle rettigheter reservert.",
        content_sha256="h1",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    is_ver, reason = verify_website_identity(
        [page_correct],
        organization_number="987654321",
        legal_name="Norge Tech AS",
    )
    assert is_ver is True

    # 2. Similarly named company belonging to someone else (different org, different city)
    page_impostor = CleanedWebPage(
        url="https://www.norgetech.se",
        visible_text="Norge Tech AB i Stockholm, Sverige. Org.nr 556677-8899.",
        content_sha256="h2",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    is_ver2, reason2 = verify_website_identity(
        [page_impostor],
        organization_number="987654321",
        legal_name="Norge Tech AS",
        city="OSLO",
    )
    assert is_ver2 is False

    # 3. Missing org number but legal name + registered municipality (Oslo) match
    page_city_match = CleanedWebPage(
        url="https://www.orkla.com",
        visible_text="Orkla ASA er et ledende merkevareselskap med hovedkontor i Oslo, Norge.",
        content_sha256="h3",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    is_ver3, reason3 = verify_website_identity(
        [page_city_match],
        organization_number="910747711",
        legal_name="ORKLA ASA",
        city="OSLO",
    )
    assert is_ver3 is True

    # 4. Parent company page vs subsidiary: Candidate page for subsidiary only belongs to parent
    page_parent = CleanedWebPage(
        url="https://www.dnb.no",
        visible_text="Velkommen til DNB Bank ASA. Vi tilbyr lån og sparing. Org.nr: 984 851 006, Oslo.",
        content_sha256="h4",
        retrieved_at="2026-10-13T10:00:00Z",
    )
    # Testing for subsidiary "DNB Eiendom AS" with org 910111213
    is_ver4, reason4 = verify_website_identity(
        [page_parent],
        organization_number="910111213",
        legal_name="DNB Eiendom AS",
        city="Trondheim",
    )
    assert is_ver4 is False
    assert reason4 == "unverified_insufficient_proof"
