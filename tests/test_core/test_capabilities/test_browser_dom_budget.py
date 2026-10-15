"""The DOM serializer is a receptor, and the DOM fragment has a price.

Two halves of the same round, and they belong together because the second
only became possible once the first landed.

**Half one.** ``serializer`` was a name on ``BrowserProfileConfig`` and
``DOMWatchdog`` did ``DOMSerializerRegistrar.get(name)()``, so
``TextSerializer(max_text_length=...)`` and
``HTMLSerializer(max_href_length=...)`` were generated into
``TextDom_SerializerConfig`` / ``HtmlDom_SerializerConfig``, validated in
YAML, and thrown away. Third instance of that defect; the structural
guard against a fourth is ``test_receptor_hygiene.py``.

**Half two.** ``browser.screenshot`` could already be told what it was
allowed to cost (``screenshot_format`` / ``screenshot_quality``);
``browser.dom`` could not, even though it is the expensive fragment. Four
budget knobs now say so — and every one of them *reports* what it cut on
``DOMObs.budget``, because a trace in which a truncated page and a sparse
page look identical is a trace that lies to the evaluator reading it.
"""

from __future__ import annotations

from typing import Any

import pytest

# Concrete DOM strategies register on import. Production gets these via the
# composer's recursive ``discover()``; these tests build watchdogs directly.
import spectragent.core.capabilities.browser.dom.cleaning.browser_use  # noqa: F401
import spectragent.core.capabilities.browser.dom.serializers.html_serializer  # noqa: F401
import spectragent.core.capabilities.browser.dom.serializers.text_serializer  # noqa: F401
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.dom.budget import DOMBudget, DOMBudgetPass
from spectragent.core.capabilities.browser.dom.models import DOMState
from spectragent.core.capabilities.browser.dom.serializers.html_serializer import (
    HTMLSerializer,
)
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import (
    TextSerializer,
)
from spectragent.core.capabilities.browser.watchdogs import DOMWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
)
from tests._helpers.dom_payloads import (
    Doc,
    El,
    by_id,
    clean,
    compile_target,
    indexed_ids,
)


def _compose(**capability_block: Any) -> BrowserCapability:
    """Build a BrowserCapability the real way — through the Composer."""
    import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401
    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    run = ConfigComposer(registrars=collect_registrars()).compose(
        {
            "capability": [
                {
                    "capability_id": "browser",
                    "mouse_trajectory": {"trajectory_type": "instant"},
                    "typing_rhythm": {"rhythm_type": "instant"},
                    "replayability": {"replayability_id": "none"},
                    "network_source": {"source_id": "live"},
                    "dom_cleaner": {"cleaner_type": "browser_use"},
                    "dom_serializer": {"serializer_type": "text"},
                    "backend": {"provider_id": "local_chrome"},
                    **capability_block,
                }
            ]
        }
    )
    return run.single("capability")


# ── a page with prose, links and controls, and known geometry ────────

_VIEWPORT = (1280, 720)


def _page() -> Any:
    """One document: prose, two on-screen controls, one below the fold.

    Geometry is explicit so ``dom_viewport_only`` has something real to
    decide on: ``#below`` sits at y=900 in a 720-tall viewport, so the
    cleaner still indexes it (an agent can scroll to it) and the budget is
    the thing that decides whether the run pays for it.
    """
    return compile_target(
        Doc(
            "MAIN",
            html_id=1,
            viewport=(float(_VIEWPORT[0]), float(_VIEWPORT[1])),
            height=3000,
            body=[
                El(
                    "p",
                    9,
                    bounds=(0, 0, 400, 40),
                    children=[
                        El(
                            "#text",
                            90,
                            bounds=(0, 0, 400, 40),
                            text="the quick brown fox jumps over the lazy dog " * 8,
                        )
                    ],
                ),
                El("button", 10, bounds=(10, 50, 80, 24), attrs={"id": "top"}),
                El(
                    "a",
                    11,
                    bounds=(10, 90, 120, 20),
                    attrs={"id": "link", "href": "https://example.com/" + "p" * 300},
                    children=[El("#text", 91, bounds=(10, 90, 120, 20), text="a link")],
                ),
                El("button", 12, bounds=(10, 900, 80, 24), attrs={"id": "below"}),
            ],
        ),
        session_id="tab",
    )


