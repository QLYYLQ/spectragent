"""BrowserUseAgent — multi-action, flash-mode, vision-capable browser agent.

M14 refactor: BrowserUseAgent is now a passive domain-knowledge provider.
Execution is driven by ``ReactLoop`` (weaved via ``AgentRuntime``);
this class only knows how to (1) build prompts and (2) parse LLM output.

Memory access is intentionally localized to ``_format_observation`` and
``build_react_prompt`` so the M17 migration (memory injected by
AgentRuntime) is mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._output_models import BrowserUseOutput
from spectragent.agents._registrar import AgentBase
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.action import Action, ActionDescriptor

# ── Prompt directory ─────────────────────────────────────────
_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / "browser_use"

# Local vocabulary map: browser-use LLM prompts were originally written
# for the upstream browser-use library (``click_element`` / ``input_text``
# / ``go_to_url``) but the canonical BrowserCapability action names are
# ``browser.click`` / ``browser.type`` / ``browser.navigate``. This dict
# is an implementation detail of ``parse_react_output`` — agent wiring
# and runtime negotiation only ever see the canonical vocabulary.
_LLM_VOCABULARY_TO_CANONICAL: dict[str, str] = {
    "click_element": "browser.click",
    "input_text": "browser.type",
    "go_to_url": "browser.navigate",
}
# Short local names (stripped of the "browser." prefix) that also map
# directly. Keeps ``parse_react_output`` resilient when the LLM emits
# either ``"browser.click"`` or the legacy ``"click"``.
_SHORT_TO_CANONICAL: dict[str, str] = {
    "click": "browser.click",
    "type": "browser.type",
    "navigate": "browser.navigate",
    "scroll": "browser.scroll",
    "send_keys": "browser.send_keys",
    "go_back": "browser.go_back",
    "go_forward": "browser.go_forward",
    "switch_tab": "browser.switch_tab",
    "extract_content": "browser.extract_content",
    "done": "browser.done",
}


def _is_anthropic_4_5_model(model_name: str | None) -> bool:
    """Check if the model is Claude Opus 4.5 or Haiku 4.5."""
    if not model_name:
        return False
    model_lower = model_name.lower()
    is_opus_4_5 = "opus" in model_lower and ("4.5" in model_lower or "4-5" in model_lower)
    is_haiku_4_5 = "haiku" in model_lower and ("4.5" in model_lower or "4-5" in model_lower)
    return is_opus_4_5 or is_haiku_4_5


@dataclass
class BrowserUseAgent(AgentBase):
    """Browser-use style agent with multi-action and flash mode."""

    __registry_key__ = "browser_use"

    # ── Cross-registry wiring (M14 + M13-Rev) ────────────────────
    # Restricts compatible loops/capabilities/observations, and declares
    # the canonical action names the agent can issue. Required actions
    # are the ones every run needs (click/type/navigate/done); optional
    # actions are the enhancement set — if a capability's ablation YAML
    # drops them, the run still starts.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react"]),
        "capability": ("capability", ["browser"]),
        # DOM + screenshot are required, browser_meta is optional
        # (we use it for url/title context but can run without it).
        "observation": (
            "observation",
            ["browser.dom", "browser.screenshot"],
            ["browser.browser_meta"],
        ),
        "action": (
            "action",
            ["browser.click", "browser.type", "browser.navigate", "browser.done"],
            [
                "browser.scroll",
                "browser.send_keys",
                "browser.extract_content",
                "browser.go_back",
                "browser.go_forward",
                "browser.switch_tab",
            ],
        ),
        "llm": "llm",  # auto-discovery — any registered LLM
    }

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

    # ── internal state populated by prepare() ──────────────────
    _task: str = field(default="", init=False, repr=False)
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_path: str = field(default="", init=False, repr=False)
    _prompt_snapshot: Any = field(default=None, init=False, repr=False)

    # ``llm`` and ``memory`` are wired from ``__wiring__`` (inherited from
    # ``AgentBase``). conscribe's metaclass attaches ``WiredField``
    # descriptors at class creation; the Composer populates them via
    # setattr at wire time. Reading them before injection raises
    # ``AttributeError`` with a clear message.

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def domain(self) -> str:
        return "web_navigation"

    @property
    def observation_types(self) -> set[str]:
        types: set[str] = {"dom", "browser_meta"}
        if self.use_vision:
            types.add("screenshot")
        return types

    @property
    def system_prompt(self) -> Any:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        """Domain action descriptors. The Capability owns the actual
        executable action set; this method just exposes them for
        introspection."""
        return list(self._action_descriptors)

    # ── Lifecycle hook called by ReactLoop ─────────────────────

    def prepare(self, task: str, action_descriptors: list[ActionDescriptor]) -> None:
        """One-shot setup invoked by ReactLoop before the first step.

        Builds the system prompt and seeds memory with it.

        [TODO M17] When M17 ships, system-prompt seeding will move to
        AgentRuntime. This method should then become a pure builder
        returning the seeded message list, with no memory side effects.
        """
        self._task = task
        self._action_descriptors = list(action_descriptors)
        self._system_prompt_text = self._build_system_prompt(task, self._action_descriptors)
        # Snapshot for trace introspection.
        from spectragent.schemas.prompt import PromptRef, TextPrompt

        self._prompt_snapshot = TextPrompt(
            ref=PromptRef(path=self._prompt_path or "<runtime>", content_hash=""),
            text=self._system_prompt_text,
        )
        self.memory.clear()
        self.memory.add_message(SystemMessage(content=self._system_prompt_text))

    # ── ReactLoop slots ────────────────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Format the observation, append to memory, return full message list.

        [TODO M17] Memory access lives here. After M17, this method
        will accept a ``messages`` parameter built by AgentRuntime.
        """
        if self.use_vision and self._observation_has_screenshot(observation):
            self._add_vision_message(observation)
        else:
            obs_text = self._format_observation(observation)
            self.memory.add_message(UserMessage(content=obs_text))
        return self.memory.get_context_messages()

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Parse a structured ``BrowserUseOutput`` into Action objects.

        The LLM emits the browser-use training vocabulary
        (``click_element``, ``input_text``, ``go_to_url``); we translate
        to the canonical ``BrowserCapability`` vocabulary here so that
        agent wiring, runtime negotiation and ``guarded_act`` all speak
        the same canonical names.
        """
        if isinstance(llm_output, BrowserUseOutput):
            actions: list[Action] = []
            for item in llm_output.actions:
                canonical_name = _LLM_VOCABULARY_TO_CANONICAL.get(
                    item.action_name,
                    _SHORT_TO_CANONICAL.get(item.action_name, item.action_name),
                )
                params: dict[str, Any] = {}

                # The ``done`` action's answer text lives in the LLM's
                # ``text`` field; ``DoneAction.execute`` reads it from
                # ``extracted_content`` — rename at parse time and drop
                # the unrelated fields (index/url/direction don't belong
                # on done).
                if canonical_name == "browser.done":
                    if item.text is not None:
                        params["extracted_content"] = item.text
                    if item.success is not None:
                        params["success"] = item.success
                else:
                    if item.index is not None:
                        params["index"] = item.index
                    if item.text is not None:
                        params["text"] = item.text
                    if item.url is not None:
                        params["url"] = item.url
                    if item.direction is not None:
                        params["direction"] = item.direction

                actions.append(Action(name=canonical_name, params=params))
            if not actions:
                raise ParseError(
                    "LLM returned a valid BrowserUseOutput but with an empty actions list.",
                    raw_output=str(llm_output)[:500],
                )
            return actions

        # Fallback: try to parse plain text as JSON
        import json

        try:
            text = llm_output if isinstance(llm_output, str) else str(llm_output)
            data = json.loads(text)
            parsed = BrowserUseOutput.model_validate(data)
            return self.parse_react_output(parsed)
        except (json.JSONDecodeError, Exception) as exc:
            raise ParseError(
                f"Failed to parse LLM output: {type(exc).__name__}: {exc}",
                raw_output=text[:500] if isinstance(text, str) else None,
            ) from exc

    def get_react_output_model(self) -> type[BaseModel] | None:
        return BrowserUseOutput

    # ── private helpers ────────────────────────────────────────

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

    def _build_system_prompt(
        self,
        task: str,
        action_space: list[ActionDescriptor],  # noqa: ARG002
    ) -> str:
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

    @staticmethod
    def _observation_has_screenshot(observation: Any) -> bool:
        """Check whether a NegotiatedObservation has screenshot data."""
        screenshot = getattr(observation, "screenshot", None)
        if screenshot is None:
            return False
        return getattr(screenshot, "image_bytes", None) is not None or (
            getattr(screenshot, "image_path", None) is not None
        )

    def _add_vision_message(self, observation: Any) -> None:
        """Append a multi-modal user message with screenshot + DOM text."""
        import base64

        from spectragent.core.llm.messages import (
            ContentPartImageParam,
            ContentPartTextParam,
            ImageURL,
        )

        screenshot = observation.screenshot
        image_bytes = getattr(screenshot, "image_bytes", None)
        if image_bytes is None and getattr(screenshot, "image_path", None):
            with open(screenshot.image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is None:
            # Defensive — shouldn't happen because we checked above.
            self.memory.add_message(UserMessage(content=self._format_observation(observation)))
            return

        b64 = base64.b64encode(image_bytes).decode()
        obs_text = self._format_observation(observation)
        content: list[Any] = [
            ContentPartTextParam(text=obs_text),
            ContentPartImageParam(
                image_url=ImageURL(
                    url=f"data:image/png;base64,{b64}",
                    detail=self.vision_detail,
                ),
            ),
        ]
        self.memory.add_message(UserMessage(content=content))

    @staticmethod
    def _format_observation(observation: Any) -> str:
        """Format a NegotiatedObservation as a text user-message body.

        Reads from fragment sub-models: ``observation.browser_meta`` for
        url/title, ``observation.dom`` for the element tree.
        """
        parts: list[str] = []

        meta = getattr(observation, "browser_meta", None)
        if meta is not None:
            url = getattr(meta, "url", "") or ""
            title = getattr(meta, "page_title", "") or ""
            if url:
                parts.append(f"URL: {url}")
            if title:
                parts.append(f"Title: {title}")
            tabs = getattr(meta, "tabs", None) or []
            if tabs:
                tab_info = ", ".join(
                    f"[{t.get('tab_id', '?')}] {t.get('title', '')} ({t.get('url', '')})"
                    for t in tabs
                )
                parts.append(f"Tabs: {tab_info}")

        dom = getattr(observation, "dom", None)
        if dom is not None:
            element_tree = getattr(dom, "element_tree", None)
            if element_tree is not None:
                dom_str = str(element_tree)
                if len(dom_str) > 10000:
                    dom_str = dom_str[:10000] + "\n... (truncated)"
                parts.append(f"DOM:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty observation)"
