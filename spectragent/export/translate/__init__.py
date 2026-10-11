"""Action translators: the teacher's action space → the student's (arch §4.15).

``ActionTranslatorRegistrar`` (``translator_id``) is the export's second
optional top-level layer and the exporter's ``translator`` receptor.
Importing :mod:`spectragent.export` registers the shipped translators
(``identity``, ``index_to_coordinate``).
"""

from spectragent.export.translate._registrar import (
    ActionTranslator,
    ActionTranslatorBase,
    ActionTranslatorRegistrar,
    Translation,
    UntranslatablePolicy,
    default_translator,
)

__all__ = [
    "ActionTranslator",
    "ActionTranslatorBase",
    "ActionTranslatorRegistrar",
    "Translation",
    "UntranslatablePolicy",
    "default_translator",
]
