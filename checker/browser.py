"""Create the Chrome WebDriver used for crawling."""

from __future__ import annotations

import logging
import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webdriver import WebDriver

log = logging.getLogger(__name__)

WINDOW_SIZE = (1366, 900)

# alert()/confirm()/prompt() pop-ups block the page and every WebDriver command
# until someone clicks them. This runs before any page script and replaces them
# with versions that log the message as a console error (so it ends up in the
# report) and answer like the user pressed "Cancel", the least risky answer
# (an "OK" to "Delete this?" could change something).
DIALOG_GUARD_JS = """
(() => {
  for (const name of ['alert', 'confirm', 'prompt']) {
    window[name] = function (message) {
      console.error(`JavaScript ${name}() opened: ${message}`);
      return name === 'confirm' ? false : null;
    };
  }
})();
"""


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
    # Chrome DevTools: run DIALOG_GUARD_JS in every page and frame before its own scripts.
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": DIALOG_GUARD_JS})
    log.info("Chrome %s ready", driver.capabilities.get("browserVersion", "?"))
    return driver
