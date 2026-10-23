"""Field-level redaction for the resolved run configuration.

A trace records what a run was *configured* as, not only what it did.
The configuration object is the composed, validated Pydantic model
(``ComposedRun.top_level_cfg``) — every default filled in and every wired
receptor inlined inside its owner — so it is also the single place every
credential in the run passes through: LLM api keys, CDP URLs carrying a
serving token, cloud-browser session tokens, proxy user:password, Android
signing keys.

(It is not *everything* a run resolved. A value a component derives from
its config at construction — ``BrowserCapability`` turning
``profile: "default"`` into a watchdog set — lands on the instance, never
in this object, so it is not here to record or to withhold.)

**Deny by default, at field level.** A field is recorded verbatim only
when something in the source says it is safe. Everything else is written
as :data:`OMITTED` — *written*, not dropped, because "this field was set"
is reproducibility information on its own and a missing key reads
identically to "never configured".

The asymmetry is the whole argument. Forgetting to mark a safe knob costs
one field that anybody can add back with a one-line edit. Forgetting to
mark a secret is an unrecoverable leak into a directory people share,
attach to issues and upload alongside papers. A blocklist of key-shaped
names inverts that asymmetry and will always lag the next backend.

Two things make a field safe:

1. An explicit mark by whoever owns the field: :func:`trace_safe` in
   place of the field's ``Field(...)``. It is a drop-in with one extra
   argument, the reason. Where the mark has to physically live to survive
   conscribe's config generation is explained there — the obvious
   spellings do not.
2. An annotation that is **safe by type** (:func:`is_safe_by_type`):
   a closed set — ``Literal[...]``, an ``Enum`` subclass, ``bool``,
   ``None`` — or a number, ``int`` / ``float``. A closed-set value can
   only be one of the alternatives the source code enumerates, so it
   cannot carry a credential; it is what makes every conscribe
   discriminator (``agent_id``, ``loop_id``, ``env_type``,
   ``provider_id``, ``trajectory_type``) and every mode switch
   (``screenshot_format``, ``coordinate_space``, ``prompt_template``)
   readable in a trace without anybody having to remember to mark them.
   A number is a count, a limit, a size or a sampling knob
   (``max_steps``, ``max_actions_per_step``, ``temperature``,
   ``viewport_width``), so numbers are recorded raw as well — the user's
   ruling, which is what lets a reader see the teacher's sampling and
   lets a branch compare the numeric agent and memory knobs before
   anything starts (``docs/architecture_design.md`` §4.1, §5.6).

Free-form strings are open and stay omitted until marked. A reader
cannot *see* an unmarked one — but a run is still told apart by it,
because
:meth:`~spectragent.core.trace.models.TraceConfig.configuration_digest`
is computed before redaction. Marking a field improves what a trace
says, never whether two traces compare equal.

The escape hatch the user asked for sits on top: ``record_secrets=True``
on the trace hook records everything verbatim, for a local debugging run
that will not be shared. A trace written that way says so
(``TraceConfig.secrets_recorded``).
"""

from __future__ import annotations

import enum
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

#: What a field that was set but is not trace-safe is recorded as.
#: Deliberately a value, not an absent key: ``null`` means "not
#: configured" and must stay distinguishable from "configured, withheld".
OMITTED = "<omitted>"

#: ``json_schema_extra`` key also honoured as a trace-safe mark, for a
#: hand-written Pydantic model that never goes through conscribe.
TRACE_SAFE_EXTRA = "trace_safe"

_UNION_ORIGINS = (Union, types.UnionType)

_NOT_STRUCTURE = object()


@dataclass(frozen=True)
class TraceSafe:
    """The mark itself: "this field's value may be recorded verbatim".

    Lives in a field's ``FieldInfo.metadata``, which is the one place a
    mark survives the trip from a source class to the model the Composer
    validates against. Put it there with :func:`trace_safe` rather than by
    hand — see that function for why the obvious spelling does not work.

    *reason* is free text for whoever reads the declaration next. Nothing
    reads it back; its job is to make a reviewer of the diff ask "is that
    true?" at the moment the mark is added, which is the only moment
    anybody will.
    """

    reason: str = ""


