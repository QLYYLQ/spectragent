"""No registry key or option value is a word YAML 1.1 reads as a boolean or null.

Every entry point reads YAML with PyYAML's ``safe_load``, which follows YAML
1.1: a bare ``on`` / ``off`` / ``yes`` / ``no`` / ``true`` / ``false`` is a
boolean and ``null`` / ``~`` is null. A registry key or a closed-set option
spelled that way arrives as the wrong type and fails validation unless the
user remembers to quote it — the replayability tier used to be ``off`` and
every block wrote ``replayability_id: "off"``. "Not enabled" is spelled
``none`` instead (``docs/architecture_design.md`` §4.2.1), and this test keeps
every future name a plain word in YAML. The words are refused in any case,
whether or not PyYAML happens to read that casing as a boolean.

Scanned: the key of every registered class of every registrar declared under
``spectragent/`` (found by reading the source, so a registrar no test imports
is still covered), and every ``enum`` / ``const`` string in the JSON schema of
the composed run configuration and of the other two YAML documents (the
flywheel chain spec and a replay site rule).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from conscribe import discover, get_registry

from spectragent import PACKAGE_ROOT
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule
from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import collect_registrars
from spectragent.flywheel.chain import ChainSpec

#: What YAML 1.1 reads as a boolean or null, compared case-insensitively.
YAML_HAZARDS = frozenset({"on", "off", "yes", "no", "true", "false", "null", "~"})


def _is_hazard(value: str) -> bool:
    return value.lower() in YAML_HAZARDS


def _registrar_names() -> set[str]:
    """Every ``create_registrar("<name>", ...)`` under ``spectragent/``."""
    names: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "generated" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            first = node.args[0]
            if name == "create_registrar" and isinstance(first, ast.Constant):
                names.add(str(first.value))
    return names


def _registry_keys() -> dict[str, str]:
    """Registry key → registrar name, for every class ``spectragent`` registers."""
    for root in (
        "spectragent.agents",
        "spectragent.core",
        "spectragent.schemas",
        "spectragent.evaluator",
        "spectragent.export",
    ):
        discover(root)
    keys: dict[str, str] = {}
    for name in sorted(_registrar_names()):
        registry = get_registry(name)
        assert registry is not None, f"registrar {name!r} is declared but never created"
        for key, cls in registry.items():
            # The registries are process-wide: skip what a test registered.
            if cls.__module__.startswith("spectragent."):
                keys[key] = name
    return keys


def _schema_strings(node: Any) -> Iterator[str]:
    """Every string a schema allows as an ``enum`` member or a ``const``."""
    if isinstance(node, dict):
        for field, value in node.items():
            if field == "enum" and isinstance(value, list):
                yield from (item for item in value if isinstance(item, str))
            elif field == "const" and isinstance(value, str):
                yield value
            else:
                yield from _schema_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_strings(item)


def _option_values() -> set[str]:
    run = ConfigComposer(registrars=collect_registrars()).top_level_type
    values: set[str] = set()
    for model in (run, ChainSpec, SiteRule):
        values.update(_schema_strings(model.model_json_schema()))
    return values


@pytest.mark.parametrize(
    ("word", "expected"),
    [("off", True), ("Off", True), ("oFF", True), ("NULL", True), ("~", True), ("none", False)],
)
def test_the_rule_reads_every_casing(word: str, expected: bool) -> None:
    assert _is_hazard(word) is expected


def test_the_hazard_words_really_are_not_strings_to_yaml() -> None:
    """The premise, measured: PyYAML turns the lowercase spellings into bools and None."""
    for word in YAML_HAZARDS:
        assert not isinstance(yaml.safe_load(f"k: {word}")["k"], str), word
    assert yaml.safe_load("k: none")["k"] == "none"


def test_no_registry_key_is_a_yaml_hazard() -> None:
    keys = _registry_keys()
    assert "pinned" in keys and "live" in keys, "the scan found too few registries"
    assert {key: name for key, name in keys.items() if _is_hazard(key)} == {}


def test_no_option_value_is_a_yaml_hazard() -> None:
    values = _option_values()
    # A canary per document, so an empty scan cannot pass.
    assert {"check", "pinned", "rehearsal"} <= values
    assert sorted(value for value in values if _is_hazard(value)) == []
