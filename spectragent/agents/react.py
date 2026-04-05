"""BaseReactAgent — abstract ReAct (Reason + Act) loop for all agents."""

import asyncio
import logging
import time
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel

from spectragent.agents._registrar import AgentBase
from spectragent.agents.env_interfaces import Environment
from spectragent.agents.memory.base import BaseMemoryManager
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)


class BaseReactAgent(AgentBase):
    """Abstract ReAct agent with a standard observe → think → act loop.

    Subclasses **must** implement:
    - :meth:`build_system_prompt`
    - :meth:`parse_llm_output`
    - :meth:`get_output_model`
    - :meth:`get_action_space`

    Subclasses **may** override:
    - :meth:`build_messages`
    - :meth:`on_step_complete`
    """

    __abstract__ = True

    # ── injected dependencies (not conscribe config) ───────────
    llm: BaseChatModel
    memory: BaseMemoryManager

    # ── class-level defaults for subclasses ────────────────────
    _default_max_steps: ClassVar[int] = 100
    _default_max_actions_per_step: ClassVar[int] = 1
    _default_timeout_seconds: ClassVar[int] = 300

    # ── prompt snapshot (set during run()) ─────────────────────
    _prompt_snapshot: Any = None  # PromptSnapshot | None

    # ── abstract methods (subclass MUST implement) ─────────────

    @abstractmethod
    def build_system_prompt(self, task: str, action_space: list[ActionDescriptor]) -> str:
        """Build the system prompt for this agent given the task and action space."""
        ...

    @abstractmethod
    def parse_llm_output(self, raw_output: str | BaseModel) -> list[Action]:
        """Parse the LLM's response into one or more actions."""
        ...

    @abstractmethod
    def get_output_model(self) -> type[BaseModel] | None:
        """Return the Pydantic model for structured output, or None for plain text."""
        ...

    @abstractmethod
    def get_action_space(self) -> list[ActionDescriptor]:
        """Declare the actions this agent can emit."""
        ...

    # ── overridable hooks ──────────────────────────────────────

    def build_messages(self, observation: Any) -> list[BaseMessage]:
        """Build the message list for the LLM call.

        Default: system messages from memory + observation as a user message.
        """
        obs_text = self._format_observation(observation)
        self.memory.add_message(UserMessage(content=obs_text))
        return self.memory.get_context_messages()

    def on_step_complete(self, step_record: StepRecord) -> None:  # noqa: ARG002
        """Hook called after each step. Override for logging/metrics."""

    # ── public interface ───────────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def observation_types(self) -> set[str]:
        return {"dom", "screenshot", "url"}

    @property
    def system_prompt(self) -> Any:
        """System prompt snapshot from the last run."""
        return self._prompt_snapshot

    async def step(self, observation: Any) -> list[Action]:
        """Single observe → think → act cycle."""
        messages = self.build_messages(observation)
        output_model = self.get_output_model()

        completion: ChatInvokeCompletion[Any]
        if output_model is not None:
            completion = await self.llm.ainvoke(messages, output_format=output_model)
        else:
            completion = await self.llm.ainvoke(messages)

        raw_output = completion.completion
        actions = self.parse_llm_output(raw_output)

        # Enforce max_actions_per_step
        max_actions = getattr(self, "max_actions_per_step", self._default_max_actions_per_step)
        if len(actions) > max_actions:
            actions = actions[:max_actions]

        # Store assistant response in memory
        output_text = raw_output if isinstance(raw_output, str) else raw_output.model_dump_json()
        self.memory.add_message(AssistantMessage(content=output_text))

        return actions

    async def run(self, env: Environment, task: str, **kwargs: Any) -> AgentResult:
        """Drive the full ReAct loop: observe -> think -> act -> repeat.

        Optional ``session_id`` kwarg targets a specific browser tab.
        When omitted, the environment uses its default tab.
        """
        max_steps = getattr(self, "max_steps", self._default_max_steps)
        timeout = getattr(self, "timeout_seconds", self._default_timeout_seconds)
        task_id = kwargs.get("task_id", "unknown")
        session_id: str | None = kwargs.get("session_id")

        await self.reset()

        # Build system prompt and seed memory
        action_space = env.get_action_space()
        system_prompt = self.build_system_prompt(task, action_space)
        self.memory.add_message(SystemMessage(content=system_prompt))

        # Store prompt snapshot for introspection
        from spectragent.schemas.prompt import PromptRef, TextPrompt

        self._prompt_snapshot = TextPrompt(
            ref=PromptRef(path=getattr(self, "_prompt_path", "<runtime>"), content_hash=""),
            text=system_prompt,
        )

        start_time = time.monotonic()
        step_records: list[StepRecord] = []
        action_history: list[str] = []
        total_usage = AgentUsage()
        last_result: ActionResult | None = None
        error_msg: str | None = None

        try:
            for step_idx in range(max_steps):
                elapsed = time.monotonic() - start_time
                if elapsed > timeout:
                    logger.warning("Agent timed out after %.1fs", elapsed)
                    return self._build_result(
                        task_id=task_id,
                        task=task,
                        start_time=start_time,
                        step_records=step_records,
                        action_history=action_history,
                        usage=total_usage,
                        agent_done="timeout",
                        env_status="success",
                    )

                # Observe
                observation = await asyncio.wait_for(
                    env.observe(session_id), timeout=max(timeout - elapsed, 1)
                )

                # Think + Act
                actions = await self.step(observation)

                step_results: list[ActionResult] = []
                for action in actions:
                    result = await env.act(action, session_id)
                    step_results.append(result)
                    action_history.append(f"{action.name}({action.params})")
                    if result.is_done:
                        last_result = result
                        break

                record = StepRecord(
                    step=step_idx,
                    observation_summary=self._summarise_observation(observation),
                    llm_output="",
                    actions=actions,
                    action_results=step_results,
                    timestamp=time.time(),
                )
                step_records.append(record)
                self.on_step_complete(record)

                if last_result is not None and last_result.is_done:
                    return self._build_result(
                        task_id=task_id,
                        task=task,
                        start_time=start_time,
                        step_records=step_records,
                        action_history=action_history,
                        usage=total_usage,
                        agent_done="done",
                        env_status="success",
                        answer=last_result.extracted_content or "",
                        agent_success=last_result.success,
                    )

            # Max steps reached
            return self._build_result(
                task_id=task_id,
                task=task,
                start_time=start_time,
                step_records=step_records,
                action_history=action_history,
                usage=total_usage,
                agent_done="max_steps",
                env_status="success",
            )

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("Agent error: %s", error_msg)
            return self._build_result(
                task_id=task_id,
                task=task,
                start_time=start_time,
                step_records=step_records,
                action_history=action_history,
                usage=total_usage,
                agent_done="error",
                env_status="failed",
                error=error_msg,
            )

    async def reset(self) -> None:
        """Clear memory and internal state."""
        self.memory.clear()
        self._prompt_snapshot = None

    # ── private helpers ────────────────────────────────────────

    def _format_observation(self, observation: Any) -> str:
        """Convert an Observation into a text string for the LLM."""
        parts: list[str] = []
        if observation.url:
            parts.append(f"URL: {observation.url}")
        if observation.page_title:
            parts.append(f"Title: {observation.page_title}")
        if observation.dom is not None:
            dom_str = str(observation.dom)
            if len(dom_str) > 10000:
                dom_str = dom_str[:10000] + "\n... (truncated)"
            parts.append(f"DOM:\n{dom_str}")
        if observation.accessibility_tree is not None:
            parts.append(f"Accessibility Tree:\n{observation.accessibility_tree}")
        if observation.screenshot_description:
            parts.append(f"Screenshot Description: {observation.screenshot_description}")
        if observation.tabs:
            tab_info = ", ".join(f"[{t.tab_id}] {t.title} ({t.url})" for t in observation.tabs)
            parts.append(f"Tabs: {tab_info}")
        return "\n\n".join(parts) if parts else "(empty observation)"

    def _summarise_observation(self, observation: Any) -> str:
        """Short summary for step records."""
        parts: list[str] = []
        if observation.url:
            parts.append(observation.url)
        if observation.page_title:
            parts.append(observation.page_title)
        return " | ".join(parts) if parts else "(empty)"

    def _build_result(
        self,
        *,
        task_id: str,
        task: str,
        start_time: float,
        step_records: list[StepRecord],
        action_history: list[str],
        usage: AgentUsage,
        agent_done: str,
        env_status: str,
        answer: str = "",
        agent_success: bool | None = None,
        error: str | None = None,
    ) -> AgentResult:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus

        return AgentResult(
            task_id=task_id,
            task=task,
            timestamp=datetime.now(timezone.utc),
            env_status=EnvironmentStatus(env_status),
            agent_done=AgentDoneStatus(agent_done),
            agent_success=agent_success,
            answer=answer,
            error=error,
            model_id=getattr(self.llm, "model", ""),
            action_history=action_history,
            metrics=AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=len(step_records),
                usage=usage,
            ),
            system_prompt=self._prompt_snapshot,
        )