def _scrollable_page() -> Any:
    """A page whose only indexed nodes are one button and one scroll box.

    A scrollable container is indexed (``is_scrollable_container`` — the
    agent can scroll it) but is *not* marked ``is_interactive``, which is
    what makes it the interesting case for both ``interactive_only`` and
    the "what does a dropped element still print" question.
    """
    return compile_target(
        Doc(
            "MAIN",
            html_id=1,
            viewport=(float(_VIEWPORT[0]), float(_VIEWPORT[1])),
            height=900,
            body=[
                El("button", 10, bounds=(10, 10, 80, 24), attrs={"id": "top"}),
                El(
                    "div",
                    20,
                    bounds=(10, 60, 300, 100),
                    attrs={"id": "feed"},
                    styles={"overflow-y": "scroll"},
                    client=(0, 0, 300, 100),
                    scroll=(0, 0, 300, 900),
                    children=[El("#text", 21, bounds=(10, 60, 300, 100), text="feed items here")],
                ),
            ],
        ),
        session_id="tab",
    )


async def _state(**cleaner_options: Any) -> DOMState:
    return await clean(_page(), **cleaner_options)


# ── 1. the serializer is a wired receptor ────────────────────────────


class TestDomSerializerIsAWiredReceptor:
    """Chosen *and configured* from YAML, like ``dom_cleaner`` before it."""

    def test_the_knob_reaches_the_instance(self) -> None:
        cap = _compose(dom_serializer={"serializer_type": "text", "max_text_length": 42})
        assert isinstance(cap.dom_serializer, TextSerializer)
        assert cap.dom_serializer.max_text_length == 42

    def test_the_strategy_itself_is_chosen_there(self) -> None:
        cap = _compose(dom_serializer={"serializer_type": "html", "max_href_length": 33})
        assert isinstance(cap.dom_serializer, HTMLSerializer)
        assert cap.dom_serializer.max_href_length == 33

    def test_the_configured_instance_is_the_one_that_serializes(self) -> None:
        """Identity, not equality — a second default-constructed one is the bug."""
        cap = _compose(dom_serializer={"serializer_type": "text", "max_text_length": 42})
        cap._cdp = object()  # type: ignore[assignment]
        cap._input_platform = "macos"  # This fixture begins after setup's measurement.
        watchdog = next(
            wd
            for wd in cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid1")
            if isinstance(wd, DOMWatchdog)
        )
        assert watchdog._serializer is cap.dom_serializer

    async def test_the_yaml_value_changes_what_the_agent_reads(self) -> None:
        """End to end: one number in the config shortens the prompt text."""
        state = await _state()
        long_text = TextSerializer().serialize(state)
        short_text = TextSerializer(max_text_length=20).serialize(state)
        assert len(short_text) < len(long_text)
        assert "..." in short_text

    def test_a_hand_built_capability_still_serializes(self) -> None:
        """No Composer: the default strategy with its defaults, as before."""
        assert isinstance(BrowserCapability().dom_serializer, TextSerializer)

    def test_the_default_strategy_has_exactly_one_home(self) -> None:
        """The capability's default and the resolver's fallback are one fact.

        ``BrowserCapability.__init__`` assigns a default over its
        ``dom_serializer`` ``WiredField``; ``DOMWatchdog`` falls back when
        ``WatchdogContext.dom_serializer`` is ``None`` (a hand-built
        watchdog). Both now call ``default_serializer()``. Written out as
        ``TextSerializer()`` in both files it is one fact in two places:
        change the capability's default and every hand-built watchdog
        silently keeps the old strategy — a quieter cousin of the defect
        this whole round is about.
        """
        from spectragent.core.capabilities.browser.dom.cleaning import default_cleaner
        from spectragent.core.capabilities.browser.dom.serializers import (
            default_serializer,
        )

        cap = BrowserCapability()
        watchdog = DOMWatchdog(_PageCDP(_page()))
        assert type(watchdog._serializer) is type(cap.dom_serializer)
        assert type(watchdog._serializer) is type(default_serializer())
        assert type(watchdog._cleaner) is type(cap.dom_cleaner)
        assert type(watchdog._cleaner) is type(default_cleaner())

    def test_the_profile_no_longer_names_a_serializer(self) -> None:
        from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig

        assert "serializer" not in BrowserProfileConfig.model_fields

    def test_vision_only_still_refuses_to_serve_the_dom_fragment(self) -> None:
        """The opt-out the old ``serializer: None`` was credited with.

        It never rode on the serializer — it rides on excluding the ``dom``
        watchdog, which is what removes the fragment from what the
        capability declares. Removing the profile field left it intact, and
        this pins that.
        """
        cap = BrowserCapability(profile="vision_only")
        assert "browser.dom" not in cap.declared_fragments
        assert "browser.screenshot" in cap.declared_fragments


