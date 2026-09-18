"""The closed action union an agent ships to a provider, built per run.

An agent that advertises verbs in prose and accepts ``list[dict[str, Any]]``
back has told the provider nothing. Under OpenAI's strict dialect that open
map compiles to an object node with no ``properties``, so a constrained
decoder can emit exactly one thing — ``{}`` — and Gemini behind an
OpenAI-compatible gateway did, every step, ending runs before the first
action. The fix is not a better prompt; it is a schema that can only spell
the negotiated verbs.

What this module builds, from :class:`ActionDescriptor` alone:

``{"browser.click": {"index": 3}}`` — one single-key object per verb,
combined into an ``anyOf``. The verb is a *property name*, so the schema
needs nothing but ``object`` / ``properties`` / ``required`` / ``anyOf`` /
``enum``: the intersection of OpenAI strict, Gemini's ``responseSchema``
subset and Anthropic's native output config. It is also the shape upstream
browser-use trained on, which the fine-tuned models still expect.

Deliberately **not** a Pydantic ``Discriminator``: a callable discriminator
compiles to ``oneOf``, which none of the three dialects accepts and which
``SchemaOptimizer.is_strict_compatible`` used to wave through. A plain
``Union`` compiles to ``anyOf``.

The union is fronted by one ``BeforeValidator`` that dispatches on the key
and validates with that verb's model. Without it a bad ``amount`` produced
one error *per branch* — seven lines of noise for one mistake — and the
Loop feeds that text straight back to the model. With it, every failure is
a single line naming the verb.

The agent never imports an action class: it reads descriptors, which is
what negotiation produced, so what the model may write is exactly what the
run can execute.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, create_model
from pydantic import Field as PydanticField

from spectragent.schemas.action import ActionDescriptor

__all__ = [
    "UnsupportedParamsSchema",
    "action_union",
    "params_model_from_schema",
]

#: JSON Schema scalar types this converter understands. Anything else is a
#: params schema no closed union can express — see
#: :class:`UnsupportedParamsSchema`.
_SCALARS: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

_UNSAFE = re.compile(r"[\W_]+")

#: ``create_model(name, **{field: (annotation, FieldInfo)})`` is Pydantic's
#: documented dynamic form and the one its overloads cannot express — every
#: keyword is read as ``__doc__``/``__module__``/... Bound once, untyped, so
#: the escape hatch is in one place with its reason next to it rather than
#: as a suppression comment on each call.
_create_model: Any = create_model


class UnsupportedParamsSchema(Exception):
    """An action's ``params_schema`` is outside the closed-union subset.

    Raised at ``prepare()`` — before the environment is set up and long
    before the first LLM call — because the alternative is a run that
    starts fine and then cannot express one of its own actions. The subset
    is deliberately small (flat objects of scalars, string enums, nullable
    scalars): it is what all three provider dialects agree on, and every
    action in the tree fits inside it.
    """

    def __init__(self, action: str, param: str, detail: str) -> None:
        super().__init__(
            f"{action}: parameter {param!r} {detail}. A closed action schema "
            f"supports a flat object of string / integer / number / boolean "
            f"params, string enums, and a null branch for an optional one. "
            f"Nest the value in a string, or teach this converter the shape."
        )
        self.action = action
        self.param = param


def _safe_name(name: str) -> str:
    """``browser.click`` → ``BrowserClick``, for a readable class name."""
    return "".join(part.title() for part in _UNSAFE.split(name) if part)


def _branch_type(action: str, param: str, schema: Mapping[str, Any]) -> Any:
    """One non-null JSON Schema node as a Python annotation."""
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        if not all(isinstance(value, str) for value in enum):
            raise UnsupportedParamsSchema(action, param, "has a non-string enum")
        return Literal[tuple(enum)]  # type: ignore[valid-type]
    kind = schema.get("type")
    if kind in _SCALARS:
        return _SCALARS[kind]
    raise UnsupportedParamsSchema(action, param, f"has unsupported type {kind!r}")


def _param_type(action: str, param: str, schema: Mapping[str, Any]) -> tuple[Any, bool]:
    """``(annotation, nullable)`` for one param's schema node."""
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        nullable = any(branch.get("type") == "null" for branch in any_of)
        branches = [b for b in any_of if b.get("type") != "null"]
        if not branches:
            raise UnsupportedParamsSchema(action, param, "is null and nothing else")
        types = tuple(_branch_type(action, param, b) for b in branches)
        annotation = types[0] if len(types) == 1 else Union[types]  # noqa: UP007
        return annotation, nullable
    return _branch_type(action, param, schema), False


