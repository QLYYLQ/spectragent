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

from pydantic import AfterValidator, BaseModel
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
# / ``go_to_url``) but the action ids are ``click`` / ``type`` /
# ``navigate``. This dict renames those three verbs and nothing else — it
# is a *vocabulary* map, not a provider map. Which capability's ``click``
# a name resolves to is decided once, at negotiation, and applied once,
# in ``build_guarded_act``; an agent that answered that question itself
# would be answering it for a capability set it cannot see.
_LLM_VOCABULARY_TO_LOCAL: dict[str, str] = {
    "click_element": "click",
    "input_text": "type",
    "go_to_url": "navigate",
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
            ["browser.dom", "browser.screenshot", "loop.task"],
            ["browser.browser_meta", "loop.step_info", "loop.history"],
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
                "browser.wait",
            ],
        ),
        "llm": "llm",  # auto-discovery — any registered LLM
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``max_steps`` / ``max_actions_per_step`` / ``timeout_seconds`` are
    # deliberately absent: the Loop owns and enforces them. AgentRuntime
    # hands the loop's limits to ``prepare()`` so the prompt advertises
    # exactly what will be executed.
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
    # ``task`` lives on every observation as ``obs.task.user_request``
    # (loop-source fragment), so the agent never holds its own copy.
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _per_run_output_model: type[BaseModel] | None = field(
        default=None, init=False, repr=False,
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_path: str = field(default="", init=False, repr=False)
    _prompt_snapshot: Any = field(default=None, init=False, repr=False)
    _max_history_steps: int = field(default=10, init=False, repr=False)

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

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_descriptors: list[ActionDescriptor],
        max_actions_per_step: int = 1,
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        AgentRuntime invokes this once after negotiation and writes the
        return value into memory; the agent does no memory IO itself.
        ``task`` flows through every step's observation as
        ``obs.task.user_request`` — not stored on ``self``.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates. The agent must not carry its own copy: the
        prompt has to promise exactly what the loop will run.
        """
        self._action_descriptors = list(action_descriptors)
        self._per_run_output_model = _build_per_run_output_model(
            self._action_descriptors,
        )
        self._system_prompt_text = self._build_system_prompt(
            task,
            self._action_descriptors,
            max_actions_per_step,
        )
        from spectragent.schemas.prompt import PromptRef, TextPrompt

        self._prompt_snapshot = TextPrompt(
            ref=PromptRef(path=self._prompt_path or "<runtime>", content_hash=""),
            text=self._system_prompt_text,
        )
        return [SystemMessage(content=self._system_prompt_text)]

    # ── ReactLoop slots ────────────────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Pure builder: return the messages to append for this step.

        Emits an upstream-browser-use-style structured user message:
        ``<agent_history>...</agent_history><agent_state>...</agent_state>
        <browser_state>...</browser_state>``. ``cache=True`` lets
        Anthropic prefix-cache the (mostly stable) task/state header.
        """
        body = self._build_step_text(observation)
        if self.use_vision and self._observation_has_screenshot(observation):
            return [self._build_vision_message(observation, body)]
        return [UserMessage(content=body, cache=True)]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Parse a ``BrowserUseOutput`` into canonical Action objects.

        The LLM follows the upstream browser-use prompt template and
        emits ``{"action": [{"<verb>": {<params>}}, ...]}``. We translate
        the verb (browser-use vocabulary like ``navigate`` /
        ``click_element`` / ``go_to_url``) to the canonical
        ``browser.*`` action names so agent wiring, runtime negotiation
        and ``guarded_act`` all speak the same vocabulary.
        """
        if isinstance(llm_output, BrowserUseOutput):
            return _items_to_actions(llm_output.action)

        # Fallback: parse plain-text JSON.
        import json

        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"LLM output is not valid JSON: {exc}",
                raw_output=text[:500],
            ) from exc

        # Validate against the per-run model so the negotiated verb set
        # rejects hallucinations here, on the text path too — not one
        # step later at ``guarded_act``. Falls back to the loose base
        # model only when ``prepare()`` has not run.
        model = self._per_run_output_model or BrowserUseOutput
        try:
            parsed = model.model_validate(data)
        except Exception as exc:
            raise ParseError(
                f"LLM JSON does not match {model.__name__} schema: "
                f"{type(exc).__name__}: {exc}",
                raw_output=text[:500],
            ) from exc

        return self.parse_react_output(parsed)

    def get_react_output_model(self) -> type[BaseModel] | None:
        # ``prepare()`` builds a per-run subclass that narrows ``action``
        # to the negotiated verb set. Falls back to the static loose
        # model if ``prepare()`` hasn't run yet (defensive — every real
        # call path goes through prepare first).
        return self._per_run_output_model or BrowserUseOutput

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
        task: str,  # noqa: ARG002 — reserved; some prompt templates may consume it later
        action_space: list[ActionDescriptor],
        max_actions_per_step: int = 1,
    ) -> str:
        if self.override_system_message:
            text = self.override_system_message
        else:
            prompt_file = _PROMPT_DIR / self._select_prompt_file()
            self._prompt_path = str(prompt_file.relative_to(PROMPT_PATH))
            template = prompt_file.read_text()
            text = template.format(
                max_actions=max_actions_per_step,
                action_list=self._render_action_list(action_space),
            )

        if self.extend_system_message:
            text += f"\n{self.extend_system_message}"
        return text

    @staticmethod
    def _render_action_list(descriptors: list[ActionDescriptor]) -> str:
        """Render action descriptors as a markdown bullet list for the prompt.

        Each line: ``- `<descriptor_name>(<params>)` — <description>``.

        The descriptor's own name is used verbatim, so the prompt
        advertises exactly the vocabulary ``env.act`` routes on. The
        parser additionally accepts each action's local id and the three
        legacy upstream verbs; a local id is resolved to a provider by
        ``build_guarded_act``, never here.

        Empty list yields an empty string — only happens before
        ``prepare()`` runs or in degenerate test fixtures.
        """
        if not descriptors:
            return ""
        lines: list[str] = []
        for d in descriptors:
            if d.params:
                params_part = ", ".join(
                    f"{k}: {v}" for k, v in d.params.items()
                )
            else:
                params_part = "no params"
            lines.append(f"- `{d.name}({params_part})` — {d.description}")
        return "\n".join(lines)

    @staticmethod
    def _observation_has_screenshot(observation: Any) -> bool:
        """Check whether a NegotiatedObservation has screenshot data."""
        screenshot = getattr(observation, "screenshot", None)
        if screenshot is None:
            return False
        return getattr(screenshot, "image_bytes", None) is not None or (
            getattr(screenshot, "image_path", None) is not None
        )

    def _build_vision_message(self, observation: Any, body: str) -> UserMessage:
        """Build a multi-modal user message with body + screenshot.

        Caller already checked ``_observation_has_screenshot`` so
        ``image_bytes`` (or ``image_path``) is guaranteed present, and
        already constructed the structured XML body.
        """
        import base64

        from spectragent.core.llm.messages import (
            ContentPartImageParam,
            ContentPartTextParam,
            ImageURL,
        )

        screenshot = observation.screenshot
        image_bytes = getattr(screenshot, "image_bytes", None)
        if image_bytes is None:
            with open(screenshot.image_path, "rb") as f:
                image_bytes = f.read()

        b64 = base64.b64encode(image_bytes).decode()
        content: list[Any] = [
            ContentPartTextParam(text=body),
            ContentPartImageParam(
                image_url=ImageURL(
                    url=f"data:image/png;base64,{b64}",
                    detail=self.vision_detail,
                ),
            ),
        ]
        return UserMessage(content=content, cache=True)

    def _build_step_text(self, observation: Any) -> str:
        """Compose the per-step XML user-message body.

        Layout (upstream browser-use prompts.py:400-416):
            <agent_history>...</agent_history>
            <agent_state>
              <user_request>...</user_request>
              <follow_up_requests>...</follow_up_requests>  (only if any)
              <step_info>...</step_info>                    (only if known)
            </agent_state>
            <browser_state>URL/Title/Tabs/DOM</browser_state>

        Scratchpad blocks (file_system, todo, plan, sensitive_data,
        page_stats, recent_events, ...) are intentionally omitted —
        their data sources land with the future ScratchpadCapability.
        """
        history_xml = self._render_history(getattr(observation, "history", None))
        state_xml = self._render_agent_state(observation)
        browser_xml = self._render_browser_state(observation)
        return (
            f"<agent_history>\n{history_xml}\n</agent_history>\n"
            f"{state_xml}\n"
            f"<browser_state>\n{browser_xml}\n</browser_state>"
        )

    def _render_agent_state(self, observation: Any) -> str:
        task = observation.task
        parts = [f"<user_request>\n{task.user_request}\n</user_request>"]
        if task.follow_ups:
            joined = "\n".join(f"- {f}" for f in task.follow_ups)
            parts.append(f"<follow_up_requests>\n{joined}\n</follow_up_requests>")
        step_info = getattr(observation, "step_info", None)
        if step_info is not None:
            parts.append(
                f"<step_info>step {step_info.step}/{step_info.max_steps}, "
                f"today={step_info.today}</step_info>"
            )
        body = "\n".join(parts)
        return f"<agent_state>\n{body}\n</agent_state>"

    def _render_history(self, history: Any) -> str:
        items = list(getattr(history, "items", []) or [])
        if not items:
            return "<step_1>\n(no prior steps)\n</step_1>"
        if len(items) > self._max_history_steps:
            items = items[-self._max_history_steps :]
        blocks: list[str] = []
        for rec in items:
            actions_str = (
                ", ".join(f"{a.name}({a.params})" for a in rec.actions) or "(none)"
            )
            results_str = (
                "; ".join(
                    f"ok={r.success} {r.error or r.extracted_content or ''}".rstrip()
                    for r in rec.action_results
                )
                or "(none)"
            )
            tag = f"step_{rec.step + 1}"
            blocks.append(
                f"<{tag}>\n"
                f"<llm_output>{_truncate(rec.llm_output, 2000)}</llm_output>\n"
                f"<actions>{actions_str}</actions>\n"
                f"<results>{results_str}</results>\n"
                f"</{tag}>"
            )
        return "\n".join(blocks)

    @staticmethod
    def _render_browser_state(observation: Any) -> str:
        """URL / Title / Tabs / DOM block. Mirrors upstream's <browser_state>
        but without page_stats / page_info / recent_events (ScratchpadCapability)."""
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
                dom_str = getattr(element_tree, "serialized_dom", "") or str(element_tree)
                if len(dom_str) > 10000:
                    dom_str = dom_str[:10000] + "\n... (truncated)"
                parts.append(f"DOM:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty browser state)"


def _build_per_run_output_model(
    descriptors: list[ActionDescriptor],
) -> type[BrowserUseOutput]:
    """Return a per-run :class:`BrowserUseOutput` subclass narrowing ``action``.

    The static ``BrowserUseOutput.action`` is ``list[dict[str, Any]]`` —
    the LLM can write any verb and the schema accepts it, so hallucinated
    actions only get caught downstream at ``guarded_act``, one step and a
    whole prompt's tokens later. Attaching a per-item validator that
    knows the negotiated allowed-verb set moves that rejection to
    ``parse_react_output`` time, and the error message carries the
    allowed set so the LLM gets a self-correction signal on its next
    turn.

    **The narrowing is Python-side only.** The verb domain lives in an
    ``AfterValidator``, which contributes nothing to
    ``model_json_schema()`` — the field still compiles to
    ``{"type": "array", "items": {"type": "object"}}``, byte-identical to
    the base model. That is deliberate: the field is an open
    ``{"<verb>": {<params>}}`` map, and JSON Schema cannot constrain a
    map's key domain in a way the provider structured-output dialects
    accept. Do not read this model as provider-enforced.

    Keeping ``action`` typed ``list[dict[str, Any]]`` (rather than
    wrapping items in a ``RootModel``) also keeps the two output models
    wire-identical downstream: ``parse_react_output`` /
    ``_items_to_actions`` see plain dicts either way, and
    ``model_dump_json()`` round-trips without a Pydantic serializer
    mismatch.

    The allowed set is the union of:

    - the descriptor names this run negotiated (``browser.click`` etc.)
    - their local ids (``click``), which the guard resolves back to a
      provider
    - legacy upstream browser-use verbs (``click_element``,
      ``input_text``, ``go_to_url``) whose local id is in the run set

    This is an *acceptance* surface, not a name-to-provider map: it says
    which strings are worth forwarding, and never which capability one of
    them means.
    """
    advertised = {d.name for d in descriptors}
    local_ids = {name.rsplit(".", 1)[-1] for name in advertised}
    legacy_aliases = {
        legacy
        for legacy, local in _LLM_VOCABULARY_TO_LOCAL.items()
        if local in local_ids
    }
    allowed: frozenset[str] = frozenset(advertised | local_ids | legacy_aliases)
    allowed_sorted = sorted(allowed)

    def _check_action_item(item: dict[str, Any]) -> dict[str, Any]:
        if len(item) != 1:
            raise ValueError(
                f"action item must have exactly one key, got {sorted(item)}"
            )
        verb, params = next(iter(item.items()))
        if verb not in allowed:
            raise ValueError(
                f"unknown action {verb!r}; allowed: {allowed_sorted}"
            )
        if not isinstance(params, dict):
            raise ValueError(
                f"params for {verb!r} must be an object, got "
                f"{type(params).__name__}"
            )
        return item

    _action_description = (
        "Action list. Each item is a single-key object where the key is the "
        "action name and the value is its params object, e.g. "
        "``{'browser.navigate': {'url': 'https://...'}}``. The key must be "
        f"one of: {', '.join(allowed_sorted)}."
    )

    # Named at definition, not patched onto ``__name__`` afterwards:
    # Pydantic freezes the class name into the core schema at creation, so
    # a later rename left ``ValidationError`` saying ``_PerRunOutput``
    # while every other surface said ``BrowserUseOutputForRun``. That
    # error text is fed back to the LLM as a self-correction signal.
    class BrowserUseOutputForRun(BrowserUseOutput):
        # Two deliberate differences from the base field:
        #
        # 1. No default — ``action`` becomes ``required`` in the emitted
        #    JSON schema, so constrained decoding forces the model to
        #    produce one. (pyright wants an override to keep the base's
        #    default; here losing it is the point.)
        # 2. The description is restated. Redeclaring a field drops the
        #    base's, and this schema is now shipped to providers, where
        #    ``description`` is the one channel every structured-output
        #    dialect passes to the model verbatim — so it carries the
        #    negotiated verb set.
        action: list[  # pyright: ignore[reportGeneralTypeIssues]
            Annotated[dict[str, Any], AfterValidator(_check_action_item)]
        ] = PydanticField(description=_action_description)

    BrowserUseOutputForRun.__qualname__ = "BrowserUseOutputForRun"
    return BrowserUseOutputForRun


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... (truncated)"


def _items_to_actions(items: list[dict[str, Any]]) -> list[Action]:
    """Translate ``[{"<verb>": {<params>}}, ...]`` into Actions.

    Each item is a single-key dict (the upstream browser-use shape).
    The key is the action verb in browser-use vocabulary; the value is
    the params dict. Legacy upstream verbs are renamed to the matching
    action's local id; everything else is passed through as written, and
    a local id is bound to a capability by ``build_guarded_act``. Minimal
    per-action param renames (``done.text`` → ``extracted_content``) are
    applied here because they are domain knowledge about the action, not
    about its provider.
    """
    if not items:
        raise ParseError(
            "LLM output had an empty 'action' list — at least one action is required.",
            raw_output=str(items)[:500],
        )

    actions: list[Action] = []
    for raw in items:
        if not isinstance(raw, dict) or len(raw) != 1:
            raise ParseError(
                f"Each action must be a single-key dict like {{'navigate': {{...}}}}; "
                f"got {raw!r}",
                raw_output=str(items)[:500],
            )
        verb, params = next(iter(raw.items()))
        if not isinstance(params, dict):
            params = {}
        name = _LLM_VOCABULARY_TO_LOCAL.get(verb, verb)
        # Done's answer text comes in under 'text'; DoneAction reads
        # 'extracted_content'. Rename onto a fresh dict so we don't
        # mutate the caller's payload.
        params = dict(params)
        if (
            name.rsplit(".", 1)[-1] == "done"
            and "text" in params
            and "extracted_content" not in params
        ):
            params["extracted_content"] = params.pop("text")
        actions.append(Action(name=name, params=params))
    return actions
