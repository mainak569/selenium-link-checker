"""Real browser against the real demo site. Run with: pytest -m integration"""

from __future__ import annotations

import pytest

from checker.browser import create_driver
from checker.browser_checks import inspect_page
from checker.link_validator import RETRY_STATUSES

BROKEN_IMAGES_PAGE = "https://the-internet.herokuapp.com/broken_images"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def driver():
    driver = create_driver(headless=True, page_load_timeout=60)
    yield driver
    driver.quit()


def load(driver, url: str):
    # The free-tier demo host sometimes answers 503 while busy; that isn't what
    # this test is about, so give it a couple of tries.
    for _ in range(3):
        page = inspect_page(driver, url, depth=0, slow_page_ms=3000, page_load_timeout=60)
        if page.http_status not in RETRY_STATUSES:
            return page
    pytest.fail(f"{url} kept answering HTTP {page.http_status}")


def test_broken_images_are_detected_in_a_real_browser(driver):
    page = load(driver, BROKEN_IMAGES_PAGE)

    assert len(page.broken_images) >= 2
    assert "https://the-internet.herokuapp.com/asdf.jpg" in page.broken_images
    assert "https://the-internet.herokuapp.com/hjkl.jpg" in page.broken_images
    # The working image on the same page must not be flagged (no false positive).
    assert not any("avatar-blank.jpg" in src for src in page.broken_images)
    assert page.load_time_ms is not None and page.load_time_ms > 0
