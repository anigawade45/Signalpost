"""Web page retrieval, safe URL validation, robots.txt adherence, HTML cleaning, and identity verification."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
import urllib.robotparser
from typing import Any, Optional
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    from html.parser import HTMLParser

import httpcore
import httpcore._backends.sync
import httpx
from pydantic import BaseModel, ConfigDict, Field

from .cache import PageCache, classify_http_outcome, normalize_url
from .models import AvailabilityStatus

DEFAULT_USER_AGENT = "SignalpostResearchAgent/1.0 (+https://builderr.ai; research-audit)"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5
MAX_PAGES_PER_COMPANY = 4

# Keywords for targeted page discovery
ABOUT_KEYWORDS = {"om-oss", "om_oss", "about", "about-us", "selskapet", "hvem-er-vi", "omoss"}
CONTACT_KEYWORDS = {"kontakt", "contact", "kontakt-oss", "contact-us", "kundeservice"}
CAREERS_KEYWORDS = {"karriere", "career", "careers", "jobs", "jobb", "ledige-stillinger", "stilling"}


PROHIBITED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".arpa",
    ".onion",
    ".test",
    ".example",
    ".invalid",
)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB response size cap


class HostnameResolutionError(ValueError):
    """Raised when DNS resolution for a hostname fails or cannot be resolved."""
    pass


class SSRFBlockedError(ValueError):
    """Raised when a URL points to a forbidden private, loopback, or non-global destination."""
    pass


def assert_public_ip(ip_str: str) -> None:
    """Validate that an individual IP address string is globally routable and public."""
    ip = ipaddress.ip_address(ip_str)

    # Check IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
    if ip.version == 6 and ip.ipv4_mapped:
        mapped = ip.ipv4_mapped
        if not mapped.is_global or mapped.is_private or mapped.is_loopback or mapped.is_link_local:
            raise SSRFBlockedError(f"Access to mapped private IPv4 {mapped} is prohibited for security")

    # Check NAT64 translation prefix (64:ff9b::/96)
    if ip.version == 6 and ip in ipaddress.IPv6Network("64:ff9b::/96"):
        embedded_v4 = ipaddress.IPv4Address(ip.packed[-4:])
        if not embedded_v4.is_global or embedded_v4.is_private or embedded_v4.is_loopback or embedded_v4.is_link_local:
            raise SSRFBlockedError(f"Access to translated private IPv4 {embedded_v4} is prohibited for security")
        return  # Valid public IPv4 via NAT64

    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise SSRFBlockedError(f"Access to non-global/private IP {ip_str} is prohibited for security")

    # Explicit check for CGNAT (100.64.0.0/10)
    if ip.version == 4 and ip in ipaddress.IPv4Network("100.64.0.0/10"):
        raise SSRFBlockedError(f"Access to CGNAT IP {ip_str} is prohibited for security")


class SafeSSRFSyncBackend(httpcore.SyncBackend):
    """Custom network backend for httpcore that enforces outbound IP safety at the socket level.

    Completely closes the DNS rebinding / TOCTOU vulnerability by:
    1. Resolving the hostname via socket.getaddrinfo and asserting that EVERY returned address is public.
    2. Directing the TCP socket connect directly to the validated IP, preventing secondary DNS resolution.
    3. Performing post-connect peer validation via sock.getpeername().
    4. Preserving the original hostname for correct TLS SNI and server certificate verification.
    """

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.NetworkStream:
        # Detect numeric/hex IP strings directly on host
        if host.startswith("0x") or host.isdigit() or (host.startswith("0") and len(host) > 1 and host[1:].isdigit()):
            try:
                int_val = int(host, 0)
                ip_obj = ipaddress.IPv4Address(int_val)
                assert_public_ip(str(ip_obj))
            except ValueError as exc:
                raise SSRFBlockedError(f"Invalid or private numeric IP representation '{host}': {exc}") from exc

        # Resolve host and validate ALL returned addresses
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            raise HostnameResolutionError(f"Could not resolve hostname '{host}': {exc}") from exc

        if not addresses:
            raise HostnameResolutionError(f"No addresses resolved for hostname '{host}'")

        for item in addresses:
            ip_str = item[4][0]
            assert_public_ip(ip_str)

        source_address = None if local_address is None else (local_address, 0)
        exc_map = {
            socket.timeout: httpcore.ConnectTimeout,
            OSError: httpcore.ConnectError,
        }

        last_exc: Exception | None = None
        sock: socket.socket | None = None

        with httpcore._backends.sync.map_exceptions(exc_map):
            for item in addresses:
                validated_ip = item[4][0]
                address = (validated_ip, port)
                try:
                    sock = socket.create_connection(
                        address,
                        timeout,
                        source_address=source_address,
                    )
                    # Support both IPv4 (2-tuple) and IPv6 (4-tuple): sock.getpeername()[0]
                    peer_ip = sock.getpeername()[0]
                    assert_public_ip(peer_ip)
                    break
                except Exception as exc:
                    if isinstance(exc, SSRFBlockedError):
                        raise
                    last_exc = exc
                    if sock is not None:
                        sock.close()
                        sock = None
                    continue

            if sock is None:
                if last_exc:
                    raise last_exc
                raise httpcore.ConnectError(f"Could not connect to any validated IP for {host}")

            for option in (socket_options or []):
                sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        return httpcore._backends.sync.SyncStream(sock)


class SafeSSRFTransport(httpx.HTTPTransport):
    """Hardened HTTP transport enforcing socket-level outbound IP verification."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pool = httpcore.ConnectionPool(
            ssl_context=self._pool._ssl_context,
            network_backend=SafeSSRFSyncBackend(),
            retries=self._pool._retries,
        )


