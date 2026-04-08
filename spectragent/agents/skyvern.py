"""SkyvernAgent — element-tree based agent with action history window.

M14 refactor: Skyvern is now a passive ReactCompatible domain provider.
Jinja2 prompt templating, action_history_window, and TOTP/2FA support
remain as agent config; the runtime/loop drive execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

import jinja2
from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._output_models import SkyvernOutput
from spectragent.agents._registrar import AgentBase
from spectragent.agents.memory.base import BaseMemoryManager
from spectragent.agents.memory.token_budget import TokenBudgetMemory
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.prompts import PROMPT_PATH
from spectragent.schemas.action import Action, ActionDescriptor

# ── Prompt directory ─────────────────────────────────────────
_PROMPT_DIR = PROMPT_PATH / "browser" / "agents" / "skyvern"


@dataclass
class SkyvernAgent(AgentBase):
    """Skyvern-style agent using element tree and action history."""

    __registry_key__ = "skyvern"

    # ── Cross-registry wiring (M14 + M13-Rev) ────────────────────
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react"]),
        "capability": ("capability", ["browser"]),
        # DOM is required, screenshot is optional (Skyvern is mostly
        # element-tree based but can use vision when available).
        "observation": ("observation", ["dom"], ["screenshot", "browser_meta"]),
        # Skyvern's element-tree driven vocabulary maps to the canonical
        # BrowserCapability action names (``input_text`` is translated to
        # ``type`` inside ``parse_react_output``).
        "action": (
            "action",
            ["click", "type", "done"],
            ["scroll", "send_keys", "go_back", "go_forward",
             "select_dropdown", "extract_content"],
        ),
        "llm": "llm",
    }

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
    # [TODO M17] Memory becomes runtime-injected in M17. Until then it
    # lives as a per-agent field accessed only by ``prepare`` and
    # ``build_react_prompt``.
    llm: BaseChatModel = field(default=None, repr=False)  # type: ignore[assignment]
    memory: BaseMemoryManager = field(default=None, repr=False)  # type: ignore[assignment]

    # ── internal state ─────────────────────────────────────────
    _action_history: list[str] = field(default_factory=list, init=False, repr=False)
    _task: str = field(default="", init=False, repr=False)
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_path: str = field(default="", init=False, repr=False)
    _prompt_snapshot: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.memory is None:
            self.memory = TokenBudgetMemory(max_tokens=8000)

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def domain(self) -> str:
        return "web_navigation"

    @property
    def observation_types(self) -> set[str]:
        return {"dom", "browser_meta"}

    @property
    def system_prompt(self) -> Any:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(self._action_descriptors)

    # ── Lifecycle hook called by ReactLoop ─────────────────────

    def prepare(
        self, task: str, action_descriptors: list[ActionDescriptor]
    ) -> None:
        """One-shot setup. Builds the Jinja-rendered system prompt and
        seeds memory."""
        self._task = task
        self._action_descriptors = list(action_descriptors)
        self._action_history.clear()
        self._system_prompt_text = self._build_system_prompt(
            task, self._action_descriptors
        )
        from spectragent.schemas.prompt import PromptRef, TextPrompt

        self._prompt_snapshot = TextPrompt(
            ref=PromptRef(
                path=self._prompt_path or "<runtime>", content_hash=""
            ),
            text=self._system_prompt_text,
        )
        self.memory.clear()
        self.memory.add_message(SystemMessage(content=self._system_prompt_text))

    # ── ReactCompatible Protocol ───────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Format the observation, append to memory, return full message list.

        [TODO M17] Memory access lives here.
        """
        obs_text = self._format_observation(observation)
        self.memory.add_message(UserMessage(content=obs_text))
        return self.memory.get_context_messages()

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        if isinstance(llm_output, SkyvernOutput):
            # When ``is_done`` is set, Skyvern terminates the task with
            # just a done action — preceding actions (if any) are the
            # LLM's justification and need not execute. This makes the
            # output compatible with ``ReactLoop.max_actions_per_step=1``
            # (the default). ``extracted_content`` flows straight into
            # ``DoneAction.execute`` — no renaming hack needed.
            if llm_output.is_done:
                self._action_history.append("done")
                return [
                    Action(
                        name="done",
                        params={
                            "extracted_content": llm_output.extracted_content or "",
                            "success": True,
                        },
                    )
                ]

            actions: list[Action] = []
            for item in llm_output.actions:
                # Translate Skyvern's training vocabulary to the canonical
                # BrowserCapability action names. Only ``input_text`` is
                # known to differ today; other names pass through unchanged.
                canonical_name = (
                    "type" if item.action_type == "input_text" else item.action_type
                )
                params: dict[str, Any] = {"reasoning": item.reasoning}
                if item.element_id is not None:
                    params["element_id"] = item.element_id
                if item.text is not None:
                    params["text"] = item.text
                actions.append(Action(name=canonical_name, params=params))

            # Track actions in the history window for the next prepare()
            for a in actions:
                self._action_history.append(f"{a.name}({a.params})")

            return actions if actions else [Action(name="wait", params={})]

        # Fallback: try to parse as JSON
        try:
            text = llm_output if isinstance(llm_output, str) else str(llm_output)
            data = json.loads(text)
            parsed = SkyvernOutput.model_validate(data)
            return self.parse_react_output(parsed)
        except json.JSONDecodeError:
            # H1: do not silently turn parse failures into "done" —
            # that masquerades as success. Emit a wait so the loop
            # retries on the next step.
            return [Action(name="wait", params={})]

    def get_react_output_model(self) -> type[BaseModel] | None:
        return SkyvernOutput

    # ── private helpers ────────────────────────────────────────

    def _build_system_prompt(
        self, task: str, action_space: list[ActionDescriptor]
    ) -> str:
        actions_text = "\n".join(
            f"- {a.name}: {a.description} (params: {a.params})" for a in action_space
        )
        history_text = (
            "\n".join(self._action_history[-self.action_history_window :]) or "(none)"
        )

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

    @staticmethod
    def _format_observation(observation: Any) -> str:
        """Format NegotiatedObservation as a text user-message body."""
        parts: list[str] = []

        meta = getattr(observation, "browser_meta", None)
        if meta is not None:
            url = getattr(meta, "url", "") or ""
            title = getattr(meta, "page_title", "") or ""
            if url:
                parts.append(f"URL: {url}")
            if title:
                parts.append(f"Title: {title}")

        dom = getattr(observation, "dom", None)
        if dom is not None:
            element_tree = getattr(dom, "element_tree", None)
            if element_tree is not None:
                dom_str = str(element_tree)
                if len(dom_str) > 10000:
                    dom_str = dom_str[:10000] + "\n... (truncated)"
                parts.append(f"Element Tree:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty observation)"
