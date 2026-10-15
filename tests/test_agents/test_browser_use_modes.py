"""``observation_mode``: three surfaces, one agent, one prompt tree.

The mode is a configured enum, never inferred from the environment. That
choice is the whole design: deriving it ("there is no ``dom`` fragment, so
this must be a vision run") would let a mistyped capability profile
silently downgrade a DOM experiment into a vision one, and an ablation you
cannot tell apart from a typo is not an ablation.

Everything here runs against the real ``BrowserCapability`` /
``BrowserEnvironment`` / ``ReactLoop`` through real negotiation — no CDP,
no network. What is pinned:

- each mode's active fragments and actions, item for item;
- ``vision`` never renders a DOM, *even when the observation carries one*;
- the configuration errors that must be loud, and where they surface;
- the §2.5 seam: what the agent emits is what ``descriptor().params``
  declares, all the way to the guard;
- the provider transforms, offline: strict-compatible, ``$ref``-free,
  ``oneOf``-free, one required property per union branch;
- the reasoning fields, against the ``<output>`` block of the very
  template each configuration renders.
"""

from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import _MODE_DEMAND, BrowserUseAgent, _ModeDemand
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.llm.schema.gemini import fix_gemini_schema
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import (
    IncompatibleActionError,
    IncompatibleFragmentError,
    extract_wiring,
    negotiate_action,
    negotiate_observation,
)
from tests._helpers.action_params import minimal_params
from tests._helpers.assembly import make_agent, make_env

MODES = ("dom", "dom_vision", "vision")

#: §4.1's table, written out. The point of writing it rather than deriving
#: it is that a change to ``_MODE_DEMAND`` should have to be restated here
#: by whoever makes it.
_EXPECTED_ACTIONS: dict[str, set[str]] = {
    "dom": {
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
    },
    "dom_vision": {
        "browser.click",
        "browser.click_coordinate",
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
    },
    "vision": {
        "browser.click_coordinate",
        "browser.type_coordinate",
        "browser.drag",
        "browser.navigate",
        "browser.done",
        "browser.scroll",
        "browser.send_keys",
        "browser.go_back",
        "browser.go_forward",
        "browser.switch_tab",
        "browser.wait",
    },
}

_EXPECTED_FRAGMENTS: dict[str, set[str]] = {
    "dom": {"browser.dom", "browser.screenshot", "browser.browser_meta", "browser.platform"},
    "dom_vision": {"browser.dom", "browser.screenshot", "browser.browser_meta", "browser.platform"},
    # No ``browser.dom``: the DOMWatchdog is never asked, so no DOM is
    # extracted at all — not extracted and then hidden.
    "vision": {"browser.screenshot", "browser.browser_meta", "browser.platform"},
}


def _png(width: int = 1280, height: int = 720) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _observation(*, with_dom: bool = True) -> Any:
    """An observation that carries *everything*, whatever the mode asked for.

    Deliberately over-provisioned: ``vision`` mode has to refuse to render
    a DOM it was handed, not merely fail to find one.
    """
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos"),
        task=SimpleNamespace(user_request="tick the checkbox", follow_ups=[]),
        screenshot=ScreenshotObs(
            image_bytes=_png(),
            image_width=1280,
            image_height=720,
            viewport_width=1280,
            viewport_height=720,
        ),
        dom=DOMObs(element_tree=SimpleNamespace(serialized_dom="[3]<button>Submit</button>"))
        if with_dom
        else None,
        browser_meta=BrowserMetaObs(url="https://example.test", page_title="Example"),
    )


class _Run:
    """One assembled, negotiated, prepared run — no CDP, no network."""

    def __init__(self, mode: str, *, profile: str = "standard", **kwargs: Any) -> None:
        self.capability = BrowserCapability(profile=profile)
        self.env = make_env(BrowserEnvironment, {"browser": self.capability})
        self.agent = make_agent(
            BrowserUseAgent,
            memory=SimpleMemory(max_messages=10),
            observation_mode=mode,
            **kwargs,
        )
        self.loop = ReactLoop(max_actions_per_step=1)
        self.obs = negotiate_observation(self.agent, self.loop, self.env)
        self.actions = negotiate_action(self.agent, self.env)
        descriptors = self.env.get_action_space(
            active_actions=self.actions.active_actions,
            fragments=self.obs.active_fragments,
        )
        action_space, self.guard = build_guarded_act(
            self.env.act,
            descriptors,
            None,
            bindings=self.actions.action_bindings,
        )
        self.descriptors = action_space.descriptors
        self.agent.prepare(task="t", action_descriptors=self.descriptors)

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