def assert_public_url(url: str) -> None:
    """Validate that the URL is public and strictly prevents SSRF or private network access.

    Rejects:
    - Non-http/https schemes
    - Missing or internal/reserved hostnames
    - Loopback, private, carrier-grade NAT, link-local, multicast, documentation, or reserved IP destinations
    - Alternate/encoded IP representations (hex, octal, decimal, IPv4-mapped IPv6)
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as exc:
        raise SSRFBlockedError(f"Malformed URL '{url}': {exc}") from exc

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")

    if scheme not in {"http", "https"} or not host:
        raise SSRFBlockedError(f"Invalid URL scheme or host in '{url}'")

    if host == "localhost" or host.endswith(PROHIBITED_HOST_SUFFIXES):
        raise SSRFBlockedError(f"Local and internal hosts are prohibited: '{host}'")

    # Detect numeric or hex integer representations (e.g. 2130706433, 0x7f000001, 017700000001)
    if host.startswith("0x") or host.isdigit() or (host.startswith("0") and len(host) > 1 and host[1:].isdigit()):
        try:
            int_val = int(host, 0)
            ip_obj = ipaddress.IPv4Address(int_val)
            assert_public_ip(str(ip_obj))
        except ValueError as exc:
            raise SSRFBlockedError(f"Invalid numeric IP representation: '{host}': {exc}") from exc

    # Resolve via socket.getaddrinfo to validate all resolved IP addresses
    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except Exception as exc:
        raise HostnameResolutionError(f"Could not resolve hostname '{host}': {exc}") from exc

    if not addresses:
        raise HostnameResolutionError(f"No addresses resolved for hostname '{host}'")

    for item in addresses:
        ip_str = item[4][0]
        assert_public_ip(ip_str)


class RobotsCache:
    """In-memory cache for robots.txt rules."""
    _parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    @classmethod
    def is_allowed(cls, url: str, user_agent: str = DEFAULT_USER_AGENT, client: Optional[httpx.Client] = None) -> bool:
        try:
            assert_public_url(url)
            parsed = urllib.parse.urlsplit(url)
            domain = f"{parsed.scheme}://{parsed.netloc}"

            if domain not in cls._parsers:
                robots_url = f"{domain}/robots.txt"
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                try:
                    assert_public_url(robots_url)
                    c = client or httpx.Client(
                        transport=SafeSSRFTransport(),
                        timeout=5.0,
                        headers={"User-Agent": user_agent},
                        follow_redirects=False,
                    )
                    resp = c.get(robots_url, follow_redirects=False)
                    if resp.status_code == 200:
                        parser.parse(resp.text.splitlines())
                    else:
                        parser.allow_all = True
                except Exception:
                    parser.allow_all = True
                cls._parsers[domain] = parser

            return cls._parsers[domain].can_fetch(user_agent, url)
        except Exception:
            return True


class CleanedWebPage(BaseModel):
    """Normalized, text-extracted representation of a crawled webpage."""
    model_config = ConfigDict(extra="ignore")

    url: str
    title: Optional[str] = None
    meta_description: Optional[str] = None
    visible_text: str
    links: list[str] = Field(default_factory=list)
    content_sha256: str
    retrieved_at: str
    status_code: int = 200


class _StdLibHTMLCleaner(HTMLParser):
    """Standard library fallback cleaner when beautifulsoup4 is not installed."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.base_domain = urllib.parse.urlsplit(base_url).netloc.lower()
        self.title: Optional[str] = None
        self.meta_description: Optional[str] = None
        self.links: list[str] = []
        self._text_chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._skip_tags = {"script", "style", "noscript", "svg", "nav", "form", "iframe", "button"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in self._skip_tags:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if tag_lower == "title":
            self._in_title = True
        elif tag_lower == "meta":
            attr_dict = {k.lower(): (v or "") for k, v in attrs}
            if attr_dict.get("name", "").lower() == "description":
                self.meta_description = attr_dict.get("content", "").strip()
        elif tag_lower == "a":
            attr_dict = {k.lower(): (v or "") for k, v in attrs}
            raw_href = attr_dict.get("href", "").strip()
            if raw_href and not raw_href.startswith(("javascript:", "mailto:", "tel:", "#")):
                try:
                    resolved = urllib.parse.urljoin(self.base_url, raw_href)
                    norm = normalize_url(resolved)
                    if urllib.parse.urlsplit(norm).netloc.lower() == self.base_domain and norm not in self.links and norm != self.base_url:
                        self.links.append(norm)
                except Exception:
                    pass

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in self._skip_tags:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if tag_lower == "title":
            self._in_title = False
        elif tag_lower in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "tr"}:
            self._text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            if self.title is None:
                self.title = data.strip()
            else:
                self.title += " " + data.strip()
        else:
            self._text_chunks.append(data)

    def get_visible_text(self) -> str:
        raw_text = "".join(self._text_chunks)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_text.splitlines()]
        return "\n".join(line for line in lines if line)


