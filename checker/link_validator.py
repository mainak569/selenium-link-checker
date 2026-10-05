"""Concurrent HTTP status checks for every collected URL."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

# Servers that answer HEAD with these are often fine with GET: 403 from bot or
# WAF rules that only cover HEAD, 405 Method Not Allowed, 501 Not Implemented.
HEAD_FALLBACK_STATUSES = frozenset({403, 405, 501})

# Statuses worth one more try because they are usually temporary.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

RETRY_BACKOFF_SECONDS = 1.0


@dataclass
class LinkResult:
    url: str
    status: int | None = None  # final status after following redirects
    final_url: str | None = None
    redirect_chain: list[int] = field(default_factory=list)  # e.g. [301, 302]
    content_type: str = ""
    error: str | None = None  # set for timeouts, SSL and connection errors
    error_kind: str | None = None  # "timeout" | "ssl" | "connection" | "other"
    method: str = "HEAD"
    attempts: int = 1

    @property
    def is_broken(self) -> bool:
        return self.error is not None or (self.status is not None and self.status >= 400)

    @property
    def is_redirect(self) -> bool:
        """Redirected and ended up somewhere healthy (a warning, not a failure)."""
        return bool(self.redirect_chain) and not self.is_broken

    @property
    def is_html(self) -> bool:
        # A missing Content-Type is treated as HTML so the browser gets a chance.
        return not self.content_type or "html" in self.content_type.lower()

    @property
    def outcome(self) -> str:
        if self.is_broken:
            return "broken"
        return "redirect" if self.is_redirect else "ok"

    def describe(self) -> str:
        """Short human-readable status, e.g. "404", "Timeout after 10s"."""
        if self.error:
            return self.error
        return str(self.status)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outcome"] = self.outcome
        return data


def _from_response(url: str, response: requests.Response, method: str) -> LinkResult:
    return LinkResult(
        url=url,
        status=response.status_code,
        final_url=response.url,
        redirect_chain=[r.status_code for r in response.history],
        content_type=response.headers.get("Content-Type", ""),
        method=method,
    )


def _from_exception(url: str, exc: requests.RequestException, timeout: float, method: str) -> LinkResult:
    # Order matters: SSLError is a subclass of ConnectionError, and
    # ConnectTimeout is a subclass of both ConnectionError and Timeout.
    if isinstance(exc, requests.exceptions.SSLError):
        kind, message = "ssl", "SSL error"
    elif isinstance(exc, requests.exceptions.Timeout):
        kind, message = "timeout", f"Timeout after {timeout:g}s"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        kind, message = "connection", "Connection error"
    elif isinstance(exc, requests.exceptions.TooManyRedirects):
        kind, message = "other", "Too many redirects"
    else:
        kind, message = "other", f"Request failed ({type(exc).__name__})"
    return LinkResult(url=url, error=message, error_kind=kind, method=method)


def _check_once(url: str, session: requests.Session, timeout: float) -> LinkResult:
    """HEAD first because it skips the body; fall back to GET when HEAD isn't usable."""
    try:
        response = session.head(url, timeout=timeout, allow_redirects=True)
        response.close()
        if response.status_code not in HEAD_FALLBACK_STATUSES:
            return _from_response(url, response, "HEAD")
    except requests.RequestException:
        pass  # some servers drop or mishandle HEAD requests; GET decides

    try:
        # stream=True reads only the status line and headers; the body is never
        # downloaded because the response is closed straight away.
        with session.get(url, timeout=timeout, allow_redirects=True, stream=True) as response:
            return _from_response(url, response, "GET")
    except requests.RequestException as exc:
        return _from_exception(url, exc, timeout, "GET")


def is_transient(result: LinkResult) -> bool:
    """True for failures that often go away on a second try."""
    if result.error_kind in ("timeout", "connection"):
        return True
    return result.status in RETRY_STATUSES


def check_url(
    url: str,
    session: requests.Session,
    *,
    timeout: float = 10,
    retries: int = 1,
    backoff: float = RETRY_BACKOFF_SECONDS,
) -> LinkResult:
    """Check one URL, retrying transient failures up to `retries` extra times."""
    attempt = 0
    while True:
        attempt += 1
        result = _check_once(url, session, timeout)
        result.attempts = attempt
        if attempt > retries or not is_transient(result):
            return result
        log.debug("Retrying %s after %s", url, result.describe())
        # A short pause, not a wait for page state: it gives a flaky server a moment.
        time.sleep(backoff)


def make_session(user_agent: str, pool_size: int = 10) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def validate_links(
    urls: Iterable[str],
    *,
    user_agent: str,
    max_workers: int = 10,
    timeout: float = 10,
    retries: int = 1,
    known: Mapping[str, LinkResult] | None = None,
    session_factory: Callable[[], requests.Session] | None = None,
) -> dict[str, LinkResult]:
    """Check every URL in parallel and return {url: LinkResult}.

    `known` holds results we already have (the crawler checks each page before
    opening it in the browser), so those URLs aren't requested twice.
    """
    unique_urls = list(dict.fromkeys(urls))
    known = known or {}
    results: dict[str, LinkResult] = {url: known[url] for url in unique_urls if url in known}
    pending = [url for url in unique_urls if url not in results]

    # requests.Session isn't guaranteed thread-safe, so each worker thread gets
    # its own session (and connection pool), created on first use.
    factory = session_factory or (lambda: make_session(user_agent))
    local = threading.local()
    sessions: list[requests.Session] = []
    sessions_lock = threading.Lock()

    def worker(url: str) -> LinkResult:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = factory()
            with sessions_lock:
                sessions.append(session)
        try:
            return check_url(url, session, timeout=timeout, retries=retries)
        except Exception as exc:  # one odd URL must not abort the whole run
            log.warning("Unexpected error checking %s: %r", url, exc)
            return LinkResult(url=url, error=f"Request failed ({type(exc).__name__})", error_kind="other")

    log.info("Checking %d links with %d workers (%d already known)", len(pending), max_workers, len(results))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(worker, url): url for url in pending}
            for done, future in enumerate(as_completed(futures), start=1):
                url = futures[future]
                results[url] = future.result()
                if results[url].is_broken:
                    log.info("Broken: %s (%s)", url, results[url].describe())
                if done % 25 == 0 or done == len(pending):
                    log.info("Checked %d/%d links", done, len(pending))
    finally:
        for session in sessions:
            session.close()
    return results
