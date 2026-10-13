"""Client for the Norwegian Central Coordinating Register for Legal Entities (Enhetsregisteret).

Calls https://data.brreg.no/enhetsregisteret/api/enheter/{organization_number}
and normalizes company identity into internal records with complete provenance.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
import httpx
from pydantic import BaseModel, ConfigDict, Field

BRREG_BASE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "signalpost-registry-client/0.1.0"


# ==============================================================================
# Error Hierarchy
# ==============================================================================

class RegistryError(Exception):
    """Base exception for all registry-related errors."""
    pass


class NotFoundError(LookupError, RegistryError):
    """Raised when an organization number is not found in the registry (HTTP 404)."""
    pass


class TransientError(RegistryError):
    """Raised on temporary network or server failures (timeouts, connection drops, HTTP 5xx)."""
    pass


class BlockedError(RegistryError):
    """Raised when access is blocked, forbidden, or rate-limited (HTTP 403, 429)."""
    pass


class MalformedDataError(RegistryError):
    """Raised when registry response is malformed, not valid JSON, or structurally invalid."""
    pass


# ==============================================================================
# Normalized Models
# ==============================================================================

class AddressRecord(BaseModel):
    """Normalized postal or business address."""
    model_config = ConfigDict(extra="ignore")

    street: Optional[list[str]] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    municipality: Optional[str] = None
    municipality_number: Optional[str] = None
    country_code: Optional[str] = None
    country: Optional[str] = None
    formatted: Optional[str] = None


class CompanyRecord(BaseModel):
    """Normalized company record from Enhetsregisteret."""
    model_config = ConfigDict(extra="ignore")

    organization_number: str
    legal_name: Optional[str] = None
    entity_type: Optional[str] = None
    entity_type_description: Optional[str] = None
    registered_status: Optional[str] = None
    is_registered: Optional[bool] = None
    is_bankrupt: Optional[bool] = None
    is_under_liquidation: Optional[bool] = None
    is_deleted: Optional[bool] = None
    deleted_date: Optional[str] = None
    registration_date: Optional[str] = None
    is_registered_in_business_register: Optional[bool] = None
    is_registered_in_vat_register: Optional[bool] = None
    latest_submitted_accounts: Optional[str] = None
    industry_code: Optional[str] = None
    industry_description: Optional[str] = None
    employee_count: Optional[int] = None
    employee_count_scope: Optional[str] = None  # "legal_entity" vs "group"
    employee_count_source: Optional[str] = None  # e.g. "NAV Aa-registeret"
    employee_count_date: Optional[str] = None
    is_part_of_group: Optional[bool] = None  # from erIKonsern
    website: Optional[str] = None
    business_address: Optional[AddressRecord] = None
    postal_address: Optional[AddressRecord] = None
    registry_source_url: str
    retrieved_at: str
    raw_response: Optional[dict[str, Any]] = Field(default=None, repr=False)



    def to_dict(self) -> dict[str, Any]:
        """Convert to standard Python dictionary."""
        return self.model_dump()


# ==============================================================================
# Validation & Normalization Helpers
# ==============================================================================

def validate_organization_number(organization_number: str | int) -> str:
    """Validate that the organization number consists of exactly nine digits.

    Args:
        organization_number: Organization number as string or int.

    Returns:
        The validated 9-digit string.

    Raises:
        ValueError: If the number is not exactly nine digits.
    """
    clean = str(organization_number).strip().replace(" ", "")
    if not re.fullmatch(r"\d{9}", clean):
        raise ValueError(
            f"Invalid organization number '{organization_number}': "
            "must be exactly nine digits."
        )
    return clean


def _normalize_address(raw_addr: Optional[dict[str, Any]]) -> Optional[AddressRecord]:
    """Parse address dictionary into AddressRecord."""
    if not raw_addr or not isinstance(raw_addr, dict):
        return None

    street_lines = raw_addr.get("adresse")
    if isinstance(street_lines, str):
        street_lines = [street_lines]
    elif not isinstance(street_lines, list):
        street_lines = None

    postnr = raw_addr.get("postnummer")
    poststed = raw_addr.get("poststed")
    land = raw_addr.get("land")

    parts: list[str] = []
    if street_lines:
        parts.append(", ".join(street_lines))
    if postnr and poststed:
        parts.append(f"{postnr} {poststed}")
    elif poststed:
        parts.append(poststed)
    if land and land.upper() not in {"NORGE", "NORWAY"}:
        parts.append(land)

    formatted = ", ".join(parts) if parts else None

    return AddressRecord(
        street=street_lines,
        postal_code=postnr,
        city=poststed,
        municipality=raw_addr.get("kommune"),
        municipality_number=raw_addr.get("kommunenummer"),
        country_code=raw_addr.get("landkode"),
        country=land,
        formatted=formatted,
    )


def normalize_registry_response(
    raw_data: dict[str, Any],
    source_url: str,
    retrieved_at: Optional[str] = None,
) -> CompanyRecord:
    """Normalize raw Enhetsregisteret API dictionary into CompanyRecord.

    Treats every field as optional to accommodate incomplete or deleted entities.
    """
    if retrieved_at is None:
        retrieved_at = datetime.now(timezone.utc).isoformat()

    org_nr = str(raw_data.get("organisasjonsnummer") or "").strip()
    legal_name = raw_data.get("navn")

    # Organisasjonsform
    org_form = raw_data.get("organisasjonsform") or {}
    entity_type = org_form.get("kode") if isinstance(org_form, dict) else None
    entity_type_desc = org_form.get("beskrivelse") if isinstance(org_form, dict) else None

    # Status attributes
    deleted_date = raw_data.get("slettedato")
    is_deleted = bool(deleted_date)
    is_bankrupt = bool(raw_data.get("konkurs"))
    is_under_liq = bool(
        raw_data.get("underAvvikling")
        or raw_data.get("underTvangsavviklingEllerTvangsopplosning")
    )
    registration_date = raw_data.get("registreringsdatoEnhetsregisteret")

    # In Enhetsregisteret, an entity returned without a deletion date (slettedato)
    # is actively registered in the register.
    # If the response explicitly states 'registrertIEnhetsregisteret': False, respect it.
    reg_flag = raw_data.get("registrertIEnhetsregisteret")
    if is_deleted or reg_flag is False:
        is_registered = False
    else:
        is_registered = True

    if is_deleted:
        registered_status = "deleted"
    elif is_bankrupt:
        registered_status = "bankrupt"
    elif is_under_liq:
        registered_status = "under_liquidation"
    elif is_registered:
        registered_status = "active"
    else:
        registered_status = "inactive"


    # Industry code
    naering = raw_data.get("naeringskode1") or {}
    ind_code = naering.get("kode") if isinstance(naering, dict) else None
    ind_desc = naering.get("beskrivelse") if isinstance(naering, dict) else None

    # Website
    website = raw_data.get("hjemmeside")
    if website and not website.startswith(("http://", "https://")):
        website = f"https://{website}"

    # Employee count and reporting scope
    emp_count = raw_data.get("antallAnsatte")
    if emp_count is not None:
        try:
            emp_count = int(emp_count)
        except (ValueError, TypeError):
            emp_count = None

    # Enhetsregisteret's antallAnsatte is registered via NAV Aa-registeret
    # for the specific legal entity (selskap) and its Norwegian subunits,
    # NOT the consolidated corporate group (konsern).
    is_part_of_group = bool(raw_data.get("erIKonsern"))
    emp_scope = "legal_entity" if emp_count is not None else None
    emp_source = "NAV Aa-registeret" if emp_count is not None else None
    emp_date = (
        raw_data.get("registreringsdatoAntallAnsatteNAVAaregisteret")
        or raw_data.get("registreringsdatoAntallAnsatteEnhetsregisteret")
    )

    return CompanyRecord(
        organization_number=org_nr,
        legal_name=legal_name,
        entity_type=entity_type,
        entity_type_description=entity_type_desc,
        registered_status=registered_status,
        is_registered=is_registered,
        is_bankrupt=is_bankrupt,
        is_under_liquidation=is_under_liq,
        is_deleted=is_deleted,
        deleted_date=deleted_date,
        registration_date=raw_data.get("registreringsdatoEnhetsregisteret"),
        is_registered_in_business_register=raw_data.get("registrertIForetaksregisteret"),
        is_registered_in_vat_register=raw_data.get("registrertIMvaregisteret"),
        latest_submitted_accounts=raw_data.get("sisteInnsendteAarsregnskap"),
        industry_code=ind_code,
        industry_description=ind_desc,
        employee_count=emp_count,
        employee_count_scope=emp_scope,
        employee_count_source=emp_source,
        employee_count_date=emp_date,
        is_part_of_group=is_part_of_group,
        website=website,
        business_address=_normalize_address(raw_data.get("forretningsadresse")),
        postal_address=_normalize_address(raw_data.get("postadresse")),
        registry_source_url=source_url,
        retrieved_at=retrieved_at,
        raw_response=raw_data,
    )



# ==============================================================================
# Client Implementation
# ==============================================================================

class RegistryClient:
    """Client for interacting with the Brønnøysund Enhetsregisteret API."""

    def __init__(
        self,
        base_url: str = BRREG_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.Client] = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self._external_client = client is not None
        self._client = client or httpx.Client(
            timeout=self.timeout,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client if internally managed."""
        if not self._external_client and not self._client.is_closed:
            self._client.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def get_company(self, organization_number: str | int) -> CompanyRecord:
        """Fetch and normalize a company record by its 9-digit organization number.

        Args:
            organization_number: Exactly nine digits.

        Returns:
            CompanyRecord: Normalized company record with raw response reference.

        Raises:
            ValueError: If organization_number format is invalid.
            NotFoundError: If company does not exist (404).
            BlockedError: If rate-limited or forbidden (403, 429).
            TransientError: If network times out, connection fails, or 5xx error.
            MalformedDataError: If response is not valid JSON.
        """
        valid_org = validate_organization_number(organization_number)
        url = f"{self.base_url}/{valid_org}"
        retrieved_at = datetime.now(timezone.utc).isoformat()

        try:
            response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Request timed out after {self.timeout}s for URL '{url}': {exc}") from exc
        except httpx.NetworkError as exc:
            raise TransientError(f"Network error accessing URL '{url}': {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"HTTP client error accessing URL '{url}': {exc}") from exc

        # Handle specific HTTP status codes
        if response.status_code == 404:
            raise NotFoundError(f"Organization '{valid_org}' not found in registry (HTTP 404).")

        if response.status_code in {403, 429}:
            raise BlockedError(
                f"Registry request blocked or rate-limited for '{valid_org}' (HTTP {response.status_code}): {response.text}"
            )

        if 500 <= response.status_code < 600:
            raise TransientError(
                f"Registry server error (HTTP {response.status_code}) for '{valid_org}': {response.text}"
            )

        # Call raise_for_status for any other unhandled 4xx error
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RegistryError(f"HTTP status error for '{valid_org}': {exc}") from exc

        # Parse JSON
        try:
            data = response.json()
        except Exception as exc:
            raise MalformedDataError(f"Invalid JSON returned from '{url}': {exc}") from exc

        if not isinstance(data, dict):
            raise MalformedDataError(f"Expected JSON object from '{url}', got {type(data).__name__}")

        return normalize_registry_response(data, source_url=url, retrieved_at=retrieved_at)


def fetch_company_record(
    organization_number: str | int,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Optional[httpx.Client] = None,
) -> CompanyRecord:
    """Convenience functional interface to fetch a normalized company record."""
    with RegistryClient(timeout=timeout, client=client) as reg_client:
        return reg_client.get_company(organization_number)
