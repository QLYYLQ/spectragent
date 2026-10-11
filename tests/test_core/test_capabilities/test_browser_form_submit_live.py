"""A form submission's next observation, in a real Chrome (§4.14, M33; fixture only, no LLM).

M31 found that clicking a form's submit button stalled the next observation
for 30 s (``error_phase="observe"``). M33 measured why — the click returns
before the navigation it starts, and under pinned's
``--disable-back-forward-cache`` Chrome refuses or never answers a screenshot
sent between that navigation's request and its commit — and fixed it in the
observation cycle (``test_browser_navigation_settle.py`` pins the parts).

Each run has the shape M30's ``replay_driver.record_config`` records with —
``browser_use`` in ``dom_vision`` mode, ``replayability: pinned``,
``browser.network`` + ``browser.storage`` + ``browser.dom_raw`` — on the
mapped fixture's ``form_submit`` pages, with a loopback scripted model that
picks elements by id: a GET form and a POST form submitted with their button,
and a search field submitted with Enter. The step after the submission must
end promptly, observing the result page (URL, a new document, the echoed
fields), with no observation error. ``off`` with no archivers runs the GET
flow as the control.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._helpers import branch_driver
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import FixtureSite
from tests._helpers.scripted_llm import Reply, ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

#: The stalled step took over 30 s; a prompt one takes well under a second here.
PROMPT_STEP_MS = 5000
TASK = "Add the kettle to the cart."
ANSWER = "submitted"


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted model is on loopback; the machine's proxy must not see it.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


def _press(keys: str) -> Reply:
    return lambda _body: driver.reply({"browser.send_keys": {"keys": keys}}, goal=f"press {keys}")


def _run(
    site: FixtureSite, base: Path, script: list[Reply], *, page: str, tier: str = "pinned"
) -> driver.Outcome:
    with ScriptedLLMServer(script) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "traces")
        if tier == "off":
            raw["environment"][0]["capabilities"][0]["replayability"] = {"replayability_id": "off"}
            raw["hook"][0]["archive"] = []
        outcome = asyncio.run(
            driver.run_task(
                raw,
                task=TASK,
                start_url=f"{site.main_origin}/form_submit/{page}",
                timeout=120,
            )
        )
        assert llm.errors == []
    return outcome


def _require_submitted(
    site: FixtureSite,
    outcome: driver.Outcome,
    *,
    step: int,
    method: str,
    query: str,
    echoed: str | None = None,
) -> None:
    """Step *step* observed the result page promptly, and the run ended there.

    *query* is the urlencoded submission; the result page echoes it decoded
    (*echoed*, the same by default).
    """
    outcome.require_finished(step + 1, ANSWER)
    trace = outcome.trace
    records = trace.step_records
    assert [(r.step, r.error_phase, r.error) for r in records if r.error_phase or r.error] == []
    duration = records[step].duration_ms
    assert duration is not None and duration < PROMPT_STEP_MS, (
        f"the step after the submission took {duration} ms"
    )
    meta = trace.observation_fragment(step, "browser.browser_meta")
    before = trace.observation_fragment(step - 1, "browser.browser_meta")
    assert meta is not None and before is not None
    expected_url = f"{site.main_origin}/form_submit/result.html"
    if method == "GET":
        expected_url += f"?{query}"
    assert meta.url == expected_url
    assert meta.loader_id and meta.loader_id != before.loader_id, "not a new document"
    dom = trace.observation_fragment(step, "browser.dom")
    assert dom is not None
    text = " ".join(dom.element_tree.serialized_dom.split())  # one text node per line
    assert f"Received by {method}: {echoed or query}" in text


class TestFormSubmission:
    def test_get_form_click_submit(self, site: FixtureSite, tmp_path: Path) -> None:
        script = [branch_driver.click("add"), driver.done(ANSWER)]
        outcome = _run(site, tmp_path, script, page="get.html")
        _require_submitted(site, outcome, step=1, method="GET", query="item=kettle")

    def test_post_form_click_submit(self, site: FixtureSite, tmp_path: Path) -> None:
        script = [branch_driver.click("add"), driver.done(ANSWER)]
        outcome = _run(site, tmp_path, script, page="post.html")
        _require_submitted(site, outcome, step=1, method="POST", query="item=kettle")

    def test_enter_key_submit(self, site: FixtureSite, tmp_path: Path) -> None:
        script = [
            branch_driver.type_into("query", "blue kettle"),
            _press("Enter"),
            driver.done(ANSWER),
        ]
        outcome = _run(site, tmp_path, script, page="enter.html")
        _require_submitted(
            site,
            outcome,
            step=2,
            method="GET",
            query="query=blue+kettle",
            echoed="query=blue kettle",
        )

    def test_off_tier_unchanged(self, site: FixtureSite, tmp_path: Path) -> None:
        script = [branch_driver.click("add"), driver.done(ANSWER)]
        outcome = _run(site, tmp_path, script, page="get.html", tier="off")
        _require_submitted(site, outcome, step=1, method="GET", query="item=kettle")
        assert not (outcome.run_dir / "env").exists(), "the control run archives nothing"
