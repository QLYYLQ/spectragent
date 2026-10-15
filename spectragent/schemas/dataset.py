"""The training dataset's manifest: what one export wrote, and from what (arch §4.15).

``spectragent export`` writes ``<dataset>/manifest.json`` through
:class:`DatasetManifest`, and every reader of a dataset — the flywheel
metrics report (arch §4.16) — reads it back through the same model. A key
renamed on one side, or a value of another type, then fails the read naming
the field, instead of reading as an empty count.

Machine-independent by construction: no wall-clock field, no absolute path,
no host name — the exporter's path knobs never enter it and the student's
configuration only as a digest. Domain-neutral like ``schemas/sample.py``:
the knobs of one export (``exporter``, ``translator``) are recorded as the
exporter dumps them, and every enumeration that belongs to the exporter's own
settings is a plain string here.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator

from spectragent.schemas.observation.geometry import CoordinateSpace

#: The dataset format this build writes and reads (``manifest.json``'s
#: ``format``). 2: decontamination is an explicit benchmark list
#: (``decontamination.decontaminated``), and the leak scan counts only the
#: samples it compared (``leak_scan.compared``).
DATASET_FORMAT: Final = "af-sft-2"


class _Part(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StudentSummary(_Part):
    """The student the samples were rendered for.

    Attributes:
        config_sha256: sha256 of the student's validated configuration, as
            canonical JSON — the configuration itself is not recorded (it
            names paths).
        agent_id / loop_id: The student's agent and Loop.
        coordinate_space: The grid the student writes points on.
    """

    config_sha256: str
    agent_id: str
    loop_id: str
    coordinate_space: CoordinateSpace


class ExportedSource(_Part):
    """One source run that produced samples."""

    path: str  # relative to the sources root, POSIX
    run_id: str
    config_hash: str
    samples: int


class SourceLedger(_Part):
    """Every matched source, by what became of it (relative path → reason)."""

    exported: list[ExportedSource]
    skipped: dict[str, str]  # cannot be exported: unreadable, never closed, secrets recorded …
    filtered: dict[str, str]  # left out by a filter or by decontamination


class StepLedger(_Part):
    """Emitted steps that did not become samples, by reason."""

    untranslatable: dict[str, int]
    skipped: dict[str, int]


class ThoughtLedger(_Part):
    """Where the target thoughts came from."""

    source: str  # the exporter's thought_source
    fields: dict[str, int]  # the teacher field each target thought came from
    dropped_index_refs: int


class CaptureLedger(_Part):
    """Capture settings the student's capture check could not compare."""

    unchecked: list[str]


class SplitLedger(_Part):
    """The train / validation split, never by step."""

    by: str
    validation_fraction: float
    salt: str
    samples: dict[str, int]  # split → samples
    keys: dict[str, str]  # split key (site or task) → split


class BenchmarkEntry(_Part):
    """An evaluation benchmark decontamination checked."""

    benchmark: str
    subset: str | None


class DecontaminationLedger(_Part):
    """Which evaluation benchmarks the sources were checked against.

    Attributes:
        decontaminated: Whether any benchmark was checked. ``False`` — the
            default, an empty ``against`` — says nothing was.
        against: The benchmarks checked, exactly as configured.
        match: How a source's task was matched.
        excluded: Sources left out as an evaluation task.
    """

    decontaminated: bool
    against: list[BenchmarkEntry]
    match: str
    excluded: int


class LeakScanLedger(_Part):
    """The reference-leak scan's coverage: compared, or why a sample was not.

    Attributes:
        compared: Samples whose reference answer was looked for in the
            prompt's own text.
        no_eval_loader: Benchmark → samples whose benchmark has no eval
            loader to read a reference from.
        no_reference_answer: Benchmark → samples whose reference carries no
            answer to look for (none, or one too short to scan for).
    """

    compared: int
    no_eval_loader: dict[str, int]
    no_reference_answer: dict[str, int]


class DatasetManifest(_Part):
    """``<dataset>/manifest.json`` (module docstring; the keys are arch §4.15's)."""

    format: str
    exporter: dict[str, Any]
    translator: dict[str, Any]
    student: StudentSummary
    sources: SourceLedger
    steps: StepLedger
    samples: int
    quantization_px: dict[str, int]  # point error in px → points
    thoughts: ThoughtLedger
    step_info: dict[str, int]  # where each target's step_info came from → targets
    capture: CaptureLedger
    split: SplitLedger
    decontamination: DecontaminationLedger
    leak_scan: LeakScanLedger
    duplicates: int  # branch samples flagged duplicate_of

    @field_validator("format")
    @classmethod
    def _this_format(cls, value: str) -> str:
        if value != DATASET_FORMAT:
            raise ValueError(
                f"dataset format {value!r}: this build reads {DATASET_FORMAT!r} — export the "
                f"dataset again"
            )
        return value


__all__ = [
    "DATASET_FORMAT",
    "BenchmarkEntry",
    "CaptureLedger",
    "DatasetManifest",
    "DecontaminationLedger",
    "ExportedSource",
    "LeakScanLedger",
    "SourceLedger",
    "SplitLedger",
    "StepLedger",
    "StudentSummary",
    "ThoughtLedger",
]
