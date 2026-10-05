"""Crawler BFS logic against a fake site: no browser, no network."""

from __future__ import annotations

import pytest
from selenium.common.exceptions import WebDriverException

from checker import crawler as crawler_module
from checker.browser_checks import ConsoleError, PageReport
from checker.config import Config
from checker.crawler import Crawler
from checker.link_validator import LinkResult

ROOT = "https://site.test/"

# page path -> links on that page
SITE = {
    "/": ["/a", "/b", "/file.pdf", "/missing", "https://external.test/", "mailto:me@site.test"],
    "/a": ["/a1", "/", "/b"],
    "/b": ["/b1"],
    "/a1": ["/deep"],
    "/b1": [],
}


class FakeDriver:
    """Stands in for a WebDriver; inspect_page is replaced in these tests."""

    def __init__(self) -> None:
        self.quit_called = False

    def quit(self) -> None:
        self.quit_called = True


def fake_page_checker(url: str) -> LinkResult:
    path = url.removeprefix("https://site.test")
    if path == "/missing":
        return LinkResult(url, status=404)
    if path.endswith(".pdf"):
        return LinkResult(url, status=200, content_type="application/pdf")
    return LinkResult(url, status=200, final_url=url, content_type="text/html; charset=utf-8")


@pytest.fixture
def loaded(monkeypatch) -> list[str]:
    """Replace the real browser inspection with a lookup in SITE."""
    order: list[str] = []

    def fake_inspect(driver, url, *, depth, **_kwargs):
        order.append(url)
        path = url.removeprefix("https://site.test")
        return PageReport(url=url, depth=depth, final_url=url, http_status=200,
                          raw_links=SITE.get(path, []), base_url=url)

    monkeypatch.setattr(crawler_module, "inspect_page", fake_inspect)
    return order


def run(**overrides) -> crawler_module.CrawlResult:
    config = Config(start_url=ROOT, delay_seconds=0, **overrides)
    return Crawler(config, driver_factory=FakeDriver, page_checker=fake_page_checker).crawl()


def test_crawls_breadth_first_within_depth(loaded):
    result = run(max_depth=1, max_pages=10)
    # depth 0, then depth-1 pages in link order; depth-2 pages are not opened
    assert loaded == [ROOT, "https://site.test/a", "https://site.test/b"]
    # ...but links found on depth-1 pages are still collected for checking
    assert "https://site.test/a1" in result.link_sources
    assert "https://site.test/b1" in result.link_sources


def test_max_pages_limits_browser_loads(loaded):
    run(max_depth=5, max_pages=2)
    assert loaded == [ROOT, "https://site.test/a"]


def test_deeper_crawl_visits_each_page_once(loaded):
    run(max_depth=3, max_pages=50)
    assert loaded == [ROOT, "https://site.test/a", "https://site.test/b",
                      "https://site.test/a1", "https://site.test/b1", "https://site.test/deep"]


def test_external_broken_and_non_html_urls_are_not_opened(loaded):
    result = run(max_depth=1, max_pages=10)
    assert "https://external.test/" not in loaded  # external: checked, never crawled
    assert "https://external.test/" in result.link_sources
    assert result.skipped["https://site.test/missing"] == "HTTP 404"
    assert result.skipped["https://site.test/file.pdf"].startswith("not an HTML page")
    assert not any(url.startswith("mailto:") for url in result.link_sources)


def test_link_sources_record_every_page_a_link_appears_on(loaded):
    result = run(max_depth=1, max_pages=10)
    assert result.link_sources["https://site.test/b"] == [ROOT, "https://site.test/a"]


def test_ignore_patterns_skip_urls(loaded):
    result = run(max_depth=1, max_pages=10, ignore_patterns=[r"/b$"])
    assert "https://site.test/b" not in loaded
    assert "https://site.test/b" not in result.link_sources


def test_problems_must_reproduce_on_reload(monkeypatch):
    """A console error seen only on the first load is dropped; one seen twice is kept."""
    loads = iter([
        [ConsoleError("network", "blip"), ConsoleError("javascript", "real bug")],
        [ConsoleError("javascript", "real bug")],
    ])

    def fake_inspect(driver, url, *, depth, **_kwargs):
        return PageReport(url=url, depth=depth, http_status=200, console_errors=next(loads))

    monkeypatch.setattr(crawler_module, "inspect_page", fake_inspect)
    monkeypatch.setattr(crawler_module.time, "sleep", lambda _s: None)
    result = run(max_depth=0, max_pages=1, retries=1)
    assert result.pages[0].console_errors == [ConsoleError("javascript", "real bug")]


def test_problem_pages_are_rechecked_after_the_crawl_not_straight_away(monkeypatch):
    order: list[str] = []

    def fake_inspect(driver, url, *, depth, **_kwargs):
        order.append(url)
        path = url.removeprefix("https://site.test")
        errors = [ConsoleError("javascript", "boom")] if path == "/" else []
        return PageReport(url=url, depth=depth, http_status=200, raw_links=SITE.get(path, []),
                          base_url=url, console_errors=errors)

    monkeypatch.setattr(crawler_module, "inspect_page", fake_inspect)
    monkeypatch.setattr(crawler_module.time, "sleep", lambda _s: None)
    result = run(max_depth=1, max_pages=10, retries=1)
    # The home page is loaded again only after every other page, and only it
    # (the clean pages aren't reloaded).
    assert order == [ROOT, "https://site.test/a", "https://site.test/b", ROOT]
    assert result.pages[0].console_errors == [ConsoleError("javascript", "boom")]


def test_server_error_page_is_loaded_again(monkeypatch):
    statuses = iter([503, 200])

    def fake_inspect(driver, url, *, depth, **_kwargs):
        return PageReport(url=url, depth=depth, http_status=next(statuses))

    monkeypatch.setattr(crawler_module, "inspect_page", fake_inspect)
    monkeypatch.setattr(crawler_module.time, "sleep", lambda _s: None)
    result = run(max_depth=0, max_pages=1, retries=1)
    assert result.pages[0].http_status == 200


def test_stuck_browser_is_replaced_and_the_page_retried(monkeypatch):
    drivers: list[FakeDriver] = []

    def factory() -> FakeDriver:
        drivers.append(FakeDriver())
        return drivers[-1]

    calls = iter([WebDriverException("timeout: Timed out receiving message from renderer: 10.000"), None])

    def fake_inspect(driver, url, *, depth, **_kwargs):
        error = next(calls)
        if error:
            raise error
        return PageReport(url=url, depth=depth, http_status=200)

    monkeypatch.setattr(crawler_module, "inspect_page", fake_inspect)
    monkeypatch.setattr(crawler_module.time, "sleep", lambda _s: None)
    crawler = Crawler(Config(start_url=ROOT, delay_seconds=0, max_depth=0, max_pages=1),
                      driver_factory=factory, page_checker=fake_page_checker)
    result = crawler.crawl()

    assert len(drivers) == 2 and drivers[0].quit_called  # old browser quit, new one started
    assert result.pages[0].http_status == 200  # the retry on the new browser worked
    assert result.pages[0].console_errors == []
    crawler.close()
    assert drivers[1].quit_called
