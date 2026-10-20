"""JSONP callback names at replay, in a real Chrome (§4.14 "JSONP 回调名"; fixture only, no LLM).

The fixture's ``jsonp`` page names its two callbacks after
``performance.timeOrigin``, the real clock pinned leaves alone, so its replay
never asks for the recording's names — the way a real page's scripts draw
their random numbers or count their requests in another order:

- ``hot.js?callback=jQuery<µs>_1``, a name its explicit endpoint rule drops
  from the match key: the replay's request is a hit, and the recorded answer
  calls the recording's name;
- ``ad.js?cb=ad_<hex>``, a name with a random part: the replay's request
  matches only under a site rule ignoring ``cb``, and the recorded answer (in
  Express's ``typeof … && …(`` wrapper) calls the recording's name too.

Each callback adds buttons to the page. The flow is recorded once with the
real runtime and a scripted model (one step, then done), then replayed with
the recording's own completions under that site rule: once as shipped, where
the replay server writes this request's name into both answers and the page
shows every button the recording showed, and once with the rewrite switched
off — the negative control: both requests are still hits, the answers call
names this page never defined, and no button appears.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.replay import responder
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayLogEntry,
    iter_replay_log,
)
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import JSONP_ITEMS, FixtureSite
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180
TASK = "Read the hot searches."
#: The endpoint of each callback, and the query parameter that names it.
ENDPOINTS = {"hot": ("/jsonp/hot.js", "callback"), "ad": ("/jsonp/ad.js", "cb")}
#: The site rule the replays run with: the random-part name may differ.
SITE_RULE = """rules:
  - id: fixture-default
    scope: {}
    exhaustion: repeat_last
    unavailable: serve_identical
    priority: -100
  - id: callback-endpoints
    scope: {path: '/jsonp/*'}
    exhaustion: repeat_last
    unavailable: serve_identical
    priority: 10
    ignore_query: [cb, callback, _]
"""


@dataclass
class Flow:
    source: driver.Outcome
    replay: driver.Outcome
    unrewritten: driver.Outcome
    #: The names each JSONP request of the recording asked for, by box.
    recorded_names: dict[str, str]


def _name(url: str, box: str) -> str:
    path, parameter = ENDPOINTS[box]
    parts = urlsplit(url)
    assert parts.path == path, url
    (value,) = [v for k, v in parse_qsl(parts.query) if k == parameter]
    return value


def _jsonp_entries(run_dir: Path) -> dict[str, ReplayLogEntry]:
    """The replay log's decision on each JSONP request, by box."""
    found: dict[str, ReplayLogEntry] = {}
    for _step, entry in iter_replay_log(open_archive(run_dir)):
        for box, (path, _parameter) in ENDPOINTS.items():
            if urlsplit(entry.url).path == path:
                assert box not in found, f"{path} was asked twice"
                found[box] = entry
    assert set(found) == set(ENDPOINTS), (
        f"the replay log has no entry for {set(ENDPOINTS) - set(found)}"
    )
    return found


def _buttons(outcome: driver.Outcome) -> set[str]:
    """The ids of the JSONP buttons the run's only step observed."""
    dom = outcome.trace.observation_fragment(0, "browser.dom")
    selector_map = getattr(dom, "selector_map", None) or {}
    ids = {
        str((getattr(node, "attributes", None) or {}).get("id", ""))
        for node in selector_map.values()
    }
    return {item for item in ids if item.split("-", 1)[0] in JSONP_ITEMS}


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted model is on loopback; the machine's proxy must not see it.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


@pytest.fixture(scope="module")
def flow(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Flow:
    base = tmp_path_factory.mktemp("jsonp")
    rules = base / "rules"
    rules.mkdir()
    (rules / "example.test.yaml").write_text(SITE_RULE)
    mark = len(site.requests)
    with ScriptedLLMServer([driver.done("read")]) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "record")
        source = asyncio.run(
            driver.run_task(raw, task=TASK, start_url=site.url("jsonp"), timeout=_TIMEOUT_S)
        )
        assert llm.errors == []
    source.require_finished(1, "read")
    recorded_names = {
        box: _name(row.path, box)
        for box, (path, _parameter) in ENDPOINTS.items()
        for row in site.requests[mark:]
        if row.bare_path == path
    }
    assert set(recorded_names) == set(ENDPOINTS), "the recording did not ask both endpoints"

    def replay(trace_dir: Path) -> driver.Outcome:
        with ScriptedLLMServer(driver.recorded_completions(source.trace)) as llm:
            replay_raw = driver.to_replay_config(raw, llm=llm, trace_dir=trace_dir)
            capability = replay_raw["environment"][0]["capabilities"][0]
            capability["network_source"]["matcher"]["site_rules"] = str(rules)
            outcome = asyncio.run(
                driver.run_task(
                    replay_raw,
                    task=TASK,
                    start_url=site.url("jsonp"),
                    replay_archive=source.run_dir,
                    timeout=_TIMEOUT_S,
                )
            )
            assert llm.errors == []
        outcome.require_finished(1, "read")
        return outcome

    replayed = replay(base / "replay")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(responder, "rewrite_callback", lambda *_args, **_kwargs: None)
        unrewritten = replay(base / "unrewritten")
    return Flow(
        source=source, replay=replayed, unrewritten=unrewritten, recorded_names=recorded_names
    )


def test_the_recording_got_its_data(flow: Flow) -> None:
    expected = {f"{box}-{item}" for box, items in JSONP_ITEMS.items() for item in items}
    assert _buttons(flow.source) == expected


def test_the_replay_asks_other_names_and_gets_its_data(flow: Flow) -> None:
    entries = _jsonp_entries(flow.replay.run_dir)
    for box, entry in entries.items():
        asked = _name(entry.url, box)
        assert asked != flow.recorded_names[box], "the names must differ, or nothing is tested"
        assert (entry.kind, entry.level, entry.answer) == ("hit", "normalized", "fulfilled")
        parameter = ENDPOINTS[box][1]
        assert entry.callback_rewrite is not None, box
        assert entry.callback_rewrite.model_dump() == {
            "parameter": parameter,
            "recorded": flow.recorded_names[box],
            "requested": asked,
        }
    assert _buttons(flow.replay) == _buttons(flow.source)
    assert driver.step_signatures(flow.replay.trace) == driver.step_signatures(flow.source.trace)


def test_without_the_rewrite_the_hits_bring_no_data(flow: Flow) -> None:
    """The negative control: the same hits, the recorded names, a page that shows nothing."""
    entries = _jsonp_entries(flow.unrewritten.run_dir)
    for box, entry in entries.items():
        assert (entry.kind, entry.level) == ("hit", "normalized"), box
        assert entry.callback_rewrite is None
        assert _name(entry.url, box) != flow.recorded_names[box]
    assert _buttons(flow.unrewritten) == set()