# ── The mode table ────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_active_fragments_match_the_table(mode: str) -> None:
    run = _Run(mode)
    env_sourced = {k for k in run.obs.active_fragments if not k.startswith("loop.")}
    assert env_sourced == _EXPECTED_FRAGMENTS[mode]


@pytest.mark.parametrize("mode", MODES)
def test_active_actions_match_the_table(mode: str) -> None:
    assert _Run(mode).names == _EXPECTED_ACTIONS[mode]


def test_dom_vision_has_two_independent_click_verbs() -> None:
    run = _Run("dom_vision")
    click = next(d for d in run.descriptors if d.name == "browser.click")
    point = next(d for d in run.descriptors if d.name == "browser.click_coordinate")
    # Distinct contracts, not one overloaded verb with half-applicable params.
    assert set(click.params) == {"index"}
    assert set(point.params) == {"coordinate_x", "coordinate_y"}


def test_vision_has_exactly_one_click_verb() -> None:
    run = _Run("vision")
    assert "browser.click" not in run.names
    assert "browser.click_coordinate" in run.names


def test_vision_drops_the_dom_dependent_param_from_scroll() -> None:
    """The descriptor context §4.2 adds: a param, not the whole action."""
    scroll = next(d for d in _Run("vision").descriptors if d.name == "browser.scroll")
    assert scroll.params_schema is not None
    assert "index" not in scroll.params
    assert "index" not in scroll.params_schema["properties"]
    assert "direction" in scroll.params


def test_dom_keeps_it() -> None:
    scroll = next(d for d in _Run("dom").descriptors if d.name == "browser.scroll")
    assert "index" in scroll.params


def test_vision_excludes_the_dom_derived_text_channel() -> None:
    """``extract_content`` returns innerText / outerHTML under another name.

    Letting it in would make "vision only" mean "vision plus a text
    channel", and every ablation drawn against it wrong.
    """
    assert "browser.extract_content" not in _Run("vision").names


# ── What reaches the model ────────────────────────────────────────────


def test_dom_mode_renders_the_tree_and_shows_no_image() -> None:
    run = _Run("dom")
    messages = run.agent.build_react_prompt(_observation())
    content = messages[0].content
    assert isinstance(content, str), "dom mode must not attach an image"
    assert "[3]<button>Submit</button>" in content


def test_dom_vision_renders_the_tree_and_shows_the_image() -> None:
    run = _Run("dom_vision")
    (message,) = run.agent.build_react_prompt(_observation())
    content = message.content
    assert isinstance(content, list)
    body = getattr(content[0], "text", "")
    assert "[3]<button>Submit</button>" in body


def test_the_image_is_labelled_from_its_own_bytes() -> None:
    """A JPEG observation must not be shipped as ``data:image/png``.

    The capability can be configured to capture JPEG
    (``screenshot_format``), and the agent is handed bytes, not a format.
    OpenAI sniffs past a wrong prefix; Anthropic rejects the request.
    """
    from PIL import Image

    from spectragent.core.llm.messages import ContentPartImageParam

    def image_of(observation: Any) -> ContentPartImageParam:
        (message,) = run.agent.build_react_prompt(observation)
        content = message.content
        assert isinstance(content, list)
        return next(p for p in content if isinstance(p, ContentPartImageParam))

    run = _Run("dom_vision")
    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720)).save(buffer, format="JPEG", quality=70)
    obs = _observation()
    obs.screenshot = ScreenshotObs(
        image_bytes=buffer.getvalue(),
        image_width=1280,
        image_height=720,
        viewport_width=1280,
        viewport_height=720,
    )

    jpeg = image_of(obs)
    assert jpeg.image_url.url.startswith("data:image/jpeg;base64,")
    assert jpeg.image_url.media_type == "image/jpeg"

    # ...and the PNG default is still labelled PNG.
    png = image_of(_observation())
    assert png.image_url.url.startswith("data:image/png;base64,")
    assert png.image_url.media_type == "image/png"


