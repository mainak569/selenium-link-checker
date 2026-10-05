"""Checks that need a real browser: rendering, JavaScript errors and timing.

A plain HTTP client can tell you an image URL returns 200, but not whether the
browser could actually decode and draw it, whether the page's JavaScript threw
errors, or how long the page really took to load. That is what this module is for.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

log = logging.getLogger(__name__)

# How long to wait for images to finish loading (or failing) after the page's
# load event. Images that are still pending after this are left out of the
# broken-image check rather than guessed at.
IMAGE_SETTLE_TIMEOUT = 10

# Returns raw attribute values plus the page's base URI; resolving and
# normalising happens in Python (crawler.normalize_url) so it can be unit tested.
COLLECT_LINKS_JS = """
return {
  base: document.baseURI,
  links: Array.from(document.querySelectorAll('a[href]'), a => a.getAttribute('href')),
  images: Array.from(document.querySelectorAll('img[src]'), img => img.getAttribute('src')),
};
"""

# An <img> is broken when the browser has finished with it (complete) but got
# no decodable pixels (naturalWidth 0). Images that are still loading, such as
# below-the-fold loading="lazy" ones, have complete == false and are skipped.
BROKEN_IMAGES_JS = """
return Array.from(document.images)
  .filter(img => img.getAttribute('src') && img.complete && img.naturalWidth === 0)
  .map(img => img.currentSrc || img.src);
