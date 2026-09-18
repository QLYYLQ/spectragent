"""How a reader is supposed to tell a failure from a fragment.

The widening that puts ``Fragment | FragmentFailure`` in an optional slot
only helps if the code reading that slot survives the failure case, and
the obvious guard does not: the slot is never ``None``, and a
``FragmentFailure`` is a truthy model, so ``if value is not None`` passes
and the *next* line — the first field access — is where it breaks.

That is a documentation problem with a code half: pydantic's own message
(*'FragmentFailure' object has no attribute 'element_tree'*) names the
symptom, on the rare path, in a run that has already gone wrong once.
These tests pin both halves — the guard that works, and the message the
wrong guard gets.
"""

from __future__ import annotations

import copy

import pytest

from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.failure import FragmentFailure


def _failure() -> FragmentFailure:
    return FragmentFailure.from_exception(
        "browser.dom", "browser", RuntimeError("scrape lost the race")
    )


def _read_element_tree(fragment: object) -> object:
    """What a prompt builder does after an ``is not None`` guard passes."""
    return fragment.element_tree  # type: ignore[attr-defined]


class TestTheGuardThatDoesNotWork:
    """Why ``is not None`` is the wrong test, stated as assertions."""

    def test_a_failure_is_neither_none_nor_falsy(self) -> None:
        failure = _failure()
        assert failure is not None
        assert bool(failure) is True, (
            "If this ever became falsy, `if value:` would start working and "
            "`if value is not None:` would still not — a half-fix is worse "
            "than the honest crash."
        )

    def test_reading_a_fragment_field_explains_the_mistake(self) -> None:
        with pytest.raises(AttributeError) as caught:
            _read_element_tree(_failure())

        message = str(caught.value)
        assert "element_tree" in message
        assert "browser.dom" in message  # which slot
        assert "browser" in message  # who owed it
        assert "lost the race" in message  # and why it is not there
        assert "isinstance" in message  # the guard that does work

    def test_it_is_still_an_attribute_error(self) -> None:
        """``getattr``-with-default and ``hasattr`` must keep working.

        A reader that already defaults per field degrades instead of
        crashing. That is not the recommended pattern — a defaulted
        ``url=""`` is the framework inventing data — but breaking it would
        turn this helpful message into a new failure mode of its own.
        """
        failure = _failure()
        assert getattr(failure, "url", "") == ""
        assert hasattr(failure, "url") is False


class TestTheGuardThatWorks:
    def test_isinstance_fragmentbase_separates_data_from_failure(self) -> None:
        assert not isinstance(_failure(), FragmentBase), (
            "FragmentFailure must never become a FragmentBase subclass: that "
            "isinstance check is the documented way to ask 'is this real "
            "data', and the trace writer's binary path relies on it too."
        )


class TestTheMessageDoesNotBreakTheModel:
    """``__getattr__`` on a pydantic model is easy to get subtly wrong."""

    def test_dunder_probes_fall_through(self) -> None:
        """``copy.deepcopy`` asks for ``__deepcopy__`` with a plain getattr.

        If the explanatory error answered that probe, deepcopy would break
        on every failure the trace tried to snapshot.
        """
        assert copy.deepcopy(_failure()).fragment_id == "browser.dom"

    def test_the_model_still_round_trips(self) -> None:
        failure = _failure()
        back = FragmentFailure.model_validate_json(failure.model_dump_json())
        assert back.error == "RuntimeError"
        assert back.capability == "browser"
        assert back.captured_at == failure.captured_at

    def test_declared_fields_are_untouched(self) -> None:
        failure = _failure()
        assert failure.error == "RuntimeError"
        assert failure.summary() == "FAILED(RuntimeError)"
