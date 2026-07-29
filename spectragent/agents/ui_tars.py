"""UITarsAgent — vision-based agent with coordinate actions and AST parsing.

UI-TARS is a passive domain-knowledge provider: it knows the verb
vocabulary the UI-TARS family of models emits, and how to translate that
vocabulary into this framework's actions. It knows nothing about which
capability will execute them — its ``__wiring__`` names domain verbs
(``click_coordinate``, ``type_coordinate``), never a provider, so the
same agent negotiates against a browser, a desktop VM or an Android
emulator unchanged.

**One vocabulary, one source.** :data:`_VERB_TABLE` is the single place
that says what verbs exist. The parser reads it to translate model output
into Actions, and the system prompt is *generated* from it, intersected
with the actions this run actually negotiated. So the prompt cannot
advertise a verb ``guarded_act`` would reject, and cannot teach a
parameter the action does not read — the two failure modes this agent
shipped with.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

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
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.prompt import PromptSnapshot

# ── The verb vocabulary ──────────────────────────────────────


class VerbError(ValueError):
    """A verb was emitted with arguments its action cannot use."""


@dataclass(frozen=True)
class _Verb:
    """One UI-TARS verb, and everything that is true about it.

    Attributes:
        action: The capability-relative action id the verb means. It
            names no provider — ``build_guarded_act`` binds it to the one
            this run holds.
        signature: The call syntax taught to the model, e.g.
            ``click(point='(x, y)')``. It is *complete*: every argument
            the verb reads appears in it, so a model that follows it can
            reach every parameter :attr:`fills` names.
        fills: The action params :attr:`to_params` can produce. Rendered
            into the prompt with the descriptor's own type text, so the
            model sees what its arguments turn into — the descriptor's
            names are the executor's, and printing them raw would teach
            a vocabulary the parser rejects.
            ``tests/test_agents/test_ui_tars_prompt_action_vocabulary.py``
            holds this to the descriptor.
        to_params: Translates the verb's kwargs into the action's params.
            Raises :class:`VerbError` when a required argument is missing
            or malformed — a loud parse failure beats an action that
            reports ``Missing param`` one round trip later.
    """

    action: str
    signature: str
    fills: tuple[str, ...]
    to_params: Callable[[Mapping[str, Any]], dict[str, Any]]


def _point(kwargs: Mapping[str, Any], key: str = "point") -> tuple[int, int]:
    x, y = _parse_point(kwargs.get(key))
    if x is None or y is None:
        raise VerbError(
            f"{key}={kwargs.get(key)!r} is not a coordinate; "
            f"expected {key}='(x, y)'"
        )
    return x, y


def _click_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    x, y = _point(kwargs)
    return {"coordinate_x": x, "coordinate_y": y}


def _type_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    x, y = _point(kwargs)
    # UI-TARS says ``content``; the action reads ``text``. Renaming here
    # is the whole reason this table exists — the agent used to forward
    # ``content`` verbatim and type the empty string.
    return {"coordinate_x": x, "coordinate_y": y, "text": kwargs.get("content", "")}


def _drag_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    sx, sy = _point(kwargs, "start_point")
    ex, ey = _point(kwargs, "end_point")
    return {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}


def _scroll_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # ``point`` and ``amount`` are both dropped, for the same reason from
    # two directions: ScrollAction scrolls the viewport or a selector_map
    # element, and a vision-only run has no selector map; how far one
    # scroll goes is ``ScrollAction.default_amount``, capability-side
    # config. An agent declares that it can scroll, not how many pixels.
    direction = kwargs.get("direction")
    if not direction:
        raise VerbError("scroll needs direction='up'|'down'|'left'|'right'")
    return {"direction": str(direction)}


def _hotkey_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    keys = kwargs.get("key") or kwargs.get("keys")
    if not keys:
        raise VerbError("hotkey needs key='ctrl+a'")
    return {"keys": str(keys)}


def _wait_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # How long a wait waits is ``WaitAction.default_seconds`` — the same
    # capability-side-config rule as ``scroll``'s amount.
    return {}


def _finished_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # DoneAction reads ``extracted_content``, not ``content``.
    return {"success": True, "extracted_content": kwargs.get("content", "")}


#: Every verb UI-TARS may emit. A verb absent from this table is a parse
#: error, not a silently manufactured action name — the previous
#: ``f"browser.{func_name}"`` fallback turned seven prompt-taught verbs
#: (``hotkey``, ``drag``, ``wait``, ``long_press``, ``open_app``,
#: ``press_home``, ``press_back``) into per-step guard rejections nobody
#: ever saw. The mobile-only four have no browser action and no verb
#: here; when an Android capability lands they arrive as its actions.
_VERB_TABLE: dict[str, _Verb] = {
    "click": _Verb(
        "click_coordinate",
        "click(point='(x, y)')",
        ("coordinate_x", "coordinate_y"),
        _click_params,
    ),
    "type": _Verb(
        "type_coordinate",
        "type(content='text to type', point='(x, y)')",
        ("coordinate_x", "coordinate_y", "text"),
        _type_params,
    ),
    "scroll": _Verb(
        "scroll",
        "scroll(direction='up|down|left|right')",
        ("direction",),
        _scroll_params,
    ),
    "hotkey": _Verb(
        "send_keys", "hotkey(key='ctrl+a')", ("keys",), _hotkey_params
    ),
    "drag": _Verb(
        "drag",
        "drag(start_point='(x1, y1)', end_point='(x2, y2)')",
        ("start_x", "start_y", "end_x", "end_y"),
        _drag_params,
    ),
    "wait": _Verb("wait", "wait()", (), _wait_params),
    "finished": _Verb(
        "done",
        "finished(content='answer if applicable')",
        ("extracted_content", "success"),
        _finished_params,
    ),
}

#: Action id → the verb that means it. Used to render the prompt from the
#: *negotiated* descriptors rather than from a hand-written list.
_ACTION_TO_VERB: dict[str, str] = {v.action: name for name, v in _VERB_TABLE.items()}


@dataclass
class UITarsAgent(AgentBase):
    """UI-TARS style agent — vision-based with AST action parsing."""

    __registry_key__ = "ui_tars"

    # ── Cross-registry wiring ────────────────────────────────────
    # Domain verbs only. No ``capability`` entry: naming one is the
    # welding F10 removed, and nothing at runtime read it — it only
    # narrowed the generated config to whichever capability happened to
    # be registered.
    #
    # Pure vision: a screenshot and the task, nothing DOM-shaped. The
    # required actions are what the model cannot work without; the
    # optional four are the rest of its native vocabulary, dropped from
    # both the prompt and the parser's reach when a capability does not
    # offer them.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react"]),
        "observation": (
            "observation",
            ["screenshot", "task"],
            ["step_info"],
        ),
        "action": (
            "action",
            ["click_coordinate", "type_coordinate", "done"],
            ["scroll", "send_keys", "drag", "wait"],
        ),
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    # The members are the on-disk stems, so nothing has to case-fold a
    # config value into a filename on the way to the loader. Hand-written
    # rather than computed from the directory: a ``Literal[tuple(...)]``
    # renders correctly through conscribe but costs a pyright error per
    # field. ``tests/test_agents/test_prompt_tree.py`` asserts the two
    # agree, so a typo now fails Pydantic validation at compose time
    # instead of ``FileNotFoundError``-ing inside ``prepare()``.
    prompt_template: Annotated[
        Literal["computer_use", "grounding", "mobile_use"],
        PydanticField(description="Which shipped prompt variant to render"),
    ] = "computer_use"
    language: Annotated[str, PydanticField(description="Thinking language")] = "English"

    # ── internal state ─────────────────────────────────────────
    # ``task`` reaches every step via ``obs.task.user_request`` (loop
    # fragment); the agent does not hold its own copy.
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(
        default=None, init=False, repr=False
    )

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
    def system_prompt(self) -> PromptSnapshot | None:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(self._action_descriptors)

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_descriptors: list[ActionDescriptor],
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        ``task`` is also baked into the system prompt template here, but
        it flows through every step's observation as
        ``obs.task.user_request`` for the per-step user message.
        """
        self._action_descriptors = list(action_descriptors)
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task, self._action_descriptors
        )
        return [SystemMessage(content=self._system_prompt_text)]

    # ── ReactLoop slots ────────────────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Pure builder: return the screenshot user-message for this step.

        UI-TARS is purely vision-driven, so we always include the
        screenshot. If the screenshot fragment is missing or has no
        bytes, we fall back to a text placeholder so the LLM call still
        succeeds (the agent will likely produce a `wait` action).
        """
        task_text = observation.task.user_request
        screenshot = getattr(observation, "screenshot", None)
        image_bytes: bytes | None = None
        if screenshot is not None:
            image_bytes = getattr(screenshot, "image_bytes", None)
            if image_bytes is None and getattr(screenshot, "image_path", None):
                with open(screenshot.image_path, "rb") as f:
                    image_bytes = f.read()

        if image_bytes is None:
            return [UserMessage(content=f"Task: {task_text}\n(no screenshot available)")]

        import base64

        b64 = base64.b64encode(image_bytes).decode()
        content: list[Any] = [
            ContentPartTextParam(text=f"Task: {task_text}"),
            ContentPartImageParam(
                image_url=ImageURL(
                    url=f"data:image/png;base64,{b64}",
                    detail="high",
                ),
            ),
        ]
        return [UserMessage(content=content)]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Parse free-form text output via AST coordinate extraction."""
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        return _parse_ui_tars_response(text)

    def get_react_output_model(self) -> type[BaseModel] | None:
        # UI-TARS uses free-form text, not structured output.
        return None

    # ── private helpers ────────────────────────────────────────

    def _build_system_prompt(
        self, task: str, action_space: list[ActionDescriptor]
    ) -> tuple[str, PromptSnapshot]:
        return self.render(
            self.prompt_template,
            {"task": task, "action_space": _render_action_space(action_space)},
        )


