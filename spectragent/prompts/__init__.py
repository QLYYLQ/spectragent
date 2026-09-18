"""The prompt tree, and the three things every reader of it needs.

Layout is one directory per registry key, one level below the kind::

    spectragent/prompts/agents/<agent_id>/<name>.md
    spectragent/prompts/evaluators/<evaluator_id>/<name>.txt

There is deliberately no alias table anywhere: ``self.__registry_key__``
*is* the path segment. Two loaders read this tree —
:mod:`spectragent.agents.prompts` and :mod:`spectragent.evaluator.prompts`
— and they share the failure mode, the content hash and the snapshot
constructors so that a prompt behaves the same whichever side loads it.

The extensions differ because the files do: an evaluator prompt is a
plain-text judge instruction, an agent prompt is a markdown document with
headings and fenced examples. Each loader pins its own suffix.
"""

import hashlib
from pathlib import Path

from spectragent.schemas.prompt import PromptRef, TemplatePrompt, TextPrompt

PROMPT_PATH: Path = Path(__file__).resolve().parent
"""Root directory for all prompt templates."""


class PromptNotFoundError(LookupError):
    """A registry key asked for a prompt file that is not on disk.

    A ``LookupError`` rather than an I/O error because the miss is a
    *keyed* one: the caller named a registry key and a template name, and
    the message says which names that directory does hold. The bare
    ``FileNotFoundError`` this replaces printed an absolute path and
    nothing else, which for a typo'd template name is the least useful
    thing to say.
    """


def hash_text(text: str) -> str:
    """SHA-256 of a prompt's bytes — the version identifier.

    Independent of filesystem metadata, so a fresh clone (mtime reset) or
    a moved repo still identifies the same prompt.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_snapshot(text: str, ref: PromptRef) -> TextPrompt:
    """Snapshot a prompt used verbatim."""
    return TextPrompt(ref=ref, text=text)


def template_snapshot(template: str, ref: PromptRef, params: dict[str, object]) -> TemplatePrompt:
    """Snapshot a prompt plus the substitutions that were applied.

    Params are stringified because ``TemplatePrompt.params`` is
    ``dict[str, str]`` — the snapshot records what the model saw, and the
    model only ever saw strings.
    """
    return TemplatePrompt(
        ref=ref,
        template=template,
        params={key: str(value) for key, value in params.items()},
    )