"""

# Selenium has no API for HTTP status codes, but Chrome exposes the document's
# status on the navigation timing entry (responseStatus, Chrome 109+).
HTTP_STATUS_JS = """
const [nav] = performance.getEntriesByType('navigation');
return nav && nav.responseStatus ? nav.responseStatus : null;
"""

IMAGES_SETTLED_JS = """
return Array.from(document.images).every(img => img.complete || img.loading === 'lazy');
"""

# Navigation Timing Level 2, with a fallback to the older performance.timing API.
# Returns null until the load event has finished so WebDriverWait keeps polling.
LOAD_TIME_JS = """
const [nav] = performance.getEntriesByType('navigation');
if (nav) {
  return nav.loadEventEnd > 0 ? nav.loadEventEnd - nav.startTime : null;
}
const t = performance.timing;
return t.loadEventEnd > 0 ? t.loadEventEnd - t.navigationStart : null;
"""


# Query strings inside console messages are mostly noise (cache busters,
# tracking parameters) and make the same error look different on every page.
QUERY_STRING_RE = re.compile(r"(https?://[^\s?#]+)\?\S*")


@dataclass(frozen=True)
class ConsoleError:
    """A SEVERE browser console entry (we only keep that level)."""

    source: str  # "javascript", "network", "page-load", ...
    message: str


@dataclass
class PageReport:
    """Everything the browser found on one page."""

    url: str
    depth: int
    final_url: str = ""
    http_status: int | None = None  # status of the document as the browser received it
    title: str = ""
    load_time_ms: int | None = None
    is_slow: bool = False
    raw_links: list[str] = field(default_factory=list)
    raw_images: list[str] = field(default_factory=list)
    base_url: str = ""
    broken_images: list[str] = field(default_factory=list)
    console_errors: list[ConsoleError] = field(default_factory=list)
    screenshot: str | None = None  # path relative to the report folder

    @property
    def load_failed(self) -> bool:
        return any(error.source == "page-load" for error in self.console_errors)

    @property
    def has_browser_problem(self) -> bool:
        return bool(self.broken_images or self.console_errors or self.is_slow)


def wait_for_document_ready(driver: WebDriver, timeout: float) -> None:
    """Explicitly wait until the DOM and all sub-resources have loaded."""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def measure_load_time_ms(driver: WebDriver, timeout: float = 10) -> int | None:
    """Page load time from the Navigation Timing API, in milliseconds.

    readyState flips to "complete" just *before* the load event handlers run,
    so loadEventEnd can still be 0 for a moment. We wait for it to be set.
    """
    try:
        value = WebDriverWait(driver, timeout).until(lambda d: d.execute_script(LOAD_TIME_JS))
    except TimeoutException:
        return None
    return round(value)


def wait_for_images(driver: WebDriver, timeout: float = IMAGE_SETTLE_TIMEOUT) -> bool:
    """Wait until every eager image has finished loading or failing."""
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.execute_script(IMAGES_SETTLED_JS))
        return True
    except TimeoutException:
        log.debug("Some images were still loading after %ss", timeout)
        return False


def find_broken_images(driver: WebDriver) -> list[str]:
    images: list[str] = driver.execute_script(BROKEN_IMAGES_JS)
    return list(dict.fromkeys(images))  # de-duplicate, keep page order


def collect_raw_links(driver: WebDriver) -> dict[str, Any]:
    """Return {"base": ..., "links": [...], "images": [...]} from the live DOM."""
    return driver.execute_script(COLLECT_LINKS_JS)


def clean_console_message(message: str) -> str:
    return QUERY_STRING_RE.sub(r"\1", message).strip()


def read_console_errors(driver: WebDriver) -> list[ConsoleError]:
    """Return SEVERE browser console entries since the last call.

    chromedriver clears its log buffer on every get_log() call, which is what
    lets us attribute entries to the page that was just loaded.
    """
    errors: dict[ConsoleError, None] = {}  # ordered set: the same error once per page
    for entry in driver.get_log("browser"):
        if entry.get("level") == "SEVERE":
            message = clean_console_message(entry.get("message", ""))
            errors[ConsoleError(entry.get("source", "console"), message)] = None
    return list(errors)


def confirm_problems(first: PageReport, second: PageReport) -> PageReport:
    """Keep only the broken images and console errors seen on both loads.

    A real bug reproduces on a second load; a one-off network blip doesn't.
    Timing and links come from the first load, because the second load is
    served partly from the browser cache and would look faster than it is.
    """
    first.broken_images = [img for img in first.broken_images if img in second.broken_images]
    first.console_errors = [err for err in first.console_errors if err in second.console_errors]
    first.screenshot = second.screenshot or first.screenshot  # shows the confirmed state
    return first


def screenshot_name(url: str) -> str:
    """Readable, collision-free file name for a page, e.g. "broken_images-3f2a9c1e.png"."""
    path = urlsplit(url).path.strip("/") or "index"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:60] or "page"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{slug}-{digest}.png"


def inspect_page(
    driver: WebDriver,
    url: str,
    *,
    depth: int,
    slow_page_ms: int,
    page_load_timeout: float,
    screenshot_dir: Path | None = None,
) -> PageReport:
    """Load one page and run every browser-side check on it.

    A screenshot is taken for every page while it is on screen. Whether a page
    has a broken *link* is only known after link validation, so screenshots of
    pages that turn out clean are deleted when the report is written.
    """
    page = PageReport(url=url, depth=depth)

    # Throw away console output from before this navigation (for example a late
    # error from the previous page) so it isn't blamed on this one.
    read_console_errors(driver)

    try:
        driver.get(url)
        wait_for_document_ready(driver, page_load_timeout)
    except TimeoutException:
        # Keep whatever did load and inspect it anyway; a page that never
        # finishes loading is a problem in its own right.
        log.warning("Page load timed out after %ss: %s", page_load_timeout, url)
        driver.execute_script("window.stop();")
        page.console_errors.append(
            ConsoleError("page-load", f"Page did not finish loading within {page_load_timeout:g}s")
        )
    except WebDriverException as exc:
        log.warning("Could not load %s: %s", url, exc.msg)
        page.console_errors.append(ConsoleError("page-load", f"Page failed to load: {exc.msg}"))
        return page

    page.final_url = driver.current_url
    page.http_status = driver.execute_script(HTTP_STATUS_JS)
    page.title = driver.title
    page.load_time_ms = measure_load_time_ms(driver)
    page.is_slow = page.load_time_ms is not None and page.load_time_ms > slow_page_ms

    wait_for_images(driver)
    page.broken_images = find_broken_images(driver)

    raw = collect_raw_links(driver)
    page.base_url = raw.get("base") or page.final_url
    page.raw_links = raw.get("links") or []
    page.raw_images = raw.get("images") or []

    page.console_errors.extend(read_console_errors(driver))

    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        name = screenshot_name(url)
        if driver.save_screenshot(str(screenshot_dir / name)):
            # Stored relative to the report folder so links work on GitHub Pages.
            page.screenshot = f"{screenshot_dir.name}/{name}"

    return page
