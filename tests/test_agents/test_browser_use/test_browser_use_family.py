"""The ``browser_use`` family: two members, each a complete ``__wiring__``.

``BrowserUseAgent`` is an abstract family root (``docs/architecture_design.md``
§4.1 "Agent 家族", "BrowserUse 家族"). ``browser_use.dom`` reads the cleaned
DOM; ``browser_use.dom_vision`` reads it and is shown the screenshot. Both
require the DOM: a model that reads only the screenshot is a computer-use
agent (``ui_tars``).

Everything here runs against the real ``BrowserCapability`` /
``BrowserEnvironment`` / ``ReactLoop`` through real negotiation — no CDP,
no network. What is pinned:

- each member's wiring and its negotiated fragments and actions, item for item;
- ``browser_use.dom`` never shows the screenshot, ``browser_use.dom_vision`` does;
- ``AgentRuntime.prepare()`` refusing a profile without a DOM, naming the member;
- the §2.5 seam: what the agent emits is what ``descriptor().params``
  declares, all the way to the guard;
- the provider transforms, offline: strict-compatible, ``$ref``-free,
  ``oneOf``-free, one required property per union branch;
- the reasoning fields, against the ``<output>`` block of the very
  template each configuration renders.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

import pytest
import yaml

from spectragent.agents import AgentRegistrar
from spectragent.agents.browser_use import (
    BrowserUseAgent,
    BrowserUseDomAgent,
    BrowserUseDomVisionAgent,
)
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.prompts import AGENT_PROMPT_ROOT
from spectragent.core.action_guard import build_guarded_act, params_refusal
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.benchmark_runner import build_composer
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.llm.messages import ContentPartImageParam, ContentPartTextParam
from spectragent.core.llm.schema.gemini import fix_gemini_schema
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import (
    IncompatibleActionError,
    IncompatibleFragmentError,
    extract_wiring,
    negotiate_action,
    negotiate_observation,
)
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_params import minimal_params
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent, make_env
from tests._helpers.fragment_formats import dom_formats

MEMBERS: tuple[type[BrowserUseAgent], ...] = (BrowserUseDomAgent, BrowserUseDomVisionAgent)

#: Each member's declaration, written out. The point of writing it rather
#: than reading it back is that a change to a member's ``__wiring__`` has to
#: be restated here by whoever makes it.
_OBSERVATION = (
    {"dom", "screenshot", "platform", "task", "browser_meta"},
    {"step_info", "history", "capture_state"},
)
_EXPECTED_WIRING: dict[type[BrowserUseAgent], dict[str, tuple[set[str], set[str]]]] = {
    BrowserUseDomAgent: {
        "observation": _OBSERVATION,
        "action": (
            {"navigate", "done", "click", "type"},
            {
                "scroll",
                "send_keys",
                "extract_content",
                "go_back",
                "go_forward",
                "switch_tab",
                "wait",
            },
        ),
    },
    BrowserUseDomVisionAgent: {
        "observation": _OBSERVATION,
        "action": (
            {"navigate", "done", "click", "type"},
            {
                "click_coordinate",
                "scroll",
                "send_keys",
                "extract_content",
                "go_back",
                "go_forward",
                "switch_tab",
                "wait",
            },
        ),
    },
}

_DOM_ACTIONS = {
    "browser.click",
    "browser.type",
    "browser.navigate",
    "browser.done",
    "browser.scroll",
    "browser.send_keys",
    "browser.extract_content",
    "browser.go_back",
    "browser.go_forward",
    "browser.switch_tab",
    "browser.wait",
}
_EXPECTED_ACTIONS: dict[type[BrowserUseAgent], set[str]] = {
    BrowserUseDomAgent: _DOM_ACTIONS,
    BrowserUseDomVisionAgent: _DOM_ACTIONS | {"browser.click_coordinate"},
}

#: Both members also negotiate the optional ``capture_state`` the browser provides.
_EXPECTED_FRAGMENTS = {
    "browser.dom",
    "browser.screenshot",
    "browser.browser_meta",
    "browser.platform",
    "browser.capture_state",
}

_SHIPPED = Path(__file__).resolve().parents[3] / "configs" / "browser_use_local.yaml"


def _png(width: int = 1280, height: int = 720) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _observation(*, image: bytes | None = None) -> Any:
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        task=SimpleNamespace(user_request="tick the checkbox", follow_ups=[]),
        screenshot=ScreenshotObs(
            image_bytes=_png() if image is None else image,
            image_width=1280,
            image_height=720,
            viewport_width=1280,
            viewport_height=720,
        ),
        dom=DOMObs(element_tree=SimpleNamespace(serialized_dom="[3]<button>Submit</button>")),
        browser_meta=BrowserMetaObs(url="https://example.test", page_title="Example"),
    )


class _Run:
    """One assembled, negotiated, prepared run — no CDP, no network.

    The agent is prepared on the space framed on its declared grid, as
    ``AgentRuntime.prepare`` frames it.
    """

    def __init__(
        self, member: type[BrowserUseAgent], *, profile: str = "standard", **kwargs: Any
    ) -> None:
        self.capability = BrowserCapability(profile=profile)
        self.env = make_env(BrowserEnvironment, {"browser": self.capability})
        self.agent = make_agent(member, memory=SimpleMemory(max_messages=10), **kwargs)
        self.loop = ReactLoop(max_actions_per_step=1)
        self.obs = negotiate_observation(self.agent, self.loop, self.env)
        self.actions = negotiate_action(self.agent, self.env)
        descriptors = self.env.get_action_space(
            active_actions=self.actions.active_actions,
            fragments=self.obs.active_fragments,
        )
        self.action_space, self.guard = build_guarded_act(
            self.env.act,
            descriptors,
            None,
            bindings=self.actions.action_bindings,
        )
        self.descriptors = self.action_space.descriptors
        self.agent.prepare(
            task="t",
            action_space=space_for(self.agent, self.descriptors),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )

    @property
    def names(self) -> set[str]:
        return {d.name for d in self.descriptors}

    @property
    def model(self) -> Any:
        return self.agent.get_react_output_model()

    def prompt(self) -> str:
        text = self.agent._system_prompt_text
        assert isinstance(text, str)
        return text

    def reply(self, *items: dict[str, Any]) -> str:
        return json.dumps({"memory": "m", "action": list(items)})


# ── The family ────────────────────────────────────────────────────────


def test_the_family_is_two_members_and_an_unregistered_root() -> None:
    assert AgentRegistrar.children("browser_use") == {
        "browser_use.dom": BrowserUseDomAgent,
        "browser_use.dom_vision": BrowserUseDomVisionAgent,
    }
    assert AgentRegistrar.get_or_none("browser_use") is None
    assert BrowserUseAgent.__registry_key__ == "browser_use"
    assert BrowserUseDomAgent().agent_id == "browser_use.dom"
    assert BrowserUseDomVisionAgent().agent_id == "browser_use.dom_vision"


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize("registry", ["observation", "action"])
def test_each_member_writes_out_its_whole_demand(
    member: type[BrowserUseAgent], registry: str
) -> None:
    assert extract_wiring(member, registry) == _EXPECTED_WIRING[member][registry]


@pytest.mark.parametrize("member", MEMBERS)
def test_the_loop_range_comes_from_the_root(member: type[BrowserUseAgent]) -> None:
    assert extract_wiring(member, "agent_loop") == ({"react", "branch"}, set())


def test_only_the_member_that_shows_the_screenshot_has_screenshot_knobs() -> None:
    """``browser_use.dom`` writes no point and shows no image: no knob for either."""
    dom = {f.name for f in dataclasses.fields(BrowserUseDomAgent)}
    dom_vision = {f.name for f in dataclasses.fields(BrowserUseDomVisionAgent)}
    assert dom_vision - dom == {"coordinate_space", "vision_detail"}


def test_the_screenshot_is_sent_at_auto_detail_by_default_as_in_skyvern() -> None:
    """A point is read off the image, so ``low`` would make ``click_coordinate`` guess.

    Both families' ``dom_vision`` members take it the same way, under one description.
    """
    from spectragent.agents.skyvern import SkyvernDomVisionAgent

    assert BrowserUseDomVisionAgent().vision_detail == "auto"
    assert SkyvernDomVisionAgent().vision_detail == "auto"
    descriptions = {
        get_type_hints(member, include_extras=True)["vision_detail"].__metadata__[0].description
        for member in (BrowserUseDomVisionAgent, SkyvernDomVisionAgent)
    }
    (description,) = descriptions
    assert "'auto' (the default" in description and "click_coordinate guess" in description


def test_the_mode_switch_and_its_predecessor_are_gone_rather_than_aliased() -> None:
    with pytest.raises(TypeError):
        BrowserUseDomAgent(observation_mode="dom")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        BrowserUseDomVisionAgent(use_vision=True)  # type: ignore[call-arg]


def test_there_is_no_vision_only_surface() -> None:
    """Vision-only is a computer-use agent's job; its prompt fragments are deleted."""
    surfaces = {p.stem for p in (AGENT_PROMPT_ROOT / "browser_use" / "_surface").glob("*.md")}
    assert surfaces == {"dom", "dom_terse", "dom_vision", "dom_vision_terse"}


