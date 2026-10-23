"""Typed action parameters — one declaration, three readers.

An action's parameter contract used to exist twice: as a hand-written
``params`` dict of prose the prompt shows the model, and as whatever
``execute()`` happened to read out of the dict it was handed. Nothing held
the two together, and ``ActionDescriptor.params_schema`` — the field meant
to carry the machine-readable half — was ``None`` on every action in the
tree.

Here the contract is a flat Pydantic model, ``ActionBase.Params``, and
everything else is derived from it:

- :func:`params_schema` — the JSON Schema an agent compiles into the
  closed per-run output model it ships to a provider.
- :func:`params_prose` — the one-line-per-param text the prompt shows.
  Rendered, never written: two agents (Skyvern, UI-TARS) print a
  descriptor's param text verbatim, so a second hand-written copy is a
  second thing to drift.
- :func:`action_fragments` / :func:`param_fragments` — which observation
  fragments a param cannot exist without.
- :func:`point_params` — which params are a point in the capability's
  viewport units (:class:`PointAxis`).
- :func:`element_params` — which params name an element of a fragment
  (:class:`AddressesElement`).
- :func:`claim_params` — which params carry the agent's claim about the task
  (:class:`TaskClaim`).

**What a param means is declared on the param, once.** Whether two params
are one point, and whether a param names an element, used to be restated by
every reader: a ``<name>_x`` / ``<name>_y`` naming convention here, a table of
coordinate actions in BrowserUse, the point pairs of UI-TARS's verbs, a list
of index actions in restoration, hard-coded param names in the viewer's
marks. The markers below are the one statement; the action's descriptor
carries it (``ActionDescriptor.point_params`` / ``element_params``), so an
Agent reads it off the action space it was handed, and a reader of recorded
actions reads it off the registered ``Params``.

**Fragment dependencies are capability knowledge.** ``index`` addresses
the selector map, which only exists when ``browser.dom`` was negotiated;
that is a fact about the browser capability, not about the agent that
emits a click. It is declared once, on the param
(:class:`RequiresFragment`), and read at two levels:

- a **required** param that needs fragment F makes the whole action need
  F (``browser.click`` needs ``browser.dom``);
- an **optional** param that needs F is *removed from the descriptor*
  when F is not active (``browser.scroll``'s ``index`` in a vision-only
  run), so the prompt never advertises a parameter the run cannot honour.

One declaration, both levels — which is why this is a param marker rather
than a pair of class-level dicts that can disagree with each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from spectragent.core.llm.schema.optimizer import SchemaOptimizer

__all__ = [
    "AddressesElement",
    "PointAxis",
    "RequiresFragment",
    "TaskClaim",
    "action_fragments",
    "claim_params",
    "element_params",
    "param_fragments",
    "point_params",
    "params_prose",
    "params_schema",
    "params_view",
]


@dataclass(frozen=True)
class RequiresFragment:
    """Marks a param that means nothing unless an observation fragment is live.

    Written into the param's ``Annotated`` metadata::

        index: Annotated[int, RequiresFragment("browser.dom")]

    The key is **fully qualified**, because it is the providing capability
    that states this: ``browser.click``'s index addresses
    ``browser.dom``'s selector map and no other capability's. This is the
    mirror image of an agent's ``__wiring__``, which is deliberately
    relative — an agent says it can click, a capability says what its
    click reads.
    """

    key: str


@dataclass(frozen=True)
class AddressesElement(RequiresFragment):
    """Marks a param whose value names one element of an observation fragment.

    Written into the param's ``Annotated`` metadata::

        index: Annotated[int, AddressesElement("browser.dom")]

    ``browser.click``'s ``index`` is a key of ``browser.dom``'s selector map.
    An element of a fragment means nothing without that fragment, so this is
    a :class:`RequiresFragment` too: the action's fragment dependency, and
    the dropping of an optional addressing param from the descriptor, follow
    from this one marker. What reads it: restoration re-finds the element a
    recorded action named, the training export translates it to a point, the
    viewer says which element an action named.
    """


@dataclass(frozen=True)
class PointAxis:
    """Marks a param as one coordinate of a point, in the capability's viewport units.

    Written into the param's ``Annotated`` metadata, once on each of the two::

        coordinate_x: Annotated[int, PointAxis("coordinate", "x")]
        coordinate_y: Annotated[int, PointAxis("coordinate", "y")]

    ``point`` names the point (a drag has a ``start`` and an ``end``); its
    ``x`` and ``y`` params are one value to whoever moves a point onto another
    grid, writes it, or draws it. Nothing is a point for its name alone: a
    param named ``offset_x`` without this marker is a number.
    """

    point: str
    axis: Literal["x", "y"]


@dataclass(frozen=True)
class TaskClaim:
    """Marks the param that carries the agent's claim about whether the task was accomplished.

    Written into the param's ``Annotated`` metadata::

        success: Annotated[bool | None, TaskClaim()] = None

    ``browser.done``'s ``success``: what the agent says about the task, carried
    out as ``ActionResult.reported_success``, never a verdict
    (``docs/architecture_design.md`` §4.1, "agent 的任务结论"). It is the one
    param of an action that is not an instruction to the environment, which
    is why a reader may need to tell it apart: the training export writes a
    teacher's ``true`` claim as the student's plain ending when the student's
    format cannot say it (§4.15 "round-trip").
    """


def param_fragments(params: type[BaseModel]) -> dict[str, frozenset[str]]:
    """Param name → the fragment keys it cannot exist without.

    Params with no :class:`RequiresFragment` marker are absent from the
    mapping rather than present with an empty set, so callers read it as
    "these are the dependent ones".
    """
    found: dict[str, frozenset[str]] = {}
    for name, field in params.model_fields.items():
        keys = frozenset(
            marker.key for marker in field.metadata if isinstance(marker, RequiresFragment)
        )
        if keys:
            found[name] = keys
    return found


def point_params(params: type[BaseModel]) -> list[tuple[str, str]]:
    """``(x, y)`` param-name pairs that are a point (:class:`PointAxis`), in declaration order.

    One pair per point, ordered by where the first of its two params is
    declared: a drag's ``start`` comes before its ``end``. Read by whoever has
    to treat a point as one value — an Agent moving it onto its model's grid,
    the training export checking a written point against the student grid's
    quantum rather than for equality (arch §4.15 "round-trip"), the viewer
    drawing it.

    Raises:
        TypeError: a point declares one of its axes twice, or only one.
    """
    axes: dict[str, dict[str, str]] = {}
    for name, field in params.model_fields.items():
        for marker in field.metadata:
            if not isinstance(marker, PointAxis):
                continue
            point = axes.setdefault(marker.point, {})
            if marker.axis in point:
                raise TypeError(
                    f"{params.__name__} declares the {marker.axis} of point {marker.point!r} "
                    f"twice: {point[marker.axis]!r} and {name!r}"
                )
            point[marker.axis] = name
    pairs: list[tuple[str, str]] = []
    for point, found in axes.items():
        if set(found) != {"x", "y"}:
            (only,) = found
            raise TypeError(
                f"{params.__name__} declares only the {only} of point {point!r} ({found[only]!r})"
            )
        pairs.append((found["x"], found["y"]))
    return pairs


def claim_params(params: type[BaseModel]) -> tuple[str, ...]:
    """The params that carry the agent's claim about the task (:class:`TaskClaim`), in declaration order."""
    return tuple(
        name
        for name, field in params.model_fields.items()
        if any(isinstance(marker, TaskClaim) for marker in field.metadata)
    )