def test_vision_never_renders_the_dom_fragment_even_when_handed_one() -> None:
    """The invariant the whole mode exists for, at the fragment.

    A fragment can reach the agent that negotiation did not activate — a
    hand-built observation, a replay, a second capability. "Vision only"
    has to mean it whatever arrives.

    This is the half that is decidable by inspection. The other half —
    DOM-derived *text* arriving through an action result — is held by
    negotiation instead (next test), because a string in a result carries
    no provenance the renderer could filter on.
    """
    run = _Run("vision")
    (message,) = run.agent.build_react_prompt(_observation(with_dom=True))
    content = message.content
    assert isinstance(content, list)
    body = getattr(content[0], "text", "")
    assert "[3]" not in body
    assert "DOM:" not in body
    # The text channel it *does* keep: url, title, tabs.
    assert "https://example.test" in body


def test_vision_negotiates_no_verb_whose_result_carries_dom_text() -> None:
    """The other half, and why it is negotiation's job rather than the renderer's.

    ``_render_history`` prints each prior step's actions and results in
    every mode — the model needs them to avoid repeating a step. A result
    is a plain string, so nothing there says whether it came from the
    element tree; filtering it would be guesswork. What is decidable is
    which verbs can *produce* such a string, and ``vision`` simply does
    not activate them: ``extract_content`` returns innerText/outerHTML —
    DOM-derived text under another name — and every index-addressed verb
    would have needed the selector map to run at all.
    """
    run = _Run("vision")
    assert "browser.extract_content" not in run.actions.active_actions
    # And what survives is advertised without its DOM-addressed params:
    # ``browser.scroll`` keeps scrolling, its optional ``index`` is gone
    # because ``RequiresFragment("browser.dom")`` is not satisfiable here.
    for descriptor in run.descriptors:
        assert "index" not in descriptor.params, (
            f"{descriptor.name} advertises an index in a run with no selector map"
        )


def test_every_mode_still_captures_the_screenshot() -> None:
    """Evaluators read screenshots out of the trace, not out of the prompt.

    ``online_mind2web`` and ``lexbench_browser`` score from them, so the
    mode decides what the model sees, never what the run records.
    """
    for mode in MODES:
        assert "browser.screenshot" in _Run(mode).obs.active_fragments


# ── Loud failures ─────────────────────────────────────────────────────


def test_a_dom_mode_against_a_vision_only_profile_fails_negotiation() -> None:
    with pytest.raises(IncompatibleFragmentError) as exc:
        _Run("dom", profile="vision_only")
    assert "dom" in str(exc.value)


def test_the_failure_names_the_mode_that_asked() -> None:
    """``required fragment 'dom' is missing`` is a puzzle without it.

    Two modes require ``dom`` and one excludes it, so the agent class no
    longer answers "who wanted a DOM" — only the configuration does. The
    message has to carry it, or the fix (``observation_mode`` or
    ``profile``, and they point in opposite directions) is a guess.
    """
    capability = BrowserCapability(profile="vision_only")
    env = make_env(BrowserEnvironment, {"browser": capability})
    agent = make_agent(
        BrowserUseAgent, memory=SimpleMemory(max_messages=10), observation_mode="dom"
    )
    assert repr(agent) == "browser_use(observation_mode='dom')"
    with pytest.raises(IncompatibleFragmentError) as exc:
        negotiate_observation(agent, ReactLoop(), env)

    message = str(exc.value)
    assert "dom ← browser_use(observation_mode='dom')" in message
    assert exc.value.requested_by == {"dom": ("browser_use(observation_mode='dom')",)}
    # What it is missing and what the environment has, both still there:
    # the attribution is an addition, not a replacement.
    assert "Missing: ['dom']" in message
    assert "browser.screenshot" in message


