"""UITarsAgent — vision-based agent with coordinate actions and AST parsing."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents.memory.base import BaseMemoryManager
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.react import BaseReactAgent
from spectragent.core.llm.base import BaseChatModel
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.action import Action, ActionDescriptor

# ── Prompt directory ─────────────────────────────────────────
_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / "ui_tars"


@dataclass
class UITarsAgent(BaseReactAgent):
    """UI-TARS style agent — vision-based with AST action parsing."""

    __registry_key__ = "ui_tars"

    # ── config (conscribe visible) ─────────────────────────────
    max_steps: Annotated[int, PydanticField(description="Maximum steps")] = 40
    prompt_template: Annotated[
        str, PydanticField(description="Prompt template: COMPUTER_USE, MOBILE_USE, or GROUNDING")
    ] = "COMPUTER_USE"
    language: Annotated[str, PydanticField(description="Thinking language")] = "English"

    # ── injected dependencies ──────────────────────────────────
    llm: BaseChatModel = field(default=None, repr=False)  # type: ignore[assignment]
    memory: BaseMemoryManager = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.memory is None:
            self.memory = SimpleMemory(max_messages=100)

    # ── observation types ──────────────────────────────────────

    @property
    def observation_types(self) -> set[str]:
        return {"screenshot", "url"}

    # ── BaseReactAgent abstract implementations ────────────────

    def build_system_prompt(self, task: str, action_space: list[ActionDescriptor]) -> str:
        variant = self.prompt_template.lower()  # "COMPUTER_USE" → "computer_use"
        prompt_file = _PROMPT_DIR / f"{variant}.md"
        self._prompt_path = str(prompt_file.relative_to(PROMPT_PATH))
        template = prompt_file.read_text()
        actions_text = "\n".join(
            f"- {a.name}: {a.description}" for a in action_space
        )
        return template.format(task=task, action_space=actions_text)

    def parse_llm_output(self, raw_output: str | BaseModel) -> list[Action]:
        text = raw_output if isinstance(raw_output, str) else str(raw_output)
        return _parse_ui_tars_response(text)

    def get_output_model(self) -> type[BaseModel] | None:
        # UI-TARS uses free-form text, not structured output
        return None

    def get_action_space(self) -> list[ActionDescriptor]:
        return [
            ActionDescriptor(
                name="click",
                description="Click at screen coordinates",
                params={"point": "str"},
                env_types=["browser", "desktop"],
            ),
            ActionDescriptor(
                name="type",
                description="Type text at coordinates",
                params={"content": "str", "point": "str"},
                env_types=["browser", "desktop"],
            ),
            ActionDescriptor(
                name="scroll",
                description="Scroll at coordinates",
                params={"point": "str", "direction": "str"},
                env_types=["browser", "desktop"],
            ),
            ActionDescriptor(
                name="hotkey",
                description="Press a keyboard shortcut",
                params={"key": "str"},
                env_types=["browser", "desktop"],
            ),
            ActionDescriptor(
                name="drag",
                description="Drag from one point to another",
                params={"start_point": "str", "end_point": "str"},
                env_types=["browser", "desktop"],
            ),
            ActionDescriptor(
                name="wait",
                description="Wait for the page to settle",
                params={},
                env_types=["browser", "desktop"],
            ),
            ActionDescriptor(
                name="finished",
                description="Signal task completion",
                params={"content": "str"},
                env_types=["browser", "desktop"],
            ),
        ]


# ── AST-based action parser ───────────────────────────────────


def _parse_ui_tars_response(text: str) -> list[Action]:
    """Parse UI-TARS text output into Action objects using AST parsing.

    Expected format:
        Thought: ...
        Action: click(point='(100, 200)')
    """
    # Extract the Action line(s)
    action_pattern = re.compile(r"Action:\s*(.+?)(?:\n|$)", re.DOTALL)
    matches = action_pattern.findall(text)

    if not matches:
        return [Action(name="wait", params={})]

    actions: list[Action] = []
    for action_str in matches:
        action_str = action_str.strip()
        # Split multiple actions on )\n\n pattern
        parts = re.split(r"\)\s*\n\s*\n", action_str)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # Ensure the part ends with )
            if not part.endswith(")"):
                part += ")"
            action = _parse_single_action(part)
            if action is not None:
                actions.append(action)

    return actions if actions else [Action(name="wait", params={})]


def _parse_single_action(action_str: str) -> Action | None:
    """Parse a single action call using Python AST."""
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

        # Map 'finished' to 'done' with is_done flag
        if func_name == "finished":
            return Action(
                name="done",
                params={"success": True, "text": kwargs.get("content", "")},
            )

        return Action(name=func_name, params=kwargs)

    except (SyntaxError, ValueError):
        return None