def element_params(params: type[BaseModel]) -> tuple[str, ...]:
    """The params that name an element of a fragment (:class:`AddressesElement`), in declaration order.

    A required one always names an element (``browser.click``'s ``index``); an
    optional one only when it is given (``browser.scroll``'s).
    """
    return tuple(
        name
        for name, field in params.model_fields.items()
        if any(isinstance(marker, AddressesElement) for marker in field.metadata)
    )


def action_fragments(params: type[BaseModel]) -> frozenset[str]:
    """Fragments an action needs before it can run at all.

    The union over its **required** params only: an optional dependent
    param is dropped from the descriptor when its fragment is missing (see
    :func:`params_view`), which leaves the action itself usable.
    """
    dependencies = param_fragments(params)
    return frozenset(
        key
        for name, keys in dependencies.items()
        if params.model_fields[name].is_required()
        for key in keys
    )


def _type_token(schema: Mapping[str, Any]) -> str:
    """The type as the prompt says it: ``int``, ``str | None``, ``a|b|c``."""
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return "|".join(str(value) for value in enum)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        tokens = [_type_token(branch) for branch in any_of]
        return " | ".join(token for token in tokens if token)

    kind = schema.get("type")
    if kind == "integer":
        return "int"
    if kind == "number":
        return "float"
    if kind == "boolean":
        return "bool"
    if kind == "string":
        return "str"
    if kind == "null":
        return "None"
    return str(kind or "")


def _prose_line(schema: Mapping[str, Any], *, required: bool, hint: str) -> str:
    """One param's prompt text: ``int (optional, default=3) -- what it means``.

    Three pieces, each earned from the model rather than written by hand:
    the type token, a parenthesis that says the param may be left out (and
    what happens then), and the param's own description after ``--``.

    ``| None`` shows in the token only where the model *must* write
    something and ``null`` is one of the things it may write. On an
    optional param the null branch says nothing the ``(optional)`` does
    not already say, so it is left out of the prose — the schema keeps it,
    because a strict-mode provider makes every property required and null
    is then how a model declines one.
    """
    if not required and isinstance(schema.get("anyOf"), list):
        schema = {
            **schema,
            "anyOf": [b for b in schema["anyOf"] if b.get("type") != "null"] or schema["anyOf"],
        }
    head = _type_token(schema)
    inside = ", ".join(part for part in (("optional" if not required else ""), hint) if part)
    if inside:
        head = f"{head} ({inside})" if head else f"({inside})"
    note = schema.get("description") or ""
    return f"{head} -- {note}" if note else head


