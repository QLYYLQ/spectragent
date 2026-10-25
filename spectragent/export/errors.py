"""Errors of the training export (arch §4.15).

Each names what it refused, so a failed export says which source, sample,
element or slot to look at.
"""

from __future__ import annotations

from spectragent.core.loops.validation import IncompatibleError


def needed_for_export(loop_id: str) -> str:
    """The ``IncompatibleError(needed_for=...)`` phrase of an export-time slot check (arch §4.1)."""
    return f"needed for training export (optional slots of loop '{loop_id}')"


def missing_export_slots(agent_name: str, loop_id: str, slots: list[str]) -> IncompatibleError:
    """The error for an agent that lacks the optional ReAct slots the export calls.

    The existing ``IncompatibleError`` (arch §4.1): no export-only error
    type, only the wording — a missing optional slot must not read like a
    Loop's required one.
    """
    return IncompatibleError(agent_name, loop_id, slots, needed_for=needed_for_export(loop_id))


class InvalidTranslationError(ValueError):
    """A translator produced an action its executor could not read.

    Every translated action passes its ``Params`` (arch §4.15); one that
    does not is a translator defect or a recorded action no registered
    class reads, never a policy decision.

    Attributes:
        sample_id: The sample being translated.
        action_name: The offending action.
        detail: Why it did not validate.
    """

    def __init__(self, sample_id: str, action_name: str, detail: str) -> None:
        self.sample_id = sample_id
        self.action_name = action_name
        self.detail = detail
        super().__init__(f"{sample_id}: translated action {action_name} is invalid: {detail}")


class OcclusionEvidenceMissingError(Exception):
    """``occlusion: check`` met a step whose recording holds no occlusion evidence.

    Raised by the ``index_to_coordinate`` translator under
    ``occlusion_unknown: raise`` (its default): converting an element to a
    point without knowing whether the point is covered would turn a click
    on a sticky header into a training label for the link beneath it.

    Attributes:
        sample_id: The sample the step would have become.
        element_index: The element the action addressed.
    """

    HINT = "bind browser.dom_raw or record with cleaner skyvern_hit_test"

    def __init__(self, sample_id: str, element_index: int) -> None:
        self.sample_id = sample_id
        self.element_index = element_index
        super().__init__(
            f"{sample_id}: no occlusion evidence for element [{element_index}] — "
            f"{self.HINT}, or set occlusion_unknown: untranslatable"
        )


class ExportError(Exception):
    """A training export that cannot finish; nothing was written (arch §4.15 "流水线").

    Every failure of the pipeline after configuration is one of these, so the
    CLI and a caller can tell "the export refused" from a defect; the output
    is written to ``<output_dir>.partial`` and renamed only when everything
    passed, so a raised error never leaves half a dataset.
    """


class ExportConfigError(ExportError):
    """The export's configuration cannot run as given (it names what to change)."""


class StudentCaptureMismatchError(ExportError):
    """The student's configuration would not capture what a source run recorded.

    Raised when the student environment's ``compare_capture`` (arch §4.3,
    §4.9) reports a difference for a sampled step — training on images the
    student never sees when it serves.

    Attributes:
        source: The source run (relative path).
        step: The step whose recording was compared.
        mismatches: The differences, as the capability named them.
    """

    def __init__(self, source: str, step: int, mismatches: tuple[str, ...]) -> None:
        self.source = source
        self.step = step
        self.mismatches = mismatches
        super().__init__(
            f"{source}: step {step} was recorded with a capture the student's configuration "
            f"would not reproduce: " + "; ".join(mismatches)
        )


class UntranslatableActionError(ExportError):
    """A step has no target in the student's action space, under ``untranslatable: fail``.

    Attributes:
        sample_id: The step's sample.
        reason: The :data:`~spectragent.schemas.sample.UntranslatableReason`.
        detail: What could not be translated or written.
    """

    def __init__(self, sample_id: str, reason: str, detail: str) -> None:
        self.sample_id = sample_id
        self.reason = reason
        self.detail = detail
        super().__init__(f"{sample_id}: untranslatable ({reason}): {detail}")


