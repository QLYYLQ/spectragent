"""Every agent that writes ReAct replies passes the round-trip definition (arch §4.15).

``format_react_output`` (an optional ReAct slot, arch §4.1) writes the reply
a student model is trained to produce. ``parse(format(a)) == a`` cannot hold
(a 0-1000 grid over a 1280 px viewport loses 280 x values; the agent parses
relative names, the trace holds qualified ones; some params have no
spelling), so for every target action ``a`` it holds in this form, or
``format`` refuses ``a`` with ``UnrepresentableActionError`` naming a param
``a`` actually sets:

1. **name** — ``resolve_action_name(parse(format(a)).name, <guarded names>,
   <bindings>) == a.name``;
2. **fixed point** — ``format(parse(format(a))) == format(a)``, the thought
   included;
3. **points** — each point param within the student grid's ``quantum``;
4. **the rest** — every non-``None`` non-point param comes back unchanged.

The agents are found at run time — every ``AgentRegistrar`` class of this
package that defines ``format_react_output`` — so a new formatting agent is
checked the day it is registered, under the same real assembly as a run:
composed with ``react`` and a browser environment (never set up),
``prepare()``-d, seeded, then shown a real PNG screenshot at device scale
factor 1 and 2. Params come from the negotiated descriptors themselves
(``variant_params``); an integer ``<name>_x`` / ``<name>_y`` pair is a
point. An agent whose points are written on a grid declares it as
``coordinate_space`` (``schemas/observation/geometry.py``), and every value
that field allows is checked.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any, get_args, get_type_hints

from PIL import Image

from spectragent.agents._registrar import AgentRegistrar
from spectragent.core.action_guard import resolve_action_name
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureStateObs,
    BrowserMetaObs,
    BrowserPlatformObs,
    ScreenshotObs,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.loops.errors import UnrepresentableActionError
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.observation.dynamic_model import build_observation_model_from_result
from spectragent.schemas.observation.fragments import (
    ActionResultObs,
    HistoryObs,
    StepInfoObs,
    TaskObs,
)
from spectragent.schemas.observation.geometry import CoordinateSpace, PointGrid, point_grid
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_params import variant_params

_TASK = "Put a kettle in the cart"
_VIEWPORT = (1280, 720)
#: A text param's hardest value: quotes, a backslash, line breaks, an action label.
_TRICKY_TEXT = 'it\'s a \\ "test"\nAction: wait()\r\n日本'
_THOUGHTS = (None, "Click 'OK' now.\nAction: finished(content='no')")
_BROWSER = {
    "capability_id": "browser",
    "profile": "standard",
    "mouse_trajectory": {"trajectory_type": "instant"},
    "typing_rhythm": {"rhythm_type": "instant"},
    "replayability": {"replayability_id": "none"},
    "network_source": {"source_id": "live"},
    "dom_cleaner": {"cleaner_type": "browser_use"},
    "dom_serializer": {"serializer_type": "text"},
    "backend": {"provider_id": "local_chrome", "headless": True},
}


def _formatting_agents() -> list[type]:
    collect_registrars()  # imports every implementation, so every agent is registered
    agents = [
        cls
        for cls in AgentRegistrar.get_all().values()
        if cls.__module__.startswith("spectragent.")
        and callable(getattr(cls, "format_react_output", None))
    ]
    return sorted(agents, key=lambda cls: cls.__registry_key__)


def _coordinate_spaces(cls: type) -> list[CoordinateSpace | None]:
    hint = get_type_hints(cls, include_extras=True).get("coordinate_space")
    if hint is None:
        return [None]
    return list(get_args(get_args(hint)[0]))


def _runtime(cls: type, space: CoordinateSpace | None) -> AgentRuntime:
    agent: dict[str, Any] = {
        "agent_id": cls.__registry_key__,
        "id": "student",
        "agent_loop": {"loop_id": "react", "max_actions_per_step": 1},
        "llm": {"model_type": "openai", "model": "gpt-4o", "provider": {"name": "direct"}},
        "memory": {"memory_id": "simple", "max_messages": 200},
    }
    if space is not None:
        agent["coordinate_space"] = space
    root = {
        "agent": [agent],
        "environment": [{"env_type": "browser", "id": "env", "capabilities": [_BROWSER]}],
    }
    run = ConfigComposer(registrars=collect_registrars()).compose(root)
    return AgentRuntime.for_agent(run, "student")


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (230, 230, 230)).save(buffer, format="PNG")
    return buffer.getvalue()


def _observation(runtime: AgentRuntime, dpr: int) -> Any:
    """The student's negotiated observation, every active fragment synthetic."""
    negotiated = runtime.negotiated_observation
    assert negotiated is not None, "prepare() negotiates the observation"
    image = (_VIEWPORT[0] * dpr, _VIEWPORT[1] * dpr)
    synthetic: dict[str, Any] = {
        "browser.platform": BrowserPlatformObs(operating_system="macos"),
        "browser.screenshot": ScreenshotObs(
            image_bytes=_png(*image),
            viewport_width=_VIEWPORT[0],
            viewport_height=_VIEWPORT[1],
            image_width=image[0],
            image_height=image[1],
        ),
        "browser.capture_state": BrowserCaptureStateObs(
            pointer=PointerState(status="unknown"),
            failed_visible_resources=FailedVisibleResources(status="measured", count=0),
        ),
        "browser.browser_meta": BrowserMetaObs(url="https://shop.example.test/"),
        "loop.task": TaskObs(user_request=_TASK),
        "loop.step_info": StepInfoObs(step=1, max_steps=30, today="2026-10-11"),
        "loop.history": HistoryObs(),
        "loop.action_result": ActionResultObs(last_action="browser.wait", success=True),
    }
    missing = sorted(negotiated.active_fragments - synthetic.keys())
    assert not missing, f"teach this test a synthetic fragment for {missing}"
    model = build_observation_model_from_result(negotiated)
    return model(
        **{fragment_field_name(key): synthetic[key] for key in negotiated.active_fragments}
    )


