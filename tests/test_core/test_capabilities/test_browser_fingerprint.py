"""The environment fingerprint, ``env/run.browser.json`` (§4.14).

A run record owned by the capability: emitted on the first collection and
again only when it changed. Pinned facts come from the tier's parameters,
``off`` facts are measured in the page; the launch flags are a digest, never
the command line; the privacy section says what the bound archivers keep.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from spectragent.core.capabilities.browser.archive import (
    DomRawArchiver,
    NetworkArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.archive.fingerprint import (
    BrowserFingerprint,
    launch_flags_digest,
)
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from tests._helpers.archive_recorder import ScriptedRecorder
from tests._helpers.browser_archive import archiving_capability

PROBE = {
    "userAgent": "Mozilla/5.0 Measured",
    "userAgentData": {"brands": [{"brand": "Chromium", "version": "155"}], "mobile": False},
    "timezone": "Asia/Shanghai",
    "locale": "zh-CN",
    "languages": ["zh-CN", "zh"],
    "devicePixelRatio": 2,
}


class _Probed(ScriptedRecorder):
    probe: dict[str, Any] = PROBE
    #: The tab's current document.
    loader: str = "L1"

    async def send(
        self, method: str, params: Any = None, session_id: Any = None, *, timeout: Any = None
    ) -> Any:  # noqa: E501
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F-main", "loaderId": self.loader}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 77}
        if method == "Runtime.evaluate":
            return {"result": {"type": "string", "value": json.dumps(self.probe)}}
        return {}


TAB = RecorderTarget(session_id="page-s", target_id="PAGE", type="page")
METADATA = {
    "command_line": ["/chrome", "--user-data-dir=/tmp/secret-profile", "--remote-debugging-port=9"],
    "launch_flags": ["--headless=new", "--user-data-dir=<user-data-dir>"],
}


async def _measure(plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    recorder = kwargs.pop("recorder", None) or _Probed()
    return await BrowserFingerprint().measure(
        recorder=recorder,
        tabs=[TAB],
        plan_fingerprint=plan,
        viewport=(1280, 720),
        session_metadata=METADATA,
        archivers=kwargs.pop("archivers", (NetworkArchiver(), StorageArchiver())),
        fresh_profile=True,
        **kwargs,
    )


async def test_off_measures_what_the_page_sees() -> None:
    document = await _measure({"tier": "off", "params": {}})
    assert document["page"] == {"source": "measured", **PROBE}
    assert document["chrome"]["product"] == "Chrome/155.0"
    assert document["viewport"] == {"width": 1280, "height": 720}
    assert document["replayability"] == {"tier": "off", "params": {}}


async def test_pinned_states_its_parameters() -> None:
    pinned = PinnedReplayability(random_seed=7, timezone="Europe/Berlin", locale="de-DE")
    plan = pinned.plan(t0_ms=1_700_000_000_000, browser_user_agent="ScriptedChrome/1.0")
    recorder = _Probed()
    document = await _measure(plan.fingerprint(), recorder=recorder)
    assert document["page"]["source"] == "replayability"
    assert (document["page"]["timezone"], document["page"]["locale"]) == ("Europe/Berlin", "de-DE")
    assert document["page"]["devicePixelRatio"] == 1.0
    assert document["replayability"]["t0_ms"] == 1_700_000_000_000
    assert document["replayability"]["params"]["random_seed"] == 7
    assert document["replayability"]["shim_sha256"]
    assert "Runtime.evaluate" not in recorder.methods(), "pinned facts are parameters, not probes"


async def test_launch_flags_are_a_digest_and_the_command_line_never_appears() -> None:
    document = await _measure({"tier": "off", "params": {}})
    expected = hashlib.sha256(canonical_json(METADATA["launch_flags"])).hexdigest()
    assert document["launch_flags_sha256"] == expected == launch_flags_digest(METADATA)
    text = json.dumps(document)
    assert "secret-profile" not in text and "command_line" not in text
    assert launch_flags_digest({}) is None


async def test_privacy_says_what_the_archivers_keep() -> None:
    with_network = await _measure({"tier": "off"})
    assert with_network["privacy"]["contains_session_state"] is True, "Set-Cookie is kept"
    assert with_network["privacy"]["level"] == "session_state"
    assert with_network["redaction"]["archivers"]["browser.storage"] == {"cookie_values": "sha256"}
    assert with_network["redaction"]["policy"] == "af-privacy-1+browser-1"
    dom_only = await _measure({"tier": "off"}, archivers=(DomRawArchiver(),))
    assert dom_only["privacy"] == {
        "contains_session_state": False,
        "fresh_profile": True,
        "level": "redacted",
        "markers": ["redacted:af-privacy-1+browser-1"],
    }


async def test_ua_metadata_is_kept_across_a_plain_http_page() -> None:
    """``navigator.userAgentData`` exists in secure contexts only (measured, Chrome 155)."""
    recorder = _Probed()
    fingerprint = BrowserFingerprint()
    common: dict[str, Any] = {
        "recorder": recorder,
        "tabs": [TAB],
        "plan_fingerprint": {"tier": "off"},
        "viewport": (1, 1),
        "session_metadata": None,
        "archivers": (),
        "fresh_profile": True,
    }
    secure = await fingerprint.measure(**common)
    recorder.probe = {**PROBE, "userAgentData": None}
    recorder.loader = "L2"  # the tab navigated to a plain http page
    insecure = await fingerprint.measure(**common)
    assert "Runtime.evaluate" in [m for m, *_ in recorder.sent[-3:]], "a new document: measured"
    assert insecure["page"]["userAgentData"] == secure["page"]["userAgentData"] is not None


async def test_emitted_on_the_first_collection_then_only_on_change() -> None:
    cap, driver = archiving_capability(archivers=(StorageArchiver(),))
    await driver.start()
    try:
        first = [f for f in (await cap.collect_archive()).files if f.scope == "run"]
        assert len(first) == 1
        (record,) = first
        assert (record.archiver_id, record.media_type, record.content_addressed) == (
            "browser",
            "application/json",
            False,
        )
        assert [f for f in (await cap.collect_archive()).files if f.scope == "run"] == []
        driver.change_run_record()
        (changed,) = [f for f in (await cap.collect_archive()).files if f.scope == "run"]
        assert json.loads(changed.data)["page"]["userAgent"].endswith("+1")
    finally:
        await cap.teardown()


async def test_off_probes_in_an_isolated_world_once_per_document() -> None:
    """The page never runs the probe in its own world, and a quiet step does not rerun it."""
    recorder = _Probed()
    fingerprint = BrowserFingerprint()
    common: dict[str, Any] = {
        "recorder": recorder,
        "tabs": [TAB],
        "plan_fingerprint": {"tier": "off"},
        "viewport": (1, 1),
        "session_metadata": None,
        "archivers": (),
        "fresh_profile": True,
    }
    first = await fingerprint.measure(**common)
    worlds = [p for m, p, _ in recorder.sent if m == "Page.createIsolatedWorld"]
    assert worlds == [{"frameId": "F-main", "worldName": "af-fingerprint"}]
    (probe,) = [p for m, p, _ in recorder.sent if m == "Runtime.evaluate"]
    assert probe["contextId"] == 77, "never the main world"

    recorder.sent.clear()
    again = await fingerprint.measure(**common)
    assert again["page"] == first["page"]
    assert [m for m, *_ in recorder.sent if m != "Browser.getVersion"] == ["Page.getFrameTree"]

    recorder.sent.clear()
    recorder.loader = "L2"
    await fingerprint.measure(**common)
    assert [m for m, *_ in recorder.sent if m != "Browser.getVersion"] == [
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Runtime.evaluate",
    ]


@pytest.mark.requires_chrome
async def test_the_page_can_neither_see_nor_bend_the_off_probe() -> None:
    """Live: page code that traps and patches what a main-world probe reads changes nothing."""
    from tests._helpers.archive_bench import VISION_FRAGMENTS, LiveRecording
    from tests._helpers.fixture_site import FixtureSite

    with FixtureSite(mapped=True) as site:
        async with LiveRecording(
            site, archivers=[StorageArchiver()], fragments=VISION_FRAGMENTS
        ) as rec:
            await rec.step(site.url("post_form"), collect=False)
            cap = rec.capability
            await cap.cdp.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        "window.__reads = 0;"
                        "Object.defineProperty(Navigator.prototype, 'userAgent',"
                        " {get() { window.__reads++; return 'PATCHED'; }});"
                        'JSON.stringify = () => \'{"userAgent": "FAKED"}\'; 1'
                    )
                },
                session_id=cap.default_session_id,
            )
            batch = await cap.collect_archive()
            (record,) = [f for f in batch.files if f.scope == "run"]
            page = json.loads(record.data)["page"]
            assert page["source"] == "measured"
            assert "Chrome/" in page["userAgent"] and page["timezone"]
            reads = await cap.cdp.send(
                "Runtime.evaluate",
                {"expression": "window.__reads", "returnByValue": True},
                session_id=cap.default_session_id,
            )
            assert reads["result"]["value"] == 0, "the page saw the probe read navigator"
