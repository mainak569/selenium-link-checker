"""Crawl a site in Chrome, check every link and image, and write an HTML report.

Examples:
    python main.py
    python main.py --url https://example.com --max-pages 10 --max-depth 1
    python main.py --headed --max-pages 3          # watch the browser work
    python main.py --fail-on-broken                # exit 1 if anything is broken

Exit codes: 0 = done (or problems found but fail_on_broken is off),
            1 = problems found and fail_on_broken is on,
            2 = the checker itself couldn't run (bad config, site unreachable, no Chrome).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from selenium.common.exceptions import WebDriverException

from checker.browser import create_driver
from checker.config import Config, load_config
from checker.crawler import Crawler
from checker.link_validator import validate_links
from checker.report import build_results, prepare_output_dir, text_summary, write_github_summary, write_report

log = logging.getLogger("checker")

EXIT_OK, EXIT_PROBLEMS, EXIT_ERROR = 0, 1, 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Selenium-based broken link and image checker.",
        epilog="Options you pass here override the values in the config file.",
    )
    parser.add_argument("--url", dest="start_url", help="page to start crawling from")
    parser.add_argument("--max-pages", type=int, help="maximum number of pages to load in the browser")
    parser.add_argument("--max-depth", type=int, help="maximum link depth from the start page (start = 0)")
    parser.add_argument("--config", default="config.yaml", help="YAML config file (default: %(default)s)")
    parser.add_argument("--output", dest="output_dir", help="report folder (default: report)")
    parser.add_argument("--headed", action="store_true", help="show the browser window (local debugging)")
    parser.add_argument(
        "--fail-on-broken",
        action=argparse.BooleanOptionalAction,
        default=None,  # None = "not given", so the config file decides
        help="exit with code 1 when problems are found",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party libraries are chatty at DEBUG; keep the output about our checks.
    for noisy in ("urllib3", "selenium", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run(config: Config) -> int:
    output_dir = Path(config.output_dir)
    screenshot_dir = prepare_output_dir(output_dir)
    started = datetime.now(timezone.utc)
    log.info(
        "Checking %s (max %d pages, depth %d, %ss delay)",
        config.start_url,
        config.max_pages,
        config.max_depth,
        config.delay_seconds,
    )

    driver = None
    try:
        driver = create_driver(
            headless=not config.headed,
            user_agent=config.user_agent,
            page_load_timeout=config.page_load_timeout_seconds,
        )
        crawl = Crawler(driver, config, screenshot_dir=screenshot_dir).crawl()
    except WebDriverException as exc:
        log.error("Browser error: %s", exc.msg or exc)
        return EXIT_ERROR
    finally:
        # Always release Chrome, even after an error or Ctrl+C, so no orphaned
        # browser processes are left behind.
        if driver is not None:
            driver.quit()

    # Pages checked during the crawl are passed as `known` so they aren't requested twice.
    link_results = validate_links(
        [*crawl.page_checks, *crawl.link_sources],
        user_agent=config.user_agent,
        max_workers=config.max_workers,
        timeout=config.timeout_seconds,
        retries=config.retries,
        known=crawl.page_checks,
    )

    results = build_results(config, crawl, link_results, started, datetime.now(timezone.utc))
    index = write_report(results, output_dir)
    if write_github_summary(results):
        log.info("Added summary to the GitHub Actions job page")
    print(text_summary(results, index))

    if not crawl.pages:
        log.error("Could not load %s, so nothing was crawled", config.start_url)
        return EXIT_ERROR
    if results["status"] == "FAIL":
        if config.fail_on_broken:
            log.error("Problems found and fail_on_broken is on: exiting with code 1")
            return EXIT_PROBLEMS
        log.warning("Problems found, but fail_on_broken is off: exiting with code 0")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    overrides = {
        "start_url": args.start_url,
        "max_pages": args.max_pages,
        "max_depth": args.max_depth,
        "output_dir": args.output_dir,
        "fail_on_broken": args.fail_on_broken,
        "headed": True if args.headed else None,
    }
    try:
        config = load_config(args.config, overrides)
    except (OSError, ValueError) as exc:
        log.error("Config error: %s", exc)
        return EXIT_ERROR
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