def clean_html(html: str, base_url: str, content_sha256: str, retrieved_at: str, status_code: int = 200) -> CleanedWebPage:
    """Clean raw HTML: strip scripts, styles, navigation, and extract visible text."""
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")

        # Extract title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        # Extract meta description
        meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        meta_desc = meta_desc_tag.get("content", "").strip() if meta_desc_tag else None

        # Discover same-domain links before stripping
        links: list[str] = []
        base_parsed = urllib.parse.urlsplit(base_url)
        base_domain = base_parsed.netloc.lower()

        for a_tag in soup.find_all("a", href=True):
            raw_href = a_tag["href"].strip()
            if not raw_href or raw_href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            try:
                resolved = urllib.parse.urljoin(base_url, raw_href)
                norm = normalize_url(resolved)
                resolved_parsed = urllib.parse.urlsplit(norm)
                if resolved_parsed.netloc.lower() == base_domain and norm not in links and norm != base_url:
                    links.append(norm)
            except Exception:
                continue

        # Decompose unwanted noise tags
        for tag in soup.find_all(["script", "style", "noscript", "svg", "nav", "form", "iframe", "button"]):
            tag.decompose()

        # Extract clean text with newline separation for readability
        raw_text = soup.get_text(separator="\n")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_text.splitlines()]
        visible_text = "\n".join(line for line in lines if line)

        return CleanedWebPage(
            url=base_url,
            title=title,
            meta_description=meta_desc,
            visible_text=visible_text,
            links=links,
            content_sha256=content_sha256,
            retrieved_at=retrieved_at,
            status_code=status_code,
        )
    else:
        # Robust standard library fallback
        cleaner = _StdLibHTMLCleaner(base_url=base_url)
        cleaner.feed(html)
        return CleanedWebPage(
            url=base_url,
            title=cleaner.title,
            meta_description=cleaner.meta_description,
            visible_text=cleaner.get_visible_text(),
            links=cleaner.links,
            content_sha256=content_sha256,
            retrieved_at=retrieved_at,
            status_code=status_code,
        )