# ── What each member negotiates ───────────────────────────────────────


@pytest.mark.parametrize("member", MEMBERS)
def test_active_fragments_are_the_dom_and_the_screenshot(member: type[BrowserUseAgent]) -> None:
    run = _Run(member)
    env_sourced = {k for k in run.obs.active_fragments if not k.startswith("loop.")}
    assert env_sourced == _EXPECTED_FRAGMENTS


@pytest.mark.parametrize("member", MEMBERS)
def test_active_actions_are_the_members_own(member: type[BrowserUseAgent]) -> None:
    assert _Run(member).names == _EXPECTED_ACTIONS[member]


def test_dom_vision_has_two_independent_click_verbs() -> None:
    run = _Run(BrowserUseDomVisionAgent)
    click = next(d for d in run.descriptors if d.name == "browser.click")
    point = next(d for d in run.descriptors if d.name == "browser.click_coordinate")
    # Distinct contracts, not one overloaded verb with half-applicable params.
    assert set(click.params) == {"index"}
    assert set(point.params) == {"coordinate_x", "coordinate_y"}


def test_dom_has_no_verb_that_takes_a_point() -> None:
    run = _Run(BrowserUseDomAgent)
    assert all(not d.point_params for d in run.descriptors)
    assert run.action_space.coordinate_space is None