def params_model_from_schema(
    descriptor: ActionDescriptor,
    *,
    relabel: Mapping[str, str] | None = None,
) -> type[BaseModel]:
    """A Pydantic model for one action's params, from its ``params_schema``.

    Args:
        descriptor: The negotiated descriptor. Its ``params_schema`` is the
            capability's typed contract — already narrowed to the params
            this run's observation fragments can honour.
        relabel: Param name → the description to show instead of the
            capability's. The one legitimate use is a unit the agent
            converts: the capability's point actions declare viewport
            pixels, and an agent that reads points off a screenshot teaches
            the grid it asked the model to answer on. The *names* are never
            relabelled — only the units differ.

    Raises:
        UnsupportedParamsSchema: A param is outside the closed subset.
    """
    schema = descriptor.params_schema or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    relabel = relabel or {}

    fields: dict[str, Any] = {}
    for name, node in properties.items():
        annotation, nullable = _param_type(descriptor.name, name, node)
        description = relabel.get(name) or node.get("description") or ""
        if name in required:
            if nullable:
                annotation = annotation | None
            fields[name] = (annotation, PydanticField(description=description))
        else:
            # Optional means "omit it and the capability's knob decides".
            # The null branch is what lets a strict-mode provider — which
            # makes every property required — still decline: the agent
            # strips nulls before the action is built, so a written null
            # and an omission arrive at the capability identically.
            fields[name] = (
                annotation | None,
                PydanticField(default=None, description=description),
            )

    return _create_model(
        f"{_safe_name(descriptor.name)}Params",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def _first_error(exc: ValidationError) -> str:
    """One line for a params failure: ``direction: Input should be 'up', ...``.

    Only the first: the Loop hands this text back to the model verbatim,
    and a model given seven complaints about one object fixes none of them.
    """
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"][1:]) or "params"
    return f"{where}: {error['msg']}"


def action_union(
    descriptors: Sequence[ActionDescriptor],
    *,
    relabel: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[Any, dict[str, type[BaseModel]]]:
    """``(annotation, verb → single-key model)`` for one run's action items.

    The annotation goes straight into the per-run output model's
    ``action: list[...]`` field. The mapping is returned because the agent
    needs the same models to name what it accepts, and rebuilding them
    would be a second answer to "which verbs exist this run".

    Raises:
        ValueError: *descriptors* is empty. A run whose model may emit no
            action at all has nothing to say, and the resulting ``Union[()]``
            fails much later and much less clearly.
        UnsupportedParamsSchema: A descriptor's params are outside the
            closed subset.
    """
    if not descriptors:
        raise ValueError(
            "A closed action schema needs at least one negotiated action; this "
            "run has none, so the model could not emit anything at all."
        )
    relabel = relabel or {}

    items: dict[str, type[BaseModel]] = {}
    for descriptor in descriptors:
        params = params_model_from_schema(descriptor, relabel=relabel.get(descriptor.name))
        # The verb is the *field name*, dot and all. Pydantic keeps it
        # verbatim through ``model_dump_json``, so no alias is involved and
        # a recorded reply replays as the key the model wrote.
        items[descriptor.name] = _create_model(
            _safe_name(descriptor.name),
            __config__=ConfigDict(extra="forbid"),
            **{
                descriptor.name: (
                    params,
                    PydanticField(description=descriptor.description),
                )
            },
        )

    allowed = ", ".join(items)
    member_types = tuple(items.values())

    def dispatch(value: Any) -> Any:
        if isinstance(value, member_types):
            return value  # already validated — re-validation of a built model
        if not isinstance(value, Mapping):
            raise ValueError(
                f"each action is an object naming one verb, e.g. "
                f"{{{next(iter(items))!r}: {{...}}}}; got "
                f"{type(value).__name__}. Allowed verbs: {allowed}"
            )
        if len(value) != 1:
            written = ", ".join(repr(key) for key in value) or "nothing"
            raise ValueError(
                f"each action names exactly one verb and gives it an object "
                f"of parameters; this one names {written}. Allowed verbs: "
                f"{allowed}"
            )
        verb, params = next(iter(value.items()))
        model = items.get(verb)
        if model is None:
            raise ValueError(f"unknown action {verb!r}. Allowed verbs: {allowed}")
        try:
            return model.model_validate({verb: params})
        except ValidationError as exc:
            raise ValueError(f"{verb}: {_first_error(exc)}") from None

    annotation = member_types[0] if len(member_types) == 1 else Union[member_types]  # noqa: UP007
    return Annotated[annotation, BeforeValidator(dispatch)], items
