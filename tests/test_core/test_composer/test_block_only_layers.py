"""A layer that lives only inside a block takes no top-level section (``docs/architecture_design.md`` §4.2.1).

A layer whose instances only a receptor inside another block consumes is
declared ``inline_only``. Its top-level section, which a run used to build and
then use for nothing, is refused with conscribe's ``inline_only_layer`` error,
which names the block the instances belong in. A build where no block can hold
such a layer leaves it out (``agent_loop``, ``memory`` and ``restorer`` in a
configuration that names no agent family), and a section of it is refused as a
layer the run does not have. The blocks themselves compose as before, and a
defaulted one left out is still its default (``test_strategy_defaults.py``).
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue, ValidationError

from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.registries import collect_registrars

Block = dict[str, JsonValue]

_BROWSER: Block = {
    "capability_id": "browser",
    "backend": {"provider_id": "local_chrome"},
    "dom_cleaner": {"cleaner_type": "browser_use"},
    "dom_serializer": {"serializer_type": "text"},
}
_ANDROID: Block = {
    "capability_id": "android",
    "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
}
_UI_TARS: Block = {
    "agent_id": "ui_tars",
    "agent_loop": {"loop_id": "react"},
    "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
    "memory": {"memory_id": "simple"},
}
_ON_BROWSER: Block = {"capability": [_BROWSER]}
_ON_ANDROID: Block = {"capability": [_ANDROID]}
_AGENT: Block = {"capability": [_BROWSER], "agent": [_UI_TARS]}

#: Every block-only layer: the sections that name the packages whose blocks
#: hold it (none for a framework axis held by a framework block) and the block
#: its refusal names.
BLOCK_ONLY: dict[str, tuple[Block, str]] = {
    "archiver": (_ON_BROWSER, "hook[trace_writer].archive"),
    "browser_provider": (_ON_BROWSER, "capability[browser].backend"),
    "mouse_trajectory": (_ON_BROWSER, "capability[browser].mouse_trajectory"),
    "typing_rhythm": (_ON_BROWSER, "capability[browser].typing_rhythm"),
    "dom_cleaner": (_ON_BROWSER, "capability[browser].dom_cleaner"),
    "dom_serializer": (_ON_BROWSER, "capability[browser].dom_serializer"),
    "replayability": (_ON_BROWSER, "capability[browser].replayability"),
    "network_source": (_ON_BROWSER, "capability[browser].network_source"),
    "miss_policy": (_ON_BROWSER, "capability[browser].network_source[archive].miss_policy"),
    "android_provider": (_ON_ANDROID, "capability[android].backend"),
    "android_task_oracle": (_ON_ANDROID, "capability[android].task_oracle"),
    "agent_loop": (_AGENT, "agent[ui_tars].agent_loop"),
    "memory": (_AGENT, "agent[ui_tars].memory"),
    "restorer": (_AGENT, "agent[ui_tars].agent_loop[branch].restorer"),
    "step_selector": ({}, "task_loader[trace_branches].step_selector"),
    "action_translator": ({}, "exporter[openai_messages].translator"),
}


def test_these_are_every_block_only_layer() -> None:
    declared = {layer for layer, registrar in collect_registrars().items() if registrar.inline_only}
    assert declared == set(BLOCK_ONLY)


@pytest.mark.parametrize("layer", sorted(BLOCK_ONLY))
def test_a_top_level_section_is_refused_naming_its_block(layer: str) -> None:
    names, block = BLOCK_ONLY[layer]
    raw: Block = {**names, layer: []}
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    (error,) = refused.value.errors()
    assert (error["type"], error["loc"]) == ("inline_only_layer", (layer,))
    assert block in error["msg"]


@pytest.mark.parametrize("layer", ["agent_loop", "memory", "restorer"])
def test_a_build_with_no_agent_leaves_the_agents_layers_out(layer: str) -> None:
    judge: Block = {"llm": [{"model_type": "openai", "model": "m", "provider": {"name": "direct"}}]}
    assert layer not in ConfigComposer.for_config(judge).registrars
    raw: Block = {**judge, layer: []}
    with pytest.raises(ComposeError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    issue = refused.value.issue
    assert issue is not None and (issue.code, issue.path) == ("no_implementation", layer)
