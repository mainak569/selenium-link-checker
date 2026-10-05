"""URL normalisation and filtering used by the crawler."""

from __future__ import annotations

import pytest

from checker.crawler import compile_patterns, filter_urls, is_same_domain, normalize_url

BASE = "https://example.com/docs/page.html"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("other.html", "https://example.com/docs/other.html"),  # relative to the page
        ("../img/logo.png", "https://example.com/img/logo.png"),  # parent directory
        ("/about", "https://example.com/about"),  # root-relative
        ("//cdn.example.net/app.js", "https://cdn.example.net/app.js"),  # protocol-relative
        ("?page=2", "https://example.com/docs/page.html?page=2"),  # query kept
        ("#section", "https://example.com/docs/page.html"),  # fragment only = same page
        ("/faq#shipping", "https://example.com/faq"),  # fragment stripped
        ("  /padded  ", "https://example.com/padded"),  # whitespace from HTML attributes
    ],
)
def test_relative_links_and_fragments(raw, expected):
    assert normalize_url(raw, BASE) == expected


def test_scheme_and_host_are_lowercased_and_default_port_dropped():
    assert normalize_url("HTTPS://Example.COM:443/Path") == "https://example.com/Path"  # path case kept
    assert normalize_url("http://example.com:80") == "http://example.com/"
    assert normalize_url("http://example.com:8080/x") == "http://example.com:8080/x"


def test_credentials_are_dropped():
    assert normalize_url("https://user:secret@example.com/x") == "https://example.com/x"


@pytest.mark.parametrize(
    "raw",
    [
        "mailto:hello@example.com",
        "tel:+911234567890",
        "javascript:void(0)",
        "JavaScript:alert(1)",
        "data:image/png;base64,iVBORw0KGgo=",
        "ftp://files.example.com/a.zip",
        "",
        "http://example.com:notaport/",
    ],
)
def test_non_web_links_are_skipped(raw):
    assert normalize_url(raw, BASE) is None


def test_filter_urls_removes_duplicates_and_keeps_order():
    raw = ["/b", "/a", "/a#top", "https://example.com/a", "mailto:x@example.com", "/b/"]
    assert filter_urls(raw, BASE) == [
        "https://example.com/b",
        "https://example.com/a",
        "https://example.com/b/",  # trailing slash can be a different page, so it's kept
    ]


def test_ignore_patterns_drop_matching_urls():
    patterns = compile_patterns([r"/logout", r"^https://ads\."])
    raw = ["/logout?next=/", "/products", "https://ads.example.net/banner.png"]
    assert filter_urls(raw, BASE, patterns) == ["https://example.com/products"]


@pytest.mark.parametrize(
    ("url", "same"),
    [
        ("https://example.com/x", True),
        ("http://example.com/x", True),  # scheme doesn't matter
        ("https://www.example.com/x", True),  # www. is the same site
        ("https://EXAMPLE.com/x", True),
        ("https://blog.example.com/x", False),  # other subdomains are external
        ("https://example.org/x", False),
        ("https://notexample.com/x", False),
    ],
)
def test_same_domain_vs_external(url, same):
    assert is_same_domain(url, "https://example.com/") is same
