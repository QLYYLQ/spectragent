"""A layer that lives only inside a block takes no top-level section (``docs/architecture_design.md`` §4.2.1).

A layer whose instances only a receptor inside another block consumes is
declared ``inline_only``. Its top-level section, which a run used to build and
then use for nothing, is refused with conscribe's ``inline_only_layer`` error,
which names the block the instances belong in. The blocks themselves compose
as before, and a defaulted one left out is still its default
(``test_strategy_defaults.py``).
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue, ValidationError

from spectragent.core.composer import ConfigComposer
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

#: Every block-only layer: the capability block that names its package (none
#: for a framework axis) and the block its refusal names.
BLOCK_ONLY: dict[str, tuple[Block | None, str]] = {
    "archiver": (_BROWSER, "hook[trace_writer].archive"),
    "browser_provider": (_BROWSER, "capability[browser].backend"),
    "mouse_trajectory": (_BROWSER, "capability[browser].mouse_trajectory"),
    "typing_rhythm": (_BROWSER, "capability[browser].typing_rhythm"),
    "dom_cleaner": (_BROWSER, "capability[browser].dom_cleaner"),
    "dom_serializer": (_BROWSER, "capability[browser].dom_serializer"),
    "replayability": (_BROWSER, "capability[browser].replayability"),
    "network_source": (_BROWSER, "capability[browser].network_source"),
    "miss_policy": (_BROWSER, "capability[browser].network_source[archive].miss_policy"),
    "android_provider": (_ANDROID, "capability[android].backend"),
    "android_task_oracle": (_ANDROID, "capability[android].task_oracle"),
    "step_selector": (None, "task_loader[trace_branches].step_selector"),
    "restorer": (None, "agent_loop[branch].restorer"),
    "action_translator": (None, "exporter[openai_messages].translator"),
    "decision_model": (None, "replay_reviewer[jev].decision_model"),
}


def test_these_are_every_block_only_layer() -> None:
    declared = {layer for layer, registrar in collect_registrars().items() if registrar.inline_only}
    assert declared == set(BLOCK_ONLY)


@pytest.mark.parametrize("layer", sorted(BLOCK_ONLY))
def test_a_top_level_section_is_refused_naming_its_block(layer: str) -> None:
    capability, block = BLOCK_ONLY[layer]
    raw: Block = {layer: []}
    if capability is not None:
        raw["capability"] = [capability]
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    (error,) = refused.value.errors()
    assert (error["type"], error["loc"]) == ("inline_only_layer", (layer,))
    assert block in error["msg"]
