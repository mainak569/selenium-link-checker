"""Build the HTML report, results.json and the GitHub Actions job summary.

build_results() turns the raw crawl and link results into one plain dict.
That dict is written as results.json as-is and is also the only input to the
HTML template, so the two outputs can never disagree.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader

from checker import __version__
from checker.browser_checks import ConsoleError
from checker.config import Config
from checker.crawler import CrawlResult, is_same_domain, normalize_url
from checker.link_validator import LinkResult

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "report.html.j2"
SCREENSHOT_DIR_NAME = "screenshots"

# India Standard Time has no daylight saving, so a fixed offset is exact and
# avoids depending on the OS time zone database.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Categories that make the run FAIL. Redirects and slow pages are warnings.
FAILING_CATEGORIES = ("broken_links", "broken_images", "console_errors")

SUMMARY_LABELS = {
    "pages_crawled": "Pages crawled",
    "links_checked": "Links checked",
    "broken_links": "Broken links",
    "broken_images": "Broken images",
    "redirects": "Redirects",
    "console_errors": "Console errors",
    "slow_pages": "Slow pages",
}


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def build_results(
    config: Config,
    crawl: CrawlResult,
    link_results: dict[str, LinkResult],
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    start = crawl.start_url
    pages_by_url = {page.url: page for page in crawl.pages}
    broken = sorted((r for r in link_results.values() if r.is_broken), key=lambda r: r.url)
    redirected = sorted((r for r in link_results.values() if r.is_redirect), key=lambda r: r.url)

    # A page keeps its screenshot only if something is wrong on it.
    problem_pages = {page.url for page in crawl.pages if page.has_browser_problem}
    for result in broken:
        problem_pages.update(crawl.link_sources.get(result.url, []))

    def screenshot_for(page_url: str) -> str | None:
        page = pages_by_url.get(page_url)
        return page.screenshot if page and page_url in problem_pages else None

    def found_on(url: str) -> list[dict[str, Any]]:
        return [{"page": src, "screenshot": screenshot_for(src)} for src in crawl.link_sources.get(url, [])]

    broken_links = [
        {
            "url": r.url,
            "result": r.describe(),
            "status": r.status,
            "error": r.error,
            "type": "image" if r.url in crawl.image_urls else "link",
            "internal": is_same_domain(r.url, start),
            "redirect_chain": r.redirect_chain,
            "final_url": r.final_url,
            "found_on": found_on(r.url),
        }
        for r in broken
    ]

    broken_images = []
    for page in crawl.pages:
        for image_url in page.broken_images:
            http = link_results.get(normalize_url(image_url) or image_url)
            broken_images.append(
                {
                    "page": page.url,
                    "image_url": image_url,
                    # A 200 here means the file exists but isn't a usable image,
                    # which only a browser check can catch.
                    "http_status": http.describe() if http else "not checked",
                    "screenshot": screenshot_for(page.url),
                }
            )

    redirects = [
        {
            "url": r.url,
            "redirect_chain": r.redirect_chain,
            "final_status": r.status,
            "final_url": r.final_url,
            "found_on": found_on(r.url),
        }
        for r in redirected
    ]

    # The same error often appears on many pages (a broken script in a shared
    # layout, a dead analytics endpoint), so group by message like broken links.
    console_pages: dict[ConsoleError, list[str]] = {}
    for page in crawl.pages:
        for error in page.console_errors:
            console_pages.setdefault(error, []).append(page.url)
    console_errors = [
        {
            "message": error.message,
            "source": error.source,
            "found_on": [{"page": url, "screenshot": screenshot_for(url)} for url in page_urls],
        }
        for error, page_urls in console_pages.items()
    ]

    slow_pages = sorted(
        (
            {"page": page.url, "load_time_ms": page.load_time_ms, "screenshot": screenshot_for(page.url)}
            for page in crawl.pages
            if page.is_slow
        ),
        key=lambda row: row["load_time_ms"] or 0,
        reverse=True,
    )

    pages = [
        {
            "url": page.url,
            "title": page.title,
            "http_status": page.http_status,
            "depth": page.depth,
            "load_time_ms": page.load_time_ms,
            "links": len(page.raw_links),
            "images": len(page.raw_images),
            "broken_images": len(page.broken_images),
            "console_errors": len(page.console_errors),
            "slow": page.is_slow,
            "screenshot": screenshot_for(page.url),
        }
        for page in crawl.pages
    ]

    summary = {
        "pages_crawled": len(crawl.pages),
        "links_checked": len(link_results),
        "broken_links": len(broken_links),
        "broken_images": len(broken_images),
        "redirects": len(redirects),
        "console_errors": len(console_errors),
        "slow_pages": len(slow_pages),
    }
    passed = not any(summary[key] for key in FAILING_CATEGORIES) and bool(crawl.pages)

    started_utc = started_at.astimezone(timezone.utc)
    return {
        "tool": "selenium-link-checker",
        "version": __version__,
        "target": config.start_url,
        "status": "PASS" if passed else "FAIL",
        "started_at": started_utc.isoformat(timespec="seconds"),
        "run_time_utc": started_utc.strftime("%d %b %Y, %H:%M:%S UTC"),
        "run_time_ist": started_utc.astimezone(IST).strftime("%d %b %Y, %I:%M:%S %p IST"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "duration": format_duration((finished_at - started_at).total_seconds()),
        "summary": summary,
        "broken_links": broken_links,
        "broken_images": broken_images,
        "redirects": redirects,
        "console_errors": console_errors,
        "slow_pages": slow_pages,
        "pages": pages,
        "not_crawled": [{"url": url, "reason": reason} for url, reason in crawl.skipped.items()],
        "config": {key: value for key, value in config.to_dict().items() if key != "headed"},
    }


# --------------------------------------------------------------------------- #
# Output files
# --------------------------------------------------------------------------- #


def prepare_output_dir(output_dir: Path) -> Path:
    """Create the report folder and clear screenshots left over from a previous run.

    Only *.png files inside <output>/screenshots are removed, never anything else.
    """
    screenshot_dir = output_dir / SCREENSHOT_DIR_NAME
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    for old in screenshot_dir.glob("*.png"):
        old.unlink()
    return screenshot_dir


def prune_screenshots(results: dict[str, Any], output_dir: Path) -> int:
    """Delete screenshots of pages that turned out to have no problems."""
    keep = {page["screenshot"] for page in results["pages"] if page["screenshot"]}
    removed = 0
    for shot in (output_dir / SCREENSHOT_DIR_NAME).glob("*.png"):
        if f"{SCREENSHOT_DIR_NAME}/{shot.name}" not in keep:
            shot.unlink()
            removed += 1
    return removed


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def verdict_sentences(summary: dict[str, int]) -> tuple[str, str | None]:
    """Plain-English headline for the report, plus an optional line about warnings."""
    if not summary["pages_crawled"]:
        return "The start page could not be loaded, so nothing was crawled.", None
    pages = _plural(summary["pages_crawled"], "page")
    nouns = {"broken_links": "broken link", "broken_images": "broken image", "console_errors": "console error"}
    problems = [_plural(summary[key], nouns[key]) for key in FAILING_CATEGORIES if summary[key]]
    if problems:
        headline = f"Found {_join(problems)} across {pages}."
    else:
        headline = f"No broken links, broken images or console errors across {pages}."
    warnings = [
        _plural(summary[key], noun)
        for key, noun in (("redirects", "redirect"), ("slow_pages", "slow page"))
        if summary[key]
    ]
    return headline, (f"Also worth a look: {_join(warnings)}." if warnings else None)


def make_display_url(target: str) -> Callable[[str], str]:
    """Show same-site URLs as paths ("/hovers"); the site is already named in the header."""
    parts = urlsplit(normalize_url(target) or target)
    origin = f"{parts.scheme}://{parts.netloc}"

    def display_url(url: str) -> str:
        if url == origin or url.startswith(origin + "/"):
            return url[len(origin):] or "/"
        return url

    return display_url


def render_html(results: dict[str, Any]) -> str:
    # autoescape matters: URLs and console messages come from the site under
    # test, and must not be able to inject markup into the report.
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    env.filters["display_url"] = make_display_url(results["target"])
    headline, subline = verdict_sentences(results["summary"])
    return env.get_template(TEMPLATE_NAME).render(
        r=results,
        labels=SUMMARY_LABELS,
        failing=FAILING_CATEGORIES,
        headline=headline,
        subline=subline,
    )


def write_report(results: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    index = output_dir / "index.html"
    index.write_text(render_html(results), encoding="utf-8")
    removed = prune_screenshots(results, output_dir)
    log.info("Report written to %s (%d clean-page screenshots removed)", index, removed)
    return index


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


def _md(text: Any) -> str:
    """Make a value safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def github_summary_markdown(results: dict[str, Any], max_rows: int = 10) -> str:
    icon = "✅" if results["status"] == "PASS" else "❌"
    lines = [
        f"## {icon} Link check: {results['status']}",
        "",
        f"**Target:** {results['target']}  ",
        f"**Run:** {results['run_time_utc']} ({results['run_time_ist']}) · **Duration:** {results['duration']}",
        "",
        "| Check | Count |",
        "| --- | ---: |",
    ]
    for key, label in SUMMARY_LABELS.items():
        lines.append(f"| {label} | {results['summary'][key]} |")

    if results["broken_links"]:
        lines += ["", "### Broken links", "", "| URL | Result | Found on |", "| --- | --- | --- |"]
        for row in results["broken_links"][:max_rows]:
            pages = ", ".join(src["page"] for src in row["found_on"][:3]) or "—"
            if len(row["found_on"]) > 3:
                pages += f" (+{len(row['found_on']) - 3} more)"
            lines.append(f"| {_md(row['url'])} | {_md(row['result'])} | {_md(pages)} |")
        if len(results["broken_links"]) > max_rows:
            lines.append(f"\n_…and {len(results['broken_links']) - max_rows} more in the full report._")

    if results["console_errors"]:
        lines += ["", "### Console errors", "", "| Message | Pages |", "| --- | ---: |"]
        for row in results["console_errors"][:max_rows]:
            lines.append(f"| {_md(row['message'][:200])} | {len(row['found_on'])} |")

    if results["broken_images"]:
        lines += ["", "### Broken images", "", "| Page | Image | HTTP |", "| --- | --- | --- |"]
        for row in results["broken_images"][:max_rows]:
            lines.append(f"| {_md(row['page'])} | {_md(row['image_url'])} | {_md(row['http_status'])} |")

    return "\n".join(lines) + "\n"


def write_github_summary(results: dict[str, Any]) -> bool:
    """Append a Markdown summary to the Actions run page when running in CI."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(github_summary_markdown(results))
    return True


def text_summary(results: dict[str, Any], report_path: Path) -> str:
    width = max(len(label) for label in SUMMARY_LABELS.values())
    lines = [
        "",
        "=" * 52,
        f" Link check {results['status']}  ·  {results['target']}",
        f" {results['run_time_utc']}  ·  took {results['duration']}",
        "-" * 52,
    ]
    for key, label in SUMMARY_LABELS.items():
        flag = "  <-- fails the run" if key in FAILING_CATEGORIES and results["summary"][key] else ""
        lines.append(f" {label:<{width}}  {results['summary'][key]:>5}{flag}")
    lines += ["-" * 52, f" Report: {report_path}", "=" * 52]
    return "\n".join(lines)
