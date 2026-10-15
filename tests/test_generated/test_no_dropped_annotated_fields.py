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

Two places declare a knob, and both are checked: class-level annotations
(dataclass fields, the action shape) and the parameters of a hand-written
``__init__`` (``BrowserCapability``, ``TraceWriter``, every archiver — the
shape a receptor owner must take). A hand-written ``__init__`` in a
``from __future__ import annotations`` module stores its parameter
annotations as strings too, and they resolve against that function's
globals — so the trap is the same, one level down.
"""

from __future__ import annotations

import pytest
from conscribe import ConfigAnnotationError, discover
from conscribe.config.extractor import extract_config_schema

from spectragent.core.registries import collect_registrars


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


def _declared_annotated_init_params(cls: type) -> dict[str, str]:
    """Parameters of a hand-written ``__init__`` whose raw annotation mentions ``Annotated``.

    Only an ``__init__`` written in a class body (with a real source file) —
    a dataclass-synthesised one restates the fields the class-level check
    already covers. Read off ``__annotations__`` raw, so a string annotation
    counts exactly like a resolved one.
    """
    for klass in cls.__mro__:
        init = vars(klass).get("__init__")
        if init is None:
            continue
        code = getattr(init, "__code__", None)
        if code is None or code.co_filename == "<string>":
            return {}
        declared: dict[str, str] = {}
        for name, annotation in getattr(init, "__annotations__", {}).items():
            if name in ("return", "self") or name.startswith("_"):
                continue
            text = annotation if isinstance(annotation, str) else repr(annotation)
            if "Annotated" in text:
                declared[name] = klass.__module__
        return declared
    return {}


def _registered_classes() -> list[tuple[str, str, type]]:
    discover("spectragent.agents")
    discover("spectragent.core")
    discover("spectragent.schemas")
    out: list[tuple[str, str, type]] = []
    for layer, registrar in collect_registrars().items():
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


@pytest.mark.parametrize(
    ("layer", "key", "cls"),
    _registered_classes(),
    ids=lambda v: v if isinstance(v, str) else getattr(v, "__name__", str(v)),
)
def test_hand_written_init_annotated_params_survive_extraction(
    layer: str, key: str, cls: type
) -> None:
    schema = extract_config_schema(cls)
    extracted = set(schema.model_fields) if schema is not None else set()
    dropped = {
        name: module
        for name, module in _declared_annotated_init_params(cls).items()
        if name not in extracted
    }
    assert not dropped, (
        f"{layer}/{key} ({cls.__name__}) declares Annotated __init__ parameter(s) that "
        f"conscribe dropped: {dropped}. The usual cause is ``from __future__ import "
        f"annotations`` in the module that defines the __init__ — remove it."
    )


def test_the_init_check_reads_hand_written_inits() -> None:
    """Positive control: the archivers and the trace hook are in scope, their knobs seen."""
    from spectragent.core.capabilities.browser.archive import NetworkArchiver
    from spectragent.core.hooks.trace_writer import TraceWriter

    assert {"max_inline_body_bytes", "allow_session_state"} <= set(
        _declared_annotated_init_params(NetworkArchiver)
    )
    assert set(_declared_annotated_init_params(TraceWriter)) == {
        "trace_dir",
        "record_secrets",
        "archive",
        "recording",
        "pending_segments",
        "write_retries",
        "retry_delay",
        "close_timeout",
    }
    layers = {layer for layer, _, _ in _registered_classes()}
    assert "archiver" in layers


def test_the_init_check_has_teeth() -> None:
    """A string annotation that cannot resolve is what conscribe drops — and what is caught."""

    class _Dropped:
        # Every registrar base the trap matters for sets this (archivers, hooks).
        __config_annotated_only__ = True

        def __init__(self, knob: "Annotated[int, Unresolvable(description='x')]" = 1) -> None:  # type: ignore[name-defined]  # noqa: F821, UP037
            self.knob = knob

    assert _declared_annotated_init_params(_Dropped) == {"knob": __name__}
    with pytest.raises(ConfigAnnotationError, match="Unresolvable"):
        extract_config_schema(_Dropped)