def test_the_action_failure_names_the_mode_too() -> None:
    """Symmetric with observation, per §2.5 — one rule, both axes."""
    # An ablated environment, reduced to the one attribute action
    # negotiation reads. The agent and the rules are the real ones.
    env = SimpleNamespace(provided_actions=frozenset({"browser.navigate", "browser.done"}))
    agent = make_agent(
        BrowserUseAgent, memory=SimpleMemory(max_messages=10), observation_mode="vision"
    )
    with pytest.raises(IncompatibleActionError) as exc:
        negotiate_action(agent, env)
    message = str(exc.value)
    assert "browser_use(observation_mode='vision')" in message
    # ``click_coordinate`` is required *because of the mode*: it is an
    # optional key in the class wiring that vision promotes.
    assert "click_coordinate ← browser_use(observation_mode='vision')" in message


def test_the_mode_table_may_only_name_optional_keys() -> None:
    """A typo in ``_MODE_DEMAND`` has to be an error, not a shrug.

    ``promote`` is self-policing — an undeclared promotion grows the
    required set and leaves the declared space, which ``bounded_demand``
    refuses. ``exclude`` is not: subtracting a key nothing has produces
    sets that are perfectly legal and quietly wrong, and the mode table is
    the single definition of the three surfaces.
    """
    for mode, registries in _MODE_DEMAND.items():
        for registry, demand in registries.items():
            _required, optional = extract_wiring(BrowserUseAgent, registry)
            assert (demand.promote | demand.exclude) <= optional, (
                f"_MODE_DEMAND[{mode!r}][{registry!r}] names a key that is not "
                f"optional in BrowserUseAgent.__wiring__"
            )


@pytest.mark.parametrize("stray", ["screnshot", "screenshot"])
def test_a_stray_exclude_is_refused_rather_than_ignored(stray: str) -> None:
    """One misspelling, one class-*required* key — both used to subtract nothing."""
    required, optional = extract_wiring(BrowserUseAgent, "observation")
    with pytest.raises(ValueError, match="not an optional key"):
        _ModeDemand(exclude=frozenset({stray})).apply(
            required, optional, where="_MODE_DEMAND['dom']['observation']"
        )


def test_vision_runs_fine_against_the_standard_profile() -> None:
    """Negotiation already skips DOM extraction; the profile is a guard rail."""
    assert "browser.dom" not in _Run("vision").obs.active_fragments


def test_a_fine_tuned_checkpoint_cannot_be_pointed_at_a_vision_run() -> None:
    """At construction — earlier than negotiation, before anything is built."""
    with pytest.raises(ValueError, match="fine-tuned"):
        BrowserUseAgent(observation_mode="vision", is_browser_use_model=True)


def test_use_vision_is_gone_rather_than_aliased() -> None:
    with pytest.raises(TypeError):
        BrowserUseAgent(use_vision=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "item",
    [
        {"browser.click": {"index": 1}},
        {"browser.scroll": {"direction": "down", "index": 2}},
    ],
)
def test_a_dom_action_is_refused_in_vision_mode(item: dict[str, Any]) -> None:
    """Structured and text paths alike — one per-run model validates both."""
    run = _Run("vision")
    with pytest.raises(ParseError):
        run.agent.parse_react_output(run.reply(item))
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        run.model.model_validate({"memory": "", "action": [item]})


# ── The §2.5 seam ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
async def test_what_the_agent_emits_is_what_the_action_declares(mode: str) -> None:
    """Negotiation matches names, not parameter contracts — so test the seam.

    For every negotiated descriptor: synthesize the minimal payload its
    own ``params_schema`` declares, put it through the agent's parser and
    the guard, and validate what arrives against the action's ``Params``.
    """
    run = _Run(mode)
    run.agent.build_react_prompt(_observation(with_dom=(mode != "vision")))

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


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("flash", [True, False])
def test_the_strict_schema_is_clean(mode: str, flash: bool) -> None:
    schema = SchemaOptimizer.create_optimized_json_schema(
        _Run(mode, flash_mode=flash).model, strict=True
    )
    assert SchemaOptimizer.is_strict_compatible(schema)
    for node in _walk(schema):
        assert "$ref" not in node and "oneOf" not in node and "discriminator" not in node
        if node.get("type") == "object":
            assert "properties" in node
    assert "$defs" not in schema