def trace_safe(reason: str, **field_kwargs: Any) -> Any:
    """Declare a config field *and* mark its value safe to record.

    A drop-in for ``Field(...)`` — same keyword arguments, plus the
    reason::

        trace_dir: Annotated[
            str,
            trace_safe(
                "output location, not a credential",
                default="traces",
                description="Base directory for trace output.",
            ),
        ] = "traces"

    It replaces ``Field(...)`` instead of sitting next to it because of
    how conscribe rebuilds a config model. Its extractor
    (``conscribe/config/extractor.py``) takes the first ``FieldInfo`` out
    of a parameter's ``Annotated``, reconstructs a fresh ``Field`` from
    the handful of attributes it knows — ``description`` and the numeric
    and length constraints — and **discards everything else**, including
    ``json_schema_extra`` and any sibling annotation. The one thing it
    carries across verbatim is ``FieldInfo.metadata``, which it re-attaches
    to the rebuilt field. So the mark has to be *inside* the FieldInfo,
    not beside it, and that is what this function does.

    Marking a field this way and then having the value record as
    ``"<omitted>"`` anyway means the mark did not survive; the test in
    ``tests/test_core/test_trace/test_config_redaction.py`` exists to
    catch that the moment conscribe changes.
    """
    info = Field(**field_kwargs)
    # ``metadata`` is FieldInfo's list of annotation-level metadata, which
    # pydantic leaves alone for objects it does not recognise as
    # constraints.
    info.metadata.append(TraceSafe(reason))
    return info


def is_trace_safe(field_info: FieldInfo) -> bool:
    """Has this field been explicitly marked safe to record verbatim?

    :class:`TraceSafe` in ``FieldInfo.metadata`` is the mark
    :func:`trace_safe` writes and the one that survives conscribe.
    ``json_schema_extra={"trace_safe": True}`` is honoured too, for a
    hand-written Pydantic model whose author reaches for the spelling
    pydantic documents.
    """
    if any(isinstance(meta, TraceSafe) or meta is TraceSafe for meta in field_info.metadata):
        return True
    extra = field_info.json_schema_extra
    return isinstance(extra, Mapping) and bool(extra.get(TRACE_SAFE_EXTRA))


def is_closed_set(annotation: Any) -> bool:
    """Can this annotation only hold values the source code enumerates?

    ``Literal["dom", "vision"]``, an ``Enum`` subclass, ``bool`` and
    ``None`` can. A value drawn from a set written out in code is not a
    credential, so recording it leaks nothing that reading the source
    would not.

    ``str``, ``int``, ``float`` and every container cannot — they are
    open (numbers are safe for another reason, :func:`is_safe_by_type`).
    ``Optional[X]`` is closed exactly when ``X`` is, since ``None`` is
    itself a closed set of one.
    """
    if annotation is None:
        # An untyped field. Nothing is known about what it can hold.
        return False
    origin = get_origin(annotation)
    if origin is Annotated:
        return is_closed_set(get_args(annotation)[0])
    if origin is Literal:
        return True
    if origin in _UNION_ORIGINS:
        return all(is_closed_set(arg) for arg in get_args(annotation))
    if annotation is bool or annotation is types.NoneType:
        return True
    return isinstance(annotation, type) and issubclass(annotation, enum.Enum)


def is_safe_by_type(annotation: Any) -> bool:
    """May a value of this annotation be recorded without a mark?

    A closed set (:func:`is_closed_set`) or a number — ``int``, ``float``
    and their subclasses (module docstring). A union is safe exactly when
    each alternative is, so ``int | None`` and ``int | Literal["unlimited"]``
    are and ``int | str`` is not. Strings and every container are not.
    """
    if annotation is None:
        return False
    origin = get_origin(annotation)
    if origin is Annotated:
        return is_safe_by_type(get_args(annotation)[0])
    if origin in _UNION_ORIGINS:
        return all(is_safe_by_type(arg) for arg in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, int | float):
        return True
    return is_closed_set(annotation)


