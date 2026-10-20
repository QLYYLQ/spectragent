"""Reading recorded action effects back as their own types.

An :class:`~spectragent.schemas.action.ActionEffect` is written whole, as its
concrete model, wherever an ``ActionResult`` is written: a step's
``action_results``, a restored step's restoration attempts and history, the
``loop.history`` fragment's records. Its ``effect_id`` is the key of the
action that produced it, and the action class's nested ``Effect`` is the model
to read it with (``docs/architecture_design.md`` §4.5, "动作效果"). There is
no effect registry: the action registry already maps that key to the class.

``schemas/`` never imports an action class, so the lookup happens here, on the
trace-reading side, the way a fragment's class is found by its key before
``from_trace`` reads it. :func:`restore_effects` walks the *schema* of the
model about to be validated — its fields' annotations, not the JSON's key
names — and replaces each recorded effect mapping with the validated
instance; pydantic then accepts the instance in the field typed
``ActionEffect``. A key no action is registered under, an action that
declares no ``Effect``, or a payload its ``Effect`` refuses is a
``ValueError``: the reader turns it into ``TraceSchemaError`` naming the file.
Nothing is ever kept as a bare dict.
"""

from __future__ import annotations

from collections.abc import Callable
from types import NoneType, UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.schemas.action import ActionEffect

#: How an ``effect_id`` becomes the class that reads it.
EffectResolver = Callable[[str], type[ActionEffect]]


def registered_effect(effect_id: str) -> type[ActionEffect]:
    """The ``Effect`` of the action registered under *effect_id*.

    Raises:
        ValueError: no action is registered under the key, or the action
            declares no ``Effect``.
    """
    try:
        action = ActionRegistrar.get(effect_id)
    except KeyError:
        raise ValueError(
            f"recorded action effect {effect_id!r} names no registered action"
        ) from None
    effect = getattr(action, "Effect", None)
    if not (isinstance(effect, type) and issubclass(effect, ActionEffect)):
        raise ValueError(
            f"recorded action effect {effect_id!r}: action {action.__name__} declares no Effect"
        )
    return effect


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
    return _Walk(resolve).value(model, data)


class _Walk:
    """One restoration: the resolver and which annotations can hold an effect."""

    def __init__(self, resolve: EffectResolver) -> None:
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
                    f"{annotation!r}: more than one union member can hold an action effect, "
                    f"so a recorded one cannot be placed"
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
        if isinstance(annotation, type) and issubclass(annotation, ActionEffect):
            return self._effect(value)
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
        """Whether a value of *annotation* can hold an action effect anywhere inside it."""
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
        if isinstance(annotation, type) and issubclass(annotation, ActionEffect):
            return True
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return any(self.carries(field.annotation) for field in annotation.model_fields.values())
        return any(self.carries(arg) for arg in get_args(annotation) if arg is not Ellipsis)

    def _effect(self, value: Any) -> ActionEffect:
        if isinstance(value, ActionEffect):
            return value
        if not isinstance(value, dict):
            raise ValueError(
                f"a recorded action effect is a JSON object, not {type(value).__name__}"
            )
        effect_id = value.get("effect_id")
        if not isinstance(effect_id, str) or not effect_id:
            raise ValueError("a recorded action effect has no effect_id naming its action")
        return self._resolve(effect_id).model_validate(value)


__all__ = ["EffectResolver", "registered_effect", "restore_effects"]