def discover_target_pages(homepage_url: str, homepage: CleanedWebPage, max_pages: int = MAX_PAGES_PER_COMPANY) -> list[str]:
    """Select targeted pages (About, Contact, Careers) from homepage links, capped per company."""
    selected = [normalize_url(homepage_url)]

    categorized: dict[str, Optional[str]] = {
        "about": None,
        "contact": None,
        "careers": None,
    }

    for link in homepage.links:
        path = urllib.parse.urlsplit(link).path.lower()
        if not categorized["about"] and any(kw in path for kw in ABOUT_KEYWORDS):
            categorized["about"] = link
        elif not categorized["contact"] and any(kw in path for kw in CONTACT_KEYWORDS):
            categorized["contact"] = link
        elif not categorized["careers"] and any(kw in path for kw in CAREERS_KEYWORDS):
            categorized["careers"] = link

    for cat_link in categorized.values():
        if cat_link and cat_link not in selected and len(selected) < max_pages:
            selected.append(cat_link)

    return selected


def verify_website_identity(
    pages: list[CleanedWebPage],
    organization_number: str,
    legal_name: str,
    city: Optional[str] = None,
    address: Optional[str] = None,
) -> tuple[bool, str]:
    """Verify that candidate pages belong to the exact Norwegian legal entity.

    Rules:
    1. Exact 9-digit organization number appears in the page text / footer. OR
    2. Legal name (or distinctive name tokens) PLUS registered municipality/city appear together.
    """
    all_text = " ".join(p.visible_text for p in pages)
    compact_digits = re.sub(r"\D", "", all_text)

    # 1. Exact 9-digit match (with or without spaces: 123456789 or 123 456 789)
    clean_org = re.sub(r"\D", "", organization_number)
    if clean_org and clean_org in compact_digits:
        return True, "verified_by_exact_org_number"

    # Also search for formatted org number: e.g. "984 851 006"
    spaced_org = f"{clean_org[:3]} {clean_org[3:6]} {clean_org[6:]}"
    if spaced_org in all_text:
        return True, "verified_by_formatted_org_number"

    # 2. Distinctive name tokens + location (city or address)
    normalized_text = all_text.lower()
    norm_name = legal_name.lower().replace(" asa", "").replace(" as", "").replace(" enk", "").strip()

    name_found = norm_name in normalized_text if len(norm_name) >= 3 else False
    loc_found = False
    if city and city.lower() in normalized_text:
        loc_found = True
    if address:
        addr_parts = [p.strip().lower() for p in re.split(r"[,0-9]+", address) if len(p.strip()) >= 4]
        if any(part in normalized_text for part in addr_parts):
            loc_found = True

    if name_found and loc_found:
        return True, "verified_by_legal_name_and_location"

    return False, "unverified_insufficient_proof"


class FetchOutcome(tuple):
    """Backwards-compatible (page, outcome) tuple carrying detailed error_class information."""

    def __new__(
        cls,
        page: Optional[CleanedWebPage],
        outcome: AvailabilityStatus,
        error_class: Optional[str] = None,
    ) -> FetchOutcome:
        inst = super().__new__(cls, (page, outcome))
        inst.page = page
        inst.outcome = outcome
        inst.error_class = error_class
        return inst


