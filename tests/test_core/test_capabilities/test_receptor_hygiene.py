"""Structural guard: a strategy is a receptor, never a name someone looks up.

Three times now the same defect shipped, and each time it was fixed as a
point fix:

1. ``mouse_trajectory`` — a ``Literal`` plus an untyped overrides dict.
2. ``dom_cleaner`` — ``BrowserProfileConfig.cleaner``, looked up by
   ``DOMCleanerRegistrar`` in ``DOMWatchdog``.
3. ``dom_serializer`` — ``BrowserProfileConfig.serializer``, looked up by
   ``DOMSerializerRegistrar.get(name)()`` in ``DOMWatchdog``.

The symptom is identical every time and it is *silent*: the strategy's
config model is generated (``TextDom_SerializerConfig.max_text_length``
exists, ``BrowserUseDom_CleanerConfig.descendants_occlude`` exists), YAML
validates against it, the Composer builds a configured instance — and the
consumer default-constructs a second one, so the knob the user set is on
an object nobody runs. Nothing fails. Nothing warns. You find out by
measuring a run that ignored you.

Two point fixes did not stop a third, so this file states the rule
structurally instead: about *shape*, not about the three known names.

Rule 1 — **one place to choose.** Nothing a capability configures may
offer a second way to pick a strategy it already wires. Two surfaces are
checked, because the defect has lived on both kinds:

* every Pydantic model defined in the capability's own package —
  ``BrowserProfileConfig`` is where instances #2 and #3 lived;
* the capability's **own** ``__init__`` parameters — the surface that
  becomes its generated config, where ``screenshot_format`` and the four
  ``dom_*`` budget knobs live, and the most plausible home for the next
  one.

And two ways, because the historical defects would have slipped a plain
name comparison: ``BrowserProfileConfig`` called them ``cleaner`` and
``serializer`` while the receptors are ``dom_cleaner`` and
``dom_serializer``. So a field collides if its *name* matches a receptor
name or its trailing segment, **or** if its *value* is a registry key of
a registry the capability wires — a field defaulting to ``"text"`` beside
a ``dom_serializer`` receptor is picking a serializer whatever it is
called.

Rule 2 — **one place to look one up.** Under ``core/capabilities/``, a
``SomeRegistrar.get(key)`` / ``.get_or_none(key)`` / ``.children(prefix)``
call may appear only in the module that declares the capability class.
Registry-key → class is the *owner's* job (and the Composer's);
everything downstream — watchdogs, cleaners, serializers, actions —
receives instances. A resolver that resolves a name is a resolver holding
a default-constructed strategy.

**What these rules do not cover**, stated here so nobody reads a
guarantee that is not on offer:

* Rule 1's receptor list comes from ``__wiring__``, so a strategy that
  never got a receptor at all is invisible to it. The guard says "do not
  add a second way to choose"; it cannot say "this ought to have been a
  receptor".
* Rule 1 skips ``spectragent.generated`` by construction — that is the
  one place a receptor name is *supposed* to be a field.
* Rule 1's value check runs only against surfaces the capability
  actually configures (its ``__init__``, and models it holds or receives
  — see ``_configuration_models``). A ``Literal["text", "html"]`` on some
  CDP event model in the same package is not a strategy selector and is
  not flagged; the price is that a defect planted on a model the
  capability never touches is not caught either.
* Rule 2 exempts the capability's own module entirely — today that is
  exactly one file, ``browser/capability.py``, and the exempt set grows
  by one file per capability that registers. ``_build_wired_action``
  lives inside that exemption on purpose (see its test below), and it is
  the weakest joint here.
* Rule 2 flags only the *key-taking* lookups. ``get_all()``, ``keys()``
  and ``tree()`` take no key and are enumeration, which is how
  ``browser_watchdog_classes`` derives "one class per fragment" without
  ever naming a watchdog — the opposite of the defect.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

from conscribe.wiring import parse_wiring
from pydantic import BaseModel

import spectragent.core.capabilities as capabilities_pkg
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.scripts.generate_all import _collect_registrars

# Importing the concrete capability packages is what populates the
# registrar; without it this file would assert over an empty set and pass
# for the wrong reason.
import spectragent.core.capabilities.browser  # noqa: F401  isort:skip

CAPABILITIES_ROOT = Path(capabilities_pkg.__file__).resolve().parent

#: Registrar methods that turn a *name* into a class. ``get_all``,
#: ``keys`` and ``tree`` take no key — enumeration, not resolution — and
#: are deliberately absent.
LOOKUP_METHODS = frozenset({"get", "get_or_none", "children"})


def _capability_classes() -> list[type]:
    """Every *shipped* capability class, newest registration order aside.

    Filtered to ``spectragent.`` on purpose. Other test modules register
    throwaway capabilities into the same global registrar (``_AuditCap``,
    ``_Cap``, ...); they are fixtures, they own no config model, and
    walking their package would import the whole test tree for its side
    effects. The rule under test is about framework capabilities.
    """
    classes = [
        cls
        for key in sorted(CapabilityRegistrar.keys())
        if (cls := CapabilityRegistrar.get(key)) is not None
        and cls.__module__.startswith("spectragent.")
    ]
    assert classes, "no shipped capability is registered — this guard would pass vacuously"
    return classes


def _receptor_specs(cls: type) -> list[Any]:
    """``__wiring__`` entries the class can actually receive a value on.

    The same rule ``ConfigComposer._receptor_slots`` applies: an
    ``__init__`` parameter or a non-``ClassVar`` class-level annotation.
    A wiring entry with no slot (``observation`` on the capability) is a
    negotiation declaration, not a receptor, and a config field of that
    name would be a different conversation.
    """
    slots = _slot_names(cls)
    return [spec for spec in parse_wiring(cls) if spec.param_name in slots]


def _slot_names(cls: type) -> set[str]:
    try:
        params = set(inspect.signature(cls.__init__).parameters) - {"self"}
    except (TypeError, ValueError):  # pragma: no cover — defensive
        params = set()
    annotated: set[str] = set()
    for klass in cls.__mro__:
        for name, annotation in getattr(klass, "__annotations__", {}).items():
            text = annotation if isinstance(annotation, str) else str(annotation)
            if name.startswith("__") or text.startswith(("ClassVar", "typing.ClassVar")):
                continue
            annotated.add(name)
    return params | annotated


def _forbidden_names(spec: Any) -> set[str]:
    """Field names that would be a second way to choose this receptor.

    The receptor name and the registry name, each also without its leading
    qualifier: ``dom_serializer`` covers ``serializer``, ``dom_cleaner``
    covers ``cleaner``. That second form is not hypothetical — it is
    literally what ``BrowserProfileConfig`` called both of them.
    """
    names = {spec.param_name, spec.registry_name} - {""}
    return names | {name.split("_", 1)[1] for name in list(names) if "_" in name}


def _registry_keys(spec: Any, registrars: dict[str, Any]) -> frozenset[str]:
    registrar = registrars.get(spec.registry_name)
    if registrar is None:
        return frozenset()
    return frozenset(registrar.keys())


def _string_values(annotation: Any, default: Any) -> frozenset[str]:
    """String values a field can hold: its default, plus any ``Literal`` arm."""
    values: set[str] = set()
    if isinstance(default, str):
        values.add(default)
    for arg in (annotation, *get_args(annotation)):
        if get_origin(arg) is Literal:
            values.update(a for a in get_args(arg) if isinstance(a, str))
        else:
            for nested in get_args(arg):
                if get_origin(nested) is Literal:
                    values.update(a for a in get_args(nested) if isinstance(a, str))
    return frozenset(values)


@dataclass(frozen=True)
class Surface:
    """One configurable field, and whether its *value* is worth checking.

    ``check_values`` is off for a model the capability merely defines in
    its package and never reads. Its name is still checked — a field
    called ``serializer`` is never a coincidence — but its ``Literal``
    arms are not compared against registry keys as generic as ``text``,
    ``html`` or ``instant``, which is where a name-blind value check would
    start flagging a CDP event's ``format`` field.
    """

    where: str
    name: str
    values: frozenset[str] = frozenset()
    check_values: bool = False


def collisions(specs: list[Any], surfaces: list[Surface], registrars: dict[str, Any]) -> list[str]:
    """Every surface that offers a second way to choose one of ``specs``.

    Pure, and takes its inputs explicitly, so the positive control below
    can hand it a deliberately defective capability without registering
    one or walking a package.
    """
    found: list[str] = []
    for spec in specs:
        names = _forbidden_names(spec)
        keys = _registry_keys(spec, registrars)
        for surface in surfaces:
            if surface.name in names:
                found.append(f"{surface.where} (name shadows {spec.param_name!r})")
            elif surface.check_values and keys and surface.values & keys:
                hit = sorted(surface.values & keys)
                found.append(
                    f"{surface.where} (holds {hit} — keys of the {spec.registry_name!r} registry)"
                )
    return found


# ── the two configurable surfaces of a capability ────────────────────


def _model_surfaces(model: type[BaseModel], *, check_values: bool) -> list[Surface]:
    return [
        Surface(
            where=f"{model.__module__}.{model.__qualname__}.{name}",
            name=name,
            values=_string_values(field.annotation, field.default),
            check_values=check_values,
        )
        for name, field in model.model_fields.items()
    ]


def _init_surfaces(cls: type) -> list[Surface]:
    """The capability's own ``__init__`` parameters — its generated config.

    This is the surface Rule 1 used to miss entirely, and the one this
    very round added four knobs to. A ``dom_serializer_type: str = "text"``
    parameter here plus a lookup in ``capability.py`` (which Rule 2
    exempts) would have been defect #4 with both rules green.

    A parameter that *is* a receptor slot — ``actions`` on
    ``BrowserCapability`` — keeps its value check but skips the name
    check, since colliding with itself is the point. A receptor slot
    typed as a plain string holding registry keys is still caught.
    """
    try:
        hints = get_type_hints(cls.__init__, include_extras=True)
    except Exception:  # pragma: no cover — unresolvable forward ref
        hints = {}
    receptors = {spec.param_name for spec in parse_wiring(cls)}
    surfaces: list[Surface] = []
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover — defensive
        return surfaces
    for name, param in params.items():
        if name in {"self", "id"}:
            continue
        default = None if param.default is inspect.Parameter.empty else param.default
        surfaces.append(
            Surface(
                where=f"{cls.__module__}.{cls.__name__}.__init__({name})",
                # A receptor slot cannot shadow itself.
                name="" if name in receptors else name,
                values=_string_values(hints.get(name, param.annotation), default),
                check_values=True,
            )
        )
    return surfaces


def _configuration_models(cls: type) -> list[type[BaseModel]]:
    """Models the capability holds or receives — value-checked.

    Computed from the capability's own type annotations (``__init__``
    parameters and class-level annotations, transitively through model
    fields), not from a list anyone maintains. On ``BrowserCapability``
    this resolves through ``_resolved_profile: BrowserProfileConfig |
    None`` to exactly the model both historical defects lived on, and to
    whatever a future capability hands its resolvers.
    """
    roots: list[Any] = []
    with contextlib.suppress(Exception):  # unresolvable forward ref
        roots.extend(get_type_hints(cls.__init__, include_extras=True).values())
    for klass in cls.__mro__:
        try:
            roots.extend(get_type_hints(klass, include_extras=True).values())
        except Exception:  # pragma: no cover — unresolvable forward ref
            continue

    found: dict[str, type[BaseModel]] = {}
    queue = list(roots)
    seen: set[int] = set()
    while queue:
        annotation = queue.pop()
        if id(annotation) in seen:
            continue
        seen.add(id(annotation))
        queue.extend(get_args(annotation))
        if (
            isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
            and annotation is not BaseModel
        ):
            key = f"{annotation.__module__}.{annotation.__qualname__}"
            if key not in found:
                found[key] = annotation
                queue.extend(f.annotation for f in annotation.model_fields.values())
    return list(found.values())


def _owned_models(cls: type) -> list[type[BaseModel]]:
    """Every Pydantic model defined in the capability's own package.

    Name-checked only (see :class:`Surface`). Generated config models are
    excluded by construction: they live in ``spectragent.generated``, and
    *that* is the one place a receptor name is supposed to be a field.
    """
    package = cls.__module__.rsplit(".", 1)[0]
    found: dict[str, type[BaseModel]] = {}
    for module in _modules_under(package):
        for _, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj is not BaseModel
                and obj.__module__.startswith(package)
            ):
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(found.values())


def _modules_under(package: str) -> list[Any]:
    pkg = importlib.import_module(package)
    modules = [pkg]
    for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package}."):
        try:
            modules.append(importlib.import_module(info.name))
        except Exception:  # pragma: no cover — optional extras (numpy, ...)
            continue
    return modules


def _surfaces_of(cls: type) -> list[Surface]:
    """Everything Rule 1 checks on one capability."""
    value_checked = {id(model) for model in _configuration_models(cls)}
    surfaces = _init_surfaces(cls)
    for model in _owned_models(cls):
        surfaces.extend(_model_surfaces(model, check_values=id(model) in value_checked))
    for model in _configuration_models(cls):
        if not model.__module__.startswith(cls.__module__.rsplit(".", 1)[0]):
            surfaces.extend(_model_surfaces(model, check_values=True))
    return surfaces


def _python_files() -> list[Path]:
    return sorted(
        path for path in CAPABILITIES_ROOT.rglob("*.py") if "__pycache__" not in path.parts
    )


def _capability_module_files() -> set[Path]:
    """Files that declare a capability class — the only lookup sites allowed."""
    files: set[Path] = set()
    for cls in _capability_classes():
        module = importlib.import_module(cls.__module__)
        source = getattr(module, "__file__", None)
        if source is not None:
            files.add(Path(source).resolve())
    return files


def registrar_lookup_calls(source: str, filename: str = "<memory>") -> list[tuple[int, str]]:
    """``(lineno, "SomeRegistrar.get")`` for every registry lookup by key."""
    tree = ast.parse(source, filename=filename)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in LOOKUP_METHODS:
            continue
        owner = func.value
        name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        if name.lower().endswith("registrar"):
            hits.append((node.lineno, f"{name}.{func.attr}"))
    # ``ast.walk`` is breadth-first, not source order; a failure message
    # that renumbers itself between runs is a failure message nobody reads.
    return sorted(hits)


# ── Rule 1: one place to choose a strategy ───────────────────────────


def test_no_config_surface_shadows_a_wired_receptor() -> None:
    """Nothing a capability configures may offer a second way to pick a strategy.

    ``BrowserProfileConfig.serializer`` was exactly this: the profile said
    ``"text"``, the capability's ``dom_serializer`` receptor held a
    configured ``TextSerializer(max_text_length=...)``, and the profile
    won because the watchdog read the profile.

    Not parametrized: the capability registry is global and other test
    modules add fixtures to it, so which classes exist depends on
    collection order. The loop runs at test time and the message names
    the offender.
    """
    registrars = _collect_registrars()
    found: list[str] = []
    checked: list[str] = []

    for capability_cls in _capability_classes():
        specs = _receptor_specs(capability_cls)
        surfaces = _surfaces_of(capability_cls)
        if not specs or not surfaces:
            continue
        checked.append(capability_cls.__name__)
        found.extend(collisions(specs, surfaces, registrars))

    assert checked, "no capability with receptors and config surfaces was checked"
    assert not found, (
        "A surface the capability configures offers a second way to choose a "
        f"wired strategy: {found}. Whichever one the consumer reads wins, and "
        "the other silently discards every knob configured on it — the "
        "mouse_trajectory / dom_cleaner / dom_serializer defect, three for "
        "three. Delete the field; the receptor is the config surface."
    )


def test_rule_one_reaches_the_capability_s_own_init() -> None:
    """Meta: the ``__init__`` surface is really in scope, not just models.

    This was the gap — Rule 1 walked Pydantic models, and a capability is
    not one. Its ``__init__`` is where ``screenshot_format`` and the four
    ``dom_*`` budget knobs live, so it is the surface the next strategy
    name is most likely to be added to.
    """
    from spectragent.core.capabilities.browser import BrowserCapability

    names = {s.name for s in _init_surfaces(BrowserCapability)}
    assert {"screenshot_format", "dom_max_elements", "profile"} <= names
    # ``actions`` is a receptor slot: name check waived, value check kept.
    actions = next(s for s in _init_surfaces(BrowserCapability) if "actions" in s.where)
    assert actions.name == "" and actions.check_values


def test_the_guard_fails_on_a_reintroduced_defect() -> None:
    """Positive control — a guard nobody has seen fail proves nothing.

    Both historical shapes, planted on a synthetic capability, in the
    disguise that matters: neither field is *named* after its receptor.
    ``output_style`` is how ``BrowserProfileConfig.serializer`` would look
    to someone who renamed it; ``dom_serializer_type`` is the shape that
    would have lived on ``__init__`` and been invisible until this round.
    """
    registrars = _collect_registrars()

    class _Profile(BaseModel):
        output_style: str = "html"

    class _Defective:
        __wiring__ = {"dom_serializer": ("dom_serializer", ["text", "html"])}
        dom_serializer: Any

        def __init__(self, dom_serializer_type: str = "text") -> None:
            self.dom_serializer_type = dom_serializer_type

    specs = _receptor_specs(_Defective)
    assert specs, "the synthetic capability must actually declare a receptor"

    surfaces = _init_surfaces(_Defective) + _model_surfaces(_Profile, check_values=True)
    found = collisions(specs, surfaces, registrars)

    assert any("output_style" in hit for hit in found), (
        f"the model-field shape must be caught by value, not name: {found}"
    )
    assert any("dom_serializer_type" in hit for hit in found), (
        f"the __init__ shape must be caught too: {found}"
    )
    assert all("dom_serializer" in hit for hit in found)


def test_the_browser_profile_names_no_strategy() -> None:
    """The specific shape, pinned by name, so a regression reads clearly.

    Rule 1 above is the general guard. This is the same statement in the
    vocabulary of the three defects that actually happened, so a failure
    points at the history rather than at an abstraction.
    """
    from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig

    fields = set(BrowserProfileConfig.model_fields)
    assert not fields & {"serializer", "cleaner", "mouse_trajectory", "typing_rhythm"}


def test_the_profile_is_value_checked_not_just_name_checked() -> None:
    """Meta: ``BrowserProfileConfig`` is in the narrowed value-check set.

    The value check is what catches a renamed strategy field, and it runs
    only on models the capability holds or receives. If ``profiles.py``
    ever stopped being reachable from ``BrowserCapability``'s annotations
    the guard would quietly weaken to a name comparison, which is what
    missed the first two defects.
    """
    from spectragent.core.capabilities.browser import BrowserCapability
    from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig

    assert BrowserProfileConfig in _configuration_models(BrowserCapability)


# ── Rule 2: one place to look one up ─────────────────────────────────


def test_only_a_capability_module_resolves_a_registry_key() -> None:
    """No resolver may turn a strategy *name* into a strategy *object*.

    ``DOMWatchdog.__init__`` did ``DOMSerializerRegistrar.get(name)()`` —
    one line, and the entire generated serializer config surface was
    unreachable. The capability module is exempt because default-building
    the wired action set from ``__wiring__`` is the receptor owner's own
    documented job (``_build_wired_action``).
    """
    allowed = _capability_module_files()
    offenders = [
        f"{path.relative_to(CAPABILITIES_ROOT.parent.parent.parent)}:{lineno} {call}(...)"
        for path in _python_files()
        if path not in allowed
        for lineno, call in registrar_lookup_calls(path.read_text(), str(path))
    ]
    assert not offenders, (
        f"Registry lookup outside the capability module: {offenders}. A "
        "consumer that resolves a name default-constructs the strategy, so "
        "every knob the Composer put on the configured instance is thrown "
        "away. Take the instance through __wiring__ / WatchdogContext "
        "instead — that is what dom_cleaner and dom_serializer now do."
    )


def test_rule_two_catches_every_key_taking_lookup() -> None:
    """Positive control, and the reason ``get_all()`` is not in the set.

    ``.get`` alone was too narrow: ``DOMSerializerRegistrar.get_or_none(
    name)()`` is the same defect one synonym away and passed. ``.get_all()``
    and ``.keys()`` take no key — that is enumeration, which is exactly how
    ``browser_watchdog_classes`` derives the installed set without naming a
    single watchdog.
    """
    caught = registrar_lookup_calls(
        "DOMSerializerRegistrar.get('text')()\n"
        "DOMSerializerRegistrar.get_or_none('text')()\n"
        "ActionRegistrar.children('browser')\n"
    )
    assert [call for _, call in caught] == [
        "DOMSerializerRegistrar.get",
        "DOMSerializerRegistrar.get_or_none",
        "ActionRegistrar.children",
    ]
    assert not registrar_lookup_calls(
        "WatchdogRegistrar.get_all()\nWatchdogRegistrar.keys()\nWatchdogRegistrar.tree()\n"
    )


def test_the_guard_is_looking_at_real_files() -> None:
    """Meta: the scan must actually reach the module that held defect #3."""
    watchdog = CAPABILITIES_ROOT / "browser" / "watchdogs" / "dom_watchdog.py"
    assert watchdog in set(_python_files())
    assert watchdog not in _capability_module_files()
