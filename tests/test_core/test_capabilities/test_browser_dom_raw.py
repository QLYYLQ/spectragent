"""``browser.dom_raw`` — the raw DOM of every committed cycle (§4.12, §4.14).

Against the fake browser, through a real ``BrowserCapability``: the DOM
resolver's own capture reaches the archive through the raw-capture sink and
only with a successful cycle (a re-fired attempt replaces the one before it);
with DOM negotiated the archive path sends no ``DOM.*`` command; a vision-only
run self-captures on the recorder connection; what is archived is redacted,
deterministic, content-addressed — and a cleaner run offline on it gives the
observation back.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spectragent.core.capabilities.browser.archive import DomRawArchiver
from spectragent.core.capabilities.browser.archive.dom_raw import (
    capture_from_document,
    read_dom_document,
)
from spectragent.core.capabilities.browser.archive.redaction import redact_raw_capture
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import BrowserUseCleaner
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner
from spectragent.core.capabilities.browser.dom.cleaning.skyvern_hit_test import (
    SkyvernHitTestCleaner,
)
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, DOMState, TargetCapture
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import TextSerializer
from tests._helpers.browser_archive import archiving_capability, settle
from tests._helpers.dom_captures import load_capture
from tests._helpers.dom_payloads import reference_page

DOM_COMMANDS = ("DOM.", "DOMSnapshot.", "Accessibility.")


class _FlakyCleaner(BrowserUseCleaner):
    """Fails ``clean`` on the attempts listed, so the cycle re-fires or fails."""

    def __init__(self, failing: set[int]) -> None:
        super().__init__()
        self.failing = failing
        self.calls = 0

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        self.calls += 1
        if self.calls in self.failing:
            raise RuntimeError(f"clean attempt {self.calls} failed")
        return await super().clean(
            capture,
            viewport,
            previous_selector_map=previous_selector_map,
            device_pixel_ratio=device_pixel_ratio,
        )


async def _dom_raw_capability(**kwargs: Any) -> tuple[Any, Any]:
    cap, driver = archiving_capability(archivers=(DomRawArchiver(),), **kwargs)
    return cap, driver


def _documents(files: list[Any]) -> list[dict[str, Any]]:
    return [read_dom_document(item.data) for item in files if item.archiver_id == "browser.dom_raw"]


class TestTheSinkPath:
    async def test_a_committed_cycle_is_archived_once_with_its_context(self) -> None:
        cap, driver = await _dom_raw_capability()
        await driver.start()
        try:
            await driver.observe()
            files = await cap.collect_archive()
            (item,) = [f for f in files.files if f.archiver_id == "browser.dom_raw"]
            assert item.content_addressed and item.meta.cycle == 1
            assert dict(item.meta.ids) == {"tab": "page-1", "loader_id": "loader-1"}
            (document,) = _documents(list(files.files))
            assert (document["source"], document["cleaner"], document["tab"]) == (
                "observation",
                "browser_use",
                "page-1",
            )
            assert document["viewport"] == [1280, 720] and document["device_pixel_ratio"] == 1.0
            assert _documents(list((await cap.collect_archive()).files)) == []
        finally:
            await cap.teardown()

    async def test_a_failed_cycle_archives_nothing(self) -> None:
        cap, driver = await _dom_raw_capability()
        cap.dom_cleaner = _FlakyCleaner({1, 2})
        await driver.start()
        try:
            with pytest.raises(RuntimeError, match="no watchdog produced"):
                await driver.observe()
            assert _documents(list((await cap.collect_archive()).files)) == []
        finally:
            await cap.teardown()

    async def test_a_refired_attempt_replaces_the_one_before(self) -> None:
        cap, driver = await _dom_raw_capability()
        cleaner = _FlakyCleaner({1})
        cap.dom_cleaner = cleaner
        await driver.start()
        try:
            await driver.observe()
            assert cleaner.calls == 2
            assert len(_documents(list((await cap.collect_archive()).files))) == 1
        finally:
            await cap.teardown()

    async def test_with_dom_negotiated_the_archive_path_sends_no_dom_command(self) -> None:
        cap, driver = await _dom_raw_capability()
        await driver.start()
        try:
            await driver.observe()
            await cap.collect_archive()
            recorder_dom = [m for m in driver.recorder.methods() if m.startswith(DOM_COMMANDS)]
            assert recorder_dom == []
            assert any(m.startswith("DOM.") for m in driver.browser.control.methods())
        finally:
            await cap.teardown()

    async def test_cycles_reach_the_archive_scope_only_for_a_vision_run(self) -> None:
        """No cross-scope handler is added to a DOM run's cycles (it would only cost)."""

        async def tab_connections(fragments: frozenset[str]) -> int:
            cap, driver = await _dom_raw_capability(fragments=fragments)
            await driver.start()
            try:
                (watchdog,) = [
                    w for w in cap._shared_watchdogs if type(w).__name__ == "ArchiveWatchdog"
                ]
                return len(watchdog._tab_connections)  # type: ignore[attr-defined]
            finally:
                await cap.teardown()

        assert await tab_connections(frozenset({"browser.dom"})) == 0
        assert await tab_connections(frozenset({"browser.screenshot"})) == 1

    async def test_unbound_the_sink_parks_nothing(self) -> None:
        cap, driver = await _dom_raw_capability()
        await driver.start()
        try:
            cap.bind_archivers([])
            await driver.observe()
            assert cap._archive is not None and len(cap._archive.raw_captures) == 0
        finally:
            await cap.teardown()


