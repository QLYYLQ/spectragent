"""Evaluator prompt resolution — registry key *is* the directory name.

``spectragent/prompts/evaluators/<evaluator_id>/<name>.txt``

There is deliberately no alias table. Before this module the repository
carried two byte-identical prompt trees — one git-tracked under
``prompts/browser/evaluators/`` with zero Python consumers, one gitignored
under ``benchmarks/prompts/`` that the real graders read — and *neither*
directory name matched a registry key (``BrowseComp`` vs ``browsecomp``,
``Online-Mind2Web`` vs ``online_mind2web``). Any evaluator built on top of
that needed a hand-maintained benchmark→directory map, which is the exact
hardcoded lookup the registrar design exists to delete. Renaming the
directories to the registry keys removes the need for the map entirely:
``self.__registry_key__`` is the path segment.

They also moved out from under ``browser/``. Scoring a trajectory is not
a browser concern — SWE-bench and OSWorld evaluators will live in the
same tree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from spectragent.evaluator.errors import PromptNotFoundError
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.prompt import (
    PromptRef,
    TemplatePrompt,
    TextPrompt,
)

EVALUATOR_PROMPT_ROOT: Path = PROMPT_PATH / "evaluators"
"""Root of the single evaluator prompt tree, keyed by ``evaluator_id``."""


def evaluator_prompt_dir(evaluator_id: str) -> Path:
    """Directory holding one evaluator's prompts."""
    return EVALUATOR_PROMPT_ROOT / evaluator_id


def load_prompt(evaluator_id: str, name: str) -> tuple[str, PromptRef]:
    """Read one prompt file and pin its content hash.

    Returns ``(text, ref)``. The ref's ``path`` is relative to the prompts
    package so a recorded ``EvalResult`` stays meaningful when the repo
    moves, and ``content_hash`` makes "was this verdict produced by the
    prompt I am reading now?" answerable.
    """
    path = evaluator_prompt_dir(evaluator_id) / f"{name}.txt"
    if not path.is_file():
        available = (
            sorted(p.stem for p in path.parent.glob("*.txt"))
            if path.parent.is_dir()
            else []
        )
        raise PromptNotFoundError(
            f"Evaluator {evaluator_id!r} requested prompt {name!r} but "
            f"{path} does not exist. Available in that directory: "
            f"{available or '(directory missing)'}."
        )
    text = path.read_text("utf-8")
    ref = PromptRef(
        path=str(path.relative_to(PROMPT_PATH)),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    return text, ref


def text_snapshot(text: str, ref: PromptRef) -> TextPrompt:
    """Snapshot a prompt used verbatim."""
    return TextPrompt(ref=ref, text=text)


def template_snapshot(
    template: str, ref: PromptRef, params: dict[str, object]
) -> TemplatePrompt:
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
