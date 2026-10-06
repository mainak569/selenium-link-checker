"""Report generation from fake results: files, counts, escaping, screenshots, job summary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from checker.browser_checks import ConsoleError, PageReport
from checker.config import Config
from checker.crawler import CrawlResult
from checker.link_validator import LinkResult
from checker.report import build_results, prepare_output_dir, verdict_sentences, write_github_summary, write_report

SITE = "https://shop.test/"
STARTED = datetime(2026, 10, 5, 3, 30, tzinfo=timezone.utc)
FINISHED = STARTED + timedelta(seconds=83)


def make_crawl(output: Path, *, with_problems: bool = True) -> tuple[CrawlResult, dict[str, LinkResult]]:
    screenshots = prepare_output_dir(output)
    home = PageReport(url=SITE, depth=0, http_status=200, load_time_ms=850, screenshot="screenshots/home.png")
    gallery = PageReport(url=f"{SITE}gallery", depth=1, http_status=200, load_time_ms=4200, is_slow=with_problems,
                         screenshot="screenshots/gallery.png")
    about = PageReport(url=f"{SITE}about", depth=1, http_status=200, load_time_ms=300,
                       screenshot="screenshots/about.png")
    if with_problems:
        gallery.broken_images = [f"{SITE}img/missing.png"]
        gallery.console_errors = [ConsoleError("javascript", "Uncaught TypeError: <script>alert(1)</script>")]
        about.console_errors = [ConsoleError("javascript", "Uncaught TypeError: <script>alert(1)</script>")]
    for page in (home, gallery, about):
        (screenshots / Path(page.screenshot).name).write_bytes(b"png")

    crawl = CrawlResult(start_url=SITE, pages=[home, gallery, about])
    links = {
        SITE: LinkResult(SITE, status=200),
        f"{SITE}gallery": LinkResult(f"{SITE}gallery", status=200),
        f"{SITE}about": LinkResult(f"{SITE}about", status=200),
        "https://partner.test/": LinkResult("https://partner.test/", status=200, final_url="https://www.partner.test/",
                                            redirect_chain=[301]),
    }
    if with_problems:
        links[f"{SITE}img/missing.png"] = LinkResult(f"{SITE}img/missing.png", status=404)
        links["https://gone.test/"] = LinkResult("https://gone.test/", error="Timeout after 10s", error_kind="timeout")
        crawl.image_urls.add(f"{SITE}img/missing.png")
    for url in links:
        crawl.add_source(url, SITE if url != f"{SITE}img/missing.png" else f"{SITE}gallery")
    return crawl, links


@pytest.fixture
def report_dir(tmp_path) -> Path:
    return tmp_path / "report"


def test_report_files_are_created_with_the_right_counts(report_dir):
    crawl, links = make_crawl(report_dir)
    results = build_results(Config(start_url=SITE), crawl, links, STARTED, FINISHED)
    index = write_report(results, report_dir)

    assert index == report_dir / "index.html"
    assert index.exists()
    data = json.loads((report_dir / "results.json").read_text())
    assert data["summary"] == {
        "pages_crawled": 3,
        "links_checked": 6,
        "broken_links": 2,
        "broken_images": 1,
        "redirects": 1,
        "console_errors": 1,  # same message on two pages counts once...
        "slow_pages": 1,
    }
    assert len(data["console_errors"][0]["found_on"]) == 2  # ...listed with both pages
    assert data["status"] == "FAIL"
    assert data["duration"] == "1m 23s"
    assert data["run_time_ist"] == "05 Oct 2026, 09:00:00 AM IST"

    broken = {row["url"]: row for row in data["broken_links"]}
    assert broken["https://gone.test/"]["result"] == "Timeout after 10s"
    assert broken["https://gone.test/"]["internal"] is False
    assert broken[f"{SITE}img/missing.png"]["type"] == "image"
    assert broken[f"{SITE}img/missing.png"]["found_on"][0]["page"] == f"{SITE}gallery"

    html = index.read_text()
    assert "Found 2 broken links, 1 broken image and 1 console error across 3 pages." in html
    assert ">FAIL<" in html


def test_html_escapes_content_from_the_site(report_dir):
    crawl, links = make_crawl(report_dir)
    write_report(build_results(Config(start_url=SITE), crawl, links, STARTED, FINISHED), report_dir)
    html = (report_dir / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_report_is_self_contained_with_relative_links(report_dir):
    crawl, links = make_crawl(report_dir)
    write_report(build_results(Config(start_url=SITE), crawl, links, STARTED, FINISHED), report_dir)
    html = (report_dir / "index.html").read_text()
    # The "Pages crawled" tile links to #pages, so that must be an open section, not a collapsed <details>.
    assert '<a href="#pages">' in html and '<section id="pages"' in html
    assert 'href="screenshots/gallery.png"' in html
    assert 'href="results.json"' in html
    assert "<link " not in html and "<script" not in html  # no external CSS or JS
    assert 'href="/' not in html and 'src="/' not in html  # nothing root-relative (breaks under /repo/ on Pages)


def test_only_screenshots_of_pages_with_problems_are_kept(report_dir):
    crawl, links = make_crawl(report_dir)
    write_report(build_results(Config(start_url=SITE), crawl, links, STARTED, FINISHED), report_dir)
    kept = sorted(p.name for p in (report_dir / "screenshots").glob("*.png"))
    # home: links to the broken gone.test; gallery: broken image + slow; about: console error
    assert kept == ["about.png", "gallery.png", "home.png"]


def test_clean_run_passes_and_shows_empty_sections(report_dir):
    crawl, links = make_crawl(report_dir, with_problems=False)
    results = build_results(Config(start_url=SITE), crawl, links, STARTED, FINISHED)
    write_report(results, report_dir)

    assert results["status"] == "PASS"  # a redirect is only a warning
    html = (report_dir / "index.html").read_text()
    assert html.count("No issues found.") == 4  # broken links, images, console errors, slow pages
    assert not list((report_dir / "screenshots").glob("*.png"))  # clean pages keep no screenshots


def test_github_summary_is_appended_only_in_actions(report_dir, tmp_path, monkeypatch):
    crawl, links = make_crawl(report_dir)
    results = build_results(Config(start_url=SITE), crawl, links, STARTED, FINISHED)

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert write_github_summary(results) is False

    summary = tmp_path / "summary.md"
    summary.write_text("existing\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert write_github_summary(results) is True
    text = summary.read_text()
    assert text.startswith("existing\n")  # appended, not overwritten
    assert "| Broken links | 2 |" in text
    assert "Timeout after 10s" in text


def test_verdict_sentences():
    summary = dict.fromkeys(
        ("pages_crawled", "links_checked", "broken_links", "broken_images", "redirects", "console_errors",
         "slow_pages"), 0)
    assert verdict_sentences({**summary, "pages_crawled": 1}) == (
        "No broken links, broken images or console errors across 1 page.", None)
    assert verdict_sentences({**summary, "pages_crawled": 4, "broken_links": 1, "redirects": 2}) == (
        "Found 1 broken link across 4 pages.", "Also worth a look: 2 redirects.")
    assert verdict_sentences(summary)[0].startswith("The start page could not be loaded")
