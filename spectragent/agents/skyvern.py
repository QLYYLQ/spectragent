"""SkyvernAgent — element-tree based agent with action history window.

Skyvern is a passive domain-knowledge provider. Jinja2 prompt templating and
the size of the action-history window are agent config; the runtime/loop
drive execution. The window itself is rendered from the Loop's ``history``
fragment — the actions that actually ran, with their results and measured
effects — so a live step, a branch's rebuilt history and a restored prefix
all show it from the same records.

The agent owns exactly one template,
``prompts/agents/skyvern/system_prompt.md``. It is not upstream
Skyvern's: it teaches the JSON shape :class:`SkyvernOutput` declares and
the action names this framework's ``guarded_act`` will accept, and it
renders the *negotiated* action set rather than a hard-coded verb list.
``tests/test_agents/test_skyvern_prompt_action_vocabulary.py`` pins the
two together.

It is also the only agent whose template is Jinja2 rather than
``str.format`` — the template branches on optional criteria, which
``str.format`` cannot express. That is *declared*
(``__prompt_engine__``), not inferred from the file's contents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._action_effects import effect_line
from spectragent.agents._browser_platform import render_browser_platform
from spectragent.agents._browser_state import render_browser_state
from spectragent.agents._capture_state import CAPTURE_STATE_KEY, capture_state_demand
from spectragent.agents._output_models import SkyvernOutput
from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompt_context import (
    place_prompt_contributions,
    restored_history_message,
)
from spectragent.agents.prompts import PromptEngine
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.prompt import PromptSnapshot, ReactPromptContext, RestoredHistory
from spectragent.schemas.step_record import StepRecord


@dataclass
class SkyvernAgent(AgentBase):
    """Skyvern-style agent using element tree and action history."""

    __registry_key__ = "skyvern"
    __prompt_engine__: ClassVar[PromptEngine] = "jinja2"

    # ── Cross-registry wiring ────────────────────────────────────
    # Capability-relative keys: the agent declares domain verbs, and
    # which capability's ``click`` they mean is decided at negotiation.
    # No ``capability`` entry — it was read by nothing at runtime and
    # only pinned the generated config to one provider.
    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset({"max_actions_per_step"})

    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        # DOM is required, screenshot is optional (Skyvern is mostly
        # element-tree based but can use vision when available). Skyvern is
        # a browser agent, so the page state (``browser_meta``: URL, title,
        # tabs) is required; the platform-neutral ``capture_state`` is
        # optional, as for every agent (``docs/architecture_design.md``
        # §4.4, "观测增强按消费方分层"). ``history`` is the Loop's record of
        # what ran — the action-history window is rendered from it.
        "observation": {
            "registry": "observation",
            "keys": ["dom", "platform", "task", "browser_meta", "history"],
            "optional_keys": [
                "screenshot",
                "step_info",
                CAPTURE_STATE_KEY,
            ],
            "declare_only": True,
        },
        # The *negotiated* action names are Skyvern's vocabulary:
        # ``prepare()`` renders those descriptors into the system prompt
        # verbatim, so there is no second verb list to keep in sync and
        # no translation table to drift.
        "action": {
            "registry": "action",
            "keys": ["click", "type", "done"],
            "optional_keys": [
                "scroll",
                "send_keys",
                "go_back",
                "go_forward",
                "select_dropdown",
                "extract_content",
            ],
            "declare_only": True,
        },
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``action_history_window`` counts *executed actions*, not steps, and
    # it is spent in the per-step user message — not the system prompt.
    # The system prompt is built once, inside ``prepare()``; rendering a
    # window that grows each step into it would mean re-rendering it
    # every step, which the pure-builder ``prepare()`` contract in
    # ``core/runtime.py`` does not allow.
    action_history_window: Annotated[
        int,
        PydanticField(
            description=(
                "Number of most recent executed actions, with their results, "
                "replayed in each step's user message"
            )
        ),
    ] = 5
    # Hand-written rather than computed from the directory listing: a
    # ``Literal[tuple(...)]`` renders correctly through conscribe but
    # costs a pyright error per field ("Variable not allowed in type
    # expression"). ``tests/test_agents/test_prompt_tree.py`` asserts it
    # equals what is on disk, so a template added without being named
    # here fails the suite instead of sitting unreachable.
    prompt_template: Annotated[
        Literal["system_prompt"],
        PydanticField(description="Skyvern template name (without extension)"),
    ] = "system_prompt"
    navigation_payload: Annotated[
        str | None,
        PydanticField(
            description=(
                "User details the task needs (form values, credentials, "
                "preferences); rendered as <user_details> in the system prompt. "
                "None (the default): an empty section."
            )
        ),
    ] = None
    complete_criterion: Annotated[
        str | None,
        PydanticField(
            description=(
                "Condition that must hold before the agent may finish with success=true. "
                "None (the default): no condition."
            )
        ),
    ] = None
    terminate_criterion: Annotated[
        str | None,
        PydanticField(
            description=(
                "Condition on which the agent must give up (success=false). None (the "
                "default): no condition."
            )
        ),
    ] = None
    verification_code_check: Annotated[
        bool,
        PydanticField(
            description=(
                "Tell the agent to give up when a page demands a verification "
                "code it cannot obtain, instead of stalling"
            )
        ),
    ] = False
    show_action_effects: Annotated[
        bool,
        PydanticField(
            description=(
                "Show each executed action's measured effect (the one line the "
                "effect describes itself with, e.g. where a click landed or how "
                "far a scroll moved) in the action history, restored history "
                "included. Off: no effect reaches the model; the trace still "
                "records every effect, so both arms of an ablation measure the same."
            )
        ),
    ] = True
    show_capture_state: Annotated[
        bool,
        PydanticField(
            description=(
                "Ask for the optional capture_state fragment (how the environment "
                "stood when captured: settle, foreground change, pointer, failed "
                "visible resources) and show it with the page state every step. "
                "Off: it is excluded from this agent's observation demand, so it "
                "is neither captured nor shown."
            )
        ),
    ] = True
    show_media_state: Annotated[
        bool,
        PydanticField(
            description=(
                "Show the media playback state measured with the screenshot (each "
                "audio and video element's frame, order, position, length and whether "
                "it plays) after the page state every step. Off (the default): it is "
                "still recorded with every screenshot and used to check restoration, "
                "but never reaches the model. An explicit experiment variable: it "
                "changes the prompt. Needs the screenshot fragment; without one the "
                "block says the state was not measured."
            )
        ),
    ] = False

    # ── internal state ─────────────────────────────────────────
    # ``task`` reaches every step via ``obs.task.user_request`` (loop
    # fragment); the agent does not hold its own copy. Neither does it hold
    # an action history: the window is the tail of the Loop's ``history``
    # fragment, the last N *executed actions* rather than the loop's full
    # per-step transcript.
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)

    # ``llm`` and ``memory`` are wired from ``__wiring__`` (inherited from
    # ``AgentBase``) via conscribe's ``WiredField`` descriptors.

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def domain(self) -> str:
        return "web_navigation"

    @property
    def system_prompt(self) -> PromptSnapshot | None:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(self._action_descriptors)

    # ── Negotiation ────────────────────────────────────────────

    def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
        """The class demand, without ``capture_state`` when ``show_capture_state`` is off."""
        required, optional = super().negotiation_demand(registry)
        return capture_state_demand(
            self, registry, required, optional, shown=self.show_capture_state
        )

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_descriptors: list[ActionDescriptor],
        execution_limits: ExecutionLimits,
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        The per-step user message gets task, step_info and the action
        history from observation fragments, not ``self``.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates. ``SkyvernOutput.actions`` is a list, so
        without it the prompt would invite a plan the loop silently cuts
        short.
        """
        max_actions_per_step = execution_limits.max_actions_per_step
        if max_actions_per_step is None:
            raise ValueError("prepare requires the Loop's max_actions_per_step execution limit")
        self._action_descriptors = list(action_descriptors)
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task, self._action_descriptors, max_actions_per_step
        )
        # cache=True: static-prefix breakpoint — see BrowserUseAgent for why it
        # is kept alongside the per-step one.
        return [SystemMessage(content=self._system_prompt_text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    async def build_react_prompt(
        self, observation: Any, *, context: ReactPromptContext
    ) -> list[BaseMessage]:
        """Pure builder: return the observation user-message for this step.

        Prefixes the page state with ``<user_request>`` (and ``<step_info>``
        when the loop provides it) so the LLM sees the task on every
        turn — the system prompt's task injection happens once at
        ``prepare()`` time and would otherwise drift out of context.

        ``<action_history>`` lives here rather than in the system prompt
        because it changes every step: the system prompt is built once,
        by ``prepare()``, and re-rendering it per step would break that
        contract. The template's "if the last step's actions did not move
        the page, try something else" rule reads this block. It is built
        from the ``history`` fragment, so it shows what actually ran — with
        each action's result and, when shown, its measured effect.
        """
        task = observation.task.user_request
        prefix_parts = [
            f"<user_request>\n{task}\n</user_request>",
            render_browser_platform(observation),
        ]
        step_info = getattr(observation, "step_info", None)
        if step_info is not None:
            prefix_parts.append(
                f"<step_info>step {step_info.step}/{step_info.max_steps} "
                f"(today={step_info.today})</step_info>"
            )
        history = getattr(observation, "history", None)
        prefix_parts.append(
            f"<action_history>\n{self._render_action_history(history)}\n</action_history>"
        )
        body = "\n".join(prefix_parts) + "\n\n" + self._render_browser_state(observation)
        return place_prompt_contributions([UserMessage(content=body, cache=True)], context=context)

    async def build_restored_history(
        self,
        observation: Any,
        *,
        history: RestoredHistory,
        context: ReactPromptContext,
    ) -> list[BaseMessage]:
        """Render actual restored facts after this step's live observation.

        The window shows the steps before this one, just as a policy prompt
        precedes its decision; this step's restored record joins the Loop's
        history afterwards, with the actions and effects actually executed.
        The source reply is never parsed and cannot populate the history
        with obsolete target identifiers.
        """
        history.validate_execution()
        messages = await self.build_react_prompt(observation, context=context)
        restored = restored_history_message(history, show_action_effects=self.show_action_effects)
        return [*messages, restored]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        if isinstance(llm_output, SkyvernOutput):
            # When ``is_done`` is set, Skyvern terminates the task with
            # just a done action — preceding actions (if any) are the
            # LLM's justification and need not execute. This makes the
            # output compatible with ``ReactLoop.max_actions_per_step=1``
            # (the default). ``extracted_content`` flows straight into
            # ``DoneAction.execute`` — no renaming hack needed.
            if llm_output.is_done:
                return [self._done_action(llm_output.extracted_content, llm_output.success)]

            actions: list[Action] = []
            for item in llm_output.actions:
                # The template teaches the negotiated action names
                # verbatim, so this is normally already a fully-qualified
                # key. A model that answers with the bare verb is left
                # bare: ``build_guarded_act`` holds this run's short →
                # fully-qualified map and is the one component allowed to
                # apply it. Completing it to ``browser.<verb>`` here
                # would name a provider the agent cannot see.
                name = item.action_type

                # ``done`` is advertised like every other negotiated
                # action but is invoked through the top-level fields. A
                # model that writes it into ``actions`` anyway must not
                # silently lose the answer.
                if name.rsplit(".", 1)[-1] == "done":
                    actions.append(
                        self._done_action(
                            item.text or llm_output.extracted_content,
                            llm_output.success,
                        )
                    )
                    continue

                params: dict[str, Any] = {"reasoning": item.reasoning}
                # ``index``, not ``element_id``: BrowserCapability's
                # selector_map is index-keyed and every element-targeting
                # action reads ``params["index"]``
                # (``actions/_common.resolve_index``).
                if item.index is not None:
                    params["index"] = item.index
                if item.text is not None:
                    params["text"] = item.text
                if item.direction is not None:
                    params["direction"] = item.direction
                if item.keys is not None:
                    params["keys"] = item.keys
                actions.append(Action(name=name, params=params))

            if not actions:
                raise ParseError(
                    "LLM returned a valid SkyvernOutput but with an empty actions list.",
                    raw_output=str(llm_output)[:500],
                )
            return actions

        # Fallback: try to parse as JSON
        try:
            text = llm_output if isinstance(llm_output, str) else str(llm_output)
            data = json.loads(text)
            parsed = SkyvernOutput.model_validate(data)
            return self.parse_react_output(parsed)
        except (json.JSONDecodeError, Exception) as exc:
            raise ParseError(
                f"Failed to parse LLM output: {type(exc).__name__}: {exc}",
                raw_output=text[:500] if isinstance(text, str) else None,
            ) from exc

    def get_react_output_model(self) -> type[BaseModel] | None:
        return SkyvernOutput

    # ── Training-export slot (optional ReAct slot, arch §4.1) ──

    @staticmethod
    def parse_react_thought(llm_output: Any) -> tuple[str, str] | None:
        """The thought of a recorded reply and the field it came from, or ``None``.

        Skyvern reasons per action: every non-empty ``reasoning`` of the
        reply's ``actions``, stripped, in order, joined by newlines. Pure:
        reads the reply only (the JSON text a run records, or the parsed
        ``SkyvernOutput``), so the training export calls it on the class.
        """
        if isinstance(llm_output, BaseModel):
            data: Any = llm_output.model_dump()
        elif isinstance(llm_output, dict):
            data = llm_output
        else:
            try:
                data = json.loads(llm_output if isinstance(llm_output, str) else str(llm_output))
            except json.JSONDecodeError:
                return None
        items = data.get("actions") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None
        reasons = [
            item["reasoning"].strip()
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("reasoning"), str)
            and item["reasoning"].strip()
        ]
        return ("\n".join(reasons), "reasoning") if reasons else None

    # ── private helpers ────────────────────────────────────────

    @staticmethod
    def _done_action(extracted_content: str | None, success: bool) -> Action:
        """The one place a terminal ``done`` Action is built.

        Capability-relative, like everything else this agent emits: the
        guard binds it to whichever capability provides ``done``.
        """
        return Action(
            name="done",
            params={
                "extracted_content": extracted_content or "",
                "success": success,
            },
        )

    def _build_system_prompt(
        self,
        task: str,
        action_space: list[ActionDescriptor],
        max_actions_per_step: int,
    ) -> tuple[str, PromptSnapshot]:
        return self.render(
            self.prompt_template,
            {
                "navigation_goal": task,
                "action_space": self._render_action_list(action_space),
                "done_action": self._done_action_name(action_space),
                "max_actions_per_step": max_actions_per_step,
                "navigation_payload": self.navigation_payload or "",
                "complete_criterion": self.complete_criterion or "",
                "terminate_criterion": self.terminate_criterion or "",
                "verification_code_check": self.verification_code_check,
            },
        )

    @staticmethod
    def _done_action_name(descriptors: list[ActionDescriptor]) -> str:
        """The negotiated key of the terminal action, for the prompt's prose.

        The template has to say which advertised action it is telling the
        model *not* to write into ``actions``, and naming it must not mean
        writing a provider into the template: the run's ``done`` is
        whichever capability provided it. Falls back to the relative name
        — the same one ``_done_action`` emits — when nothing named ``done``
        is in the negotiated set, which only a degenerate fixture reaches.
        """
        for d in descriptors:
            if d.name.rsplit(".", 1)[-1] == "done":
                return d.name
        return "done"

    @staticmethod
    def _render_action_list(descriptors: list[ActionDescriptor]) -> str:
        """Render the *negotiated* action set as the prompt's vocabulary.

        One line per descriptor::

            - `browser.click(index: int -- element index)` — Click a DOM element.

        The names are the canonical registry keys, which is exactly what
        ``parse_react_output`` emits and ``guarded_act`` accepts, so the
        prompt cannot advertise a verb the run would reject. Empty list
        yields an empty string — only reachable before ``prepare()`` runs
        or in a degenerate fixture.
        """
        lines: list[str] = []
        for d in descriptors:
            params_part = (
                ", ".join(f"{k}: {v}" for k, v in d.params.items()) if d.params else "no params"
            )
            lines.append(f"- `{d.name}({params_part})` — {d.description}")
        return "\n".join(lines)

    def _render_action_history(self, history: Any) -> str:
        """The last ``action_history_window`` executed actions, most recent last.

        Read from the Loop's ``history`` records, across steps, one line per
        action that ran: ``name(params) -> ok`` or ``-> error: ...``, then
        ``| effect: <line>`` when effects are shown and the action measured
        one. An action that raised ends its step's batch (the Loop runs no
        further action), so its line carries the step's error; actions the
        Loop never ran — cut by ``max_actions_per_step``, or after a ``done``
        or a raise — are not history.

        The ``<= 0`` branch is not defensive padding: ``lst[-0:]`` is
        ``lst[0:]``, so a window of 0 — the obvious way to say "no
        history" in YAML — would otherwise render the *entire* run.
        """
        if self.action_history_window <= 0:
            return "(disabled)"
        records: list[StepRecord] = list(getattr(history, "items", None) or [])
        lines = [line for record in records for line in self._executed_lines(record)]
        window = lines[-self.action_history_window :]
        return "\n".join(window) if window else "(no actions yet)"

    def _executed_lines(self, record: StepRecord) -> list[str]:
        """One window line per action *record* actually ran, in order."""
        lines = [
            self._action_line(action, result)
            for action, result in zip(record.actions, record.action_results, strict=False)
        ]
        raised = len(record.action_results)
        if record.error_phase == "act" and raised < len(record.actions):
            action = record.actions[raised]
            lines.append(f"{action.name}({action.params}) -> error: {record.error}")
        return lines

    def _action_line(self, action: Action, result: ActionResult) -> str:
        outcome = "ok" if result.success else _error_outcome(result.error)
        line = f"{action.name}({action.params}) -> {outcome}"
        effect = effect_line(result, shown=self.show_action_effects)
        return f"{line} | effect: {effect}" if effect is not None else line

    def _render_browser_state(self, observation: Any) -> str:
        """Format the browser-state portion of the user message body.

        The page state (URL, title, tabs and the capture state) is the
        shared browser writing, ``render_browser_state``; the element tree
        is Skyvern's own.
        """
        parts: list[str] = []
        page_state = render_browser_state(observation, show_media_state=self.show_media_state)
        if page_state:
            parts.append(page_state)

        dom = getattr(observation, "dom", None)
        if dom is not None:
            element_tree = getattr(dom, "element_tree", None)
            if element_tree is not None:
                # ``element_tree`` is a ``DOMState``; the form the agent
                # is meant to read is its ``serialized_dom`` field.
                serialized = getattr(element_tree, "serialized_dom", None)
                dom_str = serialized if isinstance(serialized, str) else str(element_tree)
                parts.append(f"Element Tree:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty browser state)"


def _error_outcome(error: str | None) -> str:
    """A failed action's outcome in the window: ``error: <why>``, or just ``error``."""
    return f"error: {error}" if error else "error"
