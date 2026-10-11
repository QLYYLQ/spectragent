"""``identity``: the teacher's actions are the student's (arch §4.15).

For a student that speaks the teacher's action space — a UI-TARS run
exported for a UI-TARS student records coordinate actions already. Each
action is still validated against its ``Params``; an action no registered
class reads is untranslatable (``no_counterpart``), not invented.
"""

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.trace.reader import RunTrace
from spectragent.export.translate._registrar import ActionTranslatorBase, Translation
from spectragent.schemas.sample import CanonicalStepSample


class IdentityTranslator(ActionTranslatorBase):
    """The teacher's executed actions, unchanged."""

    __registry_key__ = "identity"

    def translate(self, sample: CanonicalStepSample, trace: RunTrace) -> Translation:
        for action in sample.teacher_actions:
            if ActionRegistrar.get_or_none(action.name) is None:
                return Translation.refused(
                    "no_counterpart", f"{action.name} is not a registered action"
                )
        return Translation(
            actions=[self.validated(sample.sample_id, action) for action in sample.teacher_actions]
        )


__all__ = ["IdentityTranslator"]
