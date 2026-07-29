"""Agent prompt resolution — registry key *is* the directory name.

``spectragent/prompts/agents/<agent_id>/<name>.md``

The mirror of :mod:`spectragent.evaluator.prompts`, and for the same
reason. Before this module every agent carried its own
``_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / <name>`` constant: a
literal path fragment repeated three times, naming a ``browser/`` domain
segment that no code needed and that was factually wrong for UI-TARS —
whose three templates contain no browser vocabulary at all and whose
model family drives desktops and phones. Deriving the directory from
``self.__registry_key__`` deletes the constant, the domain segment, and
the possibility of the two drifting.

The one thing the evaluator tree does not have to solve is the engine
split: ``browser_use`` and ``ui_tars`` templates are ``str.format``,
``skyvern``'s is Jinja2. The engine is **declared**
(``AgentBase.__prompt_engine__``), never sniffed from the template text.
Sniffing would make a template's meaning depend on whether it happens to
contain a ``{%``, so adding a literal ``{%`` to a format template would
silently re-interpret the whole file.

:func:`template_variables` exists so the engine is known in exactly one
place. ``tests/test_agents/test_prompt_tree.py`` asserts that a
template's variables are precisely the params its agent supplies — the
mechanical detector for "the negotiated action set never reached the
prompt", which shipped once and made an agent unable to finish a task.
Letting that test re-derive the engine dispatch would let the two drift
apart in the one place whose whole job is to catch drift.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import Literal

import jinja2
import jinja2.meta

from spectragent.prompts import (
    PROMPT_PATH,
    PromptNotFoundError,
    hash_text,
)
from spectragent.schemas.prompt import PromptRef

AGENT_PROMPT_ROOT: Path = PROMPT_PATH / "agents"
"""Root of the single agent prompt tree, keyed by ``agent_id``."""

PROMPT_SUFFIX = ".md"
"""Agent prompts are markdown documents — headings, lists, fenced JSON."""

PromptEngine = Literal["format", "jinja2"]
"""How a template's placeholders are substituted. Declared, never sniffed."""


def agent_prompt_dir(agent_id: str) -> Path:
    """Directory holding one agent's prompts."""
    return AGENT_PROMPT_ROOT / agent_id


def agent_prompt_variants(agent_id: str) -> tuple[str, ...]:
    """Every template name this agent has on disk, sorted.

    The source of truth for a ``prompt_template`` field's ``Literal``:
    the field is hand-written (a directory-computed ``Literal`` costs a
    pyright error per field) and a test asserts the two agree, so a
    template added without being wired up fails the suite instead of
    sitting unreachable.
    """
    directory = agent_prompt_dir(agent_id)
    if not directory.is_dir():
        return ()
    return tuple(sorted(p.stem for p in directory.glob(f"*{PROMPT_SUFFIX}")))


def load_prompt(agent_id: str, name: str) -> tuple[str, PromptRef]:
    """Read one prompt file and pin its content hash.

    Returns ``(text, ref)``. The ref's ``path`` is relative to the
    prompts package so a recorded run stays meaningful when the repo
    moves, and ``content_hash`` makes "was this trajectory produced by
    the prompt I am reading now?" answerable — agents used to record the
    empty string there.
    """
    path = agent_prompt_dir(agent_id) / f"{name}{PROMPT_SUFFIX}"
    if not path.is_file():
        available = agent_prompt_variants(agent_id)
        raise PromptNotFoundError(
            f"Agent {agent_id!r} requested prompt {name!r} but {path} does "
            f"not exist. Available in that directory: "
            f"{list(available) or '(directory missing)'}."
        )
    text = path.read_text("utf-8")
    ref = PromptRef(path=str(path.relative_to(PROMPT_PATH)), content_hash=hash_text(text))
    return text, ref


def template_variables(engine: PromptEngine, template: str) -> set[str]:
    """Every name *template* substitutes, under *engine*.

    Deliberately not "every name it could possibly reference": for the
    format engine a nested field access (``{obs.dom}``) or an index
    (``{items[0]}``) is reported as the root name, because the root is
    what ``format`` looks up in the params dict.
    """
    if engine == "jinja2":
        parsed = jinja2.Environment(autoescape=False).parse(template)
        return set(jinja2.meta.find_undeclared_variables(parsed))
    return {
        field.split(".")[0].split("[")[0]
        for _, field, _, _ in string.Formatter().parse(template)
        if field
    }


def render_template(engine: PromptEngine, template: str, params: dict[str, object]) -> str:
    """Substitute *params* into *template* using *engine*."""
    if engine == "jinja2":
        return jinja2.Template(template, autoescape=False).render(**params)
    return template.format(**params)
