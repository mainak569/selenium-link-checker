"""Breadth-first, same-domain crawl, plus the URL normalisation and filtering it relies on."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver

from checker.browser_checks import ConsoleError, PageReport, confirm_problems, inspect_page
from checker.config import Config
from checker.link_validator import (
    RETRY_BACKOFF_SECONDS,
    RETRY_STATUSES,
    LinkResult,
    check_url,
    is_transient,
    make_session,
)

log = logging.getLogger(__name__)

# Not web pages, so there is nothing to crawl or check. Any other non-http(s)
# scheme (ftp:, blob:, about:, ...) is skipped too; these are just the common ones.
SKIPPED_SCHEMES = frozenset({"mailto", "tel", "javascript", "data"})
DEFAULT_PORTS = {"http": 80, "https": 443}

# Without the start page there is nothing to crawl, so a flaky start page gets
# a few more attempts (with a longer pause) than other pages.
START_PAGE_EXTRA_ATTEMPTS = 2
START_PAGE_BACKOFF_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# URL helpers (pure functions, unit tested without a browser)
# --------------------------------------------------------------------------- #


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return a canonical absolute URL, or None if it isn't an http(s) link.

    - resolves relative paths against `base`
    - drops the #fragment (same document, so no extra request needed)
    - lower-cases scheme and host, removes default ports and user:pass@
    - turns an empty path into "/" so "https://x.com" == "https://x.com/"
    The query string is kept: ?page=2 is a different resource.
    """
    url = (url or "").strip()
    if not url:
        return None
    if base:
        url = urljoin(base, url)
    url, _fragment = urldefrag(url)

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in SKIPPED_SCHEMES or scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:  # e.g. "http://example.com:abc"
        return None

    host = parts.hostname  # already lower-cased, without port or credentials
    if ":" in host:  # IPv6 literal needs its brackets back
        host = f"[{host}]"
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def site_key(url: str) -> str:
    """Host used for same-site comparisons; "www.example.com" == "example.com"."""
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def is_same_domain(url: str, root_url: str) -> bool:
    return site_key(url) == site_key(root_url)


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