@pytest.mark.parametrize("member", MEMBERS)
def test_scroll_keeps_its_index(member: type[BrowserUseAgent]) -> None:
    scroll = next(d for d in _Run(member).descriptors if d.name == "browser.scroll")
    assert "index" in scroll.params


# ── What reaches the model ────────────────────────────────────────────


async def test_dom_renders_the_tree_and_never_shows_the_screenshot() -> None:
    """The observation carries a screenshot with bytes; this member records it only."""
    run = _Run(BrowserUseDomAgent)
    (message,) = await run.agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    assert isinstance(message.content, str), "browser_use.dom must not attach an image"
    assert "[3]<button>Submit</button>" in message.content


async def test_dom_vision_renders_the_tree_and_shows_the_screenshot() -> None:
    run = _Run(BrowserUseDomVisionAgent)
    (message,) = await run.agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    content = message.content
    assert isinstance(content, list)
    text, image = content
    assert isinstance(text, ContentPartTextParam) and isinstance(image, ContentPartImageParam)
    assert "[3]<button>Submit</button>" in text.text
    assert image.image_url.detail == "auto"


async def test_dom_vision_sends_text_alone_when_the_screenshot_has_no_bytes() -> None:
    """The same message ``browser_use.dom`` sends, not an image part with nothing in it."""
    observation = _observation()
    observation.screenshot = ScreenshotObs(
        image_bytes=None, image_width=1280, image_height=720, viewport_width=1280
    )
    context = ReactPromptContext(step=0, phase="policy")
    (shown,) = await _Run(BrowserUseDomVisionAgent).agent.build_react_prompt(
        observation, context=context
    )
    (dom,) = await _Run(BrowserUseDomAgent).agent.build_react_prompt(observation, context=context)
    assert isinstance(shown.content, str)
    assert shown.content == dom.content


