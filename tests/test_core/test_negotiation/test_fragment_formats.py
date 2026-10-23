"""Which fragment format descriptions an agent's prompt quotes, resolved for a run.

``docs/architecture_design.md`` §4.4 "观测格式由提供方描述": an agent names the
fragments whose format it quotes (``__fragment_formats__``, in its wiring's
own names) and receives their descriptions through ``prepare(...,
fragment_formats=...)``. The description comes from whoever provides the
fragment, once per run. A quoted fragment that is active but undescribed is
refused before the run, never sent out as a prompt without its format.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import pytest

from spectragent.core.negotiation import FragmentFormatError, quoted_fragment_formats
from spectragent.core.negotiation.observation import ObservationNegotiationResult


class _Quiet:
    """Declares no quoted formats: the runtime passes it nothing (UI-TARS's shape)."""

    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot", "task"], ["dom"]),
    }

    def prepare(self, task: str, action_space: Any) -> list[Any]:
        return []


class _Quoting(_Quiet):
    __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"dom"})

    def prepare(  # type: ignore[override]
        self, task: str, action_space: Any, fragment_formats: Mapping[str, str]
    ) -> list[Any]:
        return []


class _Env:
    """Describes what it is given, and counts how often it is asked."""

    def __init__(self, formats: dict[str, str]) -> None:
        self.formats = formats
        self.asked = 0

    def fragment_formats(self) -> dict[str, str]:
        self.asked += 1
        return dict(self.formats)


def _negotiated(*active: str) -> ObservationNegotiationResult:
    keys = frozenset(active)
    return ObservationNegotiationResult(
        active_fragments=keys,
        env_sourced=frozenset(k for k in keys if not k.startswith("loop.")),
        loop_sourced=frozenset(k for k in keys if k.startswith("loop.")),
        fragment_bindings={key.partition(".")[2]: key for key in keys},
    )


def test_an_agent_that_declares_nothing_is_handed_nothing() -> None:
    env = _Env({"browser.dom": "lines"})
    assert quoted_fragment_formats(_Quiet(), _negotiated("browser.dom"), env) is None
    assert env.asked == 0


def test_an_active_quoted_fragment_gets_its_provider_s_words() -> None:
    env = _Env({"browser.dom": "[42]<button /> is element 42", "browser.other": "x"})
    formats = quoted_fragment_formats(
        _Quoting(), _negotiated("browser.dom", "browser.screenshot"), env
    )
    assert formats == {"dom": "[42]<button /> is element 42"}
    with pytest.raises(TypeError):
        formats["dom"] = "rewritten"  # type: ignore[index]


def test_an_inactive_quoted_fragment_has_no_entry_and_nothing_is_asked() -> None:
    """A vision-only run never sees the DOM, so it is told nothing about its format."""
    env = _Env({})
    assert quoted_fragment_formats(_Quoting(), _negotiated("browser.screenshot"), env) == {}
    assert env.asked == 0


def test_an_active_quoted_fragment_nobody_describes_is_refused() -> None:
    with pytest.raises(FragmentFormatError, match="'browser.dom' comes from 'browser'"):
        quoted_fragment_formats(_Quoting(), _negotiated("browser.dom"), _Env({}))


def test_a_fully_qualified_declaration_is_looked_up_as_written() -> None:
    class Pinned(_Quoting):
        __wiring__: ClassVar[dict[str, Any]] = {
            "observation": ("observation", ["browser.dom"], []),
        }
        __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"browser.dom"})

    negotiated = ObservationNegotiationResult(
        active_fragments=frozenset({"browser.dom"}),
        env_sourced=frozenset({"browser.dom"}),
        loop_sourced=frozenset(),
    )
    formats = quoted_fragment_formats(Pinned(), negotiated, _Env({"browser.dom": "lines"}))
    assert formats == {"browser.dom": "lines"}


def test_a_name_the_wiring_does_not_declare_is_refused() -> None:
    class Stray(_Quoting):
        __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"terminal"})

    with pytest.raises(FragmentFormatError, match="does not declare"):
        quoted_fragment_formats(Stray(), _negotiated("browser.dom"), _Env({}))


def test_the_declaration_must_be_a_set_of_names() -> None:
    class Listed(_Quoting):
        __fragment_formats__ = ["dom"]  # type: ignore[assignment]

    with pytest.raises(FragmentFormatError, match="must be a set"):
        quoted_fragment_formats(Listed(), _negotiated("browser.dom"), _Env({}))


@pytest.mark.parametrize("signature", ["missing", "catch_all"])
def test_prepare_must_name_the_keyword(signature: str) -> None:
    """``**kwargs`` cannot stand in for the parameter, as for execution limits."""

    class Narrow(_Quiet):
        __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"dom"})

        if signature == "catch_all":

            def prepare(self, task: str, action_space: Any, **kwargs: Any) -> list[Any]:  # type: ignore[override]
                return []

    with pytest.raises(FragmentFormatError, match="explicitly accepts fragment_formats"):
        quoted_fragment_formats(
            Narrow(), _negotiated("browser.dom"), _Env({"browser.dom": "lines"})
        )


def test_an_env_less_run_cannot_describe_an_active_loop_fragment() -> None:
    class LoopQuoting(_Quoting):
        __wiring__: ClassVar[dict[str, Any]] = {"observation": ("observation", ["task"], [])}
        __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"task"})

    with pytest.raises(FragmentFormatError, match="describes no format"):
        quoted_fragment_formats(LoopQuoting(), _negotiated("loop.task"), None)
