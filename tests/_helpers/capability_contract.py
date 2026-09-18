"""The observation-cycle contract every Capability has to satisfy.

Six capabilities are planned (browser, docker, tool_sandbox, desktop_vm,
android, gpu_profiling) and exactly one exists. The cycle rule — one
``observe()`` is one cycle; every fragment of a cycle comes from one
capture; the next cycle re-captures — used to live only inside
``BrowserCapability``, as a docstring plus a "have I served this key
before" guess. Anyone writing the second capability would have had to read
that file and reinvent it, and the failure mode when they got it wrong is
a trace that lies: an agent shown a screenshot from before the click it is
reacting to, with nothing on disk to say so.

So the rule lives here, as assertions, and every capability is run through
them. A new capability author writes a :class:`CaptureHarness` factory,
adds it to :data:`HARNESS_FACTORIES`, and inherits the whole suite —
``tests/test_core/test_capabilities/test_capability_contract.py`` fails
until they do.

The contract itself is stated on
:meth:`spectragent.core.capabilities.base.BaseCapability.begin_observation`.
What is pinned here:

1. Two resolves of one fragment inside a cycle hit **one** capture.
2. Every fragment of one cycle comes from that same capture.
3. A new cycle **re-captures**, even for a fragment nobody asked for
   twice — ``begin_observation`` drops the previous capture rather than
   ageing individual keys out of it.
4. Every fragment carries the time it was captured.
5. A cycle whose capture **failed** costs one capture too. The
   environment isolates failures per fragment, so it goes on asking the
   same capability for the rest of its slice; a capability that re-enters
   its capture (and its retry budget) for each of them turns one bad step
   into N full round trips.
6. The capability states, rather than implies, whether a foreign
   capability's action invalidates it.

Clauses 1 and 5 assume a capability whose fragments **share** a capture,
which is what this layer is for — the whole point of announcing the cycle
is that one round trip can answer several fragments. A capability that
genuinely captures per fragment does not fit the suite as written, and
should come here to change it rather than quietly reporting one capture
per resolve.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.base import BaseCapability

# Imported for its side effect of registering the capability, so
# ``CapabilityRegistrar.get_all()`` sees it in the completeness guard.
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.observations import DOMObs, ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs.events import BrowserStateSummary


@dataclass
class CaptureHarness:
    """One capability, ready to observe, with its captures countable.

    Attributes:
        capability: The instance under contract. Already "set up" as far
            as resolving goes — no network, no container, no browser: a
            contract test must be runnable by anyone, on any machine, in
            milliseconds, or it stops being run.
        fragments: At least two fragment keys this capability serves
            **from one capture**.
        captures: How many real captures (round trips to the underlying
            world) have happened so far. For a capability that retries
            internally, count *attempts* — the clause about failed cycles
            is precisely about a retry budget being spent more than once.
        break_capture: Make every capture from now on fail. Required, not
            optional: what a capability does on its bad day is half of
            this contract, and a harness that cannot produce one cannot
            check it. Break the *capture*, not ``begin_observation`` —
            failing to open a cycle is a different clause, checked at the
            environment level.
    """

    capability: BaseCapability
    fragments: tuple[str, ...]
    captures: Callable[[], int]
    break_capture: Callable[[], None]

    def __post_init__(self) -> None:
        if len(self.fragments) < 2:
            raise ValueError(
                "A capture harness needs at least two fragment keys — the "
                "contract is about what several fragments of one cycle share."
            )


# ── The contract, as assertions ───────────────────────────────────────


async def assert_one_cycle_is_one_capture(harness: CaptureHarness) -> None:
    """Every fragment of one cycle comes from one capture, however often read."""
    cap = harness.capability
    await cap.begin_observation(1)

    first = {key: await cap.resolve_fragment(key) for key in harness.fragments}
    again = {key: await cap.resolve_fragment(key) for key in harness.fragments}

    assert harness.captures() == 1, (
        f"{cap.capability_id}: reading {len(harness.fragments)} fragments twice "
        f"inside one cycle cost {harness.captures()} captures. One cycle is one "
        f"capture — a second read is the same observation, not the next one."
    )
    for key in harness.fragments:
        assert first[key] is again[key], (
            f"{cap.capability_id}: {key!r} was rebuilt inside one cycle. The "
            f"agent would be shown two different worlds in one observation."
        )


async def assert_next_cycle_resamples(harness: CaptureHarness) -> None:
    """Crossing a cycle boundary re-captures — no fragment survives it."""
    cap = harness.capability
    await cap.begin_observation(1)
    before = {key: await cap.resolve_fragment(key) for key in harness.fragments}

    await cap.begin_observation(2)
    after = {key: await cap.resolve_fragment(key) for key in harness.fragments}

    assert harness.captures() == 2, (
        f"{cap.capability_id}: the second cycle cost {harness.captures() - 1} "
        f"extra capture(s), expected 1. Serving a fragment across a cycle "
        f"boundary hands the agent stale data as current."
    )
    for key in harness.fragments:
        assert before[key] is not after[key], (
            f"{cap.capability_id}: {key!r} came back from the previous cycle's "
            f"capture."
        )


async def assert_begin_observation_clears_the_cache(harness: CaptureHarness) -> None:
    """The boundary is the announcement, not "which keys I already served".

    The discriminating case, and the reason this clause is not a rerun of
    :func:`assert_next_cycle_resamples`: cycle 1 reads **only** the first
    fragment, cycle 2 reads **only** the second. A capability that
    invalidates on "I have handed this key out before" — the heuristic
    this contract replaced — sees a key it has never served, answers it
    from cycle 1's capture, and hands the agent a screenshot taken before
    the click it is reacting to. Announcing the cycle has to be enough on
    its own.
    """
    cap = harness.capability
    first_key, second_key = harness.fragments[0], harness.fragments[1]

    await cap.begin_observation(1)
    stale = await cap.resolve_fragment(first_key)

    await cap.begin_observation(2)
    # A key this capability has never been asked for. Nothing about the
    # *read* history says it is stale; only the announcement does.
    await cap.resolve_fragment(second_key)

    assert harness.captures() == 2, (
        f"{cap.capability_id}: begin_observation(2) did not drop cycle 1's "
        f"capture — {second_key!r} was served out of it because it had not "
        f"been asked for before. The cycle boundary is the caller's "
        f"announcement, not this capability's read history."
    )
    fresh = await cap.resolve_fragment(first_key)
    assert harness.captures() == 2, (
        f"{cap.capability_id}: re-reading {first_key!r} inside cycle 2 started "
        f"a third capture."
    )
    assert stale is not fresh, (
        f"{cap.capability_id}: {first_key!r} is still the object captured in "
        f"cycle 1."
    )


async def assert_fragments_carry_capture_time(
    harness: CaptureHarness, *, now: Callable[[], float]
) -> None:
    """Each fragment says when it was captured, so skew is recoverable.

    Capabilities are resolved concurrently, so one observation holds
    samples taken at different instants. Without this the skew is
    invisible at runtime *and* absent from the trace, and an evaluator
    replaying the run cannot tell how far apart the halves were.
    """
    cap = harness.capability
    started = now()
    await cap.begin_observation(1)
    fragments = [await cap.resolve_fragment(key) for key in harness.fragments]
    finished = now()

    for key, fragment in zip(harness.fragments, fragments, strict=True):
        captured_at = getattr(fragment, "captured_at", None)
        assert isinstance(captured_at, float), (
            f"{cap.capability_id}: {key!r} carries no captured_at. Inherit "
            f"FragmentBase rather than declaring the field yourself."
        )
        assert started <= captured_at <= finished, (
            f"{cap.capability_id}: {key!r} claims to have been captured at "
            f"{captured_at}, outside the window [{started}, {finished}] in "
            f"which it was actually resolved."
        )


async def assert_a_failed_cycle_costs_one_capture(harness: CaptureHarness) -> None:
    """A bad cycle costs one capture, not one per fragment asked for.

    The asymmetry this catches is easy to miss because each half is
    reasonable on its own. ``Environment.observe`` isolates failures per
    *fragment* — right for a capability that captures one at a time, and
    it keeps a peer's good capture when one fragment dies. But it means
    that after the first key of a slice raises, the environment asks the
    same capability for the next one. A capability whose capture is
    shared, and which does not remember that this cycle's capture already
    failed, re-enters it — retry budget and all — once per fragment. The
    recorded case: a dom + screenshot + meta run spent six full DOM
    extractions and six overlay toggles failing a single observation, on
    exactly the path that was already slow because something was wrong.

    Measured as a ratio rather than an absolute, because how many attempts
    one capture is worth is the capability's business: a failed cycle that
    reads every fragment must cost what a failed cycle that reads one
    fragment costs.
    """
    cap = harness.capability
    harness.break_capture()

    await cap.begin_observation(1)
    start = harness.captures()
    for key in harness.fragments:
        await _expect_failure(harness, key)
    whole_slice = harness.captures() - start

    await cap.begin_observation(2)
    start = harness.captures()
    await _expect_failure(harness, harness.fragments[0])
    single = harness.captures() - start

    assert whole_slice == single, (
        f"{cap.capability_id}: failing a cycle that read "
        f"{len(harness.fragments)} fragments cost {whole_slice} capture(s); "
        f"failing one that read a single fragment cost {single}. The cost of a "
        f"failed observation is scaling with the number of fragments the agent "
        f"negotiated — record the failure on the cycle and refuse the rest of "
        f"the slice with it, the way BrowserCapability uses "
        f"_ObserveCycleCache.error."
    )


async def _expect_failure(harness: CaptureHarness, key: str) -> None:
    """Resolve *key* expecting a raise; fail the harness if it succeeds."""
    try:
        await harness.capability.resolve_fragment(key)
    except Exception:
        return
    raise AssertionError(
        f"{harness.capability.capability_id}: after break_capture(), "
        f"resolve_fragment({key!r}) returned instead of raising. Either the "
        f"harness does not really break the capture, or the capability served "
        f"a fragment it could not capture — which is the failure "
        f"FragmentFailure exists to make impossible."
    )


def assert_foreign_action_policy_is_declared(harness: CaptureHarness) -> None:
    """The capability answers "does a peer's action invalidate me?" itself.

    The default is ``False`` — capabilities are independent worlds, so a
    ``browser.click`` must not cost an Android capability an ADB round
    trip. What this pins is that the answer is a declared class attribute
    the environment can read, rather than an assumption the environment
    makes on the capability's behalf.
    """
    declared = getattr(
        type(harness.capability), "__invalidated_by_foreign_actions__", None
    )
    assert isinstance(declared, bool), (
        f"{harness.capability.capability_id}: "
        f"__invalidated_by_foreign_actions__ must be a bool ClassVar; it is "
        f"inherited as False from CapabilityBase, so this fails only if "
        f"something overrode it with a non-bool."
    )


# ── Harnesses, one per registered capability ──────────────────────────


class _CountingBrowserScope:
    """Stands in for the per-tab ``EventScope``, answering every request.

    One emission is one CDP round trip, which is what the contract counts.
    Each answer is distinguishable from the last so a fragment served
    across a cycle boundary is visible as such.
    """

    def __init__(self) -> None:
        self.emissions = 0
        self.broken = False

    def emit(self, event: Any) -> None:
        self.emissions += 1
        if self.broken:
            # A watchdog that is listening and produces nothing — the
            # shape of a lost DOM race, not of a dead connection.
            return
        tag = f"capture-{self.emissions}"
        event.record_result(
            connection_id=f"conn-{self.emissions}",
            handler_name="ContractWatchdog",
            result=BrowserStateSummary(
                fragments={
                    "browser.dom": DOMObs(element_tree=tag),
                    "browser.screenshot": ScreenshotObs(image_bytes=tag.encode()),
                }
            ),
        )


def browser_harness() -> CaptureHarness:
    """A ``BrowserCapability`` whose CDP round trips are counted, not made.

    Reaches into the capability's internals (``_scopes``,
    ``_default_session_id``) the same way ``test_browser_lazy_batch.py``
    does: a real ``setup()`` needs a Chrome, and a contract test that needs
    a Chrome is a contract test nobody runs.
    """
    cap = BrowserCapability()
    cap._default_session_id = "contract_sid"
    scope = _CountingBrowserScope()
    cap._scopes["contract_sid"] = scope  # type: ignore[assignment]
    cap._selector_maps["contract_sid"] = {}
    # What negotiation would have narrowed it to for this run.
    cap.apply_negotiation({"browser.dom", "browser.screenshot"})
    def break_capture() -> None:
        scope.broken = True

    return CaptureHarness(
        capability=cap,
        fragments=("browser.dom", "browser.screenshot"),
        # Emissions, not successful captures: one BrowserStateRequestEvent
        # is one CDP round trip whether or not a watchdog answered it, and
        # the failed-cycle clause is about how many of those a bad step
        # buys.
        captures=lambda: scope.emissions,
        break_capture=break_capture,
    )


#: capability_id → harness factory. **Add your capability here.**
#:
#: A fresh harness per test: these assertions drive cycles, and a shared
#: instance would carry one test's cycle number into the next.
HARNESS_FACTORIES: dict[str, Callable[[], CaptureHarness]] = {
    "browser": browser_harness,
}