async def test_the_image_is_labelled_from_its_own_bytes() -> None:
    """A JPEG observation must not be shipped as ``data:image/png``.

    The capability can be configured to capture JPEG
    (``screenshot_format``), and the agent is handed bytes, not a format.
    OpenAI sniffs past a wrong prefix; Anthropic rejects the request.
    """
    from PIL import Image

    run = _Run(BrowserUseDomVisionAgent)

    async def image_of(observation: Any) -> ContentPartImageParam:
        (message,) = await run.agent.build_react_prompt(
            observation, context=ReactPromptContext(step=0, phase="policy")
        )
        content = message.content
        assert isinstance(content, list)
        return next(p for p in content if isinstance(p, ContentPartImageParam))

    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720)).save(buffer, format="JPEG", quality=70)
    jpeg = await image_of(_observation(image=buffer.getvalue()))
    assert jpeg.image_url.url.startswith("data:image/jpeg;base64,")
    assert jpeg.image_url.media_type == "image/jpeg"

    # ...and the PNG default is still labelled PNG.
    png = await image_of(_observation())
    assert png.image_url.url.startswith("data:image/png;base64,")
    assert png.image_url.media_type == "image/png"


@pytest.mark.parametrize("member", MEMBERS)
def test_each_member_quotes_its_own_surface(member: type[BrowserUseAgent]) -> None:
    prompt = _Run(member, flash_mode=False).prompt()
    assert dom_formats()["dom"] in prompt
    assert ("<browser_vision>" in prompt) is (member is BrowserUseDomVisionAgent)


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize(
    "config",
    [
        {"flash_mode": True},
        {"flash_mode": False, "use_thinking": True},
        {"flash_mode": False, "use_thinking": False},
    ],
)
def test_a_fine_tuned_template_quotes_the_runs_dom_format(
    member: type[BrowserUseAgent], config: dict[str, Any]
) -> None:
    """The checkpoint learned one DOM format; this run's serializer may write another.

    So the fine-tuned templates quote the run's surface like every other
    template: the serializer's description, and the grid when the run has a
    coordinate action.
    """
    run = _Run(member, is_browser_use_model=True, **config)
    assert run.agent.select_prompt_variant().startswith("system_prompt_browser_use")
    prompt = run.prompt()
    assert dom_formats()["dom"] in prompt
    assert ("<coordinates>" in prompt) is (member is BrowserUseDomVisionAgent)


