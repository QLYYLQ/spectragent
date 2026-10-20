"""The platform-neutral capture state has one writing: ``render_capture_state``.

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``". Four
facts — settle, foreground change, pointer, failed visible resources — each in
every state its schema allows, in words that name no platform. "Unknown" and
"unavailable" are said as such; a measured zero says none.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar, Literal

import pytest

from spectragent.agents._capture_state import render_capture_state
from spectragent.schemas.observation.capture_state import (
    CaptureStateFragment,
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
    SettleState,
)
from spectragent.schemas.observation.failure import FragmentFailure

_SETTLED = SettleState(outcome="settled", waited_ms=640)
_KNOWN = PointerState(status="known", x=412, y=230)
_NONE_FAILED = FailedVisibleResources(status="measured", count=0)


def _state(
    *,
    settle: SettleState | None = _SETTLED,
    foreground: ForegroundChange | None = None,
    pointer: PointerState = _KNOWN,
    failed: FailedVisibleResources = _NONE_FAILED,
) -> Any:
    """A plain object with the fragment's four facts — structural reads need no more."""
    return SimpleNamespace(
        settle_state=settle,
        foreground_change=foreground,
        pointer=pointer,
        failed_visible_resources=failed,
    )


def _render(**facts: Any) -> str:
    return render_capture_state(SimpleNamespace(capture_state=_state(**facts)))


def _line(text: str, label: str) -> str:
    (line,) = [line for line in text.splitlines() if line.startswith(f"{label}: ")]
    return line.removeprefix(f"{label}: ")


def test_no_fragment_renders_nothing() -> None:
    assert render_capture_state(SimpleNamespace()) == ""
    assert render_capture_state(SimpleNamespace(capture_state=None)) == ""


def test_a_failed_capture_says_the_state_could_not_be_measured() -> None:
    failure = FragmentFailure(
        fragment_id="browser.capture_state", capability="browser", error="TimeoutError"
    )
    assert render_capture_state(SimpleNamespace(capture_state=failure)) == (
        "<capture_state>\n"
        "The capture state could not be measured this step: settle, foreground change, "
        "pointer and failed visible resources are all unknown.\n"
        "</capture_state>"
    )


def test_the_whole_block() -> None:
    """The exact block: four labelled lines, in this order."""
    assert _render() == (
        "<capture_state>\n"
        "Settle: settled after waiting 640 ms.\n"
        "Foreground change: none.\n"
        "Pointer: (412, 230) in viewport pixels.\n"
        "Failed visible resources: none failed.\n"
        "</capture_state>"
    )


@pytest.mark.parametrize(
    ("settle", "expected"),
    [
        (None, "no wait before this capture."),
        (SettleState(outcome="settled", waited_ms=0), "settled after waiting 0 ms."),
        (
            SettleState(outcome="capped", waited_ms=3000, pending=("network", "layout")),
            "capped: the wait ended at its limit after 3000 ms; still pending: network, layout.",
        ),
        (
            SettleState(outcome="capped", waited_ms=3000),
            "capped: the wait ended at its limit after 3000 ms.",
        ),
        (SettleState(outcome="unknown", waited_ms=120), "unknown after waiting 120 ms."),
    ],
)
def test_settle(settle: SettleState | None, expected: str) -> None:
    assert _line(_render(settle=settle), "Settle") == expected


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (None, "none."),
        (
            ForegroundChange(reason="opened", from_surface="A", to_surface="B"),
            "the environment opened a new surface and the capture shows it.",
        ),
        (
            ForegroundChange(reason="closed", from_surface="B", to_surface="A"),
            "the focused surface closed itself and the capture shows another.",
        ),
    ],
)
def test_foreground_change(change: ForegroundChange | None, expected: str) -> None:
    assert _line(_render(foreground=change), "Foreground change") == expected


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        (PointerState(status="known", x=0, y=0), "(0, 0) in viewport pixels."),
        (PointerState(status="unknown"), "unknown (no confirmed position)."),
        (PointerState(status="unavailable"), "no pointer on this platform."),
    ],
)
def test_pointer(pointer: PointerState, expected: str) -> None:
    assert _line(_render(pointer=pointer), "Pointer") == expected


@pytest.mark.parametrize(
    ("failed", "expected"),
    [
        (FailedVisibleResources(status="measured", count=0), "none failed."),
        (
            FailedVisibleResources(status="measured", count=2, identifiers=("hero.png", "a.svg")),
            "2 failed: hero.png, a.svg.",
        ),
        (
            FailedVisibleResources(status="measured", count=12, identifiers=("a.png", "b.png")),
            "12 failed; the first 2: a.png, b.png.",
        ),
        (FailedVisibleResources(status="measured", count=3), "3 failed."),
        (
            FailedVisibleResources(status="unknown", detail="probe ran out of its 1 s budget"),
            "unknown (probe ran out of its 1 s budget).",
        ),
        (FailedVisibleResources(status="unknown"), "unknown."),
        (FailedVisibleResources(status="unavailable"), "not measurable here."),
        (
            FailedVisibleResources(status="unavailable", detail="no image surface"),
            "not measurable here (no image surface).",
        ),
    ],
)
def test_failed_visible_resources(failed: FailedVisibleResources, expected: str) -> None:
    assert _line(_render(failed=failed), "Failed visible resources") == expected


def test_a_measured_zero_is_not_an_unmeasured_fact() -> None:
    measured = _render(failed=FailedVisibleResources(status="measured", count=0))
    unknown = _render(failed=FailedVisibleResources(status="unknown"))
    unavailable = _render(failed=FailedVisibleResources(status="unavailable"))
    assert len({measured, unknown, unavailable}) == 3
    assert "0" not in _line(unknown, "Failed visible resources")


def test_the_wording_names_no_platform() -> None:
    """Every state of every fact, with nothing a desktop or phone would not have."""
    renders = [
        _render(
            settle=settle,
            foreground=foreground,
            pointer=pointer,
            failed=failed,
        )
        for settle in (None, _SETTLED, SettleState(outcome="capped", waited_ms=1))
        for foreground in (
            None,
            ForegroundChange(reason="opened", from_surface="x", to_surface="y"),
            ForegroundChange(reason="closed", from_surface="y", to_surface="x"),
        )
        for pointer in (_KNOWN, PointerState(status="unavailable"))
        for failed in (_NONE_FAILED, FailedVisibleResources(status="unavailable"))
    ]
    for text in renders:
        lowered = text.lower()
        for word in ("page", "tab", "url", "browser", "document"):
            assert word not in lowered, (word, text)


def test_a_concrete_capability_fragment_renders_through_the_base_class() -> None:
    """A capability's subclass projects settle and focus through the two properties."""

    class ProbeCaptureState(CaptureStateFragment):
        __registry_key__ = "probe.capture_state"
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        waited: int

        @property
        def settle_state(self) -> SettleState | None:
            return SettleState(outcome="settled", waited_ms=self.waited)

        @property
        def foreground_change(self) -> ForegroundChange | None:
            return ForegroundChange(reason="opened", from_surface="1", to_surface="2")

    fragment = ProbeCaptureState(waited=90, pointer=_KNOWN, failed_visible_resources=_NONE_FAILED)
    text = render_capture_state(SimpleNamespace(capture_state=fragment))
    assert "Settle: settled after waiting 90 ms." in text
    assert "the environment opened a new surface" in text