# ── 2. resolve_profile has one parameter ─────────────────────────────


def test_resolve_profile_takes_only_a_name() -> None:
    """``override`` / ``watchdogs_add`` / ``watchdogs_remove`` are gone.

    No caller in ``spectragent/`` or ``tests/`` ever passed them, and
    ``override`` silently dropped every key that was not ``"serializer"``
    — a config surface that looked general and was not.
    """
    import inspect

    from spectragent.core.capabilities.browser.profiles import resolve_profile

    assert list(inspect.signature(resolve_profile).parameters) == ["name"]


def test_resolve_profile_hands_back_a_copy() -> None:
    from spectragent.core.capabilities.browser.profiles import (
        BUILTIN_PROFILES,
        resolve_profile,
    )

    profile = resolve_profile("lightweight")
    profile.watchdogs_exclude.append("dom")
    assert "dom" not in BUILTIN_PROFILES["lightweight"].watchdogs_exclude


# ── 3. the budget cuts, and says it cut ──────────────────────────────


def _apply(state: DOMState, budget: DOMBudget, serializer: Any = None) -> Any:
    """Run one budget pass over ``state`` exactly as the resolver does.

    ``DOMBudgetPass.apply`` is the only public entry point on purpose: the
    order (element cuts → serialize → length cut) is the budget's
    contract, and every way of getting it wrong by calling three methods
    by hand failed *silently*, by dropping a configured knob.
    """
    return DOMBudgetPass(budget).apply(state, (serializer or TextSerializer()).serialize)


