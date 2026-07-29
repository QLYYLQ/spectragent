"""Observation fragments that belong to no capability package.

The rule this module holds to: **a fragment lives with the code that
produces it.** A fragment owns its own JSON shape, so a fragment that
carries a domain's internal types has to sit inside that domain's
package — the browser ones are in
``core/capabilities/browser/observations.py``, next to the
``DOMElementNode`` that ``DOMObs`` has to encode. That is the same split
as ``core/actions/`` (protocol + registrar) vs
``core/capabilities/browser/actions/`` (the concrete units), and it is
why nothing here names a field of any domain.

What is left is what has no capability to live in:

- the ``loop.*`` fragments, filled by the Loop rather than by an
  Environment. "Loop" is a framework concept, not a domain, so this is
  their home;
- ``docker.terminal`` and ``tool_sandbox.tool_result``, placeholders for
  capabilities that are not built yet. They carry plain scalars and name
  no internal type; they move into their capability packages when those
  packages land.

See docs/architecture_design.md §4.4 for the Fragment registry design and
docs/execution_dag.md M02 for the complete fragment list.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.step_record import StepRecord

# ── Loop-sourced fragments ─────────────────────────────────────────


class ActionResultObs(FragmentBase):
    """Result of the last action — filled by Loop, not Environment."""

    __registry_key__ = "loop.action_result"
    __source__: ClassVar[Literal["env", "loop"]] = "loop"

    last_action: str
    success: bool
    error: str | None = None


class TaskObs(FragmentBase):
    """User task / request — filled by Loop on every step.

    ``user_request`` is the original task string. ``follow_ups`` carries
    additional user requests appended mid-run (mirrors browser-use's
    ``add_new_task`` flow); empty for fresh runs.
    """

    __registry_key__ = "loop.task"
    __source__: ClassVar[Literal["env", "loop"]] = "loop"

    user_request: str
    follow_ups: list[str] = Field(default_factory=list)


class StepInfoObs(FragmentBase):
    """Per-step counters and date — filled by Loop.

    ``step`` is 1-indexed to match upstream browser-use prompt convention.
    ``today`` is ISO date string; sourced from ``ReactLoop.today_override``
    when set (for replay/test reproducibility), else from real clock.
    """

    __registry_key__ = "loop.step_info"
    __source__: ClassVar[Literal["env", "loop"]] = "loop"

    step: int
    max_steps: int
    today: str


class HistoryObs(FragmentBase):
    """Trajectory history — filled by Loop.

    Carries raw ``StepRecord`` instances; rendering and truncation are
    each agent's responsibility (per the architecture's separation of
    Agent prompt building from Loop execution).
    """

    __registry_key__ = "loop.history"
    __source__: ClassVar[Literal["env", "loop"]] = "loop"

    items: list[StepRecord] = Field(default_factory=list)


# ── SWE / Docker fragments ────────────────────────────────────────


class TerminalObs(FragmentBase):
    """Terminal output from Docker capability."""

    __registry_key__ = "docker.terminal"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    stdout: str
    stderr: str = ""
    exit_code: int = 0
    cwd: str = ""

    def summary(self) -> str:
        return f"exit={self.exit_code} cwd={self.cwd or '?'}"


# ── Tool-Use fragments ────────────────────────────────────────────


class ToolResultObs(FragmentBase):
    """Result of a tool invocation from ToolSandbox capability."""

    __registry_key__ = "tool_sandbox.tool_result"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    tool_name: str
    result: Any = None
    success: bool = True
