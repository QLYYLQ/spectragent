"""Every ``Annotated`` knob a registered class declares must be configurable.

This is the package-wide tripwire for the PEP 563 trap documented on
``spectragent/evaluator/_registrar.py`` and
``spectragent/core/capabilities/browser/actions/_mixins.py``:

A base or mixin module that uses ``from __future__ import annotations``
stores its field annotations as *strings*. The dataclass-synthesised
``__init__`` of a subclass carries the **subclass module's**
``__globals__``, so resolving ``"Annotated[int, PydanticField(...)]"``
there fails unless that module happens to import ``PydanticField`` too.
conscribe then sees ``field_info is None`` and
``__config_annotated_only__`` drops the field — silently. The knob keeps
working in Python and becomes unreachable from YAML.

That is how six browser actions lost ``post_delay_ms`` while
``browser.navigate`` kept it (its own module imports ``PydanticField``),
and the failure is invisible to any test that exercises the class
directly. Only a cross-check between what a class *declares* and what
conscribe *extracts* catches it.
"""

from __future__ import annotations

import pytest
from conscribe import discover
from conscribe.config.extractor import extract_config_schema

from spectragent.scripts.generate_all import _collect_registrars


def _declared_annotated_fields(cls: type) -> dict[str, type]:
    """Public names whose raw annotation mentions ``Annotated``.

    Read straight off each ``__mro__`` entry's own ``__annotations__`` so
    a string annotation counts exactly like a resolved one — the whole
    point is to notice when the two disagree.
    """
    declared: dict[str, type] = {}
    for klass in reversed(cls.__mro__):
        for name, annotation in vars(klass).get("__annotations__", {}).items():
            if name.startswith("_"):
                continue
            text = annotation if isinstance(annotation, str) else repr(annotation)
            if "Annotated" in text:
                declared[name] = klass
    return declared


def _registered_classes() -> list[tuple[str, str, type]]:
    discover("spectragent.agents")
    discover("spectragent.core")
    discover("spectragent.schemas")
    out: list[tuple[str, str, type]] = []
    for layer, registrar in _collect_registrars().items():
        for key in sorted(registrar.keys()):
            out.append((layer, key, registrar.get(key)))
    return out


@pytest.mark.parametrize(
    ("layer", "key", "cls"),
    _registered_classes(),
    ids=lambda v: v if isinstance(v, str) else getattr(v, "__name__", str(v)),
)
def test_declared_annotated_fields_survive_extraction(layer: str, key: str, cls: type) -> None:
    schema = extract_config_schema(cls)
    extracted = set(schema.model_fields) if schema is not None else set()
    dropped = {
        name: owner.__module__
        for name, owner in _declared_annotated_fields(cls).items()
        if name not in extracted
    }
    assert not dropped, (
        f"{layer}/{key} ({cls.__name__}) declares Annotated field(s) that "
        f"conscribe dropped: {dropped}. The usual cause is ``from __future__ "
        f"import annotations`` in the module that declares them — remove it."
    )