class RoundTripError(ExportError):
    """A reply the student's ``format_react_output`` wrote does not read back as its actions.

    A defect of the formatter, never an untranslatable step (arch §4.15
    "round-trip"): the reply would train the student to do something other
    than the action it was written for.

    Attributes:
        sample_id: The step whose reply failed.
        detail: Which clause failed, and how.
    """

    def __init__(self, sample_id: str, detail: str) -> None:
        self.sample_id = sample_id
        self.detail = detail
        super().__init__(f"{sample_id}: the student's reply does not round-trip: {detail}")


class ThoughtRewriteCacheError(ExportError):
    """An entry of the thought-rewrite cache does not read back as the rewrite asked for.

    The cache is content addressed (arch §4.15 "等待动作"): an entry that
    does not parse, or whose recorded inputs differ from the request its
    name stands for, is damaged or was written by something else. It is
    never used as if it fitted; delete it and export again.

    Attributes:
        path: The entry's file name inside the cache directory.
        detail: What does not fit.
    """

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(
            f"thought-rewrite cache entry {path}: {detail} — delete it and export again"
        )


class ExportEmptyError(ExportError):
    """The export produced no sample; the counts say where every source and step went.

    Attributes:
        counts: Reason → how many sources or steps it took out.
    """

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = dict(sorted(counts.items()))
        listing = ", ".join(f"{reason}: {count}" for reason, count in self.counts.items())
        super().__init__(
            "the export produced no sample (nothing was written)"
            + (f"; {listing}" if listing else "; no source matched")
        )


class ExportValidationError(ExportError):
    """A dataset check failed before anything was written (arch §4.15 "校验").

    Attributes:
        check: Which validator (``images``, ``coordinates``, ``reference_leak``,
            ``credentials``, ``length``).
        sample_id: The offending sample, or ``""`` for a file-level finding.
    """

    def __init__(self, check: str, sample_id: str, detail: str) -> None:
        self.check = check
        self.sample_id = sample_id
        self.detail = detail
        where = f"{sample_id}: " if sample_id else ""
        super().__init__(f"{check} check failed: {where}{detail}")


class ReferenceLeakError(ExportValidationError):
    """A task's reference answer appears in text the export itself puts into a prompt.

    Attributes:
        field: Where it appeared (``seed`` messages or ``task`` text).
    """

    def __init__(self, sample_id: str, field: str) -> None:
        self.field = field
        super().__init__(
            "reference_leak",
            sample_id,
            f"the task's reference answer appears in the {field} text the prompt carries",
        )


class CredentialLeakError(ExportValidationError):
    """A credential-shaped string would be written into the dataset.

    Never echoes the value: the file, the sample and the pattern's label.

    Attributes:
        file: The dataset file (relative to its root).
        label: The ``SECRET_PATTERNS`` label that matched.
    """

    def __init__(self, file: str, sample_id: str, label: str) -> None:
        self.file = file
        self.label = label
        super().__init__(
            "credentials",
            sample_id,
            f"{file} would contain a {label} (value withheld); nothing was written",
        )


class SampleTooLongError(ExportValidationError):
    """A record's estimated length exceeds ``length.max_tokens`` — never truncated.

    Attributes:
        estimated: The estimate.
        max_tokens: The ceiling.
    """

    def __init__(self, sample_id: str, estimated: int, max_tokens: int) -> None:
        self.estimated = estimated
        self.max_tokens = max_tokens
        super().__init__(
            "length",
            sample_id,
            f"an estimated {estimated} tokens exceeds length.max_tokens {max_tokens} "
            f"(the record is never truncated)",
        )


__all__ = [
    "CredentialLeakError",
    "ExportConfigError",
    "ExportEmptyError",
    "ExportError",
    "ExportValidationError",
    "InvalidTranslationError",
    "OcclusionEvidenceMissingError",
    "ReferenceLeakError",
    "RoundTripError",
    "SampleTooLongError",
    "StudentCaptureMismatchError",
    "UntranslatableActionError",
    "missing_export_slots",
    "needed_for_export",
]
