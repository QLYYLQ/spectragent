"""SkyvernAgent — element-tree based agent with action history window.

M14 refactor: Skyvern is now a passive domain-knowledge provider.
Jinja2 prompt templating and the action-history window remain as agent
config; the runtime/loop drive execution.

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

from spectragent.agents._output_models import SkyvernOutput
from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompts import PromptEngine
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.prompt import PromptSnapshot


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
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react"]),
        # DOM is required, screenshot is optional (Skyvern is mostly
        # element-tree based but can use vision when available).
        "observation": (
            "observation",
            ["dom", "task"],
            [
                "screenshot",
                "browser_meta",
                "step_info",
                "history",
            ],
        ),
        # The *negotiated* action names are Skyvern's vocabulary:
        # ``prepare()`` renders those descriptors into the system prompt
        # verbatim, so there is no second verb list to keep in sync and
        # no translation table to drift.
        "action": (
            "action",
            ["click", "type", "done"],
            [
                "scroll",
                "send_keys",
                "go_back",
                "go_forward",
                "select_dropdown",
                "extract_content",
            ],
        ),
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``action_history_window`` counts *actions*, not steps, and it is
    # spent in the per-step user message — not the system prompt. The
    # system prompt is built once, inside ``prepare()``; rendering a
    # window that grows each step into it would mean re-rendering it
    # every step, which the pure-builder ``prepare()`` contract in
    # ``core/runtime.py`` does not allow.
    action_history_window: Annotated[
        int,
        PydanticField(
            description=("Number of most recent actions replayed in each step's user message")
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
                "preferences); rendered as <user_details> in the system prompt"
            )
        ),
    ] = None
    complete_criterion: Annotated[
        str | None,
        PydanticField(
            description=("Condition that must hold before the agent may finish with success=true")
        ),
    ] = None
    terminate_criterion: Annotated[
        str | None,
        PydanticField(description="Condition on which the agent must give up (success=false)"),
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

    # ── internal state ─────────────────────────────────────────
    # ``task`` reaches every step via ``obs.task.user_request`` (loop
    # fragment); the agent does not hold its own copy. ``_action_history``
    # is agent-owned and per-run: ``prepare()`` clears it, every parsed
    # step appends to it, and ``build_react_prompt`` renders the tail of
    # it. It is what makes Skyvern's recency window a narrow list of the
    # last N *actions* rather than the loop's full per-step transcript.
    _action_history: list[str] = field(default_factory=list, init=False, repr=False)
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

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_descriptors: list[ActionDescriptor],
        max_actions_per_step: int = 1,
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        Action history is non-memory state owned by the agent, so the
        per-run reset happens here; it is *rendered* per step, in
        ``build_react_prompt``. The per-step user message gets
        task/step_info from observation fragments, not ``self``.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates. ``SkyvernOutput.actions`` is a list, so
        without it the prompt would invite a plan the loop silently cuts
        short.
        """
        self._action_descriptors = list(action_descriptors)
        self._action_history.clear()
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task, self._action_descriptors, max_actions_per_step
        )
        # cache=True: static-prefix breakpoint — see BrowserUseAgent for why it
        # is kept alongside the per-step one.
        return [SystemMessage(content=self._system_prompt_text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Pure builder: return the observation user-message for this step.

        Prefixes the page state with ``<user_request>`` (and ``<step_info>``
        when the loop provides it) so the LLM sees the task on every
        turn — the system prompt's task injection happens once at
        ``prepare()`` time and would otherwise drift out of context.

        ``<action_history>`` lives here rather than in the system prompt
        because it changes every step: the system prompt is built once,
        by ``prepare()``, and re-rendering it per step would break that
        contract. The template's "if the last step's actions did not move
        the page, try something else" rule reads this block.
        """
        task = observation.task.user_request
        prefix_parts = [f"<user_request>\n{task}\n</user_request>"]
        step_info = getattr(observation, "step_info", None)
        if step_info is not None:
            prefix_parts.append(
                f"<step_info>step {step_info.step}/{step_info.max_steps} "
                f"(today={step_info.today})</step_info>"
            )
        prefix_parts.append(f"<action_history>\n{self._render_action_history()}\n</action_history>")
        body = "\n".join(prefix_parts) + "\n\n" + self._render_browser_state(observation)
        return [UserMessage(content=body, cache=True)]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        if isinstance(llm_output, SkyvernOutput):
            # When ``is_done`` is set, Skyvern terminates the task with
            # just a done action — preceding actions (if any) are the
            # LLM's justification and need not execute. This makes the
            # output compatible with ``ReactLoop.max_actions_per_step=1``
            # (the default). ``extracted_content`` flows straight into
            # ``DoneAction.execute`` — no renaming hack needed.
            if llm_output.is_done:
                done = self._done_action(llm_output.extracted_content, llm_output.success)
                self._action_history.append(f"{done.name}({done.params})")
                return [done]

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

            # Track actions in the window rendered by the next
            # ``build_react_prompt``.
            for a in actions:
                self._action_history.append(f"{a.name}({a.params})")

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
        max_actions_per_step: int = 1,
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

    def _render_action_history(self) -> str:
        """The last ``action_history_window`` actions, most recent last.

        The ``<= 0`` branch is not defensive padding: ``lst[-0:]`` is
        ``lst[0:]``, so a window of 0 — the obvious way to say "no
        history" in YAML — would otherwise render the *entire* run.
        """
        if self.action_history_window <= 0:
            return "(disabled)"
        window = self._action_history[-self.action_history_window :]
        return "\n".join(window) if window else "(no actions yet)"

    @staticmethod
    def _render_browser_state(observation: Any) -> str:
        """Format the browser-state portion of the user message body."""
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
                # ``element_tree`` is a ``DOMState``; the form the agent
                # is meant to read is its ``serialized_dom`` field. Its
                # bare repr is dataclass noise that the truncation below
                # would eat long before any ``[index]`` appeared.
                dom_str = getattr(element_tree, "serialized_dom", "") or str(element_tree)
                if len(dom_str) > 10000:
                    dom_str = dom_str[:10000] + "\n... (truncated)"
                parts.append(f"Element Tree:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty browser state)"
