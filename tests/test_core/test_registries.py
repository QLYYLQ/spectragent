"""The registrar catalog: the framework's axes, then what each capability package declares.

``collect_registrars()`` is the one list of YAML-configurable layers that
composition, inspection and generation share (``docs/architecture_design.md``
§4.2.1). The framework lists its own axes; every subpackage of
``spectragent/core/capabilities/`` declares its own in ``declarations.py``,
so adding a capability edits no framework file. These tests pin what the
catalog holds against what the source defines, read statically (as
``tests/test_docs/test_registrar_coverage.py`` reads it), so a registrar no
test imports is still covered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conscribe import get_registry

from spectragent import PACKAGE_ROOT
from spectragent.agents._registrar import AgentRegistrar
from spectragent.core.capabilities.browser import declarations as browser_declarations
from spectragent.core.capabilities.browser.input.rhythm import TypingRhythmRegistrar
from spectragent.core.registries import (
    capability_declarations,
    collect_registrars,
)

#: Registrars YAML never selects: registered by import, never a config layer.
NOT_CONFIG_LAYERS = frozenset({"observation", "negotiation_result", "task_setup"})

CAPABILITIES_DIR = PACKAGE_ROOT / "core" / "capabilities"


def _defined_registrars() -> dict[str, Path]:
    """Every ``create_registrar("<name>", ...)`` under ``spectragent/``, by name."""
    found: dict[str, Path] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "generated" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            first = node.args[0]
            if name == "create_registrar" and isinstance(first, ast.Constant):
                found[str(first.value)] = path
    return found


def _capability_of(path: Path) -> str | None:
    """The capability package *path* belongs to, or ``None`` for framework code."""
    if not path.is_relative_to(CAPABILITIES_DIR):
        return None
    parts = path.relative_to(CAPABILITIES_DIR).parts
    return parts[0] if len(parts) > 1 else None


DEFINED = _defined_registrars()


def test_the_scan_found_registrars() -> None:
    # Positive control: an empty scan would let every check below pass.
    assert {"agent", "capability", "mouse_trajectory", "task_setup"} <= set(DEFINED)


def test_the_catalog_is_the_framework_axes_then_each_capability_packages_axes() -> None:
    collected = list(collect_registrars())
    declared = [
        layer for capability in capability_declarations() for layer in capability.strategy_axes
    ]
    framework = [layer for layer in collected if layer not in declared]
    assert collected == framework + declared
    assert sorted(framework) == sorted(
        name
        for name, path in DEFINED.items()
        if _capability_of(path) is None and name not in NOT_CONFIG_LAYERS
    )


def test_each_capability_package_declares_every_registrar_it_defines() -> None:
    for capability in capability_declarations():
        defined = {
            name
            for name, path in DEFINED.items()
            if _capability_of(path) == capability.capability_id
        }
        assert set(capability.strategy_axes) == defined - NOT_CONFIG_LAYERS, (
            f"{capability.capability_id}'s declarations.py and the registrars its package "
            f"defines disagree"
        )


def test_nothing_collected_is_not_a_config_layer() -> None:
    collected = collect_registrars()
    assert not NOT_CONFIG_LAYERS & set(collected)
    assert set(collected) <= set(DEFINED)


def test_each_layer_name_is_its_registrars_own_name() -> None:
    for layer, registrar in collect_registrars().items():
        registry = get_registry(layer)
        assert registry is not None and registry.protocol is registrar.protocol, layer


def test_every_call_returns_a_fresh_catalog_in_the_same_order() -> None:
    first, second = collect_registrars(), collect_registrars()
    assert first is not second
    assert list(first) == list(second)


def test_a_mislabelled_layer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    axes = {**browser_declarations.STRATEGY_AXES, "mouse_trajectory": TypingRhythmRegistrar}
    monkeypatch.setattr(browser_declarations, "STRATEGY_AXES", axes)
    with pytest.raises(ValueError, match="'mouse_trajectory'.*create_registrar name"):
        collect_registrars()


def test_a_layer_two_owners_declare_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    axes = {**browser_declarations.STRATEGY_AXES, "agent": AgentRegistrar}
    monkeypatch.setattr(browser_declarations, "STRATEGY_AXES", axes)
    with pytest.raises(ValueError, match="'agent'"):
        collect_registrars()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STRATEGY_AXES", ["mouse_trajectory"]),
        ("TASK_SETUP", None),
        # A capability may only keep its own modules out of discovery.
        ("OPTIONAL_EXTRA_MODULES", ("spectragent.core.registries",)),
    ],
)
def test_a_malformed_declaration_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, value: object
) -> None:
    monkeypatch.setattr(browser_declarations, name, value)
    with pytest.raises(TypeError, match=name):
        capability_declarations()
