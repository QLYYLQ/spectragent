"""BrowserUseAgent — multi-action, flash-mode, vision-capable browser agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._output_models import BrowserUseOutput
from spectragent.agents.memory.base import BaseMemoryManager
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.react import BaseReactAgent
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.messages import BaseMessage, UserMessage
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.action import Action, ActionDescriptor

# ── Prompt directory ─────────────────────────────────────────
_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / "browser_use"


def _is_anthropic_4_5_model(model_name: str | None) -> bool:
    """Check if the model is Claude Opus 4.5 or Haiku 4.5."""
    if not model_name:
        return False
    model_lower = model_name.lower()
    is_opus_4_5 = "opus" in model_lower and ("4.5" in model_lower or "4-5" in model_lower)
    is_haiku_4_5 = "haiku" in model_lower and ("4.5" in model_lower or "4-5" in model_lower)
    return is_opus_4_5 or is_haiku_4_5


@dataclass
class BrowserUseAgent(BaseReactAgent):
    """Browser-use style agent with multi-action and flash mode."""

    __registry_key__ = "browser_use"

    # ── config (conscribe visible) ─────────────────────────────
    max_steps: Annotated[int, PydanticField(description="Maximum steps")] = 100
    max_actions_per_step: Annotated[int, PydanticField(description="Max actions per step")] = 3
    timeout_seconds: Annotated[int, PydanticField(description="Timeout in seconds")] = 300
    flash_mode: Annotated[bool, PydanticField(description="Skip thinking for speed")] = True
    use_vision: Annotated[bool, PydanticField(description="Include screenshots")] = False
    vision_detail: Annotated[
        Literal["auto", "low", "high"], PydanticField(description="Screenshot detail level")
    ] = "low"
    use_thinking: Annotated[bool, PydanticField(description="Enable extended thinking")] = True
    is_browser_use_model: Annotated[
        bool, PydanticField(description="Using fine-tuned browser-use model")
    ] = False
    is_anthropic: Annotated[bool, PydanticField(description="Using Anthropic model")] = False
    model_name: Annotated[
        str | None, PydanticField(description="Model name for prompt selection")
    ] = None
    override_system_message: Annotated[
        str | None, PydanticField(description="Override the entire system prompt")
    ] = None
    extend_system_message: Annotated[
        str | None, PydanticField(description="Append text to system prompt")
    ] = None

    # ── injected dependencies ──────────────────────────────────
    llm: BaseChatModel = field(default=None, repr=False)  # type: ignore[assignment]
    memory: BaseMemoryManager = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.memory is None:
            self.memory = SlidingWindowMemory(window_size=20)

    # ── observation types ──────────────────────────────────────

    @property
    def observation_types(self) -> set[str]:
        types = {"dom", "url"}
        if self.use_vision:
            types.add("screenshot")
        return types

    # ── prompt selection ───────────────────────────────────────

    def _select_prompt_file(self) -> str:
        """Select the appropriate prompt template file based on config."""
        is_anthropic_4_5 = _is_anthropic_4_5_model(self.model_name)

        if self.is_browser_use_model:
            if self.flash_mode:
                return "system_prompt_browser_use_flash.md"
            elif self.use_thinking:
                return "system_prompt_browser_use.md"
            else:
                return "system_prompt_browser_use_no_thinking.md"
        elif is_anthropic_4_5 and self.flash_mode:
            return "system_prompt_anthropic_flash.md"
        elif self.flash_mode and self.is_anthropic:
            return "system_prompt_flash_anthropic.md"
        elif self.flash_mode:
            return "system_prompt_flash.md"
        elif self.use_thinking:
            return "system_prompt.md"
        else:
            return "system_prompt_no_thinking.md"

    # ── BaseReactAgent abstract implementations ────────────────

    def build_system_prompt(self, task: str, action_space: list[ActionDescriptor]) -> str:
        if self.override_system_message:
            text = self.override_system_message
        else:
            prompt_file = _PROMPT_DIR / self._select_prompt_file()
            self._prompt_path = str(prompt_file.relative_to(PROMPT_PATH))
            template = prompt_file.read_text()
            text = template.format(max_actions=self.max_actions_per_step)

        if self.extend_system_message:
            text += f"\n{self.extend_system_message}"
        return text

    def parse_llm_output(self, raw_output: str | BaseModel) -> list[Action]:
        if isinstance(raw_output, BrowserUseOutput):
            actions: list[Action] = []
            for item in raw_output.actions:
                params: dict[str, Any] = {}
                if item.index is not None:
                    params["index"] = item.index
                if item.text is not None:
                    params["text"] = item.text
                if item.url is not None:
                    params["url"] = item.url
                if item.direction is not None:
                    params["direction"] = item.direction
                if item.success is not None:
                    params["success"] = item.success
                actions.append(Action(name=item.action_name, params=params))
            return actions if actions else [Action(name="wait", params={})]
        # Fallback: try to parse as JSON text
        import json

        try:
            data = json.loads(raw_output if isinstance(raw_output, str) else str(raw_output))
            parsed = BrowserUseOutput.model_validate(data)
            return self.parse_llm_output(parsed)
        except (json.JSONDecodeError, Exception):
            return [Action(name="done", params={"error": "Failed to parse LLM output"})]

    def get_output_model(self) -> type[BaseModel] | None:
        return BrowserUseOutput

    def get_action_space(self) -> list[ActionDescriptor]:
        return [
            ActionDescriptor(
                name="click_element",
                description="Click on an element by index",
                params={"index": "int"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="input_text",
                description="Type text into an element",
                params={"index": "int", "text": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="scroll",
                description="Scroll the page",
                params={"direction": "str", "amount": "int"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="go_to_url",
                description="Navigate to a URL",
                params={"url": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="go_back",
                description="Navigate back",
                params={},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="extract_content",
                description="Extract page content",
                params={"selector": "str"},
                env_types=["browser"],
            ),
            ActionDescriptor(
                name="done",
                description="Signal task completion",
                params={"success": "bool", "text": "str"},
                env_types=["browser"],
            ),
        ]

    # ── message building ───────────────────────────────────────

    def build_messages(self, observation: Any) -> list[BaseMessage]:
        """Build messages, optionally including screenshots."""
        if self.use_vision and observation.screenshot is not None:
            import base64

            from spectragent.core.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL

            b64 = base64.b64encode(observation.screenshot).decode()
            obs_text = self._format_observation(observation)
            content: list[Any] = [
                ContentPartTextParam(text=obs_text),
                ContentPartImageParam(
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail=self.vision_detail),
                ),
            ]
            self.memory.add_message(UserMessage(content=content))
            return self.memory.get_context_messages()

        return super().build_messages(observation)
