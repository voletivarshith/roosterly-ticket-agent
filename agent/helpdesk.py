from __future__ import annotations

import random
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import httpx

ACTIONS = ("reply", "handoff", "close")
PAGE_SIZE = 10 
MAX_RATE_LIMIT_WAITS = 20
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0


class HelpdeskError(Exception):
    """Base class for helpdesk failures."""


class AlreadyHandled(HelpdeskError):
    """409: this ticket already has an answer. A customer never gets two."""


class RequestRejected(HelpdeskError):
    """400 / 401 / 404 / 422: the request itself is wrong, so retrying cannot help."""

    def __init__(self, status: int, error: str, problems: Any = None):
        super().__init__(f"{status} {error}" + (f": {problems}" if problems else ""))
        self.status, self.error, self.problems = status, error, problems


class Unavailable(HelpdeskError):
    """The helpdesk kept failing after every retry. The ticket stays open."""


class ServerError(HelpdeskError):
    """A write got no usable response, so we do not know whether it was saved."""


@dataclass(frozen=True)
class Delivery:
    """How a decision reached the helpdesk."""

    confirmed: bool  # False: the response was lost, but the ticket shows as handled
    attempts: int


class HelpdeskClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        email: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_per_second: float = 4.0,
        max_attempts: int = 6,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "X-Candidate-Email": email},
        )
        self._min_interval = 1.0 / max_per_second
        self._max_attempts = max_attempts
        self._sleep, self._clock = sleep, clock
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._per_thread = threading.local()

    @property
    def stats(self) -> Counter[str]:
        """Request counts of the calling thread, so each ticket's log line shows its own retries."""
        if not hasattr(self._per_thread, "stats"):
            self._per_thread.stats = Counter()
        return self._per_thread.stats

    def status(self) -> dict:
        return self._request("GET", "/status")

    def examples(self) -> list[dict]:
        return _list_in(self._request("GET", "/examples"))

    def open_tickets(self) -> list[dict]:
        """Every ticket that has no answer yet, following next_cursor to the end."""
        tickets: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"status": "open", "limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            page = self._request("GET", "/tickets", params=params)
            tickets.extend(_list_in(page))
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not cursor or cursor in seen_cursors:
                return tickets
            seen_cursors.add(cursor)

    def is_open(self, ticket_id: str) -> bool:
        """The API defines "open" as "not yet handled"; the open list is the source of truth."""
        return any(t.get("id") == ticket_id for t in self.open_tickets())

    def post_decision(self, ticket_id: str, action: str, body: dict) -> Delivery:
        """Send one decision. Never sends a second one for the same ticket."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        path = f"/tickets/{ticket_id}/{action}"
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._request("POST", path, json=body, resend_after_lost_response=False)
                return Delivery(confirmed=True, attempts=attempt)
            except ServerError:
                if not self.is_open(ticket_id):
                    return Delivery(confirmed=False, attempts=attempt)
                self._sleep(self._backoff(attempt))
        raise Unavailable(f"could not deliver the decision for {ticket_id}")

    def reset(self) -> dict:
        """Re-open every practice ticket. This wipes the previous run."""
        return self._request("POST", "/practice/reset", resend_after_lost_response=True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        resend_after_lost_response: bool = True,
    ) -> Any:
        failures = rate_limit_waits = 0
        while True:
            self._throttle()
            self.stats["requests"] += 1
            try:
                response = self._http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                self.stats["lost_responses"] += 1
                if not resend_after_lost_response:
                    raise ServerError(str(exc)) from exc
                failures += 1
                if failures >= self._max_attempts:
                    raise Unavailable(f"{method} {path}: {exc}") from exc
                self._sleep(self._backoff(failures))
                continue

            status = response.status_code
            if status < 400:
                return response.json()
            if status == 429:  # rejected before it was processed: always safe to resend
                self.stats["429"] += 1
                rate_limit_waits += 1
                if rate_limit_waits > MAX_RATE_LIMIT_WAITS:
                    raise Unavailable(f"{method} {path}: still rate limited")
                self._sleep(_retry_after(response))
                continue
            if status == 503: 
                self.stats["503"] += 1
                failures += 1
                if failures >= self._max_attempts:
                    raise Unavailable(f"{method} {path}: 503 after {failures} attempts")
                self._sleep(self._backoff(failures))
                continue
            if status == 409:
                raise AlreadyHandled(path)
            if status >= 500:  # any other server error says nothing about what was saved
                self.stats["lost_responses"] += 1
                if not resend_after_lost_response:
                    raise ServerError(f"{status} from {method} {path}")
                failures += 1
                if failures >= self._max_attempts:
                    raise Unavailable(f"{method} {path}: {status} after {failures} attempts")
                self._sleep(self._backoff(failures))
                continue
            error, problems = _error_details(response)
            raise RequestRejected(status, error, problems)

    def _throttle(self) -> None:
        """Space requests so we never exceed max_per_second, even with several threads."""
        with self._lock:
            wait = self._next_slot - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._next_slot = max(self._clock(), self._next_slot) + self._min_interval

    def _backoff(self, attempt: int) -> float:
        """0.5s, 1s, 2s, 4s, 8s ... with jitter, so retries do not arrive in step."""
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return ceiling / 2 + self._rng.uniform(0, ceiling / 2)


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _error_details(response: httpx.Response) -> tuple[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200], None
    if isinstance(body, dict):
        return str(body.get("error", "error")), body.get("problems")
    return "error", None


def _list_in(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []
