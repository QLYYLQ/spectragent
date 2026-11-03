"""Reading recorded action effects and restoration evidence back as their own types.

An :class:`~spectragent.schemas.action.ActionEffect` is written whole, as its
concrete model, wherever an ``ActionResult`` is written: a step's
``action_results``, a restored step's restoration attempts and history, the
``loop.history`` fragment's records. Its ``effect_id`` is the key of the
action that produced it, and the action class's nested ``Effect`` is the model
to read it with (``docs/architecture_design.md`` §4.5, "动作效果"). There is
no effect registry: the action registry already maps that key to the class.

A capability's :class:`~spectragent.schemas.restoration.RestorationEvidence`
goes the same way (§4.9): written whole in a restoration check, read back by
its ``capability_id`` — the key of the capability that measured it — with the
``RestorationEvidence`` that capability's class names, found in the
capability registry.

``schemas/`` never imports an action or a capability class, so the lookup
happens here, on the trace-reading side, the way a fragment's class is found
by its key before ``from_trace`` reads it. Each restore walks the *schema* of
the model about to be validated — its fields' annotations, not the JSON's key
names — and replaces each recorded payload of its kind with the validated
instance; pydantic then accepts the instance in the field typed with the
base. An id nothing is registered under, a class that declares no model, or
a payload its model refuses is a ``ValueError``: the reader turns it into
``TraceSchemaError`` naming the file. Nothing is ever kept as a bare dict.
"""

from __future__ import annotations

from collections.abc import Callable
from types import NoneType, UnionType
from typing import Annotated, Any, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.schemas.action import ActionEffect
from spectragent.schemas.restoration import RestorationEvidence

#: How an ``effect_id`` becomes the class that reads it.
EffectResolver = Callable[[str], type[ActionEffect]]

#: How a ``capability_id`` becomes the class that reads its restoration evidence.
EvidenceResolver = Callable[[str], type[RestorationEvidence]]

M = TypeVar("M", bound=BaseModel)


def registered_effect(effect_id: str) -> type[ActionEffect]:
    """The ``Effect`` of the action registered under *effect_id*.

    Raises:
        ValueError: no action is registered under the key, or the action
            declares no ``Effect``.
    """
    return _declared_model(
        ActionRegistrar.get, effect_id, "Effect", ActionEffect, noun="action effect", owner="action"
    )


def registered_restoration_evidence(capability_id: str) -> type[RestorationEvidence]:
    """The ``RestorationEvidence`` of the capability registered under *capability_id*.

    Raises:
        ValueError: no capability is registered under the key, or the
            capability declares no ``RestorationEvidence``.
    """
    return _declared_model(
        CapabilityRegistrar.get,
        capability_id,
        "RestorationEvidence",
        RestorationEvidence,
        noun="restoration evidence",
        owner="capability",
    )


def _declared_model(
    lookup: Callable[[str], type], key: str, attribute: str, base: type[M], *, noun: str, owner: str
) -> type[M]:
    """The *base* subclass that the class registered under *key* names as its *attribute*."""
    try:
        registered = lookup(key)
    except KeyError:
        raise ValueError(f"recorded {noun} {key!r} names no registered {owner}") from None
    model = getattr(registered, attribute, None)
    if not (isinstance(model, type) and issubclass(model, base)):
        raise ValueError(
            f"recorded {noun} {key!r}: {owner} {registered.__name__} declares no {attribute}"
        )
    return model


def restore_effects(model: type[BaseModel], data: Any, resolve: EffectResolver) -> Any:
    """*data*, the JSON form of a *model*, with every recorded effect rebuilt as its type.

    Only positions whose annotation is (or holds) an ``ActionEffect`` are
    touched; every other value is returned as it is, for the model's own
    validation to judge. *data* itself is not modified.

    Raises:
        ValueError: an effect is not a JSON object, has no string
            ``effect_id``, names no action with an ``Effect`` (from
            *resolve*), or does not validate as that ``Effect``.
    """
    return _Walk(ActionEffect, "effect_id", "action effect", resolve).value(model, data)


