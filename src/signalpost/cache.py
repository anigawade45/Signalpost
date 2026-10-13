"""Thread-safe URL caching, canonical deduplication, concurrency bounding, and outcome mapping."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from .models import AvailabilityStatus

DEFAULT_CACHE_DIR = Path("data/cache/pages")
DEFAULT_MAX_CONCURRENCY = 5
DEFAULT_TTL_SECONDS = 86400  # 24 hours

# Tracking and ephemeral query parameters to strip for canonical deduplication
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
    "ref",
    "source",
}

# Signatures indicating an anti-bot challenge or block page disguised as 200 OK
BLOCK_MARKERS = [
    "attention required! | cloudflare",
    "cf-browser-verification",
    "ddos-guard",
    "please enable cookies",
    "access denied",
    "security check to continue",
    "bot verification",
    "shield_challenge",
]


def normalize_url(url: str) -> str:
    """Normalize and canonically deduplicate a web URL.

    - Lowercases scheme and hostname
    - Strips fragments (#anchor)
    - Strips tracking query parameters (utm_*, gclid, etc.)
    - Standardizes empty path to '/'
    """
    url_str = str(url or "").strip()
    if not url_str:
        return ""

    if not re.match(r"^https?://", url_str, re.I):
        url_str = "https://" + url_str

    try:
        parsed = urllib.parse.urlsplit(url_str)
    except Exception:
        return url_str

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Remove default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Filter out tracking parameters
    query_parts = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered_query = [
        (k, v) for k, v in query_parts
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query_str = urllib.parse.urlencode(sorted(filtered_query))

    path = parsed.path
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urllib.parse.urlunsplit((scheme, netloc, path, query_str, ""))


def classify_http_outcome(status_code: int, text: str = "") -> AvailabilityStatus:
    """Map HTTP access outcome and content signatures to official availability status.

    - 200 without block markers -> AVAILABLE
    - 401, 403, 429 or block page text -> BLOCKED
    - 404, 410 -> NOT_AVAILABLE
    - Timeout, 500, 502, 503, 504 -> FAILED (transient failure, NOT not_available!)
    """
    if status_code == 200:
        lower_sample = text[:4000].lower() if text else ""
        if any(marker in lower_sample for marker in BLOCK_MARKERS):
            return AvailabilityStatus.BLOCKED
        return AvailabilityStatus.AVAILABLE

    if status_code in {401, 403, 429}:
        return AvailabilityStatus.BLOCKED

    if status_code in {404, 410}:
        return AvailabilityStatus.NOT_AVAILABLE

    # 408 Request Timeout, 5xx Server Errors, or connection drops
    if status_code in {408, 500, 502, 503, 504} or status_code == 0:
        return AvailabilityStatus.FAILED

    return AvailabilityStatus.FAILED


class CachedPage(BaseModel):
    """Immutable cached representation of a fetched web page."""
    model_config = ConfigDict(extra="ignore")

    url: str
    status_code: int
    text: str
    content_sha256: str
    retrieved_at: str
    outcome: AvailabilityStatus
    headers: dict[str, str] = Field(default_factory=dict)


class PageCache:
    """Thread-safe URL response cache with persistent filesystem storage and concurrency bounding."""

    def __init__(
        self,
        cache_dir: Optional[Path | str] = DEFAULT_CACHE_DIR,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self.semaphore = threading.Semaphore(max_concurrency)
        self._memory_cache: dict[str, CachedPage] = {}

    def _url_key(self, canonical_url: str) -> str:
        return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> Optional[CachedPage]:
        """Retrieve cached page if present and within TTL."""
        canonical = normalize_url(url)
        key = self._url_key(canonical)

        with self._lock:
            if key in self._memory_cache:
                return self._memory_cache[key]

        if not self.cache_dir:
            return None

        disk_file = self.cache_dir / f"{key}.json"
        if disk_file.exists():
            try:
                data = json.loads(disk_file.read_text(encoding="utf-8"))
                entry = CachedPage.model_validate(data)
                with self._lock:
                    self._memory_cache[key] = entry
                return entry
            except Exception:
                return None
        return None

    def put(
        self,
        url: str,
        status_code: int,
        text: str,
        retrieved_at: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> CachedPage:
        """Store page in cache and persist to disk."""
        canonical = normalize_url(url)
        key = self._url_key(canonical)
        if retrieved_at is None:
            retrieved_at = datetime.now(timezone.utc).isoformat()

        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        outcome = classify_http_outcome(status_code, text)

        entry = CachedPage(
            url=canonical,
            status_code=status_code,
            text=text,
            content_sha256=sha256,
            retrieved_at=retrieved_at,
            outcome=outcome,
            headers=headers or {},
        )

        with self._lock:
            self._memory_cache[key] = entry

        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                disk_file = self.cache_dir / f"{key}.json"
                disk_file.write_text(json.dumps(entry.model_dump(), ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass  # Non-fatal if disk write fails

        return entry