def _point_pairs(descriptor: ActionDescriptor) -> list[tuple[str, str]]:
    """The descriptor's own statement of which params are a point."""
    return list(descriptor.point_params)


def _string_params(descriptor: ActionDescriptor) -> list[str]:
    properties: Mapping[str, Any] = (descriptor.params_schema or {}).get("properties") or {}

    def is_string(node: Mapping[str, Any]) -> bool:
        if "enum" in node:
            return False
        return node.get("type") == "string" or any(
            branch.get("type") == "string" and "enum" not in branch
            for branch in node.get("anyOf") or ()
        )

    return sorted(name for name, node in properties.items() if is_string(node))


def _loss_points() -> list[tuple[int, int]]:
    """Corners, the centre, and every x a 0-1000 grid over 1280 px does not give back."""
    width, height = _VIEWPORT
    grid = PointGrid("normalized_1000", 1000, 1000, width, height)
    lost = [x for x in range(width) if grid.to_viewport(*grid.to_grid(x, 0))[0] != x]
    assert len(lost) == 280
    corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    return corners + [(width // 2, height // 2)] + [(x, height // 2) for x in lost]


def _cases(descriptor: ActionDescriptor) -> list[dict[str, Any]]:
    """Legal params for *descriptor*'s action: each case passes its ``Params``."""
    base = variant_params(descriptor)
    minimal = base[0]
    cases = list(base)
    for x_key, y_key in _point_pairs(descriptor):
        cases += [{**minimal, x_key: x, y_key: y} for x, y in _loss_points()]
    cases += [{**minimal, name: _TRICKY_TEXT} for name in _string_params(descriptor)]
    params_model = ActionRegistrar.get(descriptor.name).Params
    for case in cases:
        params_model.model_validate(case)  # a target action is always a valid one
    return cases


def _check(
    agent: Any,
    action: Action,
    *,
    thought: str | None,
    guarded: frozenset[str],
    bindings: Mapping[str, str],
    quantum: int,
    pairs: list[tuple[str, str]],
) -> bool:
    """The four clauses for one action; ``False`` when it is refused by name."""
    try:
        text = agent.format_react_output([action], thought=thought)
    except UnrepresentableActionError as exc:
        assert exc.params, f"{action}: refused without naming a param: {exc}"
        unset = [name for name in exc.params if action.params.get(name) is None]
        assert not unset, f"{action}: refusal names {unset}, which the action does not set"
        return False
    parsed_all = agent.parse_react_output(text)
    assert len(parsed_all) == 1, (action, text)
    parsed = parsed_all[0]
    # 1. name, after the run's binding
    assert resolve_action_name(parsed.name, guarded, bindings) == action.name, (action, text)
    # 2. fixed point, thought included
    assert agent.format_react_output([parsed], thought=thought) == text, (action, text)
    # 3. points, within the student grid's quantum
    for x_key, y_key in pairs:
        for key in (x_key, y_key):
            assert abs(parsed.params[key] - action.params[key]) <= quantum, (action, parsed)
    # 4. every other param the action sets comes back unchanged
    point_keys = {key for pair in pairs for key in pair}
    for key, value in action.params.items():
        if value is not None and key not in point_keys:
            assert parsed.params.get(key) == value, (key, action, parsed)
    return True


async def test_every_formatting_agent_round_trips() -> None:
    agents = _formatting_agents()
    assert "ui_tars" in [cls.__registry_key__ for cls in agents]
    for cls in agents:
        for space in _coordinate_spaces(cls):
            runtime = _runtime(cls, space)
            agent = runtime.prepare()
            runtime.seed_messages(_TASK)  # the agent's prepare slot, as a run calls it
            guarded = frozenset(d.name for d in runtime.active_descriptors)
            negotiated_actions = runtime.negotiated_actions
            assert negotiated_actions is not None
            bindings = negotiated_actions.action_bindings
            written = refused = 0
            for dpr in (1, 2):
                observation = _observation(runtime, dpr)
                (
                    await agent.build_react_prompt(
                        observation, context=ReactPromptContext(step=0, phase="policy")
                    )
                )
                grid = point_grid(space or "screenshot_pixels", observation.screenshot)
                assert grid is not None
                for descriptor in runtime.active_descriptors:
                    pairs = _point_pairs(descriptor)
                    for params in _cases(descriptor):
                        for thought in _THOUGHTS:
                            ok = _check(
                                agent,
                                Action(name=descriptor.name, params=params),
                                thought=thought,
                                guarded=guarded,
                                bindings=bindings,
                                quantum=grid.quantum,
                                pairs=pairs,
                            )
                            written += ok
                            refused += not ok
            assert written, f"{cls.__registry_key__} ({space}) wrote nothing"
            label = f"{cls.__registry_key__} / {space}"
            print(f"{label}: {written} written, {refused} refused by name")