#: Every configuration's variant: the three base templates and the three
#: written for the fine-tuned checkpoint.
_EVERY_VARIANT: list[dict[str, Any]] = [
    {"is_browser_use_model": tuned, **config}
    for tuned in (False, True)
    for config in (
        {"flash_mode": True},
        {"flash_mode": False, "use_thinking": True},
        {"flash_mode": False, "use_thinking": False},
    )
]


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize("config", _EVERY_VARIANT)
def test_the_prompt_says_how_actions_run_and_how_indexes_hold(
    member: type[BrowserUseAgent], config: dict[str, Any]
) -> None:
    """What the prompt says about execution is what the Loop and the capability do.

    ``ReactLoop`` stops a step at its first failed action (the user's ruling
    of 2026-10-24) and at ``done`` or an action that raises; a page change
    does not stop it. An index is the element's backend-node handle
    (``dom/models.py::element_handle``), the same from one observation to the
    next.
    """
    run = _Run(member, **config)
    prompt = run.prompt()
    for claim in (
        "automatically skipped",
        "interrupted",
        "renumbered every step",
        "rebuilt every step",
        "may now name something else",
        "even when the one before it failed",
    ):
        assert claim not in prompt, (run.agent.select_prompt_variant(), claim)
    assert "not renumbered between steps" in prompt
    if run.agent.select_prompt_variant() in {"system_prompt", "system_prompt_no_thinking"}:
        assert (
            "An action that fails stops the step: the actions after it are not executed, "
            "and <agent_history> says which."
        ) in prompt
        assert "An action that changes the page does not stop the step" in prompt


def test_every_member_still_captures_the_screenshot() -> None:
    """Evaluators read screenshots out of the trace, not out of the prompt.

    ``online_mind2web`` and ``lexbench_browser`` score from them, so the
    member decides what the model sees, never what the run records.
    """
    for member in MEMBERS:
        assert "browser.screenshot" in _Run(member).obs.active_fragments


# ── The history window ────────────────────────────────────────────────


def _history(steps: int) -> HistoryObs:
    return HistoryObs(
        items=[
            StepRecord(
                step=number,
                llm_output=f"reply {number}",
                actions=[Action(name="browser.scroll", params={"direction": "down"})],
                action_results=[ActionResult(success=True)],
            )
            for number in range(steps)
        ]
    )


async def _replayed_steps(agent: BrowserUseAgent, steps: int) -> list[str]:
    observation = _observation()
    observation.history = _history(steps)
    (message,) = await agent.build_react_prompt(
        observation, context=ReactPromptContext(step=steps, phase="policy")
    )
    return re.findall(r"<(step_\d+)>", message.text)


@pytest.mark.parametrize("member", MEMBERS)
async def test_the_history_replays_the_last_ten_steps_by_default(
    member: type[BrowserUseAgent],
) -> None:
    replayed = await _replayed_steps(_Run(member).agent, 12)
    assert replayed == [f"step_{n}" for n in range(3, 13)]


async def test_the_history_window_is_a_config_field() -> None:
    """An experiment variable a YAML can set, not a constant hidden in the class."""
    raw = yaml.safe_load(_SHIPPED.read_text(encoding="utf-8"))
    raw["agent"][0]["step_history_window"] = 2
    raw.pop("hook", None)
    agent = build_composer().compose(raw).single("agent")
    assert agent.step_history_window == 2
    assert await _replayed_steps(agent, 5) == ["step_4", "step_5"]


def test_a_window_of_no_steps_is_refused_by_the_config() -> None:
    from pydantic import ValidationError

    raw = yaml.safe_load(_SHIPPED.read_text(encoding="utf-8"))
    raw["agent"][0]["step_history_window"] = 0
    with pytest.raises(ValidationError, match="step_history_window"):
        build_composer().compose(raw)


# ── Loud failures ─────────────────────────────────────────────────────


@pytest.mark.parametrize("member", MEMBERS)
def test_prepare_refuses_a_profile_without_a_dom(member: type[BrowserUseAgent]) -> None:
    """Through the real composer and ``AgentRuntime.prepare``, before any setup.

    ``vision_only`` installs no DOM resolver. Both members require ``dom``,
    so the run fails here, naming the member, instead of quietly becoming
    a vision run.
    """
    raw = yaml.safe_load(_SHIPPED.read_text(encoding="utf-8"))
    raw["agent"][0]["agent_id"] = member.__registry_key__
    raw["environment"][0]["capabilities"][0]["profile"] = "vision_only"
    raw.pop("hook", None)
    run = build_composer().compose(raw)
    runtime = AgentRuntime.for_agent(run, "main")
    with pytest.raises(IncompatibleFragmentError) as exc:
        runtime.prepare()
    message = str(exc.value)
    assert f"dom ← {member.__registry_key__}" in message
    assert exc.value.requested_by == {"dom": (member.__registry_key__,)}
    assert "Missing: ['dom']" in message
    assert "browser.screenshot" in message


