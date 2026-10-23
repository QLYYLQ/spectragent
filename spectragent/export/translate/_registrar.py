"""``ActionTranslatorRegistrar``: how a teacher's actions become a student's (arch §4.15).

A translator does the student-independent half of the conversion — an
element index becomes a point on the recorded screenshot, say; whether the
student can *write* the result is decided when it is rendered. Both halves
report what they cannot do as an :class:`~spectragent.schemas.sample.Untranslatable`
reason, and both go through the one ``untranslatable`` policy a translator
carries; the renderer applies it (drop the step, truncate, keep it as text,
fail).

Every action a translator hands back passes its action's ``Params`` — a
translation that produced an action its executor could not read would be a
training label for a reply that fails.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import Field, ValidationError

from spectragent.core.actions._registrar import registered_params
from spectragent.core.trace.reader import RunTrace
from spectragent.export.errors import InvalidTranslationError
from spectragent.schemas.action import Action
from spectragent.schemas.sample import (
    CanonicalStepSample,
    TranslationRecord,
    Untranslatable,
    UntranslatableReason,
)

UntranslatablePolicy = Literal["drop_step", "truncate", "keep_as_text", "fail"]
"""What the renderer does with a step that has no target in the student's space.

``drop_step``: the step is not a sample and ends the context segment; the
next renderable step starts a new one. ``truncate``: the trajectory ends
there. ``keep_as_text``: not a sample, but its turns stay in the context,
the assistant turn written as ``[not expressible: <reason>] <action JSON>``.
``fail``: the export fails, naming the sample and reason.
"""


@dataclass(frozen=True)
class Translation:
    """One step's translation: the student-space actions, or why there are none.

    Attributes:
        actions: The translated actions, fully qualified and validated, or
            ``None`` when the step is untranslatable.
        untranslatable: Why, when ``actions`` is ``None``.
        records: The translator's provenance, one record per action it
            converted (opaque to the sample).
    """

    actions: list[Action] | None
    untranslatable: Untranslatable | None = None
    records: list[TranslationRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (self.actions is None) == (self.untranslatable is None):
            raise ValueError("a Translation carries actions or an untranslatable reason, not both")

    @classmethod
    def refused(
        cls,
        reason: UntranslatableReason,
        detail: str,
        records: Sequence[TranslationRecord] = (),
    ) -> "Translation":
        return cls(
            actions=None,
            untranslatable=Untranslatable(reason=reason, detail=detail),
            records=list(records),
        )

    def applied(self, sample: CanonicalStepSample) -> CanonicalStepSample:
        """*sample* with this translation recorded on it."""
        return sample.model_copy(
            update={
                "actions": self.actions,
                "untranslatable": self.untranslatable,
                "translation": list(self.records),
            }
        )


@runtime_checkable
class ActionTranslator(Protocol):
    """Turns one canonical sample's teacher actions into the student's action space."""

    @property
    def translator_id(self) -> str: ...

    untranslatable: UntranslatablePolicy

    def translate(self, sample: CanonicalStepSample, trace: RunTrace) -> Translation:
        """The translation of *sample*'s ``teacher_actions``, reading *trace* for evidence.

        Reports an untranslatable step in the result; raises only for what no
        policy can handle (a configured check without its evidence, say).
        """
        ...


ActionTranslatorRegistrar = create_registrar(
    "action_translator",
    ActionTranslator,
    discriminator_field="translator_id",
)


class ActionTranslatorBase(metaclass=ActionTranslatorRegistrar.Meta):
    """Base for translators — subclass to auto-register.

    Not a dataclass: knobs are hand-written ``__init__`` keyword params. A
    translator with no knob of its own writes no ``__init__`` (conscribe then
    reads this one); one with knobs writes its whole signature, this knob
    included.
    """

    __abstract__ = True
    __registry_key__: str

    # ── the knobs, as attributes (assigned in __init__) ──
    # Declared on the class so a reader (and the shipped-config gate) sees
    # their type; conscribe reads the ``__init__`` signature.
    untranslatable: UntranslatablePolicy

    def __init__(
        self,
        *,
        untranslatable: Annotated[
            Literal["drop_step", "truncate", "keep_as_text", "fail"],
            Field(
                description=(
                    "What the export does with a step that has no target in the student's "
                    "action space: drop_step (end the context segment), truncate (end the "
                    "trajectory), keep_as_text (keep its turns, write the action as text) "
                    "or fail."
                )
            ),
        ] = "drop_step",
    ) -> None:
        self.untranslatable = untranslatable

    @property
    def translator_id(self) -> str:
        return self.__registry_key__

    def translate(self, sample: CanonicalStepSample, trace: RunTrace) -> Translation:
        raise NotImplementedError(f"translator {self.translator_id!r} must implement translate()")

    @staticmethod
    def validated(sample_id: str, action: Action) -> Action:
        """*action* with its params checked against, and coerced by, its ``Params``.

        Only the params the action sets are kept, so a validated action says
        no more than the one handed in.

        Raises:
            InvalidTranslationError: No registered action has the name, or
                the params do not validate.
        """
        params_model = registered_params(action.name)
        if params_model is None:
            raise InvalidTranslationError(sample_id, action.name, "no registered action")
        try:
            checked = params_model.model_validate(action.params)
        except ValidationError as exc:
            raise InvalidTranslationError(sample_id, action.name, str(exc)) from exc
        return Action(name=action.name, params=checked.model_dump(exclude_unset=True))


def default_translator() -> ActionTranslator:
    """The translator when none is configured: ``identity``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for an exporter built by hand.
    """
    from spectragent.export.translate.identity import IdentityTranslator

    return IdentityTranslator()


__all__ = [
    "ActionTranslator",
    "ActionTranslatorBase",
    "ActionTranslatorRegistrar",
    "Translation",
    "UntranslatablePolicy",
    "default_translator",
]
