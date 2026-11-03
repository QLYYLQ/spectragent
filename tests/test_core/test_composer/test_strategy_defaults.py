"""A strategy block left out of YAML is its owner's Python default (``docs/architecture_design.md`` §4.2.1).

A receptor that holds exactly one strategy, whose owner installs a default
strategy when built by hand, and whose default is off, neutral or the only
option, names that strategy as its wiring's ``default``. A block that leaves
the receptor out composes that strategy, and the resolved configuration
records it as if the block had been written. Pinned against the partial build
every run composes with (``ConfigComposer.for_config``):

- left out or written as the default, a block gives the same resolved config,
  recorded and full; the full dump is exactly what ``config_hash`` hashes
  (``AgentRuntime`` hands ``top_level_cfg`` to ``TraceWriter.on_run_start``,
  which digests ``redact_config(cfg, record_secrets=True)``), and
  ``config explain`` shows the default;
- the strategy composed for a left-out block is the one the owner holds when
  built by hand;
- the table below is every such default the catalog declares, and the
  strategies that choose behaviour stay required;
- a written block is still checked: an unknown strategy, a misspelled key, a
  stray knob, ``null``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from conscribe.introspect import wiring_slots
from pydantic import JsonValue, ValidationError

import spectragent.cli.configuration as configuration
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.replay.archive_source import ArchiveNetworkSource
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.configuration_catalog import ConfigurationCatalog
from spectragent.core.loops.branch import BranchLoop
from spectragent.core.trace.redaction import redact_config

#: A configuration, or one block of it.
Block = dict[str, JsonValue]
#: The receptor blocks written into an owner's block.
Written = Mapping[str, JsonValue]

#: Every strategy receptor a block may leave out, by owner, with the block that
#: leaving it out stands for: the owner's Python default.
OMITTABLE: dict[tuple[str, str], dict[str, Block]] = {
    ("capability", "browser"): {
        "mouse_trajectory": {"trajectory_type": "instant"},
        "typing_rhythm": {"rhythm_type": "instant"},
        "replayability": {"replayability_id": "none"},
        "network_source": {"source_id": "live"},
    },
    ("network_source", "archive"): {"miss_policy": {"miss_policy_id": "strict"}},
    ("capability", "android"): {"ui_tree_source": {"source_id": "accessibility_service"}},
    ("agent_loop", "branch"): {"restorer": {"restorer_id": "prefix_replay"}},
}

#: Strategy receptors that stay required because a default would choose
#: behaviour: what the agent sees, how many branch runs a sweep makes, what a
#: student is trained on (each owner has a Python default nonetheless), how a
#: touch and text reach a phone and what an agent sees of its UI tree
#: (Android's owner has none).
REQUIRED: dict[tuple[str, str], tuple[str, ...]] = {
    ("capability", "browser"): ("dom_cleaner", "dom_serializer"),
    ("capability", "android"): (
        "tap_injector",
        "typing_backend",
        "ui_tree_cleaner",
        "ui_tree_serializer",
    ),
    ("task_loader", "trace_branches"): ("step_selector",),
    ("exporter", "openai_messages"): ("translator",),
}


def _browser(written: Written) -> Block:
    return {
        "capability": [
            {
                "capability_id": "browser",
                "backend": {"provider_id": "local_chrome"},
                "dom_cleaner": {"cleaner_type": "browser_use"},
                "dom_serializer": {"serializer_type": "text"},
                **written,
            }
        ]
    }


def _archive(written: Written) -> Block:
    return _browser({"network_source": {"source_id": "archive", **written}})


def _android(written: Written) -> Block:
    block: Block = {
        "capability_id": "android",
        "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
        "service_apk": "spectragent-device.apk",
        "tap_injector": {"injector_id": "shell_injection"},
        "typing_backend": {"typing_id": "set_text"},
        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
        "ui_tree_serializer": {"serializer_id": "m3a"},
    }
    return {"capability": [{**block, **written}]}


def _branch(written: Written) -> Block:
    """A UI-TARS agent on a ``branch`` loop: the loop is written only in an agent block."""
    agent: Block = {
        "agent_id": "ui_tars",
        "agent_loop": {"loop_id": "branch", **written},
        "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
        "memory": {"memory_id": "simple"},
    }
    return {**_browser({}), "agent": [agent]}


@dataclass(frozen=True)
class _Owner:
    """One owner of the table: its YAML, the composed owner and the owner built by hand."""

    #: The configuration holding the owner, with the given receptor blocks written.
    config: Callable[[Written], Block]
    composed: Callable[[ComposedRun], object]
    built: Callable[[], object]


OWNERS: dict[tuple[str, str], _Owner] = {
    ("capability", "browser"): _Owner(
        _browser, lambda run: run.single("capability"), BrowserCapability
    ),
    ("network_source", "archive"): _Owner(
        _archive, lambda run: run.single("capability").network_source, ArchiveNetworkSource
    ),
    ("capability", "android"): _Owner(
        _android,
        lambda run: run.single("capability"),
        lambda: AndroidCapability(service_apk="spectragent-device.apk"),
    ),
    ("agent_loop", "branch"): _Owner(
        _branch, lambda run: run.single("agent").agent_loop, BranchLoop
    ),
}

CASES = [(owner, receptor) for owner, receptors in OMITTABLE.items() for receptor in receptors]


@pytest.fixture(scope="module")
def catalog() -> ConfigurationCatalog:
    """The whole catalog, every package imported."""
    return ConfigComposer.for_every_package().catalog


@pytest.mark.parametrize(
    ("owner", "receptor"), CASES, ids=[f"{layer}.{key}.{name}" for (layer, key), name in CASES]
)
def test_a_block_left_out_is_recorded_as_its_default(owner: tuple[str, str], receptor: str) -> None:
    shape = OWNERS[owner]
    left_out, written = shape.config({}), shape.config({receptor: OMITTABLE[owner][receptor]})
    composer = ConfigComposer.for_config(left_out)
    runs = [composer.compose(raw) for raw in (left_out, written)]
    assert redact_config(runs[0].top_level_cfg) == redact_config(runs[1].top_level_cfg)
    assert redact_config(runs[0].top_level_cfg, record_secrets=True) == redact_config(
        runs[1].top_level_cfg, record_secrets=True
    )
    # The strategy composed for the left-out block is the one a hand-built owner holds.
    held = getattr(shape.composed(runs[0]), receptor)
    assert type(held) is type(getattr(shape.built(), receptor))


def test_the_table_is_every_strategy_default_the_catalog_declares(
    catalog: ConfigurationCatalog,
) -> None:
    declared: dict[tuple[str, str], dict[str, str]] = {}
    for layer, entries in catalog.components.items():
        for key, definition in entries.items():
            defaults = {
                name: entry.default_key
                for name, entry in wiring_slots(definition.implementation).items()
                if entry.default_key is not None
            }
            if defaults:
                declared[(layer, key)] = defaults
    tabled = {
        owner: {name: next(iter(block.values())) for name, block in receptors.items()}
        for owner, receptors in OMITTABLE.items()
    }
    assert declared == tabled


def test_a_strategy_that_chooses_behaviour_stays_required(catalog: ConfigurationCatalog) -> None:
    optional = [
        f"{layer}/{key}.{name}"
        for (layer, key), names in REQUIRED.items()
        for name in names
        if not catalog.components[layer][key].config_model.model_fields[name].is_required()
    ]
    assert optional == []


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (_browser({"replayability": {"replayability_id": "nonee"}}), "union_tag_invalid"),
        (_browser({"replayability": {"replayabilityid": "none"}}), "union_tag_not_found"),
        (_browser({"replayability": {"replayability_id": "none", "seed": 3}}), "extra_forbidden"),
        (_browser({"replayabilty": {"replayability_id": "none"}}), "extra_forbidden"),
        (_browser({"replayability": None}), "model_attributes_type"),
        (_archive({"miss_policy": {"miss_policy_id": "lenient"}}), "literal_error"),
        (_archive({"miss_policy": {"miss_policyid": "strict"}}), "extra_forbidden"),
        (_archive({"miss_policy": None}), "model_type"),
        (_android({"ui_tree_source": {"source_id": "uiautomator"}}), "literal_error"),
        (_android({"ui_tree_source": None}), "model_type"),
    ],
    ids=[
        "unknown-strategy",
        "misspelled-discriminator",
        "stray-knob",
        "misspelled-receptor",
        "null",
        "one-strategy-unknown-strategy",
        "one-strategy-misspelled-discriminator",
        "one-strategy-null",
        "android-one-strategy-unknown-strategy",
        "android-one-strategy-null",
    ],
)
def test_a_written_block_is_still_checked(raw: Block, error: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ConfigComposer.for_config(raw).validate(raw)
    assert error in {item["type"] for item in caught.value.errors()}


def test_explain_shows_the_default_of_a_block_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The clean-interpreter fingerprint probe answers another question.
    monkeypatch.setattr(configuration, "source_fingerprints", lambda: {})
    explained = []
    for name, raw in (
        ("left_out", _browser({})),
        ("written", _browser(OMITTABLE[("capability", "browser")])),
    ):
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        explained.append(configuration.explain_config(path))
    left_out, written = explained
    assert left_out["resolved"]["capability"][0]["replayability"] == {"replayability_id": "none"}
    assert left_out["resolved"] == written["resolved"]
    assert left_out["resolved_config_sha256"] == written["resolved_config_sha256"]
    inputs = {entry["path"]: entry["source"] for entry in left_out["provenance"]["inputs"]}
    assert inputs["capability[0].replayability"] == "default"