def test_the_action_failure_names_the_member_too() -> None:
    """Symmetric with observation, per §2.5 — one rule, both axes."""
    # An ablated environment, reduced to the one attribute action
    # negotiation reads. The agent and the rules are the real ones.
    env = SimpleNamespace(provided_actions=frozenset({"browser.navigate", "browser.done"}))
    agent = make_agent(BrowserUseDomAgent, memory=SimpleMemory(max_messages=10))
    with pytest.raises(IncompatibleActionError) as exc:
        negotiate_action(agent, env)
    assert "click ← browser_use.dom" in str(exc.value)


@pytest.mark.parametrize(
    "item",
    [
        {"browser.click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}},
        {"browser.drag": {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4}},
    ],
)
def test_a_verb_the_member_lacks_is_refused(item: dict[str, Any]) -> None:
    """Structured and text paths alike — one per-run model validates both."""
    from pydantic import ValidationError

    from spectragent.core.loops.errors import ParseError

    run = _Run(BrowserUseDomAgent)
    with pytest.raises(ParseError):
        run.agent.parse_react_output(run.reply(item))
    with pytest.raises(ValidationError):
        run.model.model_validate({"memory": "", "action": [item]})


# ── The §2.5 seam ─────────────────────────────────────────────────────


@pytest.mark.parametrize("member", MEMBERS)
async def test_what_the_agent_emits_is_what_the_action_declares(
    member: type[BrowserUseAgent],
) -> None:
    """Negotiation matches names, not parameter contracts — so test the seam.

    For every negotiated descriptor: synthesize the minimal payload its
    own ``params_schema`` declares, put it through the agent's parser and
    the guard, and validate what arrives against the action's ``Params``.
    """
    run = _Run(member)
    await run.agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )

    routed: list[Any] = []

    async def _capture(action: Any, **_kw: Any) -> Any:
        from spectragent.schemas.action import ActionResult

        routed.append(action)
        return ActionResult(success=True)

    _kept, guard = build_guarded_act(
        _capture, run.descriptors, None, bindings=run.actions.action_bindings
    )

    for descriptor in run.descriptors:
        params = minimal_params(descriptor)
        (action,) = run.agent.parse_react_output(run.reply({descriptor.name: params}))
        await guard(action)
        emitted = routed[-1]
        assert emitted.name == descriptor.name
        assert set(emitted.params) <= set(descriptor.params), (
            f"{descriptor.name} emitted {sorted(set(emitted.params) - set(descriptor.params))}, "
            f"which its descriptor does not declare"
        )
        ActionRegistrar.get(descriptor.name).Params.model_validate(emitted.params)


# ── Provider transforms, offline ──────────────────────────────────────


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


#: Keywords OpenAI's ``"strict": true`` dialect refuses, per its own
#: "supported schemas" list. A request carrying one comes back 400 on the
#: *schema* — before the model has read a word of the prompt — so this is
#: the cheapest thing in the file and the most expensive to get wrong.
#: ``default`` is not on the list and is not checked here: under strict
#: every property is required, so a default can never be applied.
_REFUSED_BY_STRICT = frozenset(
    {
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "unevaluatedItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minProperties",
        "maxProperties",
        "patternProperties",
        "propertyNames",
        "unevaluatedProperties",
    }
)


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize("flash", [True, False])
def test_the_strict_schema_is_clean(member: type[BrowserUseAgent], flash: bool) -> None:
    schema = SchemaOptimizer.create_optimized_json_schema(
        _Run(member, flash_mode=flash).model, strict=True
    )
    assert SchemaOptimizer.is_strict_compatible(schema)
    for node in _walk(schema):
        assert "$ref" not in node and "oneOf" not in node and "discriminator" not in node
        if node.get("type") == "object":
            assert "properties" in node
    assert "$defs" not in schema


