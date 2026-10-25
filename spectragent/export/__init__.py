"""Training export: recorded runs in, a student model's dataset out (arch §4.15).

An offline consumer of ``RunTrace``, like an evaluator: it sets up no
environment, calls no student or teacher model (only, under
``wait_actions: drop_when_capped``, its own thought rewriter) and never writes
a source trace. The pipeline
(``runner.py``, driven by ``spectragent export`` in ``cli.py``) — sources and
filters (``filters.py``), canonical samples (``canonical.py``), translation
into the student's action space (``translate/``), student rendering
(``render.py``), the optional removal of waits taken on capped captures and
the rewriting of the thoughts it affects (``waits.py``), an exporter's record
format (``_registrar.py``,
``openai_messages.py``), validation (``validate.py``), split and
decontamination (``split.py``), and the benchmark loaders both
decontamination and the leak scan read through (``benchmarks.py``) — is
described in the architecture's §4.15.

Importing this package registers the implementations, as the evaluator
package does, so a composed ``exporter`` or ``action_translator`` key
resolves: ``OpenAIMessagesExporter``, ``IdentityTranslator`` and
``IndexToCoordinateTranslator`` are imported below for that.
"""

from spectragent.export._registrar import (
    Exporter,
    ExporterBase,
    ExporterRegistrar,
    ExportRecord,
    ImageStore,
)
from spectragent.export.canonical import (
    ThoughtSource,
    canonicalise,
    executed_actions,
    is_exportable,
    target_actions,
)
from spectragent.export.errors import (
    InvalidTranslationError,
    OcclusionEvidenceMissingError,
    ThoughtRewriteCacheError,
    missing_export_slots,
    needed_for_export,
)
from spectragent.export.openai_messages import OpenAIMessagesExporter
from spectragent.export.translate import (
    ActionTranslator,
    ActionTranslatorBase,
    ActionTranslatorRegistrar,
    Translation,
    UntranslatablePolicy,
    default_translator,
)
from spectragent.export.translate.identity import IdentityTranslator
from spectragent.export.translate.index_to_coordinate import (
    IndexToCoordinateTranslator,
)

__all__ = [
    "ActionTranslator",
    "ActionTranslatorBase",
    "ActionTranslatorRegistrar",
    "ExportRecord",
    "Exporter",
    "ExporterBase",
    "ExporterRegistrar",
    "IdentityTranslator",
    "ImageStore",
    "IndexToCoordinateTranslator",
    "InvalidTranslationError",
    "OcclusionEvidenceMissingError",
    "OpenAIMessagesExporter",
    "ThoughtRewriteCacheError",
    "ThoughtSource",
    "Translation",
    "UntranslatablePolicy",
    "default_translator",
    "canonicalise",
    "executed_actions",
    "is_exportable",
    "missing_export_slots",
    "needed_for_export",
    "target_actions",
]