def redact_config(config: Any, *, record_secrets: bool = False) -> dict[str, Any]:
    """Turn a resolved configuration object into a recordable dict.

    Args:
        config: The composed, validated config — normally
            ``ComposedRun.top_level_cfg``. ``None`` (a run assembled
            without the Composer) yields ``{}``.
        record_secrets: Record every value verbatim. For a local
            debugging run whose trace will not be shared; the trace marks
            itself so a reader knows what is in it.

    Returns:
        A JSON-serializable dict mirroring the config's structure, with
        unsafe values replaced by :data:`OMITTED`.
    """
    if config is None:
        return {}
    if isinstance(config, BaseModel):
        return _redact_model(config, record_secrets=record_secrets)
    if isinstance(config, Mapping):
        # A plain dict carries no field declarations, so nothing in it can
        # be marked and nothing in it is recorded. Deny-by-default has no
        # opinion to consult here, and guessing from key names is the
        # blocklist this design exists to avoid.
        return {
            str(key): (_jsonable(value) if record_secrets else OMITTED)
            for key, value in config.items()
        }
    return {}


def _redact_model(model: BaseModel, *, record_secrets: bool) -> dict[str, Any]:
    """Redact one Pydantic model, field by field, in declaration order.

    Declaration order (not sorted) keeps the dump readable *and*
    deterministic, which is what lets ``TraceConfig.configuration_digest``
    hash it and mean something.
    """
    out: dict[str, Any] = {}
    for name, field_info in type(model).model_fields.items():
        out[name] = _redact_field(
            getattr(model, name, None), field_info, record_secrets=record_secrets
        )
    # ``extra="allow"`` models (conscribe emits one per open schema) carry
    # values that were never declared, so there is no declaration to mark
    # them safe. They stay omitted — this is the fail-closed direction.
    for name, value in (getattr(model, "__pydantic_extra__", None) or {}).items():
        out[str(name)] = _jsonable(value) if record_secrets else OMITTED
    return out


def _redact_field(value: Any, field_info: FieldInfo, *, record_secrets: bool) -> Any:
    if record_secrets:
        return _jsonable(value)
    if value is None:
        # "Not configured" is a fact worth recording, and it cannot be a
        # secret. An api_key resolved from the environment never enters
        # the config model at all, so it shows up here — correctly — as
        # null rather than as a withheld value.
        return None
    structure = _redact_structure(value, record_secrets=record_secrets)
    if structure is not _NOT_STRUCTURE:
        return structure
    if is_trace_safe(field_info) or is_safe_by_type(field_info.annotation):
        return _jsonable(value)
    return OMITTED


def _redact_structure(value: Any, *, record_secrets: bool) -> Any:
    """Recurse into nested config *structure*, or say this is a value.

    Nesting is not data: a list of agent entries or a wired ``backend:``
    block is shape, and dropping it would hide which components the run
    was built from. Its leaves go through the same field-level rules.

    An empty container is recorded as itself. It carries no value to
    leak, and "configured, and empty" — an ablation list that names no
    actions — is exactly the kind of fact a trace is for.

    A mapping's *keys* are kept: a dict of models is keyed by instance id
    (``capabilities: {"browser": …}``), which is structure. Its values go
    through the field rules like anything else. Note that a marked field
    cannot buy its way out of this — structure is checked first, so
    marking a nested model does not dump its subtree verbatim.
    """
    if isinstance(value, BaseModel):
        return _redact_model(value, record_secrets=record_secrets)
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        if all(isinstance(item, BaseModel) for item in value):
            return [_redact_model(item, record_secrets=record_secrets) for item in value]
        return _NOT_STRUCTURE
    if isinstance(value, Mapping):
        if not value:
            return {}
        if all(isinstance(item, BaseModel) for item in value.values()):
            return {
                str(key): _redact_model(item, record_secrets=record_secrets)
                for key, item in value.items()
            }
        return _NOT_STRUCTURE
    return _NOT_STRUCTURE


def _jsonable(value: Any) -> Any:
    """Best-effort JSON-safe rendering of a value being recorded."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


__all__ = [
    "OMITTED",
    "TRACE_SAFE_EXTRA",
    "TraceSafe",
    "is_closed_set",
    "is_safe_by_type",
    "is_trace_safe",
    "redact_config",
    "trace_safe",
]
