"""``start_page.py``: which task start pages the browser can open (§4.10 "起始页").

Setup opens a task's start page before the agent's first observation, and only
one complete http(s) URL can be opened. Every LexBench start page was written
without a scheme and seven L1 ones as text or several sites; handed over as
they were, each task failed at setup after its trace was opened. The rule here
is what ``BrowserCapability.check_setup`` applies at ``prepare()``
(``test_browser_setup_check.py`` drives it through the runtime).
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.start_page import start_page_problem


@pytest.mark.parametrize(
    "start_url",
    [
        None,
        "",
        "https://www.bilibili.com",
        "https://s.weibo.com/top/summary",
        "http://127.0.0.1:8080/page.html?q=1#top",
        "HTTPS://WWW.EXAMPLE.COM/",
        "https://例子.中国/",
    ],
)
def test_an_openable_start_page_passes(start_url: object) -> None:
    assert start_page_problem(start_url) is None


@pytest.mark.parametrize(
    ("start_url", "reason"),
    [
        ("www.bilibili.com", "it has no scheme"),
        ("www.xiachufang.com或其他美食网站", "it has no scheme"),
        ("jd.com + tmall.com + suning.com", "it contains whitespace, so it is not one URL"),
        (
            "https://www.ctrip.com + https://www.qunar.com",
            "it contains whitespace, so it is not one URL",
        ),
        ("ftp://files.example.com/x", "its scheme is 'ftp'"),
        ("about:blank", "its scheme is 'about'"),
        ("javascript:alert(1)", "its scheme is 'javascript'"),
        ("https:/a.example", "it names no host"),
        ("https://[::1", "it is not a well-formed URL: Invalid IPv6 URL"),
        (
            "https://a.example:port/",
            "it is not a well-formed URL: Port could not be cast to integer value as 'port'",
        ),
        (42, "it is not text: int"),
    ],
)
def test_anything_else_is_named_with_its_reason(start_url: object, reason: str) -> None:
    assert start_page_problem(start_url) == (
        f"start_url {start_url!r} ↔ browser setup: the task's start page is not an http(s) "
        f"URL ({reason}); setup opens the start page before the agent's first observation "
        f"and opens only one complete http:// or https:// URL, so correct the task's start page"
    )