@pytest.mark.parametrize("member", MEMBERS)
def test_the_strict_schema_carries_nothing_strict_refuses(member: type[BrowserUseAgent]) -> None:
    """The closed union is what first made GPT ask for strict at all.

    While ``action`` was an open map the request shipped ``strict: false``
    and every one of these keywords went out unchallenged. Now that the
    model *is* strict-compatible, one ``Field(min_length=1)`` on the action
    list — an entirely ordinary thing to write, and the reason an empty
    action list is caught locally — would fail the first call of every run
    on the schema. The Pydantic bound stays; ``minItems`` does not reach
    the wire.
    """
    run = _Run(member)
    strict = SchemaOptimizer.create_optimized_json_schema(run.model, strict=True)
    offenders = {key for node in _walk(strict) for key in node if key in _REFUSED_BY_STRICT}
    assert offenders == set()

    # The bound is still enforced where it belongs — locally, by Pydantic.
    with pytest.raises(Exception, match="at least 1 item"):
        run.model.model_validate({"action": []})

    # And it is still *stated* for a provider that can express it: the
    # non-strict compilation is untouched, so only the dialect that
    # refuses the keyword loses it.
    loose = SchemaOptimizer.create_optimized_json_schema(run.model, strict=False)
    assert loose["properties"]["action"]["minItems"] == 1


@pytest.mark.parametrize("member", MEMBERS)
async def test_the_per_run_schema_republishes_default_null_on_purpose(
    member: type[BrowserUseAgent],
) -> None:
    """The ``default``-free guarantee is descriptor-level, and stops there.

    ``params_view`` strips ``default`` so a descriptor never states a value
    that is not what omitting the param gives you. The per-run compilation
    then puts ``default: null`` back on every optional param, because a
    strict-mode provider makes all properties required and ``null`` is the
    only way left to decline one. That is deliberate and it is safe:
    ``default`` is not on strict's refused list, under strict it can never
    be applied, and the agent strips the null before building the Action —
    so an omission and a written null reach the capability identically,
    which is what keeps a knob like ``clear_by_default`` in charge.
    """
    run = _Run(member)
    for descriptor in run.descriptors:
        # The keyword, not the word: the knob's value is *stated* in the
        # description ("optional, default=true"), which is the point — it
        # is told to the model without being a schema default.
        for node in _walk(descriptor.params_schema or {}):
            assert "default" not in node, f"{descriptor.name} publishes a default"

    strict = SchemaOptimizer.create_optimized_json_schema(run.model, strict=True)
    branches = strict["properties"]["action"]["items"]["anyOf"]
    optional_nulls = [
        (verb, name)
        for branch in branches
        for verb, node in branch["properties"].items()
        for name, field in node["properties"].items()
        if field.get("default", "unset") is None
    ]
    assert optional_nulls, "no optional param — this test would prove nothing"
    # ...and the null never reaches the capability. Rendering a step first
    # is what a real run does, and it is what gives a point verb its grid.
    await run.agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    verb, name = optional_nulls[0]
    descriptor = next(d for d in run.descriptors if d.name == verb)
    (action,) = run.agent.parse_react_output(
        run.reply({verb: {**minimal_params(descriptor), name: None}})
    )
    assert name not in action.params


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize("structured", [False, True], ids=["text", "structured"])
def test_done_with_no_answer_keeps_its_null_and_passes_the_params_check(
    member: type[BrowserUseAgent], structured: bool
) -> None:
    """``done``'s ``extracted_content`` is required and nullable: null is the answer.

    Only an optional param's null is a decline. Stripping every null turned
    this correct reply into ``extracted_content: Field required`` at the
    params check, so a task with no answer to return could not end.
    """
    run = _Run(member)
    payload = {"browser.done": {"extracted_content": None, "success": None}}
    reply: Any = (
        run.model.model_validate({"memory": "m", "action": [payload]})
        if structured
        else run.reply(payload)
    )
    (action,) = run.agent.parse_react_output(reply)
    assert action == Action(name="browser.done", params={"extracted_content": None})
    assert params_refusal(run.action_space, action) is None