class TestTheVisionSelfCapture:
    async def test_a_vision_only_cycle_self_captures_on_the_recorder(self) -> None:
        cap, driver = await _dom_raw_capability(fragments=frozenset({"browser.screenshot"}))
        await driver.start()
        try:
            await driver.observe()
            await settle()
            recorder_session = driver.page_session()
            sent = [(m, sid) for m, sid in ((s.method, s.session_id) for s in driver.recorder.sent)]
            assert ("DOM.getDocument", recorder_session) in sent
            assert ("DOMSnapshot.captureSnapshot", recorder_session) in sent
            assert not [
                m for m in driver.browser.control.methods() if m.startswith(DOM_COMMANDS)
            ], "the agent's connection captured no DOM: it did not negotiate one"
            (document,) = _documents(list((await cap.collect_archive()).files))
            assert (document["source"], document["cleaner"]) == ("archive", "self_capture")
        finally:
            await cap.teardown()

    async def test_a_failed_self_capture_fails_the_next_collection(self) -> None:
        cap, driver = await _dom_raw_capability(fragments=frozenset({"browser.screenshot"}))
        await driver.start()
        try:
            for archiver in cap.bound_archivers:
                archiver.self_capture_timeout_s = 0.05  # type: ignore[attr-defined]
            driver.browser.hold("DOM.getDocument", session_id=driver.page_session())
            await driver.observe()
            with pytest.raises(Exception, match="raw DOM missing"):
                await cap.collect_archive()
        finally:
            await cap.teardown()


class TestWhatIsArchived:
    async def test_redacted_before_it_is_stored(self) -> None:
        cap, driver = await _dom_raw_capability()
        await driver.start()
        try:
            await driver.observe()
            batch = await cap.collect_archive()
            (document,) = _documents(list(batch.files))
            assert driver.browser.password_value not in json.dumps(document)
            assert {e["rule"] for e in document["redaction"]["entries"]} == {"input_value"}
            assert "shoes" in json.dumps(document), "a non-sensitive value is kept"
        finally:
            await cap.teardown()

    async def test_the_same_page_is_the_same_bytes(self) -> None:
        cap, driver = await _dom_raw_capability()
        await driver.start()
        try:
            await driver.observe()
            first = [f for f in (await cap.collect_archive()).files if f.content_addressed]
            await driver.observe()
            second = [f for f in (await cap.collect_archive()).files if f.content_addressed]
            assert first[0].data == second[0].data, "deterministic gzip JSON: stored once"
            assert first[0].meta.cycle == 1 and second[0].meta.cycle == 2
        finally:
            await cap.teardown()

    async def test_an_offline_clean_of_the_archive_reproduces_the_observation(self) -> None:
        """BrowserUse's cleaner is pure given capture, viewport and DPR (PRD §6.3)."""
        capture = reference_page()
        cleaner, serializer = BrowserUseCleaner(), TextSerializer()
        live = await cleaner.clean(capture, (1280, 720), device_pixel_ratio=1.0)
        data, _ = redact_raw_capture(capture)
        archived = capture_from_document(json.loads(json.dumps(data)))
        offline = await cleaner.clean(archived, (1280, 720), device_pixel_ratio=1.0)
        assert serializer.serialize(offline) == serializer.serialize(live)
        assert sorted(offline.selector_map) == sorted(live.selector_map)


def _skyvern_capture_with_hit_tests() -> TargetCapture:
    """The committed Skyvern capture, with hit-test results: the covered link covered."""

    def add(capture: TargetCapture) -> None:
        results: dict[int, Any] = {}
        for ref, node in capture.raw_dom["skyvern_nodes"].items():
            attributes = node.get("attributes") or []
            element_id = dict(zip(attributes[::2], attributes[1::2], strict=False)).get("id")
            if element_id == "covered":
                results[ref] = {"center_topmost": False, "offset": None}
            elif element_id:
                results[ref] = {"center_topmost": True, "offset": [4.0, 4.0]}
        capture.raw_dom["skyvern_hit_tests"] = results
        for child in capture.frames.values():
            add(child)

    capture = load_capture("skyvern_sticky")
    add(capture)
    return capture


@pytest.mark.parametrize(
    "cleaner", [SkyvernCleaner(), SkyvernHitTestCleaner()], ids=["skyvern", "skyvern_hit_test"]
)
async def test_an_offline_skyvern_clean_of_the_archive_reproduces_the_observation(
    cleaner: Any,
) -> None:
    """The Skyvern ref maps come back keyed by the integers the cleaners look up.

    JSON keys are strings: read back as they were stored, every node lookup
    by ``ref`` missed and the offline tree lost its elements' backend ids.
    """
    capture = _skyvern_capture_with_hit_tests()
    live = await cleaner.clean(capture, (1280, 720))
    data, _ = redact_raw_capture(capture)
    archived = capture_from_document(json.loads(json.dumps(data)))
    for key in ("skyvern_nodes", "skyvern_hit_tests"):
        assert archived.raw_dom[key] and all(isinstance(ref, int) for ref in archived.raw_dom[key])
    offline = await cleaner.clean(archived, (1280, 720))
    serializer = TextSerializer()
    assert serializer.serialize(offline) == serializer.serialize(live)
    assert sorted(offline.selector_map) == sorted(live.selector_map)
    assert live.selector_map, "the comparison has elements to compare"
    assert {
        handle: (node.backend_node_id, node.hit_test)
        for handle, node in offline.selector_map.items()
    } == {
        handle: (node.backend_node_id, node.hit_test) for handle, node in live.selector_map.items()
    }


def test_declarations() -> None:
    archiver = DomRawArchiver()
    assert archiver.archiver_id == "browser.dom_raw"
    assert archiver.needs == frozenset({"raw_dom", "observation_cycle"})
    assert archiver.privacy_markers == frozenset({"redacted:af-privacy-1+browser-2"})