def _render_action_space(descriptors: list[ActionDescriptor]) -> str:
    """Render the negotiated actions as UI-TARS call syntax.

    One block per negotiated action::

        - click(point='(x, y)')
          Click at absolute screen coordinates.
          executes: coordinate_x: int, coordinate_y: int

    The verb and its syntax come from :data:`_VERB_TABLE`, which is what
    the parser reads; the description and the parameter types come from
    the descriptor, which is what the action actually executes. Both
    halves are derived, so the prompt has no second vocabulary to drift
    from — the hand-written ``# Output Format`` verb table that used to
    contradict this list is gone.

    The ``executes`` line is restricted to the params the verb can
    produce, and named as a *consequence* rather than as syntax: the
    descriptor's names belong to the executor (``coordinate_x``), the
    model writes the verb's (``point``), and a model shown both without
    that distinction writes the wrong one.

    A negotiated action with no verb is skipped rather than advertised:
    the model has no syntax for it, so teaching its name would invite an
    unparseable call. The wiring and the table are kept in agreement by
    ``tests/test_agents/test_ui_tars_react_compatible.py``.
    """
    blocks: list[str] = []
    for d in descriptors:
        verb_name = _ACTION_TO_VERB.get(d.name.rsplit(".", 1)[-1])
        if verb_name is None:
            continue
        verb = _VERB_TABLE[verb_name]
        lines = [f"- {verb.signature}", f"  {d.description}"]
        filled = [(k, d.params[k]) for k in verb.fills if k in d.params]
        if filled:
            rendered = ", ".join(f"{k}: {v}" for k, v in filled)
            lines.append(f"  executes: {rendered}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


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

    Names come out capability-relative — ``click_coordinate``, not
    ``browser.click_coordinate``. Binding one to the capability this run
    holds is ``build_guarded_act``'s job, and doing it here is what
    pinned a computer-use model to the browser.

    An unknown verb raises :class:`ParseError` rather than being turned
    into a plausible-looking action name. The Loop feeds the detail back
    to the model, which is a correction signal; the old behaviour
    produced a guard rejection per step with no route back to the prompt.
    """
    try:
        node = ast.parse(action_str, mode="eval")
    except SyntaxError:
        return None
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
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except ValueError:
                return None

    verb = _VERB_TABLE.get(func_name)
    if verb is None:
        raise ParseError(
            f"Unknown UI-TARS verb {func_name!r}. "
            f"Known verbs: {sorted(_VERB_TABLE)}.",
            raw_output=action_str[:500],
        )

    try:
        params = verb.to_params(kwargs)
    except VerbError as exc:
        raise ParseError(
            f"Cannot execute {func_name!r}: {exc}",
            raw_output=action_str[:500],
        ) from exc

    return Action(name=verb.action, params=params)


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