class TestDomBudgetReportsEveryCut:
    """The point of the whole exercise: no silent truncation.

    Each test asserts both halves — that the budget actually removed
    something, and that ``DOMObs.budget`` names what it removed. A budget
    that cut without reporting would turn every trace of a large page into
    a plausible-looking lie.
    """

    async def test_an_unbudgeted_fragment_reports_nothing(self) -> None:
        state = await _state()
        assert _apply(state, DOMBudget()) is None, (
            "no budget configured must mean no budget report — the presence "
            "of the field is the signal that something was cut"
        )

    async def test_max_elements_caps_the_selector_map_and_says_so(self) -> None:
        state = await _state()
        before = len(state.selector_map)
        assert before >= 3, "fixture must index enough elements to cap"

        report = _apply(state, DOMBudget(max_elements=2))

        assert len(state.selector_map) == 2
        assert report is not None
        assert report.applied == ["max_elements"]
        assert (report.elements_before, report.elements_after) == (before, 2)

    async def test_a_capped_element_is_no_longer_actionable(self) -> None:
        """The cut is one cut, not two: unindexed means unreachable.

        Dropping an element from the serialization but leaving it in the
        selector map would let the model act on something it was never
        shown; leaving it indexed in the serialization but not in the map
        would advertise a click that fails.
        """
        state = await _state()
        _apply(state, DOMBudget(max_elements=1))

        dropped = [
            node
            for node in (state.dom_tree.descendants() if state.dom_tree else [])
            if node.attributes.get("id") == "below"
        ]
        assert dropped and dropped[0].element_index is None
        assert dropped[0] not in state.selector_map.values()
        assert "[" + str(12) + "]" not in state.serialized_dom

    async def test_a_dropped_scroll_container_keeps_its_line_and_loses_its_reach(
        self,
    ) -> None:
        """The honest boundary of the element budgets, pinned.

        What a dropped element loses is its selector-map entry, not its
        existence in the prompt. ``TextSerializer`` renders a node whose
        tag is structural regardless of its index (``should_render =
        is_interactive or is_scrollable or is_iframe``), so a de-indexed
        scroll container still prints its ``|scroll element|`` line *and*
        its subtree. That is not a leak — ``build_guarded_act`` routes on
        the selector map, and the entry is gone — but the invariant to
        rely on is "unindexed means unreachable", not "gone", and a
        reader budgeting tokens off ``max_elements`` should know that
        these lines survive.
        """
        state = await clean(_scrollable_page())
        handle, node = by_id(state, "feed")
        assert node.is_scrollable and not node.is_interactive
        assert "|scroll element|<div id=feed" in state.serialized_dom

        report = _apply(state, DOMBudget(max_elements=1))

        assert handle not in state.selector_map, "unindexed means unreachable"
        assert node.element_index is None
        assert "|scroll element|<div id=feed" in state.serialized_dom, (
            "a de-indexed scroll container still prints — only its reach is cut"
        )
        assert "feed items here" in state.serialized_dom, "and so does its subtree"
        assert report is not None and report.applied == ["max_elements"]

    async def test_interactive_only_is_what_actually_removes_those_lines(self) -> None:
        """The other half of the boundary above: one knob does remove nodes.

        ``max_elements`` and ``viewport_only`` cut reach; only
        ``interactive_only`` prunes the tree, and it prunes exactly the
        nodes that are neither indexed nor an ancestor of an indexed one
        — which is what takes the scroll container's line and its prose
        out of the prompt.
        """
        state = await clean(_scrollable_page())
        assert "|scroll element|" in state.serialized_dom

        report = _apply(state, DOMBudget(interactive_only=True))

        assert "|scroll element|" not in state.serialized_dom
        assert "feed items here" not in state.serialized_dom
        assert "[10]<button id=top" in state.serialized_dom, "the control survives"
        assert report is not None and report.nodes_dropped > 0

    async def test_viewport_only_drops_what_is_below_the_fold(self) -> None:
        state = await _state()
        assert "below" in indexed_ids(state)

        report = _apply(state, DOMBudget(viewport_only=True))

        assert "below" not in indexed_ids(state)
        assert "top" in indexed_ids(state)
        assert report is not None and "viewport_only" in report.applied

    async def test_viewport_only_cuts_nothing_when_the_viewport_is_unknown(self) -> None:
        """Never cut what you cannot measure — a zero viewport is not an empty page."""
        state = await _state()
        state.scroll_info.viewport_width = 0
        state.scroll_info.viewport_height = 0
        before = dict(state.selector_map)

        assert _apply(state, DOMBudget(viewport_only=True)) is None
        assert state.selector_map == before

    async def test_interactive_only_removes_the_prose_and_says_so(self) -> None:
        state = await _state()
        full = TextSerializer().serialize(state)
        assert "quick brown fox" in full

        report = _apply(state, DOMBudget(interactive_only=True))

        assert "quick brown fox" not in state.serialized_dom
        assert "top" in indexed_ids(state), "controls must survive"
        assert report is not None
        assert "interactive_only" in report.applied
        assert report.nodes_dropped > 0

    async def test_max_serialized_chars_trims_and_labels_the_text(self) -> None:
        state = await _state()
        full = len(TextSerializer().serialize(state))
        limit = full // 2

        report = _apply(state, DOMBudget(max_serialized_chars=limit))

        assert "[dom budget:" in state.serialized_dom, (
            "the model reading the prompt must be told the page was cut, not just the trace"
        )
        assert report is not None
        assert report.applied == ["max_serialized_chars"]
        assert report.serialized_chars_before == full
        assert report.serialized_chars_after <= limit

    async def test_the_truncation_note_is_not_charged_against_the_budget(self) -> None:
        """Documented overshoot, pinned so nobody 'fixes' it into a lie.

        ``max_serialized_chars`` budgets page *content*. The note that
        tells the model it is reading a cut page is metadata about the
        cut, so the final string is ~45 characters longer than the limit
        and ``serialized_chars_after`` counts the kept content only.
        """
        state = await _state()
        limit = len(TextSerializer().serialize(state)) // 2

        report = _apply(state, DOMBudget(max_serialized_chars=limit))

        assert report is not None
        assert report.serialized_chars_after <= limit
        assert len(state.serialized_dom) > limit
        assert len(state.serialized_dom) < limit + 80

    async def test_a_page_that_fits_is_not_labelled(self) -> None:
        state = await _state()
        full = len(TextSerializer().serialize(state))

        assert _apply(state, DOMBudget(max_serialized_chars=full + 1000)) is None
        assert "[dom budget:" not in state.serialized_dom

    async def test_a_pass_is_one_shot(self) -> None:
        """Counters describe one capture; reusing a pass would blend two."""
        state = await _state()
        budget_pass = DOMBudgetPass(DOMBudget(max_elements=1))
        budget_pass.apply(state, TextSerializer().serialize)
        with pytest.raises(RuntimeError, match="one-shot"):
            budget_pass.apply(state, TextSerializer().serialize)


# ── 4. through the real resolver, onto the real fragment ─────────────


class _PageCDP:
    """CDP stub that answers exactly the calls one DOM capture makes."""

    def __init__(self, capture: Any) -> None:
        self.is_connected = True
        self._capture = capture

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        if method == "DOM.getDocument":
            return self._capture.raw_dom
        if method == "DOMSnapshot.captureSnapshot":
            return self._capture.snapshot
        if method == "Accessibility.getFullAXTree":
            return self._capture.ax_tree
        if method == "Runtime.evaluate":
            return {"result": {"value": 1.0}}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None


