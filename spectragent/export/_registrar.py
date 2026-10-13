"""``ExporterRegistrar``: one export job and its record format (arch §4.15 "配置").

An exporter is the training export's top-level configuration: an
``exporter:`` block in an otherwise empty run YAML names the sources, the
filters, the split, the student's run configuration and where the dataset
goes, and carries the ``translator`` receptor (``ActionTranslatorRegistrar``,
``translator_id``) that maps the teacher's actions into the student's action
space. What differs between exporters is only the record format —
:meth:`Exporter.format` turns one rendered sample into one record; the
pipeline around it (``runner.py``) is the same for every format.

**Not a dataclass.** :class:`ExporterBase` hand-writes its keyword
``__init__`` with every job field as an ``Annotated`` parameter, and the
``translator`` receptor is an annotation-only class attribute with an
in-Python default (:func:`default_translator`, ``identity``) — the
``trace_branches`` shape (af-add-a-receptor §6). An exporter with no knob of
its own writes no ``__init__``; conscribe then reads this one.

.. note::
   ``from __future__ import annotations`` for the reason ``trace_branches.py``
   gives: conscribe writes a ``.pyi`` stub next to every registered class with
   an injected receptor and renders evaluated ``Annotated[..., Field(...)]``
   parameters as ``FieldInfo(...)`` reprs, while PEP 563 strings come out
   verbatim. The inherited hand-written ``__init__`` resolves its strings
   against this module's globals (its own ``__globals__``), so a subclass in
   another module loses nothing (``test_no_dropped_annotated_fields.py``
   checks).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import Field, TypeAdapter

from spectragent.core.trace.sources import source_pattern_problem
from spectragent.export.canonical import ThoughtSource
from spectragent.export.render import RenderedSample
from spectragent.export.settings import (
    DEFAULT_DECONTAMINATION,
    DEFAULT_FILTERS,
    DEFAULT_SPLIT,
    Decontamination,
    ExportFilters,
    LengthBudget,
    SplitSettings,
    ThoughtIndexRefs,
)
from spectragent.export.translate._registrar import ActionTranslator, default_translator

#: ``sources`` when the config leaves it null: every run directly under the root.
DEFAULT_SOURCES: tuple[str, ...] = ("*",)
#: ``sources_root`` when the config leaves it null (relative to the working directory).
DEFAULT_SOURCES_ROOT = "traces"


_FILTERS: TypeAdapter[ExportFilters] = TypeAdapter(ExportFilters)
_SPLIT: TypeAdapter[SplitSettings] = TypeAdapter(SplitSettings)
_DECONTAMINATION: TypeAdapter[Decontamination] = TypeAdapter(Decontamination)
_LENGTH: TypeAdapter[LengthBudget] = TypeAdapter(LengthBudget)


@runtime_checkable
class ImageStore(Protocol):
    """Where an exporter puts the images its records reference.

    ``image_store: files`` (v1): each distinct image is written once, named
    by its sha256, and referenced by a path relative to the dataset root.
    """

    def put(self, data: bytes, media_type: str) -> str:
        """Store *data* (once) and return the path a record references it by."""
        ...


@dataclass(frozen=True)
class ExportRecord:
    """One sample in an exporter's format.

    Attributes:
        sample_id: The sample it renders.
        record: The JSON object written as one line of ``<split>.jsonl``.
        images: The stored image paths the record references, in order of
            appearance (one entry per image part).
    """

    sample_id: str
    record: dict[str, Any]
    images: tuple[str, ...] = ()


@runtime_checkable
class Exporter(Protocol):
    """One export job: what to export, how to translate it, and the record format."""

    @property
    def exporter_id(self) -> str: ...

    @property
    def translator(self) -> ActionTranslator:
        """The configured translator (the ``translator`` receptor)."""
        ...

    sample: Literal["per_step"]
    image_store: Literal["files"]
    sources_root: str | None
    sources: tuple[str, ...]
    filters: ExportFilters
    split: SplitSettings
    decontamination: Decontamination
    student_run_config: str
    thought_source: ThoughtSource
    thought_index_refs: ThoughtIndexRefs
    length: LengthBudget | None
    output_dir: str

    def root(self) -> Path:
        """The sources root, absolute."""
        ...

    def format(self, rendered: RenderedSample, images: ImageStore) -> ExportRecord:
        """*rendered* as one record of this format, its images put in *images*."""
        ...


ExporterRegistrar = create_registrar(
    "exporter",
    Exporter,
    discriminator_field="exporter_id",
)


class ExporterBase(metaclass=ExporterRegistrar.Meta):
    """Base for exporters — subclass to auto-register; implement :meth:`format`."""

    __abstract__ = True
    __registry_key__: str
    __wiring__: ClassVar[dict[str, Any]] = {"translator": "action_translator"}

    # Annotation only: conscribe installs a ``WiredField`` and the Composer
    # injects the configured translator; ``__init__`` shadows it with the default.
    translator: ActionTranslator  # type: ignore[misc]

    # ── the job, as attributes (assigned in __init__) ──
    sample: Literal["per_step"]
    image_store: Literal["files"]
    sources_root: str | None
    sources: tuple[str, ...]
    filters: ExportFilters
    split: SplitSettings
    decontamination: Decontamination
    student_run_config: str
    thought_source: Literal["teacher", "none"]
    thought_index_refs: Literal["drop", "keep"]
    length: LengthBudget | None
    output_dir: str

    def __init__(
        self,
        *,
        student_run_config: Annotated[
            str,
            Field(
                description=(
                    "The student's run YAML (one agent, a react-family loop, no hooks): "
                    "samples are rendered by that agent's prepare / build_react_prompt / "
                    "memory and its format_react_output, so the training format is its "
                    "serving format."
                ),
            ),
        ],
        output_dir: Annotated[
            str,
            Field(
                description=(
                    "Where the dataset is written (relative to the working directory). "
                    "Written to <output_dir>.partial first and renamed only when every "
                    "check passed; an existing output_dir is refused."
                ),
            ),
        ],
        sample: Annotated[
            Literal["per_step"],
            Field(description="One sample per exportable step (the only v1 value)."),
        ] = "per_step",
        image_store: Annotated[
            Literal["files"],
            Field(
                description=(
                    "Images stored once per dataset as images/<sha256><suffix> and "
                    "referenced by that path (the only v1 value)."
                ),
            ),
        ] = "files",
        sources_root: Annotated[
            str | None,
            Field(
                description=(
                    "The directory the 'sources' globs are relative to ('~' expanded). "
                    "None: 'traces' in the working directory."
                ),
            ),
        ] = None,
        sources: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Globs relative to the sources root naming recorded run directories "
                    "(branch runs included); matches are sorted by path, and a match "
                    "without a manifest.json is ignored. Null (the default): ['*']."
                ),
            ),
        ] = None,
        filters: Annotated[
            ExportFilters | None,
            Field(
                description=(
                    "Which sources become samples: agent_outcome, require_eval, "
                    "exclude_restoration_diverged, max_document_misses, exclude_sites. "
                    "Null: the defaults (every agent outcome; branches whose restoration "
                    "failed a check and runs with a document miss left out). A source's trace "
                    "is always closed and complete through every step exported."
                ),
            ),
        ] = None,
        split: Annotated[
            SplitSettings | None,
            Field(
                description=(
                    "How samples split into train / validation — by site or by task, "
                    "never by step. Null: by site, 10% validation."
                ),
            ),
        ] = None,
        decontamination: Annotated[
            Decontamination | None,
            Field(
                description=(
                    "Which evaluation benchmarks rule sources out (against, match), read "
                    "through the document's eval_loader / task_loader blocks. Only the "
                    "benchmarks against lists are checked. Null: the defaults (against: [], "
                    "nothing decontaminated; match: either)."
                ),
            ),
        ] = None,
        thought_source: Annotated[
            Literal["teacher", "none"],
            Field(
                description=(
                    "teacher: each sample's thought is the teacher's own, read by its "
                    "agent's parse_react_thought; none: no thought in any turn."
                ),
            ),
        ] = "teacher",
        thought_index_refs: Annotated[
            Literal["drop", "keep"],
            Field(
                description=(
                    "drop: a teacher thought naming element indices ([123]) is dropped "
                    "when the student does not see the fragment those indices address "
                    "(the sample stays, without a thought); keep: kept as written."
                ),
            ),
        ] = "drop",
        length: Annotated[
            LengthBudget | None,
            Field(
                description=(
                    "The estimated token ceiling of one record (max_tokens, image_tokens: "
                    "qwen_vl | fixed); a longer record fails the export by name, never "
                    "truncated. Null: no length check."
                ),
            ),
        ] = None,
    ) -> None:
        # The receptor's in-Python default; the Composer injects the configured one.
        self.translator = default_translator()
        if not student_run_config:
            raise ValueError(f"{self.exporter_id}: student_run_config names the student's run YAML")
        if not output_dir:
            raise ValueError(f"{self.exporter_id}: output_dir names where the dataset goes")
        self.student_run_config = student_run_config
        self.output_dir = output_dir
        self.sample = sample
        self.image_store = image_store
        self.sources_root = sources_root
        self.sources = tuple(sources) if sources is not None else DEFAULT_SOURCES
        for pattern in self.sources:
            problem = source_pattern_problem(pattern)
            if problem is not None:
                raise ValueError(f"{self.exporter_id}: source glob {pattern!r} {problem}")
        # The Composer hands a nested knob over as a dict; Python callers pass the type.
        self.filters = (
            DEFAULT_FILTERS
            if filters is None
            else filters
            if isinstance(filters, ExportFilters)
            else _FILTERS.validate_python(filters)
        )
        self.split = (
            DEFAULT_SPLIT
            if split is None
            else split
            if isinstance(split, SplitSettings)
            else _SPLIT.validate_python(split)
        )
        self.decontamination = (
            DEFAULT_DECONTAMINATION
            if decontamination is None
            else decontamination
            if isinstance(decontamination, Decontamination)
            else _DECONTAMINATION.validate_python(decontamination)
        )
        self.thought_source = thought_source
        self.thought_index_refs = thought_index_refs
        self.length = (
            length
            if length is None or isinstance(length, LengthBudget)
            else _LENGTH.validate_python(length)
        )

    @property
    def exporter_id(self) -> str:
        return self.__registry_key__

    def root(self) -> Path:
        """The sources root, absolute (``sources_root``, or ``traces`` in the working directory)."""
        root = self.sources_root if self.sources_root is not None else DEFAULT_SOURCES_ROOT
        return Path(root).expanduser().resolve()

    def format(self, rendered: RenderedSample, images: ImageStore) -> ExportRecord:
        raise NotImplementedError(f"exporter {self.exporter_id!r} must implement format()")


__all__ = [
    "DEFAULT_SOURCES",
    "DEFAULT_SOURCES_ROOT",
    "ExportRecord",
    "Exporter",
    "ExporterBase",
    "ExporterRegistrar",
    "ImageStore",
]
