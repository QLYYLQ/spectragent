"""First-batch observation fragments — 6 fragments covering major scenarios.

See docs/architecture_design.md §4.4 for the Fragment registry design and
docs/execution_dag.md M02 for the complete fragment list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field

from spectragent.schemas.observation._registrar import FragmentBase

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.dom.models import DOMState


# ── Browser fragments ──────────────────────────────────────────────


class DOMObs(FragmentBase):
    """DOM tree observation from browser capability."""

    __registry_key__ = "dom"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    element_tree: Any = None  # DOMState at type-check time
    selector_map: dict[str, Any] = Field(default_factory=dict)


class ScreenshotObs(FragmentBase):
    """Screenshot observation from browser or desktop capability.

    Either ``image_path`` (on-disk PNG) or ``image_bytes`` (in-memory PNG)
    must be set; consumers prefer ``image_path`` when both are present.
    """

    __registry_key__ = "screenshot"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_path: str | None = None
    image_bytes: bytes | None = None
    viewport_width: int = 0
    viewport_height: int = 0


class BrowserMetaObs(FragmentBase):
    """Browser metadata: URL, title, tab info."""

    __registry_key__ = "browser_meta"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    url: str = ""
    page_title: str = ""
    tabs: list[dict[str, Any]] = Field(default_factory=list)


# ── Loop-sourced fragment ──────────────────────────────────────────


class ActionResultObs(FragmentBase):
    """Result of the last action — filled by Loop, not Environment."""

    __registry_key__ = "action_result"
    __source__: ClassVar[Literal["env", "loop"]] = "loop"

    last_action: str
    success: bool
    error: str | None = None


# ── SWE / Docker fragments ────────────────────────────────────────


class TerminalObs(FragmentBase):
    """Terminal output from Docker capability."""

    __registry_key__ = "terminal"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    stdout: str
    stderr: str = ""
    exit_code: int = 0
    cwd: str = ""


# ── Tool-Use fragments ────────────────────────────────────────────


class ToolResultObs(FragmentBase):
    """Result of a tool invocation from ToolSandbox capability."""

    __registry_key__ = "tool_result"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    tool_name: str
    result: Any = None
    success: bool = True