@pytest.mark.parametrize("member", MEMBERS)
def test_each_action_branch_names_exactly_one_verb(member: type[BrowserUseAgent]) -> None:
    run = _Run(member)
    schema = SchemaOptimizer.create_optimized_json_schema(run.model, strict=True)
    branches = schema["properties"]["action"]["items"]["anyOf"]
    assert {next(iter(b["properties"])) for b in branches} == run.names
    assert all(len(b["required"]) == 1 for b in branches)


@pytest.mark.parametrize("member", MEMBERS)
def test_the_gemini_transform_keeps_the_union(member: type[BrowserUseAgent]) -> None:
    schema = fix_gemini_schema(SchemaOptimizer.create_gemini_optimized_schema(_Run(member).model))
    text = json.dumps(schema)
    assert "anyOf" in text
    assert "oneOf" not in text and "$ref" not in text
    assert "additionalProperties" not in text


@pytest.mark.parametrize("member", MEMBERS)
def test_a_gemini_placeholder_is_stripped_before_validation(
    member: type[BrowserUseAgent],
) -> None:
    """Gemini's own schema fix invents ``_placeholder`` for an empty object.

    A zero-parameter action is exactly that, so the model answers with the
    invented key and a closed params model refuses it. The provider that
    added it takes it away — the agent stays provider-agnostic.
    """
    from pydantic import ValidationError

    from spectragent.core.llm.providers.google import _strip_placeholders

    run = _Run(member)
    assert "browser.go_back" in run.names
    payload = {
        "memory": "m",
        "action": [{"browser.go_back": {"_placeholder": ""}}],
    }
    with pytest.raises(ValidationError):
        run.model.model_validate(payload)
    run.model.model_validate(_strip_placeholders(payload))


def test_strict_is_decided_by_the_family_and_the_payload() -> None:
    from spectragent.core.llm.providers.openai import ChatOpenAI

    model = _Run(BrowserUseDomVisionAgent).model
    assert ChatOpenAI(model="gpt-4o", api_key="t").use_strict_schema(model) is True
    # A Gemini model behind an OpenAI-compatible gateway is not a strict
    # family, so the request stays non-strict whatever the schema can do.
    assert ChatOpenAI(model="gemini-3.5-flash", api_key="t").use_strict_schema(model) is False


# ── Reasoning fields follow the template that was rendered ────────────

_OUTPUT_BLOCK = re.compile(r"<output>(.*?)</output>", re.DOTALL)
_TAUGHT_KEY = re.compile(r'"([a-z_]+)"\s*:')


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize(
    "config",
    [
        {"flash_mode": True},
        {"flash_mode": False, "use_thinking": True},
        {"flash_mode": False, "use_thinking": False},
    ],
)
def test_the_model_asks_for_exactly_what_the_template_teaches(
    member: type[BrowserUseAgent], config: dict[str, Any]
) -> None:
    """Read back out of the rendered prompt, not out of a second table.

    Under a strict dialect every property becomes required, so a flash run
    carrying the full reasoning set would be forced to emit a ``thinking``
    block its prompt never mentions.
    """
    run = _Run(member, **config)
    block = _OUTPUT_BLOCK.search(run.prompt())
    assert block is not None, run.agent.select_prompt_variant()
    taught = set(_TAUGHT_KEY.findall(block.group(1)))
    # ``<action_name>`` / ``<param>`` placeholders are not top-level keys.
    taught -= {"action_name", "param", "param1", "param2", "value", "value1", "value2"}
    assert taught == set(run.model.model_fields), run.agent.select_prompt_variant()
