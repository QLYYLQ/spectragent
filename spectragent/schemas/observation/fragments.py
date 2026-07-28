"""First-batch observation fragments — 6 fragments covering major scenarios.

See docs/architecture_design.md §4.4 for the Fragment registry design and
docs/execution_dag.md M02 for the complete fragment list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field, field_serializer

from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.step_record import StepRecord

if TYPE_CHECKING:
    pass


# ── DOM dataclass → JSON-safe dict ─────────────────────────────────
# DOMElementNode has a parent ↔ children cycle (see core/capabilities/
# browser/dom/models.py). Pydantic's default dataclass serializer trips
# on that cycle, so DOMObs converts the tree top-down here, dropping
# back-references and using a visited set to short-circuit any further
# cycles introduced by iframe / shadow DOM links.

_DOM_SKIP_FIELDS: frozenset[str] = frozenset(
    {"parent", "content_document", "shadow_roots", "_compound_children"}
)


def _dom_node_to_dict(node: Any, visited: set[int]) -> Any:
    if node is None:
        return None
    nid = id(node)
    if nid in visited:
        return {"__cycle__": getattr(node, "backend_node_id", None)}
    visited.add(nid)
    out: dict[str, Any] = {}
    for fname in node.__dataclass_fields__:
        if fname in _DOM_SKIP_FIELDS:
            continue
        out[fname] = _dom_value_to_dict(getattr(node, fname), visited)
    return out


def _dom_value_to_dict(value: Any, visited: set[int]) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dataclass_fields__"):
        if "tag_name" in value.__dataclass_fields__:
            return _dom_node_to_dict(value, visited)
        return {
            f: _dom_value_to_dict(getattr(value, f), visited)
            for f in value.__dataclass_fields__
        }
    if isinstance(value, dict):
        return {str(k): _dom_value_to_dict(v, visited) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dom_value_to_dict(v, visited) for v in value]
    return value


# ── Browser fragments ──────────────────────────────────────────────


class DOMObs(FragmentBase):
    """DOM tree observation from browser capability."""

    __registry_key__ = "browser.dom"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    element_tree: Any = None  # DOMState at type-check time
    # Keys are ``backend_node_id`` (int) per upstream agent-cdp contract
    # — see ``DOMSelectorMap = dict[int, DOMElementNode]`` in
    # ``core/capabilities/browser/dom/models.py``. The serializer below
    # stringifies keys for JSON output (where dict keys must be strings).
    selector_map: dict[int, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        return f"{len(self.selector_map)} interactive elements"

    @field_serializer("element_tree")
    def _serialize_element_tree(self, value: Any) -> Any:
        return _dom_value_to_dict(value, set())

    @field_serializer("selector_map")
    def _serialize_selector_map(self, value: dict[int, Any]) -> dict[str, Any]:
        visited: set[int] = set()
        return {str(k): _dom_value_to_dict(v, visited) for k, v in value.items()}


class ScreenshotObs(FragmentBase):
    """Screenshot observation from browser or desktop capability.

    Either ``image_path`` (on-disk PNG) or ``image_bytes`` (in-memory PNG)
    must be set; consumers prefer ``image_path`` when both are present.
    """

    __registry_key__ = "browser.screenshot"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_path: str | None = None
    image_bytes: bytes | None = None
    viewport_width: int = 0
    viewport_height: int = 0

    @field_serializer("image_bytes")
    def _serialize_image_bytes(self, value: bytes | None) -> str | None:
        if value is None:
            return None
        import base64

        return base64.b64encode(value).decode("ascii")

    def trace_binary(self) -> tuple[str, bytes] | None:
        """Persist the PNG as a real ``.png`` file, not base64 inside JSON."""
        if self.image_bytes is None:
            return None
        return ".png", self.image_bytes

    def trace_json_exclude(self) -> set[str]:
        return {"image_bytes"}

    def summary(self) -> str:
        if not self.viewport_width and not self.viewport_height:
            return ""
        return f"{self.viewport_width}x{self.viewport_height}"


class BrowserMetaObs(FragmentBase):
    """Browser metadata: URL, title, tab info."""

    __registry_key__ = "browser.browser_meta"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    url: str = ""
    page_title: str = ""
    tabs: list[dict[str, Any]] = Field(default_factory=list)

    def summary(self) -> str:
        if not self.url and not self.page_title:
            return ""
        return f"{self.page_title or '(untitled)'} <{self.url}>"


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
