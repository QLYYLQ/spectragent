"""Whether a task's start page is one the browser can open (``docs/architecture_design.md`` §4.10).

Setup opens the task's start page with ``browser.navigate`` before the agent's
first observation, and only one complete ``http://`` or ``https://`` URL can be
opened that way. A zero-token screening of LexBench (2026-10-14) found every
start page of that dataset written without a scheme (``www.bilibili.com``)
and seven written as text or as several sites
(``www.xiachufang.com或其他美食网站``). Handed over as they were, every such
task failed at setup — after its trace was opened, without a model call — and
the sweep's pre-flight never saw it.

:func:`start_page_problem` is the rule ``BrowserCapability.check_setup``
applies, so ``prepare()`` refuses such a task before anything is opened, and
setup applies it again before it launches anything. It never completes a start
page: a dataset that writes bare hosts is completed by its own loader, before
the trace records the start page (``lexbench_browser``), because the flywheel
report and the export recognise a run's site by the recorded URL's host.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The schemes a start page may have.
START_PAGE_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def start_page_problem(start_url: object) -> str | None:
    """Why *start_url* cannot be opened as a task's start page; ``None`` when it can.

    ``None`` and the empty string are no start page at all (setup opens
    nothing), so they pass. Anything else must be text without whitespace that
    parses as one URL with an ``http`` or ``https`` scheme and a host. The
    returned line names the start page and the reason, in the ``party ↔
    party: reason`` form of every other line of the setup check.
    """
    if start_url is None or start_url == "":
        return None
    if not isinstance(start_url, str):
        return _problem(start_url, f"it is not text: {type(start_url).__name__}")
    if any(character.isspace() for character in start_url):
        return _problem(start_url, "it contains whitespace, so it is not one URL")
    try:
        parts = urlsplit(start_url)
        _ = parts.port  # a port that is not a number raises here
    except ValueError as exc:
        return _problem(start_url, f"it is not a well-formed URL: {exc}")
    if not parts.scheme:
        return _problem(start_url, "it has no scheme")
    if parts.scheme.lower() not in START_PAGE_SCHEMES:
        return _problem(start_url, f"its scheme is {parts.scheme!r}")
    if not parts.hostname:
        return _problem(start_url, "it names no host")
    return None


def _problem(start_url: object, reason: str) -> str:
    return (
        f"start_url {start_url!r} ↔ browser setup: the task's start page is not an "
        f"http(s) URL ({reason}); setup opens the start page before the agent's first "
        f"observation and opens only one complete http:// or https:// URL, so correct "
        f"the task's start page"
    )


__all__ = ["START_PAGE_SCHEMES", "start_page_problem"]
