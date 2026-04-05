"""SkyvernAgent — element-tree based agent with action history window."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any

import jinja2
from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._output_models import SkyvernOutput
from spectragent.agents.memory.base import BaseMemoryManager
from spectragent.agents.memory.token_budget import TokenBudgetMemory
from spectragent.agents.react import BaseReactAgent
from spectragent.core.llm.base import BaseChatModel
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.step_record import StepRecord

# ── Prompt directory ─────────────────────────────────────────
_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / "skyvern"


@dataclass
class SkyvernAgent(BaseReactAgent):
    """Skyvern-style agent using element tree and action history."""

    __registry_key__ = "skyvern"

    # ── config (conscribe visible) ─────────────────────────────
    max_steps: Annotated[int, PydanticField(description="Maximum steps")] = 50
    action_history_window: Annotated[
        int, PydanticField(description="Number of recent actions to include in prompt")
    ] = 5
    enable_totp: Annotated[bool, PydanticField(description="Enable TOTP 2FA support")] = False
    prompt_template: Annotated[
        str, PydanticField(description="Skyvern template name (without extension)")
    ] = "extract-action"
    navigation_payload: Annotated[
        str | None, PydanticField(description="Navigation payload for task")
    ] = None
    complete_criterion: Annotated[
        str | None, PydanticField(description="Criterion for task completion")
    ] = None
    terminate_criterion: Annotated[
        str | None, PydanticField(description="Criterion for task termination")
    ] = None
    verification_code_check: Annotated[
        bool, PydanticField(description="Enable verification code checking")
    ] = False

    # ── injected dependencies ──────────────────────────────────
    llm: BaseChatModel = field(default=None, repr=False)  # type: ignore[assignment]
    memory: BaseMemoryManager = field(default=None, repr=False)  # type: ignore[assignment]

    # ── internal state ─────────────────────────────────────────
    _action_history: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.memory is None:
            self.memory = TokenBudgetMemory(max_tokens=8000)

    # ── observation types ──────────────────────────────────────

    @property
    def observation_types(self) -> set[str]:
        return {"dom", "accessibility_tree", "url"}

    # ── BaseReactAgent abstract implementations ────────────────

    def build_system_prompt(self, task: str, action_space: list[ActionDescriptor]) -> str:
        actions_text = "\n".join(
            f"- {a.name}: {a.description} (params: {a.params})" for a in action_space
        )
        history_text = "\n".join(self._action_history[-self.action_history_window :]) or "(none)"

        prompt_file = _PROMPT_DIR / f"{self.prompt_template}.md"
        self._prompt_path = str(prompt_file.relative_to(PROMPT_PATH))
        raw_template = prompt_file.read_text()
        template = jinja2.Template(raw_template)
        return template.render(
            navigation_goal=task,
            action_space=actions_text,
            action_history=history_text,
            history_window=self.action_history_window,
            navigation_payload=self.navigation_payload or "",
            complete_criterion=self.complete_criterion or "",
            terminate_criterion=self.terminate_criterion or "",
            verification_code_check=self.verification_code_check,
        )

    def parse_llm_output(self, raw_output: str | BaseModel) -> list[Action]:
        if isinstance(raw_output, SkyvernOutput):
            actions: list[Action] = []
            for item in raw_output.actions:
                params: dict[str, Any] = {"reasoning": item.reasoning}
                if item.element_id is not None:
                    params["element_id"] = item.element_id
                if item.text is not None:
                    params["text"] = item.text
                actions.append(Action(name=item.action_type, params=params))

            # Handle done signal
            if raw_output.is_done:
                actions.append(
                    Action(
                        name="done",
                        params={
                            "success": True,
                            "text": raw_output.extracted_content or "",
                        },
                    )
                )
            return actions if actions else [Action(name="wait", params={})]

        # Fallback: try to parse as JSON
        try:
            text = raw_output if isinstance(raw_output, str) else str(raw_output)
            data = json.loads(text)
            parsed = SkyvernOutput.model_validate(data)
            return self.parse_llm_output(parsed)
        except (json.JSONDecodeError, Exception):
            return [Action(name="done", params={"error": "Failed to parse LLM output"})]

    def get_output_model(self) -> type[BaseModel] | None:
        return SkyvernOutput

    def get_action_space(self) -> list[ActionDescriptor]:
        actions = [
            ActionDescriptor(
                name="click",
                description="Click an element by ID",
                params={"element_id": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="input_text",
                description="Type text into an element",
                params={"element_id": "str", "text": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="select_option",
                description="Select a dropdown option",
                params={"element_id": "str", "value": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="scroll",
                description="Scroll the page or element",
                params={"direction": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="wait",
                description="Wait for page to load",
                params={},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="go_back",
                description="Navigate back",
                params={},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="done",
                description="Signal task completion",
                params={"success": "bool", "text": "str"},
                env_types=["browser"],
            ),
        ]
        if self.enable_totp:
            actions.append(
                ActionDescriptor(
                    name="totp",
                    description="Generate and input a TOTP code",
                    params={"element_id": "str", "secret": "str"},
                    env_types=["browser"],
                )
            )
        return actions

    # ── hooks ──────────────────────────────────────────────────

    def on_step_complete(self, step_record: StepRecord) -> None:
        """Track action history for prompt injection."""
        for action in step_record.actions:
            self._action_history.append(f"{action.name}({action.params})")

    async def reset(self) -> None:
        await super().reset()
        self._action_history.clear()