def fetch_page_with_cache(
    url: str,
    cache: PageCache,
    client: Optional[httpx.Client] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchOutcome:
    """Fetch a single web page with cache lookup, robots.txt check, and safety controls."""
    canonical = normalize_url(url)
    if not canonical:
        return FetchOutcome(None, AvailabilityStatus.NOT_AVAILABLE, "EmptyURL")

    # 1. Check Cache
    cached = cache.get(canonical)
    if cached:
        cleaned = clean_html(
            html=cached.text,
            base_url=cached.url,
            content_sha256=cached.content_sha256,
            retrieved_at=cached.retrieved_at,
            status_code=cached.status_code,
        )
        return FetchOutcome(cleaned, cached.outcome, None)

    # 2. Safety & Robots Check
    try:
        assert_public_url(canonical)
    except HostnameResolutionError:
        return FetchOutcome(None, AvailabilityStatus.FAILED, "HostnameResolutionError")
    except (SSRFBlockedError, ValueError):
        return FetchOutcome(None, AvailabilityStatus.BLOCKED, "SSRFBlockedError")

    if not RobotsCache.is_allowed(canonical, user_agent=user_agent, client=client):
        return FetchOutcome(None, AvailabilityStatus.BLOCKED, "RobotsTxtBlocked")

    # 3. Network Fetch bounded by concurrency semaphore and manual safe-redirect handling
    owned_client = client is None
    http_client = client or httpx.Client(
        transport=SafeSSRFTransport(),
        timeout=timeout,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,  # CRITICAL: Validate every redirect destination manually!
    )

    try:
        current_url = canonical
        redirect_count = 0
        final_resp = None

        while True:
            # Enforce public safety check immediately before EVERY outbound request
            try:
                assert_public_url(current_url)
            except HostnameResolutionError:
                return FetchOutcome(None, AvailabilityStatus.FAILED, "HostnameResolutionError")
            except (SSRFBlockedError, ValueError):
                return FetchOutcome(None, AvailabilityStatus.BLOCKED, "SSRFBlockedError")

            with cache.semaphore:
                resp = http_client.get(current_url)

            # Enforce max response size limit to prevent memory bombs
            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
                return FetchOutcome(None, AvailabilityStatus.BLOCKED, "ResponseTooLarge")
            if len(resp.content) > MAX_RESPONSE_BYTES:
                return FetchOutcome(None, AvailabilityStatus.BLOCKED, "ResponseTooLarge")

            if resp.is_redirect:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS:
                    return FetchOutcome(None, AvailabilityStatus.FAILED, "TooManyRedirects")

                loc = resp.headers.get("location")
                if not loc:
                    return FetchOutcome(None, AvailabilityStatus.FAILED, "MissingRedirectLocation")

                resolved = urllib.parse.urljoin(current_url, loc)
                if not resolved:
                    return FetchOutcome(None, AvailabilityStatus.FAILED, "InvalidRedirectURL")

                # Validate redirect target BEFORE next loop iteration
                try:
                    assert_public_url(resolved)
                except HostnameResolutionError:
                    return FetchOutcome(None, AvailabilityStatus.FAILED, "HostnameResolutionError")
                except (SSRFBlockedError, ValueError):
                    return FetchOutcome(None, AvailabilityStatus.BLOCKED, "SSRFBlockedError")

                current_url = resolved
                continue

            final_resp = resp
            break

        final_url = str(final_resp.url)
        status_code = final_resp.status_code
        text = final_resp.text

        cached_entry = cache.put(
            url=final_url,
            status_code=status_code,
            text=text,
            headers=dict(final_resp.headers),
        )

        if cached_entry.outcome != AvailabilityStatus.AVAILABLE:
            err_name = "HTTP403Blocked" if cached_entry.outcome == AvailabilityStatus.BLOCKED else f"HTTP{status_code}Error"
            return FetchOutcome(None, cached_entry.outcome, err_name)

        cleaned = clean_html(
            html=text,
            base_url=final_url,
            content_sha256=cached_entry.content_sha256,
            retrieved_at=cached_entry.retrieved_at,
            status_code=status_code,
        )
        return FetchOutcome(cleaned, AvailabilityStatus.AVAILABLE, None)

    except httpx.TimeoutException as exc:
        return FetchOutcome(None, AvailabilityStatus.FAILED, type(exc).__name__)
    except httpx.NetworkError as exc:
        return FetchOutcome(None, AvailabilityStatus.FAILED, type(exc).__name__)
    except Exception as exc:
        return FetchOutcome(None, AvailabilityStatus.FAILED, type(exc).__name__)
    finally:
        if owned_client:
            http_client.close()