@pytest.mark.parametrize("mode", MODES)
def test_the_strict_schema_carries_nothing_strict_refuses(mode: str) -> None:
    """The closed union is what first made GPT ask for strict at all.

    While ``action`` was an open map the request shipped ``strict: false``
    and every one of these keywords went out unchallenged. Now that the
    model *is* strict-compatible, one ``Field(min_length=1)`` on the action
    list — an entirely ordinary thing to write, and the reason an empty
    action list is caught locally — would fail the first call of every run
    on the schema. The Pydantic bound stays; ``minItems`` does not reach
    the wire.
    """
    run = _Run(mode)
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


@pytest.mark.parametrize("mode", MODES)
def test_the_per_run_schema_republishes_default_null_on_purpose(mode: str) -> None:
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
    run = _Run(mode)
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
    run.agent.build_react_prompt(_observation(with_dom=(mode != "vision")))
    verb, name = optional_nulls[0]
    descriptor = next(d for d in run.descriptors if d.name == verb)
    item = run.model.model_validate(
        {"memory": "m", "action": [{verb: {**minimal_params(descriptor), name: None}}]}
    ).action[0]
    assert name not in run.agent._action_for(item).params


@pytest.mark.parametrize("mode", MODES)
def test_each_action_branch_names_exactly_one_verb(mode: str) -> None:
    run = _Run(mode)
    schema = SchemaOptimizer.create_optimized_json_schema(run.model, strict=True)
    branches = schema["properties"]["action"]["items"]["anyOf"]
    assert {next(iter(b["properties"])) for b in branches} == run.names
    assert all(len(b["required"]) == 1 for b in branches)


@pytest.mark.parametrize("mode", MODES)
def test_the_gemini_transform_keeps_the_union(mode: str) -> None:
    schema = fix_gemini_schema(SchemaOptimizer.create_gemini_optimized_schema(_Run(mode).model))
    text = json.dumps(schema)
    assert "anyOf" in text
    assert "oneOf" not in text and "$ref" not in text
    assert "additionalProperties" not in text


@pytest.mark.parametrize("mode", MODES)
def test_a_gemini_placeholder_is_stripped_before_validation(mode: str) -> None:
    """Gemini's own schema fix invents ``_placeholder`` for an empty object.

    A zero-parameter action is exactly that, so the model answers with the
    invented key and a closed params model refuses it. The provider that
    added it takes it away — the agent stays provider-agnostic.
    """
    from spectragent.core.llm.providers.google import _strip_placeholders

    run = _Run(mode)
    assert "browser.go_back" in run.names
    payload = {
        "memory": "m",
        "action": [{"browser.go_back": {"_placeholder": ""}}],
    }
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        run.model.model_validate(payload)
    run.model.model_validate(_strip_placeholders(payload))


def test_strict_is_decided_by_the_family_and_the_payload() -> None:
    from spectragent.core.llm.providers.openai import ChatOpenAI

    model = _Run("vision").model
    assert ChatOpenAI(model="gpt-4o", api_key="t")._use_strict_schema(model) is True
    # A Gemini model behind an OpenAI-compatible gateway is not a strict
    # family, so the request stays non-strict whatever the schema can do.
    assert ChatOpenAI(model="gemini-3.5-flash", api_key="t")._use_strict_schema(model) is False


# ── Reasoning fields follow the template that was rendered ────────────

_OUTPUT_BLOCK = re.compile(r"<output>(.*?)</output>", re.DOTALL)
_TAUGHT_KEY = re.compile(r'"([a-z_]+)"\s*:')


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "config",
    [
        {"flash_mode": True},
        {"flash_mode": False, "use_thinking": True},
        {"flash_mode": False, "use_thinking": False},
    ],
)
def test_the_model_asks_for_exactly_what_the_template_teaches(
    mode: str, config: dict[str, Any]
) -> None:
    """Read back out of the rendered prompt, not out of a second table.

    Under a strict dialect every property becomes required, so a flash run
    carrying the full reasoning set would be forced to emit a ``thinking``
    block its prompt never mentions.
    """
    run = _Run(mode, **config)
    block = _OUTPUT_BLOCK.search(run.prompt())
    assert block is not None, run.agent.select_prompt_variant()
    taught = set(_TAUGHT_KEY.findall(block.group(1)))
    # ``<action_name>`` / ``<param>`` placeholders are not top-level keys.
    taught -= {"action_name", "param", "param1", "param2", "value", "value1", "value2"}
    assert taught == set(run.model.model_fields), run.agent.select_prompt_variant()