def params_view(
    params: type[BaseModel],
    *,
    hints: Mapping[str, str] | None = None,
    fragments: frozenset[str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """``(prose, schema)`` for one action, in this run's observation context.

    Args:
        params: The action's ``Params`` model.
        hints: Param name → the current value of the capability-side knob
            that governs it (``"default=true"``). It lands in the prose
            *and* in the JSON Schema's ``description`` — deliberately not
            as a schema ``default``, because the default belongs to the
            capability: a model that omits the param gets the knob's
            value, whatever a schema default claimed.
        fragments: The run's active observation fragments, fully
            qualified. ``None`` means "no context" — nothing is dropped,
            which is what an introspection caller wants. Given a set,
            optional params whose :class:`RequiresFragment` is not in it
            are removed from both halves.

    The two halves are rendered together, once, so the text the prompt
    shows and the schema the provider constrains on cannot describe
    different parameters.

    The compilation is :meth:`SchemaOptimizer.create_optimized_json_schema`
    — the same compiler ``ToolDefinition.from_model`` wraps for the
    provider wire format, so the prompt's schema and a native tool
    declaration cannot drift into two different ideas of what a legal
    schema is. This used to call ``params.model_json_schema()`` raw. For
    every action in the tree today the two agree (the optimizer's only
    effect on a flat scalar model is dropping ``title``), so this is not a
    bug fix; it removes the *possibility* of a divergence, on one
    reachable shape in particular:

    a param typed as a Python enum class (``StrEnum``, or ``str, Enum``) —
    which ``architecture_design.md`` §4.5 explicitly admits ("字符串枚举") —
    compiles to ``{"$ref": "#/$defs/…"}`` plus a ``$defs`` block, and this
    function reads ``properties`` and drops the rest. The descriptor would
    then carry a ``$ref`` with nothing to resolve against, which surfaces
    much later as ``UnsupportedParamsSchema: has unsupported type None``
    when an agent compiles the closed union. The optimizer inlines it to
    ``{"enum": [...], "type": "string"}``, which is inside the subset.
    Every shipped action spells its enums ``Literal[...]`` — which Pydantic
    inlines — so nothing in the tree hit this.

    The provider-facing :class:`~spectragent.core.llm.tools.ToolDefinition`
    is deliberately *not* imported here: merging it with
    ``ActionDescriptor`` is deferred until the FuncCall loop gives it a
    real consumer, and it has an open blocker
    (``docs/action_descriptor_vs_tool_definition.md``). Sharing the
    compiler is not that merge.
    """
    hints = hints or {}
    compiled = SchemaOptimizer.create_optimized_json_schema(params)
    properties: dict[str, Any] = dict(compiled.get("properties") or {})
    required = set(compiled.get("required") or ())
    dependencies = param_fragments(params)

    prose: dict[str, str] = {}
    kept: dict[str, Any] = {}
    kept_required: list[str] = []
    for name, field_schema in properties.items():
        needs = dependencies.get(name, frozenset())
        if fragments is not None and needs and name not in required and not needs <= fragments:
            continue
        # ``title`` is Pydantic's auto-generated echo of the field name; it
        # tells a model nothing and costs tokens in every provider dialect.
        # ``default`` is dropped for a stronger reason: the default an
        # omitted param falls back to is the *capability's* knob, applied
        # in ``execute``. Publishing a schema default here would state a
        # value that is not the one a model omitting the param will get.
        #
        # This is the *descriptor's* schema. An agent compiling it into a
        # per-run output model re-adds ``default: null`` on each optional
        # param, because a strict-mode provider makes every property
        # required and ``null`` is then the only way to decline one
        # (``core/actions/action_union.py``). That null never reaches
        # ``execute``: the agent strips it (``model_dump(exclude_none)``),
        # so an omission and a written null arrive identically — which is
        # what stops ``clear: null`` from overriding ``clear_by_default``.
        field_schema = {k: v for k, v in field_schema.items() if k not in ("title", "default")}
        line = _prose_line(field_schema, required=name in required, hint=hints.get(name, ""))
        prose[name] = line
        # The description a provider sees is the line the prompt shows, so
        # a model reading only the schema learns the same thing — including
        # the knob value, which exists nowhere in the schema's structure.
        field_schema["description"] = line
        kept[name] = field_schema
        if name in required:
            kept_required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": kept,
        "required": kept_required,
        "additionalProperties": False,
    }
    return prose, schema


def params_prose(
    params: type[BaseModel],
    *,
    hints: Mapping[str, str] | None = None,
    fragments: frozenset[str] | None = None,
) -> dict[str, str]:
    """Just the prompt text half of :func:`params_view`."""
    return params_view(params, hints=hints, fragments=fragments)[0]


def params_schema(
    params: type[BaseModel],
    *,
    hints: Mapping[str, str] | None = None,
    fragments: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Just the JSON Schema half of :func:`params_view`."""
    return params_view(params, hints=hints, fragments=fragments)[1]
