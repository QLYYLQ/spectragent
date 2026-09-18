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
from typing import Any

from pydantic import BaseModel

from spectragent.core.llm.schema.optimizer import SchemaOptimizer

__all__ = [
    "RequiresFragment",
    "action_fragments",
    "param_fragments",
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
        # (``agents/_action_union.py``). That null never reaches
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