def restore_restoration_evidence(
    model: type[BaseModel], data: Any, resolve: EvidenceResolver
) -> Any:
    """*data*, the JSON form of a *model*, with every recorded restoration evidence rebuilt as its type.

    The twin of :func:`restore_effects`, keyed by ``capability_id``.

    Raises:
        ValueError: an evidence is not a JSON object, has no string
            ``capability_id``, names no capability with a
            ``RestorationEvidence`` (from *resolve*), or does not validate as it.
    """
    return _Walk(RestorationEvidence, "capability_id", "restoration evidence", resolve).value(
        model, data
    )


class _Walk:
    """One restoration of one kind: the base it rebuilds, how, and which annotations hold it."""

    def __init__(
        self,
        base: type[BaseModel],
        id_field: str,
        noun: str,
        resolve: Callable[[str], type[BaseModel]],
    ) -> None:
        self._base = base
        self._id_field = id_field
        self._noun = noun
        self._resolve = resolve
        self._carries: dict[Any, bool] = {}

    def value(self, annotation: Any, value: Any) -> Any:
        if value is None:
            return None
        origin = get_origin(annotation)
        if origin is Annotated:
            return self.value(get_args(annotation)[0], value)
        if origin is Union or origin is UnionType:
            arms = [arm for arm in get_args(annotation) if arm is not NoneType]
            holding = [arm for arm in arms if self.carries(arm)]
            if not holding:
                return value
            if len(holding) > 1:
                raise TypeError(
                    f"{annotation!r}: more than one union member can hold a recorded "
                    f"{self._noun}, so one cannot be placed"
                )
            return self.value(holding[0], value)
        if origin in (list, tuple, set, frozenset):
            args = [arg for arg in get_args(annotation) if arg is not Ellipsis]
            if not isinstance(value, list) or len(args) != 1:
                return value
            return [self.value(args[0], item) for item in value]
        if origin is dict:
            value_type = get_args(annotation)[1]
            if not isinstance(value, dict):
                return value
            return {key: self.value(value_type, item) for key, item in value.items()}
        if isinstance(annotation, type) and issubclass(annotation, self._base):
            return self._typed(value)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if not isinstance(value, dict):
                return value
            fields = annotation.model_fields
            return {
                key: (
                    self.value(fields[key].annotation, item)
                    if key in fields and self.carries(fields[key].annotation)
                    else item
                )
                for key, item in value.items()
            }
        return value

    def carries(self, annotation: Any) -> bool:
        """Whether a value of *annotation* can hold this walk's kind anywhere inside it."""
        try:
            known = self._carries.get(annotation)
        except TypeError:  # an annotation with unhashable metadata: not cached
            return self._find(annotation)
        if known is not None:
            return known
        self._carries[annotation] = False  # a model that refers to itself
        found = self._find(annotation)
        self._carries[annotation] = found
        return found

    def _find(self, annotation: Any) -> bool:
        if isinstance(annotation, type) and issubclass(annotation, self._base):
            return True
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return any(self.carries(field.annotation) for field in annotation.model_fields.values())
        return any(self.carries(arg) for arg in get_args(annotation) if arg is not Ellipsis)

    def _typed(self, value: Any) -> BaseModel:
        if isinstance(value, self._base):
            return value
        if not isinstance(value, dict):
            raise ValueError(
                f"a recorded {self._noun} is a JSON object, not {type(value).__name__}"
            )
        key = value.get(self._id_field)
        if not isinstance(key, str) or not key:
            raise ValueError(f"a recorded {self._noun} has no {self._id_field} naming its producer")
        return self._resolve(key).model_validate(value)


__all__ = [
    "EffectResolver",
    "EvidenceResolver",
    "registered_effect",
    "registered_restoration_evidence",
    "restore_effects",
    "restore_restoration_evidence",
]
