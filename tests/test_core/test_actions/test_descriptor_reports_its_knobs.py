"""A descriptor that states a default must state *this instance's* default.

Per-action knobs are YAML-reachable (``d156d54``), and the descriptor is
what the prompt is generated from — so a literal default baked into a
descriptor string teaches the model the value of whoever wrote the file,
not the value the run was configured with. ``TypeAction`` and
``TypeCoordinateAction`` both said ``"bool (optional, default true)"``
while ``clear_by_default: false`` sat in the capability's YAML.

This is a whole-registry tripwire rather than a per-action test: it walks
every registered action, finds every place a descriptor spells out a
default, and requires the descriptor to move when the field behind it
moves. Actions whose descriptor deliberately omits a knob
(``post_delay_ms`` is a settle detail, not a parameter) are not touched —
the rule only binds a descriptor to what it chose to say.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest

# Import for its registration side effect: the registry is the subject.
import spectragent.core.capabilities.browser.actions as _browser_actions  # noqa: F401
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.schemas.action import ActionDescriptor


def _descriptor_text(descriptor: ActionDescriptor) -> str:
    """Everything in a descriptor a prompt renderer can put in front of a model."""
    parts = [descriptor.description]
    parts += [f"{key}: {value}" for key, value in sorted(descriptor.params.items())]
    if descriptor.params_schema is not None:
        parts.append(repr(descriptor.params_schema))
    return " | ".join(parts)


def _render(value: Any) -> str:
    """How a default reads inside descriptor prose (``True`` → ``true``)."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _perturb(value: Any) -> Any | None:
    """A second, definitely different value of the same type."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, float):
        return value + 7.5
    if isinstance(value, str):
        return f"{value}_alt"
    return None


def _configurable_defaults(cls: type) -> list[tuple[str, Any]]:
    if not dataclasses.is_dataclass(cls):
        return []
    return [
        (f.name, f.default)
        for f in dataclasses.fields(cls)
        if f.default is not None and f.default is not dataclasses.MISSING
    ]


def _stated_defaults() -> list[tuple[str, str, Any]]:
    """Every ``(action_key, field, default)`` a descriptor spells out."""
    found: list[tuple[str, str, Any]] = []
    for key in sorted(ActionRegistrar.keys()):
        cls = ActionRegistrar.get(key)
        text = _descriptor_text(cls().descriptor())
        for name, default in _configurable_defaults(cls):
            if _perturb(default) is None:
                continue
            phrase = re.compile(rf"defaults?\b[\s=:]*{re.escape(_render(default))}", re.IGNORECASE)
            if phrase.search(text):
                found.append((key, name, default))
    return found


_STATED = _stated_defaults()


def test_some_descriptor_states_a_default() -> None:
    """Guard the guard: an empty list would make the case below vacuous."""
    assert _STATED, (
        "no descriptor spells out a configurable default — either the scan "
        "broke or every action stopped advertising its knobs"
    )


@pytest.mark.parametrize(
    ("action_key", "field_name", "default"),
    _STATED,
    ids=[f"{key}.{name}" for key, name, _ in _STATED],
)
def test_a_stated_default_tracks_the_instance(
    action_key: str, field_name: str, default: Any
) -> None:
    cls = ActionRegistrar.get(action_key)
    other = _perturb(default)

    stock = _descriptor_text(cls().descriptor())
    configured = _descriptor_text(cls(**{field_name: other}).descriptor())

    assert configured != stock, (
        f"{action_key}.descriptor() states a default for {field_name!r} but "
        f"returns the same text with {field_name}={other!r} — the literal is "
        f"hardcoded, so a run configured from YAML teaches the model the "
        f"wrong default"
    )
    assert _render(other) in configured, (
        f"{action_key}.descriptor() changed with {field_name}={other!r} but "
        f"does not report the configured value"
    )


def test_the_two_type_actions_report_the_clear_they_will_apply() -> None:
    """The instance of the rule that shipped, named so it cannot regress quietly.

    ``browser.type`` reaches a real prompt through BrowserUseAgent and
    Skyvern, which render every ``descriptor().params`` entry verbatim.
    """
    for key in ("browser.type", "browser.type_coordinate"):
        cls = ActionRegistrar.get(key)
        assert "true" in cls().descriptor().params["clear"]
        assert "false" in cls(clear_by_default=False).descriptor().params["clear"]
