"""Instance-level narrowing: what a *configured* agent takes into negotiation.

``design_discussion.md`` §2.5 Q3 decided that a config may adjust the
required/optional sets an instance hands to negotiation. This is that
decision, implemented — and it is deliberately the *only* new degree of
freedom: the negotiation rules themselves (``_observation_rules``,
``_action_rules``) still resolve one ``(required, optional)`` pair and
cannot tell that a mode chose it. What they gained is a *label* for the
party that handed it over, used in one place — naming the requester when
a required key is missing — because "who required ``dom``" stopped being
answerable from the class the moment an instance could narrow.

The boundary is the point. A class's ``__wiring__`` is what conscribe
compiles into the generated config's ``Literal``, so:

- an instance may **promote** a declared optional key to required, or
  **exclude** one it will not use;
- it may not drop a key the class declared required — that would turn a
  loud negotiation failure into a silent degradation;
- it may not name a key outside the declaration — no YAML could have
  configured such a run, so it would not be reproducible from its config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from spectragent.core.negotiation import (
    DemandOutsideWiringError,
    bounded_demand,
    demand_of,
)


class _Declared:
    """A wiring-bearing stand-in — no registry, no metaclass, no run."""

    __registry_key__ = "declared"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot", "task"], ["dom", "step_info"]),
        "action": ("action", ["done"], ["click", "scroll"]),
    }


class _Narrowing(_Declared):
    """Promotes one optional and excludes another, like an observation mode."""

    __registry_key__ = "narrowing"

    def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
        if registry != "observation":
            return bounded_demand(self, registry, {"done"}, {"click", "scroll"})
        return bounded_demand(self, registry, {"screenshot", "task", "dom"}, {})


# ── The default: no narrowing at all ──────────────────────────────────


def test_an_object_without_negotiation_demand_falls_back_to_its_class() -> None:
    """Every test double in the tree relies on this, and so does UI-TARS."""
    assert demand_of(_Declared(), "observation") == (
        {"screenshot", "task"},
        {"dom", "step_info"},
    )


def test_a_class_passed_where_an_instance_belongs_still_reads_its_wiring() -> None:
    assert demand_of(_Declared, "action") == ({"done"}, {"click", "scroll"})


def test_an_unknown_registry_is_empty_rather_than_an_error() -> None:
    """A loop declaring no ``action`` wiring is not a mistake."""
    assert demand_of(_Declared(), "memory") == (set(), set())


# ── Narrowing inside the declared space ───────────────────────────────


def test_promoting_an_optional_key_makes_it_required() -> None:
    required, optional = demand_of(_Narrowing(), "observation")
    assert "dom" in required
    assert "dom" not in optional


def test_excluding_an_optional_key_removes_it_entirely() -> None:
    _required, optional = demand_of(_Narrowing(), "observation")
    assert "step_info" not in optional


def test_what_was_not_narrowed_is_unchanged() -> None:
    assert demand_of(_Narrowing(), "action") == ({"done"}, {"click", "scroll"})


# ── The boundary ──────────────────────────────────────────────────────


def test_dropping_a_class_required_key_is_refused() -> None:
    """The auto-downgrade this design exists to prevent.

    An instance that quietly stopped requiring ``screenshot`` would run
    against an environment that cannot provide one, and the failure would
    surface as an empty prompt rather than as a config error.
    """
    with pytest.raises(DemandOutsideWiringError) as exc:
        bounded_demand(_Declared(), "observation", {"task"}, {"dom"})
    assert "screenshot" in str(exc.value)


def test_a_key_outside_the_declaration_is_refused() -> None:
    """It could not have come from YAML: it is not in the generated Literal."""
    with pytest.raises(DemandOutsideWiringError) as exc:
        bounded_demand(_Declared(), "observation", {"screenshot", "task", "terminal"}, set())
    assert "terminal" in str(exc.value)


def test_an_undeclared_optional_key_is_refused_too() -> None:
    with pytest.raises(DemandOutsideWiringError):
        bounded_demand(_Declared(), "observation", {"screenshot", "task"}, {"accessibility_tree"})


def test_required_and_optional_may_not_overlap() -> None:
    with pytest.raises(DemandOutsideWiringError) as exc:
        bounded_demand(_Declared(), "observation", {"screenshot", "task", "dom"}, {"dom"})
    assert "both required and optional" in str(exc.value)


def test_the_error_names_the_instance_not_just_the_class() -> None:
    """``browser_use(observation_mode='vision')`` beats ``BrowserUseAgent``."""
    with pytest.raises(DemandOutsideWiringError) as exc:
        bounded_demand(_Declared(), "action", set(), set())
    assert exc.value.owner == "declared"
    assert exc.value.registry == "action"


def test_an_instance_that_describes_itself_is_named_by_that_description() -> None:
    """A ``repr`` starting with the registry key is used whole."""

    class _Configured(_Declared):
        __registry_key__ = "declared"

        def __repr__(self) -> str:
            return "declared(mode='vision')"

    with pytest.raises(DemandOutsideWiringError) as exc:
        bounded_demand(_Configured(), "action", {"terminal"}, set())
    assert exc.value.owner == "declared(mode='vision')"


def test_a_generated_repr_is_not_mistaken_for_a_description() -> None:
    """A dataclass repr names the *class* and dumps every field.

    Neither half helps: the reader cannot act on ``BrowserUseAgent(``, and
    the fields would put an LLM client into an error message.
    """

    @dataclass
    class _Dumped(_Declared):
        __registry_key__ = "declared"
        secret: str = "an-api-key"

    with pytest.raises(DemandOutsideWiringError) as exc:
        bounded_demand(_Dumped(), "action", {"terminal"}, set())
    assert exc.value.owner == "declared"
    assert "an-api-key" not in str(exc.value)


# ── The boundary is enforced, not merely documented ───────────────────


def test_an_override_that_skips_bounded_demand_is_still_bounded() -> None:
    """``demand_of`` re-checks whatever an override hands back.

    The documented rule is that ``negotiation_demand`` narrows inside the
    class declaration. A rule only its callers enforce is a rule that
    holds until someone writes a new agent, and this is the one new degree
    of freedom in the whole negotiation path — so the framework checks it
    rather than trusting each implementation to check itself.
    """

    class _Rogue(_Declared):
        __registry_key__ = "rogue"

        def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
            # Drops a class-required key *and* names an undeclared one.
            return {"terminal"}, set()

    with pytest.raises(DemandOutsideWiringError) as exc:
        demand_of(_Rogue(), "observation")
    assert "screenshot" in str(exc.value)
    assert "terminal" in str(exc.value)


def test_enforcing_twice_changes_nothing() -> None:
    """An implementation that already checks itself is not penalised."""
    assert demand_of(_Narrowing(), "observation") == (
        {"screenshot", "task", "dom"},
        set(),
    )


# ── The Loop's half is not narrowed ───────────────────────────────────


def test_only_the_agent_s_half_is_narrowed() -> None:
    """``negotiate_observation`` narrows the agent and reads the Loop's class.

    An agent cannot exclude a fragment the Loop needs to do its job: the
    Loop's declaration goes in untouched, so an agent in a mode that drops
    ``action_result`` still gets it because the Loop asked.
    """
    import spectragent.schemas.observation.fragments  # noqa: F401 — registers them
    from spectragent.core.negotiation.observation import negotiate_observation

    class _Loop:
        __registry_key__ = "stub_loop"
        __wiring__: ClassVar[dict[str, Any]] = {
            "observation": ("observation", ["action_result"], []),
        }
        # A structural Loop says which loop-source fragments it builds.
        __loop_fragments__ = frozenset({"loop.task", "loop.action_result"})

    class _Agent:
        __registry_key__ = "stub_agent"
        __wiring__: ClassVar[dict[str, Any]] = {
            "observation": ("observation", ["task"], ["action_result"]),
        }

        def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
            # Excludes exactly what the Loop requires.
            return bounded_demand(self, registry, {"task"}, set())

    class _Env:
        provided_fragments: frozenset[str] = frozenset()

    result = negotiate_observation(_Agent(), _Loop(), _Env())
    assert "loop.action_result" in result.active_fragments
    assert "loop.task" in result.active_fragments
