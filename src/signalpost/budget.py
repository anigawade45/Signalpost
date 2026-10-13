"""Budget tracking, rate limiting, and safe transient retry orchestration.

Tracks:
- Elapsed time
- Outbound requests
- API cost
- Retries
- Cache hits
- Completed and failed companies

Enforces:
- Stopping optional research when budgets/limits approach to ensure results are always written.
- Safe retries with exponential backoff exclusively for transient errors (timeouts, 5xx, 429).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional, TypeVar
import httpx
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class BudgetLimits(BaseModel):
    """Configurable execution budget limits."""
    model_config = ConfigDict(extra="ignore")

    max_requests_per_company: int = 15
    max_time_seconds_per_company: float = 30.0
    max_total_requests: int = 200
    max_total_cost_usd: float = 10.0
    max_total_time_seconds: float = 300.0
    max_retries: int = 3
    backoff_factor: float = 0.1


class BudgetTracker:
    """Live resource budget tracker providing visible log counters and safety valves."""

    def __init__(self, limits: Optional[BudgetLimits] = None) -> None:
        self.limits = limits or BudgetLimits()
        self.start_time: float = time.perf_counter()
        self.outbound_requests: int = 0
        self.api_cost_usd: float = 0.0
        self.retries_count: int = 0
        self.cache_hits: int = 0
        self.completed_companies: int = 0
        self.failed_companies: int = 0

        # Current entity scope
        self.current_company_org: Optional[str] = None
        self.current_company_requests: int = 0
        self.current_company_start_time: float = self.start_time

    def start_company(self, org_number: str) -> None:
        """Reset per-company counters for a new target entity."""
        self.current_company_org = org_number
        self.current_company_requests = 0
        self.current_company_start_time = time.perf_counter()

    def record_request(self, cost_usd: float = 0.0) -> None:
        """Record an outbound HTTP or API request."""
        self.outbound_requests += 1
        self.current_company_requests += 1
        self.api_cost_usd += max(cost_usd, 0.0)

    def record_retry(self) -> None:
        """Record a transient error retry attempt."""
        self.retries_count += 1

    def record_cache_hit(self) -> None:
        """Record a cache hit avoiding an outbound request."""
        self.cache_hits += 1

    def record_completed(self) -> None:
        """Record successful completion of a company research task."""
        self.completed_companies += 1

    def record_failed(self) -> None:
        """Record failure of a company research task."""
        self.failed_companies += 1

    @property
    def total_elapsed_seconds(self) -> float:
        """Elapsed time since tracker initialization in seconds."""
        return time.perf_counter() - self.start_time

    @property
    def current_company_elapsed_seconds(self) -> float:
        """Elapsed time for current target company in seconds."""
        return time.perf_counter() - self.current_company_start_time

    def should_stop_optional_research(self) -> bool:
        """Determine if optional web research should be stopped to protect the budget.

        Returns True if per-company requests, total requests, or runtime approach limits (80%+).
        Always ensures core registry facts and terminal envelope can be written without timing out.
        """
        # Per-company requests limit (80% threshold)
        if self.current_company_requests >= int(self.limits.max_requests_per_company * 0.8):
            return True

        # Per-company time limit (80% threshold)
        if self.current_company_elapsed_seconds >= (self.limits.max_time_seconds_per_company * 0.8):
            return True

        # Total requests limit (90% threshold)
        if self.outbound_requests >= int(self.limits.max_total_requests * 0.9):
            return True

        # Total runtime limit (90% threshold)
        if self.total_elapsed_seconds >= (self.limits.max_total_time_seconds * 0.9):
            return True

        # Total cost limit
        if self.api_cost_usd >= self.limits.max_total_cost_usd:
            return True

        return False

    def format_log_summary(self) -> str:
        """Return formatted single-line string of all budget counters for run logs."""
        return (
            f"[Budget] Elapsed: {self.total_elapsed_seconds:.2f}s | "
            f"Requests: {self.outbound_requests} | "
            f"Retries: {self.retries_count} | "
            f"Cache Hits: {self.cache_hits} | "
            f"Cost: ${self.api_cost_usd:.5f} | "
            f"Completed: {self.completed_companies} | "
            f"Failed: {self.failed_companies}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Export snapshot of counters."""
        return {
            "elapsed_seconds": round(self.total_elapsed_seconds, 2),
            "outbound_requests": self.outbound_requests,
            "api_cost_usd": round(self.api_cost_usd, 5),
            "retries_count": self.retries_count,
            "cache_hits": self.cache_hits,
            "completed_companies": self.completed_companies,
            "failed_companies": self.failed_companies,
        }


def is_transient_error(exc: Exception) -> bool:
    """Determine if an exception represents a retryable transient failure.

    Transient (Retryable):
    - httpx.TimeoutException
    - httpx.ConnectError
    - HTTP 429 (Rate Limit / Too Many Requests)
    - HTTP 500, 502, 503, 504 (Server Errors)

    Permanent (Non-Retryable):
    - HTTP 400 (Bad Request)
    - HTTP 401 (Unauthorized)
    - HTTP 403 (Forbidden / Blocked)
    - HTTP 404 (Not Found)
    - HTTP 422 (Unprocessable)
    - MalformedData / Parsing Errors
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in {429, 500, 502, 503, 504}:
            return True
        return False

    # Check for custom TransientError attribute or naming
    if "transient" in type(exc).__name__.lower():
        return True

    return False


def execute_with_retry(
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    backoff_factor: float = 0.1,
    budget_tracker: Optional[BudgetTracker] = None,
    **kwargs: Any,
) -> T:
    """Execute a function with exponential backoff retries strictly on transient failures."""
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not is_transient_error(exc) or attempt >= max_retries:
                raise exc

            attempt += 1
            if budget_tracker:
                budget_tracker.record_retry()

            delay = backoff_factor * (2 ** (attempt - 1))
            time.sleep(delay)