def is_ignored(url: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(p.search(url) for p in patterns)


def filter_urls(raw_urls: Iterable[str], base: str, patterns: Iterable[re.Pattern[str]] = ()) -> list[str]:
    """Normalise, drop non-http(s) and ignored URLs, and de-duplicate (keeping order)."""
    patterns = list(patterns)
    cleaned: dict[str, None] = {}  # dict as an ordered set
    for raw in raw_urls:
        url = normalize_url(raw, base)
        if url and not is_ignored(url, patterns):
            cleaned[url] = None
    return list(cleaned)


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #


@dataclass
class CrawlResult:
    start_url: str
    pages: list[PageReport] = field(default_factory=list)
    # Every collected URL -> the pages it was found on, in discovery order.
    link_sources: dict[str, list[str]] = field(default_factory=dict)
    image_urls: set[str] = field(default_factory=set)
    # HTTP results for pages checked before opening them in the browser.
    page_checks: dict[str, LinkResult] = field(default_factory=dict)
    # Same-domain URLs that were not opened in the browser, with the reason.
    skipped: dict[str, str] = field(default_factory=dict)

    def add_source(self, url: str, page_url: str) -> None:
        sources = self.link_sources.setdefault(url, [])
        if page_url not in sources:
            sources.append(page_url)


class Crawler:
    """Crawls one site. Owns the browser so it can replace one that gets stuck.

    Call close() when done (main.py does it in a finally block).
    """

    def __init__(
        self,
        config: Config,
        *,
        driver_factory: Callable[[], WebDriver],
        screenshot_dir: Path | None = None,
        page_checker: Callable[[str], LinkResult] | None = None,
    ) -> None:
        self.driver_factory = driver_factory
        self.driver: WebDriver | None = None
        self.config = config
        self.screenshot_dir = screenshot_dir
        self.patterns = compile_patterns(config.ignore_patterns)
        self._page_checker = page_checker

    def crawl(self) -> CrawlResult:
        start = normalize_url(self.config.start_url)
        if start is None:
            raise ValueError(f"Not a crawlable URL: {self.config.start_url}")

        session = None
        page_checker = self._page_checker
        if page_checker is None:
            session = make_session(self.config.user_agent)
            # Same time budget as a browser page load: the first request to a
            # sleeping free-tier host can take 30s+ while it wakes up.
            page_checker = partial(
                check_url,
                session=session,
                timeout=self.config.page_load_timeout_seconds,
                retries=self.config.retries,
            )

        if self.driver is None:
            self.driver = self.driver_factory()
        try:
            return self._crawl(start, page_checker)
        finally:
            if session is not None:
                session.close()

    def close(self) -> None:
        """Quit the browser (whichever one is current after any restarts)."""
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    def _restart_browser(self) -> None:
        try:
            self.close()
        except WebDriverException:
            pass  # the old browser is already broken; we only want it gone
        self.driver = self.driver_factory()

    def _crawl(self, start: str, page_checker: Callable[[str], LinkResult]) -> CrawlResult:
        cfg = self.config
        result = CrawlResult(start_url=start)
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        enqueued = {start}  # never queue the same URL twice
        loaded: set[str] = set()  # URLs (and redirect targets) opened in the browser
        first_request = True

        while queue and len(result.pages) < cfg.max_pages:
            url, depth = queue.popleft()

            # Politeness: pause between requests to the site. This is a fixed
            # rate limit, not a wait for page state (those use WebDriverWait).
            if not first_request and cfg.delay_seconds:
                time.sleep(cfg.delay_seconds)
            first_request = False

            # Selenium can't see HTTP status codes, so ask with requests first.
            # This keeps 404s, auth prompts and file downloads out of the browser.
            check = self._precheck(url, depth, page_checker)
            result.page_checks[url] = check
            reason = self._skip_reason(check, start)
            if reason:
                log.info("Skipping %s: %s", url, reason)
                result.skipped[url] = reason
                continue

            # /old -> /new: if /new was already loaded there's nothing new to see.
            final_url = normalize_url(check.final_url or url) or url
            if final_url in loaded:
                result.skipped[url] = f"redirects to {final_url}, already crawled"
                continue
            loaded.update({url, final_url})
            enqueued.add(final_url)  # and don't load the redirect target again later

            log.info("[%d/%d] depth=%d %s", len(result.pages) + 1, cfg.max_pages, depth, url)
            page = self._load_page(url, depth)
            result.pages.append(page)
            self._log_page(page)

            base = page.base_url or page.final_url or url
            links = filter_urls(page.raw_links, base, self.patterns)
            images = filter_urls(page.raw_images, base, self.patterns)
            for found in links + images:
                result.add_source(found, url)
            result.image_urls.update(images)

            if depth >= cfg.max_depth:
                continue  # links on this page get checked, but not crawled
            for link in links:
                if link not in enqueued and is_same_domain(link, start):
                    enqueued.add(link)
                    queue.append((link, depth + 1))

        self._recheck_problem_pages(result.pages)
        log.info(
            "Crawl finished: %d pages loaded, %d unique URLs collected, %d same-domain URLs skipped",
            len(result.pages),
            len(result.link_sources),
            len(result.skipped),
        )
        return result

    @staticmethod
    def _precheck(url: str, depth: int, page_checker: Callable[[str], LinkResult]) -> LinkResult:
        """HTTP check before opening a page. The start page gets extra attempts."""
        check = page_checker(url)
        attempts_left = START_PAGE_EXTRA_ATTEMPTS if depth == 0 else 0
        while attempts_left and is_transient(check):
            log.warning("Start page answered %s, trying again in %gs", check.describe(), START_PAGE_BACKOFF_SECONDS)
            time.sleep(START_PAGE_BACKOFF_SECONDS)
            check = page_checker(url)
            attempts_left -= 1
        return check

    def _load_page(self, url: str, depth: int) -> PageReport:
        """Open a page in the browser, loading it again if the first try failed.

        The pre-flight request can get a 200 while the browser's own request a
        moment later hits a struggling server (a 429/5xx, or a load that never
        finishes). The crawl needs this page's links, so it is retried straight
        away (up to `retries` times).
        """
        page = self._inspect(url, depth)
        for _ in range(self.config.retries):
            if page.http_status in RETRY_STATUSES:
                log.warning("    browser got HTTP %s, loading again", page.http_status)
            elif page.load_failed:
                log.warning("    page did not load, trying once more")
            else:
                break
            self._pause()
            page = self._inspect(url, depth)
        return page

    def _recheck_problem_pages(self, pages: list[PageReport]) -> None:
        """Load pages with broken images or console errors once more; keep what happens again.

        This runs after the crawl, minutes after the first load, so a server
        hiccup during the first load has usually passed. (Reloading straight
        away tends to land in the same bad patch: on the demo host, the first
        page's CSS and JS often time out with 503s while the server warms up.)
        Real bugs show up both times; one-off failures don't.
        """
        suspects = [page for page in pages if page.broken_images or page.console_errors]
        if not suspects or not self.config.retries:
            return
        log.info("Re-checking %d page(s) with problems to rule out one-off failures", len(suspects))
        for page in suspects:
            self._pause()
            second = self._inspect(page.url, page.depth)
            if second.http_status in RETRY_STATUSES:
                log.warning("    %s answered HTTP %s, keeping the first result", page.url, second.http_status)
                continue
            before = len(page.broken_images) + len(page.console_errors)
            confirm_problems(page, second)
            after = len(page.broken_images) + len(page.console_errors)
            log.info("    %s: %d of %d problem(s) happened again", page.url, after, before)

    def _inspect(self, url: str, depth: int) -> PageReport:
        cfg = self.config
        try:
            page = inspect_page(
                self.driver,
                url,
                depth=depth,
                slow_page_ms=cfg.slow_page_ms,
                page_load_timeout=cfg.page_load_timeout_seconds,
                screenshot_dir=self.screenshot_dir,
            )
        except WebDriverException as exc:
            # The browser itself stopped responding (e.g. chromedriver's "Timed out
            # receiving message from renderer" after a hung page load). Record it on
            # this page and start a fresh browser so one bad page can't end the run.
            reason = (exc.msg or type(exc).__name__).splitlines()[0]
            log.warning("    browser stopped responding (%s); starting a new one", reason)
            self._restart_browser()
            return PageReport(url=url, depth=depth, console_errors=[
                ConsoleError("page-load", f"Browser stopped responding: {reason}")
            ])
        # Console errors that mention an ignored URL (e.g. a third-party tracker) are dropped too.
        page.console_errors = [err for err in page.console_errors if not is_ignored(err.message, self.patterns)]
        return page

    def _pause(self) -> None:
        # Politeness/backoff pause before hitting the same page again.
        time.sleep(max(self.config.delay_seconds, RETRY_BACKOFF_SECONDS))

    @staticmethod
    def _skip_reason(check: LinkResult, start: str) -> str | None:
        if check.is_broken:
            return f"HTTP {check.describe()}"
        if not check.is_html:
            return f"not an HTML page ({check.content_type.split(';')[0]})"
        if check.final_url and not is_same_domain(check.final_url, start):
            return f"redirects off-site to {check.final_url}"
        return None

    @staticmethod
    def _log_page(page: PageReport) -> None:
        notes = []
        if page.broken_images:
            notes.append(f"{len(page.broken_images)} broken image(s)")
        if page.console_errors:
            notes.append(f"{len(page.console_errors)} console error(s)")
        if page.is_slow:
            notes.append("slow")
        load = f"{page.load_time_ms} ms" if page.load_time_ms is not None else "n/a"
        log.info("    loaded in %s%s", load, f" | {', '.join(notes)}" if notes else "")
