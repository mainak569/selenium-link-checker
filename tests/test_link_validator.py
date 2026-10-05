"""link_validator with a mocked requests session: no network needed."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
import requests

from checker import link_validator
from checker.link_validator import LinkResult, check_url, validate_links


def make_response(status: int, url: str, history: list[requests.Response] | None = None,
                  content_type: str = "text/html") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.history = history or []
    response.headers["Content-Type"] = content_type
    response.raw = io.BytesIO(b"")  # lets response.close() work like a real one
    return response


def fake_session(head=None, get=None) -> MagicMock:
    """A session whose head()/get() return or raise what we tell them to.

    Passing a list makes successive calls return successive items (side_effect).
    """
    session = MagicMock(spec=requests.Session)
    session.head.side_effect = head if isinstance(head, list) else [head] * 5
    session.get.side_effect = get if isinstance(get, list) else [get] * 5
    return session


URL = "https://example.com/page"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # Retries pause briefly; tests shouldn't.
    monkeypatch.setattr(link_validator.time, "sleep", lambda _seconds: None)


def test_200_is_ok_and_uses_head_only():
    session = fake_session(head=make_response(200, URL))
    result = check_url(URL, session)
    assert result.outcome == "ok"
    assert result.status == 200
    assert result.method == "HEAD"
    session.get.assert_not_called()


def test_301_then_200_is_a_redirect_warning_with_final_url():
    final = "https://example.com/new-page"
    session = fake_session(head=make_response(200, final, history=[make_response(301, URL)]))
    result = check_url(URL, session)
    assert result.outcome == "redirect"
    assert not result.is_broken
    assert result.redirect_chain == [301]
    assert result.final_url == final


def test_404_is_broken_and_not_retried():
    session = fake_session(head=make_response(404, URL))
    result = check_url(URL, session, retries=1)
    assert result.is_broken
    assert result.describe() == "404"
    assert result.attempts == 1  # a 404 won't fix itself, so no retry


def test_500_is_broken_after_one_retry():
    session = fake_session(head=make_response(500, URL))
    result = check_url(URL, session, retries=1)
    assert result.is_broken
    assert result.status == 500
    assert result.attempts == 2
    assert session.head.call_count == 2


def test_transient_503_that_recovers_on_retry_is_ok():
    session = fake_session(head=[make_response(503, URL), make_response(200, URL)])
    result = check_url(URL, session, retries=1)
    assert result.outcome == "ok"
    assert result.attempts == 2


def test_timeout_is_broken_with_readable_error():
    timeout = requests.exceptions.ReadTimeout("read timed out")
    session = fake_session(head=timeout, get=timeout)
    result = check_url(URL, session, timeout=10, retries=1)
    assert result.is_broken
    assert result.error == "Timeout after 10s"
    assert result.error_kind == "timeout"
    assert result.attempts == 2


def test_connection_error_is_broken():
    error = requests.exceptions.ConnectionError("Name or service not known")
    session = fake_session(head=error, get=error)
    result = check_url(URL, session, retries=0)
    assert result.is_broken
    assert result.error == "Connection error"
    assert result.status is None


def test_ssl_error_is_broken_and_not_retried():
    error = requests.exceptions.SSLError("certificate verify failed")
    session = fake_session(head=error, get=error)
    result = check_url(URL, session, retries=1)
    assert result.error == "SSL error"
    assert result.attempts == 1


def test_head_405_falls_back_to_streaming_get():
    session = fake_session(head=make_response(405, URL), get=make_response(200, URL))
    result = check_url(URL, session)
    assert result.outcome == "ok"
    assert result.method == "GET"
    _args, kwargs = session.get.call_args
    assert kwargs["stream"] is True  # headers only, body not downloaded


@pytest.mark.parametrize("status", [403, 501])
def test_other_head_unfriendly_statuses_fall_back_to_get(status):
    session = fake_session(head=make_response(status, URL), get=make_response(200, URL))
    assert check_url(URL, session).method == "GET"


def test_head_exception_falls_back_to_get():
    session = fake_session(head=requests.exceptions.ConnectionError("reset"), get=make_response(200, URL))
    result = check_url(URL, session)
    assert result.outcome == "ok"
    assert result.method == "GET"


def test_validate_links_checks_unique_urls_in_parallel_and_reuses_known_results():
    statuses = {"https://a.com/ok": 200, "https://a.com/missing": 404, "https://a.com/error": 500}

    def head(url, **_kwargs):
        return make_response(statuses[url], url)

    sessions: list[MagicMock] = []

    def factory():
        session = MagicMock(spec=requests.Session)
        session.head.side_effect = head
        sessions.append(session)
        return session

    known = {"https://a.com/known": LinkResult("https://a.com/known", status=200)}
    urls = [*statuses, "https://a.com/ok", "https://a.com/known"]  # duplicate + known
    results = validate_links(urls, user_agent="test", max_workers=3, retries=0, known=known,
                             session_factory=factory)

    assert set(results) == {*statuses, "https://a.com/known"}
    assert results["https://a.com/ok"].outcome == "ok"
    assert results["https://a.com/missing"].is_broken
    assert results["https://a.com/error"].is_broken
    assert results["https://a.com/known"] is known["https://a.com/known"]
    requested = [call.args[0] for s in sessions for call in s.head.call_args_list]
    assert sorted(requested) == sorted(statuses)  # each URL once, known one never
    assert all(s.close.called for s in sessions)


def test_validate_links_turns_unexpected_exceptions_into_broken_results():
    def factory():
        session = MagicMock(spec=requests.Session)
        session.head.side_effect = UnicodeError("bad label")
        return session

    results = validate_links(["https://bad.example/"], user_agent="test", session_factory=factory)
    assert results["https://bad.example/"].error == "Request failed (UnicodeError)"
