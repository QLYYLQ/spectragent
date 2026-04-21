"""UITarsAgent — vision-based agent with coordinate actions and AST parsing.

M14 refactor: UITars is now a passive domain-knowledge provider.
Pure-vision (no DOM) wiring, AST coordinate parser, and prompt template
selection (COMPUTER_USE / MOBILE_USE) remain agent-side.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._registrar import AgentBase
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops.errors import ParseError
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.action import Action, ActionDescriptor

# ── Prompt directory ─────────────────────────────────────────
_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / "ui_tars"


@dataclass
class UITarsAgent(AgentBase):
    """UI-TARS style agent — vision-based with AST action parsing."""

    __registry_key__ = "ui_tars"

    # ── Cross-registry wiring (M14 + M13-Rev) ────────────────────
    # Pure-vision: only screenshot is required, no DOM, no browser_meta.
    # UI-TARS emits coordinate-based clicks (``click_coordinate``) and
    # text-input via ``type`` — see ``_parse_single_action``.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react"]),
        "capability": ("capability", ["browser"]),
        "observation": ("observation", ["screenshot"], []),
        "action": (
            "action",
            ["click_coordinate", "type", "done"],
            ["scroll", "send_keys", "navigate"],
        ),
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    max_steps: Annotated[int, PydanticField(description="Maximum steps")] = 40
    prompt_template: Annotated[
        str,
        PydanticField(description="Prompt template: COMPUTER_USE, MOBILE_USE, or GROUNDING"),
    ] = "COMPUTER_USE"
    language: Annotated[str, PydanticField(description="Thinking language")] = "English"

    # ── internal state ─────────────────────────────────────────
    _task: str = field(default="", init=False, repr=False)
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_path: str = field(default="", init=False, repr=False)
    _prompt_snapshot: Any = field(default=None, init=False, repr=False)

    # ``llm`` and ``memory`` are wired from ``__wiring__`` (inherited from
    # ``AgentBase``) via conscribe's ``WiredField`` descriptors.

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def domain(self) -> str:
        return "web_navigation_vision"

    @property
    def observation_types(self) -> set[str]:
        return {"screenshot"}

    @property
    def system_prompt(self) -> Any:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(self._action_descriptors)

    # ── Lifecycle hook called by ReactLoop ─────────────────────

    def prepare(self, task: str, action_descriptors: list[ActionDescriptor]) -> None:
        """One-shot setup. Builds the templated system prompt and
        seeds memory."""
        self._task = task
        self._action_descriptors = list(action_descriptors)
        self._system_prompt_text = self._build_system_prompt(task, self._action_descriptors)
        from spectragent.schemas.prompt import PromptRef, TextPrompt

        self._prompt_snapshot = TextPrompt(
            ref=PromptRef(path=self._prompt_path or "<runtime>", content_hash=""),
            text=self._system_prompt_text,
        )
        self.memory.clear()
        self.memory.add_message(SystemMessage(content=self._system_prompt_text))

    # ── ReactLoop slots ────────────────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Append a screenshot user-message to memory and return the full list.

        UI-TARS is purely vision-driven, so we always include the
        screenshot. If the screenshot fragment is missing or has no
        bytes, we fall back to a text placeholder so the LLM call still
        succeeds (the agent will likely produce a `wait` action).

        [TODO M17] Memory access lives here.
        """
        screenshot = getattr(observation, "screenshot", None)
        image_bytes: bytes | None = None
        if screenshot is not None:
            image_bytes = getattr(screenshot, "image_bytes", None)
            if image_bytes is None and getattr(screenshot, "image_path", None):
                with open(screenshot.image_path, "rb") as f:
                    image_bytes = f.read()

        if image_bytes is None:
            self.memory.add_message(UserMessage(content="(no screenshot available)"))
        else:
            import base64

            b64 = base64.b64encode(image_bytes).decode()
            content: list[Any] = [
                ContentPartTextParam(text=f"Task: {self._task}"),
                ContentPartImageParam(
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{b64}",
                        detail="high",
                    ),
                ),
            ]
            self.memory.add_message(UserMessage(content=content))

        return self.memory.get_context_messages()

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Parse free-form text output via AST coordinate extraction."""
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        return _parse_ui_tars_response(text)

    def get_react_output_model(self) -> type[BaseModel] | None:
        # UI-TARS uses free-form text, not structured output.
        return None

    # ── private helpers ────────────────────────────────────────

    def _build_system_prompt(self, task: str, action_space: list[ActionDescriptor]) -> str:
        variant = self.prompt_template.lower()  # "COMPUTER_USE" → "computer_use"
        prompt_file = _PROMPT_DIR / f"{variant}.md"
        self._prompt_path = str(prompt_file.relative_to(PROMPT_PATH))
        template = prompt_file.read_text()
        actions_text = "\n".join(f"- {a.name}: {a.description}" for a in action_space)
        return template.format(task=task, action_space=actions_text)


# ── AST-based action parser ───────────────────────────────────


def _parse_ui_tars_response(text: str) -> list[Action]:
    """Parse UI-TARS text output into Action objects using AST parsing.

    Expected format:
        Thought: ...
        Action: click(point='(100, 200)')
    """
    action_pattern = re.compile(r"Action:\s*(.+?)(?:\n|$)", re.DOTALL)
    matches = action_pattern.findall(text)

    if not matches:
        raise ParseError(
            "No 'Action: ...' pattern found in UI-TARS output.",
            raw_output=text[:500],
        )

    actions: list[Action] = []
    for action_str in matches:
        action_str = action_str.strip()
        # Split multiple actions on )\n\n pattern
        parts = re.split(r"\)\s*\n\s*\n", action_str)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if not part.endswith(")"):
                part += ")"
            action = _parse_single_action(part)
            if action is not None:
                actions.append(action)

    if not actions:
        raise ParseError(
            "UI-TARS output contained 'Action:' lines but none were parseable.",
            raw_output=text[:500],
        )
    return actions


def _parse_single_action(action_str: str) -> Action | None:
    """Parse a single action call using Python AST.

    UI-TARS emits coordinate-based actions (``click(point='(x,y)')``);
    we map ``click`` -> ``click_coordinate`` so the BrowserCapability
    routes to the coordinate event class. ``finished`` -> ``done`` for
    consistency with the standard action vocabulary.
    """
    try:
        node = ast.parse(action_str, mode="eval")
        if not isinstance(node.body, ast.Call):
            return None

        # Extract function name
        if isinstance(node.body.func, ast.Name):
            func_name = node.body.func.id
        elif isinstance(node.body.func, ast.Attribute):
            func_name = node.body.func.attr
        else:
            return None

        # Extract keyword arguments
        kwargs: dict[str, Any] = {}
        for kw in node.body.keywords:
            if kw.arg is not None:
                kwargs[kw.arg] = ast.literal_eval(kw.value)

        # Map UI-TARS verbs to standard browser action names.
        if func_name == "finished":
            # BrowserCapability/DoneEvent reads ``extracted_content``,
            # not ``text`` — rename so the answer survives.
            return Action(
                name="done",
                params={
                    "success": True,
                    "extracted_content": kwargs.get("content", ""),
                },
            )

        if func_name == "click":
            x, y = _parse_point(kwargs.get("point"))
            if x is not None and y is not None:
                return Action(
                    name="click_coordinate",
                    params={"coordinate_x": x, "coordinate_y": y},
                )
            return None

        return Action(name=func_name, params=kwargs)

    except (SyntaxError, ValueError):
        return None


def _parse_point(point: Any) -> tuple[int | None, int | None]:
    """Extract (x, y) coordinates from a UI-TARS point string.

    Accepts formats like ``"(100, 200)"`` and ``"100,200"``.
    """
    if point is None:
        return (None, None)
    if isinstance(point, (tuple, list)) and len(point) == 2:
        try:
            return int(point[0]), int(point[1])
        except (TypeError, ValueError):
            return (None, None)
    if not isinstance(point, str):
        return (None, None)
    cleaned = point.strip().strip("()")
    parts = [p.strip() for p in cleaned.split(",")]
    if len(parts) != 2:
        return (None, None)
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return (None, None)