async def _resolve_dom(**kwargs: Any) -> Any:
    watchdog = DOMWatchdog(_PageCDP(_page()), viewport=_VIEWPORT, **kwargs)
    summary = await watchdog._on_browser_state_request(
        BrowserStateRequestEvent(fragments=frozenset({"browser.dom"}))
    )
    assert summary is not None
    return summary.fragments["browser.dom"]


class TestTheFragmentCarriesTheBudget:
    """``DOMObs.budget`` is the field a later reader actually has."""

    async def test_no_budget_means_no_field(self) -> None:
        fragment = await _resolve_dom()
        assert fragment.budget is None
        assert fragment.summary().endswith("interactive elements")

    async def test_the_cut_reaches_the_fragment_and_its_summary(self) -> None:
        fragment = await _resolve_dom(budget=DOMBudget(max_elements=1))
        assert len(fragment.selector_map) == 1
        assert fragment.budget is not None
        assert fragment.budget.applied == ["max_elements"]
        assert "budget max_elements" in fragment.summary()

    async def test_the_cut_survives_serialization_to_the_trace(self) -> None:
        """The WAL writes JSON; the report has to be in it, not just in memory."""
        fragment = await _resolve_dom(budget=DOMBudget(max_elements=1))
        dumped = fragment.model_dump(mode="json")
        assert dumped["budget"]["applied"] == ["max_elements"]
        assert dumped["budget"]["elements_after"] == 1

    async def test_the_watchdog_serializes_with_the_instance_it_was_given(self) -> None:
        fragment = await _resolve_dom(serializer=TextSerializer(max_text_length=15))
        long_run = await _resolve_dom(serializer=TextSerializer(max_text_length=400))
        assert len(fragment.element_tree.serialized_dom) < len(long_run.element_tree.serialized_dom)

    async def test_a_budget_cut_does_not_make_an_element_new_next_step(self) -> None:
        """``*`` means "not on the page last step", not "not paid for last step".

        New-node tracking compares against the *pre*-budget map. Against
        the post-budget one, every element a ``dom_viewport_only`` run
        scrolled away from and back to would be marked new, and the
        marker exists precisely so the model can trust that a ``*`` is a
        page change worth reacting to.
        """
        watchdog = DOMWatchdog(
            _PageCDP(_page()), viewport=_VIEWPORT, budget=DOMBudget(max_elements=1)
        )
        event = BrowserStateRequestEvent(fragments=frozenset({"browser.dom"}))

        first = await watchdog._on_browser_state_request(event)
        assert first is not None and len(first.fragments["browser.dom"].selector_map) == 1

        second = await watchdog._on_browser_state_request(event)
        assert second is not None
        tree = second.fragments["browser.dom"].element_tree.dom_tree
        assert tree is not None
        fresh = [n.attributes.get("id") for n in tree.descendants() if n.is_new]
        assert not fresh, (
            f"nothing changed on the page, yet {fresh} came back marked new — "
            "new-node tracking is reading the budgeted map"
        )


@pytest.mark.parametrize(
    "knobs",
    [
        {"dom_max_elements": 5},
        {"dom_max_serialized_chars": 1000},
        {"dom_interactive_only": True},
        {"dom_viewport_only": True},
    ],
)
def test_every_knob_reaches_the_resolver_from_yaml(knobs: dict[str, Any]) -> None:
    """The config surface is scalars on the capability, like the screenshot half."""
    cap = _compose(**knobs)
    cap._cdp = object()  # type: ignore[assignment]
    cap._input_platform = "macos"  # This fixture begins after setup's measurement.
    ctx = cap._watchdog_context("sid1")
    watchdog = next(
        wd
        for wd in cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid1")
        if isinstance(wd, DOMWatchdog)
    )
    assert ctx.dom_budget == cap.dom_budget
    assert watchdog._budget == cap.dom_budget
    assert not cap.dom_budget.is_open


def test_a_nonsense_budget_fails_validation_not_the_run() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="dom_max_elements"):
        _compose(dom_max_elements=0)


@pytest.mark.parametrize("knob", ["dom_max_elements", "dom_max_serialized_chars"])
def test_no_cap_is_written_unlimited_and_is_the_default(knob: str) -> None:
    """A numeric cap never uses null (§4.2.1): ``unlimited`` is the shown default."""
    from pydantic import ValidationError

    assert getattr(_compose(), knob) == "unlimited"
    cap = _compose(**{knob: "unlimited"})
    assert cap.dom_budget.is_open
    with pytest.raises(ValidationError, match=knob):
        _compose(**{knob: None})
