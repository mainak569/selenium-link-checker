"""Create the Chrome WebDriver used for crawling."""

from __future__ import annotations

import logging
import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webdriver import WebDriver

log = logging.getLogger(__name__)

WINDOW_SIZE = (1366, 900)


def create_driver(
    *,
    headless: bool = True,
    user_agent: str | None = None,
    page_load_timeout: float = 60,
    window_size: tuple[int, int] = WINDOW_SIZE,
) -> WebDriver:
    """Start Chrome with console logging enabled.

    Selenium Manager (built into Selenium 4.6+) finds the installed Chrome and
    downloads a matching chromedriver automatically, so there is nothing to
    install by hand, locally or in CI.
    """
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    width, height = window_size
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument("--hide-scrollbars")  # cleaner screenshots
    options.add_argument("--disable-dev-shm-usage")  # /dev/shm is tiny in containers and some CI runners
    if os.environ.get("CI"):
        # GitHub's Ubuntu runners restrict the user namespaces Chrome's sandbox
        # needs. The runner is a throwaway VM, so turning the sandbox off there is
        # an acceptable trade-off. Locally the sandbox stays on.
        options.add_argument("--no-sandbox")
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    # Ask chromedriver to keep the browser console so we can read it with
    # driver.get_log("browser"). We keep every level and filter to SEVERE later.
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    # "normal" = driver.get() returns once the load event has fired.
    options.page_load_strategy = "normal"

    log.info("Starting Chrome (%s)", "headless" if headless else "headed")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(page_load_timeout)
    log.info("Chrome %s ready", driver.capabilities.get("browserVersion", "?"))
    return driver
