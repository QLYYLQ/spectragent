"""The run configuration and its benchmark each write part of a capability's configuration, merged.

``docs/architecture_design.md`` §4.10.1 "capability 配置合并", §4.2 "基准层在
``prepare()`` 里加的检查". Through ``ConfigComposer`` on the real Android
capability and the ``androidworld`` benchmark, whose part is its block's
``capability`` list (the AVD) and, computed in code, its ``open_app`` name
table read from beside its task lists:

- a field one side writes is taken from it, a strategy block both write is
  merged inside, and the merged block is what the run validates, constructs
  and records;
- a field two sides write differently is refused naming the field and both
  sides, two strategies for one receptor at the discriminator;
- a list (``actions``) is the run configuration's or, left out, every action:
  a part sets options on an entry there and never adds or removes one;
- a part that breaks the capability's schema is refused where it writes it,
  and one for a capability the run has no block of is refused;
- a required setting neither side writes is refused naming it, with or
  without a benchmark;
- a root that starts no environment (an export's) merges nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from spectragent.benchmarks.androidworld.open_app import OPEN_APP_FILE, OPEN_APP_FORMAT
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
from spectragent.benchmarks.androidworld.tasks import instance_seed
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.redaction import redact_config

Block = dict[str, JsonValue]

#: The table AndroidWorld's task list writer reads off its checkout, in short.
TABLE: dict[str, JsonValue] = {
    "clock": "com.google.android.deskclock/com.android.deskclock.DeskClock",
    "markor": "net.gsantner.markor/net.gsantner.markor.activity.MainActivity",
}


def _android(**written: JsonValue) -> Block:
    """A complete Android capability block, but for what *written* leaves out (``None`` drops a field)."""
    block: Block = {
        "capability_id": "android",
        "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
        "service_apk": "spectragent-device.apk",
        "tap_injector": {"injector_id": "shell_injection"},
        "typing_backend": {"typing_id": "set_text"},
        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
        "ui_tree_serializer": {"serializer_id": "m3a"},
    }
    block.update(written)
    return {key: value for key, value in block.items() if value is not None}


def _table(data: Path, apps: Mapping[str, JsonValue] = TABLE, commit: str = PINNED_COMMIT) -> Path:
    data.mkdir(parents=True, exist_ok=True)
    table = {"format": OPEN_APP_FORMAT, "commit": commit, "apps": dict(apps)}
    (data / OPEN_APP_FILE).write_text(json.dumps(table))
    return data


def _run(
    capability: Block, data: Path | None, *parts: Block, environment: bool = True
) -> dict[str, JsonValue]:
    """A run root: the Android *capability* in an environment, and the androidworld benchmark."""
    root: dict[str, JsonValue] = (
        {"environment": [{"env_type": "android", "capabilities": [capability]}]}
        if environment
        else {}
    )
    if data is not None:
        benchmark: Block = {
            "benchmark_id": "androidworld",
            "task_loader": {"benchmark_name": "androidworld", "data_dir": str(data)},
            "setup": {"setup_id": "androidworld"},
        }
        if parts:
            benchmark["capability"] = list(parts)
        root["benchmark"] = [benchmark]
    return root


def _merged(raw: dict[str, JsonValue]) -> dict[str, object]:
    """The run's Android block, as validated after the merge."""
    config = ConfigComposer.for_config(raw).validate(raw)
    dumped = config.model_dump(mode="json", exclude_unset=True)
    (environment,) = dumped["environment"]
    (capability,) = environment["capabilities"]
    return capability


def _refused(raw: dict[str, JsonValue]) -> ComposeError:
    with pytest.raises(ComposeError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    return refused.value


# ── what is merged ───────────────────────────────────────────────────


def test_a_field_only_the_benchmark_writes_is_taken_from_it(tmp_path: Path) -> None:
    raw = _run(
        _android(backend=None),
        _table(tmp_path),
        {"capability_id": "android", "backend": {"provider_id": "local_emulator", "avd": "AW"}},
    )

    assert _merged(raw)["backend"] == {"provider_id": "local_emulator", "avd": "AW"}


def test_a_strategy_both_write_is_merged_inside(tmp_path: Path) -> None:
    raw = _run(
        _android(backend={"provider_id": "local_emulator", "boot_timeout_s": 300}),
        _table(tmp_path),
        {"capability_id": "android", "backend": {"avd": "AW"}},
    )

    backend = _merged(raw)["backend"]
    assert backend == {"provider_id": "local_emulator", "avd": "AW", "boot_timeout_s": 300}


def test_the_same_value_on_both_sides_is_no_conflict(tmp_path: Path) -> None:
    raw = _run(
        _android(),
        _table(tmp_path),
        {
            "capability_id": "android",
            "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
        },
    )

    assert _merged(raw)["backend"] == {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"}


def test_the_computed_table_sets_open_app_and_every_action_stays(tmp_path: Path) -> None:
    merged = _merged(_run(_android(), _table(tmp_path)))

    actions = merged["actions"]
    assert isinstance(actions, list)
    assert [action["action_id"] for action in actions] == list(
        AndroidCapability.__wiring__["actions"][1]
    ), "left out by the run configuration: the whole default list"
    (open_app,) = [action for action in actions if action["action_id"] == "android.open_app"]
    assert open_app == {"action_id": "android.open_app", "apps": TABLE}


def test_the_table_sets_open_app_in_the_run_configurations_own_list(tmp_path: Path) -> None:
    listed = [
        {"action_id": "android.click_coordinate"},
        {"action_id": "android.open_app"},
        {"action_id": "android.scroll", "duration_ms": 300},
    ]
    merged = _merged(_run(_android(actions=listed), _table(tmp_path)))

    assert merged["actions"] == [
        {"action_id": "android.click_coordinate"},
        {"action_id": "android.open_app", "apps": TABLE},
        {"action_id": "android.scroll", "duration_ms": 300},
    ]


def test_the_merged_block_is_what_the_run_builds_and_records(tmp_path: Path) -> None:
    raw = _run(
        _android(backend=None),
        _table(tmp_path / "data"),
        {"capability_id": "android", "backend": {"provider_id": "local_emulator", "avd": "AW"}},
    )
    # The loader reads its tasks when it is built.
    (tmp_path / "data" / "data_info.json").write_text(json.dumps({"split": {"All": "All.json"}}))
    task = {
        "commit": PINNED_COMMIT,
        "family": "android",
        "task": "SystemBrightnessMax",
        "index": 0,
        "suite_seed": 42,
        "params": {"max_or_min": "max", "seed": instance_seed(42, "SystemBrightnessMax", 0)},
        "goal": "Turn brightness to the max value.",
        "goal_at_initialize": "unchanged",
        "complexity": 1.0,
        "start_on_home_screen": True,
    }
    (tmp_path / "data" / "All.json").write_text(
        json.dumps({"format": "androidworld-tasks-2", "tasks": [task]})
    )
    run = ConfigComposer.for_config(raw).compose(raw)

    capability = run.single("capability")
    assert isinstance(capability, AndroidCapability)
    assert capability.backend.avd == "AW"  # type: ignore[attr-defined]
    open_app = next(a for a in capability.actions if a.action_id == "android.open_app")
    assert open_app.apps == TABLE  # type: ignore[attr-defined]
    assert run.top_level_cfg is not None
    resolved = run.top_level_cfg.model_dump(mode="json")
    (environment,) = resolved["environment"]
    assert environment["capabilities"][0]["backend"]["avd"] == "AW"
    assert resolved["benchmark"][0]["capability"][0]["backend"]["avd"] == "AW", "its part stays"


# ── what is refused ──────────────────────────────────────────────────


def test_a_field_two_sides_write_differently_is_refused_naming_both(tmp_path: Path) -> None:
    raw = _run(
        _android(),
        _table(tmp_path),
        {"capability_id": "android", "backend": {"provider_id": "local_emulator", "avd": "AW"}},
    )

    refused = _refused(raw)

    assert refused.issue is not None and refused.issue.code == "capability_setting_conflict"
    assert refused.issue.path == "environment[0].capabilities[0].backend.avd"
    assert str(refused).startswith(
        "environment[0].capabilities[0].backend.avd: the run configuration "
        "(environment[0].capabilities[0]) writes 'AndroidWorldAvd' and the benchmark "
        "androidworld (benchmark[0].capability[0]) writes 'AW'; a run takes each setting from one "
        "side only"
    )


def test_two_strategies_for_one_receptor_are_refused_at_the_discriminator(tmp_path: Path) -> None:
    raw = _run(
        _android(),
        _table(tmp_path),
        {"capability_id": "android", "tap_injector": {"injector_id": "accessibility_gesture"}},
    )

    refused = _refused(raw)

    assert refused.issue is not None
    assert refused.issue.path == "environment[0].capabilities[0].tap_injector.injector_id"
    assert "writes 'shell_injection' and the benchmark androidworld" in str(refused)


def test_an_app_table_in_the_run_configuration_is_refused_naming_where_the_tables_differ(
    tmp_path: Path,
) -> None:
    """A free-form map is one value; its conflict names the keys that differ, never the whole map."""
    table: dict[str, JsonValue] = {f"app {n}": f"com.example.app{n}" for n in range(6)}
    apps: dict[str, JsonValue] = {"Clock": "com.google.android.deskclock", "app 0": "com.other"}
    listed: list[JsonValue] = [{"action_id": "android.open_app", "apps": apps}]
    raw = _run(_android(actions=listed), _table(tmp_path, table))

    refused = _refused(raw)

    assert refused.issue is not None and refused.issue.code == "capability_setting_conflict"
    assert refused.issue.path == "environment[0].capabilities[0].actions[android.open_app].apps"
    assert str(refused) == (
        "environment[0].capabilities[0].actions[android.open_app].apps: the run configuration "
        "(environment[0].capabilities[0]) writes 2 entries and the benchmark androidworld, "
        "computed in code (benchmark[0]) writes 6 entries, which differ (only the first has "
        "'Clock'; only the second has 'app 1', 'app 2', 'app 3' and 2 more; both have 'app 0' "
        "with different values); a run takes each setting from one side only: leave it out of "
        "one of them, or write the same value in both."
    )


def test_a_list_left_without_the_entry_the_benchmark_sets_is_refused(tmp_path: Path) -> None:
    raw = _run(_android(actions=[{"action_id": "android.click_coordinate"}]), _table(tmp_path))

    refused = _refused(raw)

    assert refused.issue is not None and refused.issue.code == "capability_setting_conflict"
    assert refused.issue.path == "environment[0].capabilities[0].actions[android.open_app]"
    assert "sets options on 'android.open_app', and the run configuration's actions" in str(refused)
    assert "never adds or removes one" in str(refused)


def test_a_part_that_breaks_the_schema_is_refused_where_it_is_written(tmp_path: Path) -> None:
    raw = _run(
        _android(),
        _table(tmp_path),
        {"capability_id": "android", "settle_quiet_ms": -1, "speed": 2},
    )

    refused = _refused(raw)

    assert refused.issue is not None and refused.issue.code == "capability_contribution_invalid"
    assert {line.split(":")[0].strip() for line in str(refused).splitlines()[1:]} == {
        "benchmark[0].capability[0].settle_quiet_ms",
        "benchmark[0].capability[0].speed",
    }


def test_a_part_for_a_capability_the_run_has_no_block_of_is_refused(tmp_path: Path) -> None:
    raw: dict[str, JsonValue] = {
        "capability": [{"capability_id": "browser", "backend": {"provider_id": "local_chrome"}}],
        "benchmark": [
            {
                "benchmark_id": "androidworld",
                "task_loader": {
                    "benchmark_name": "androidworld",
                    "data_dir": str(_table(tmp_path)),
                },
                "setup": {"setup_id": "androidworld"},
            }
        ],
    }

    refused = _refused(raw)

    assert refused.issue is not None and refused.issue.code == "capability_without_block"
    assert refused.issue.path == "benchmark[0]"
    assert "has no 'android' capability block to merge it into" in str(refused)


def test_a_required_setting_neither_side_writes_is_named(tmp_path: Path) -> None:
    refused = _refused(_run(_android(backend=None, service_apk=None), _table(tmp_path)))

    assert refused.issue is not None and refused.issue.code == "capability_setting_missing"
    message = "required, and neither the run configuration nor a benchmark writes it"
    assert f"environment[0].capabilities[0].backend: {message}" in str(refused)
    assert f"environment[0].capabilities[0].service_apk: {message}" in str(refused)


def test_without_a_benchmark_the_run_configuration_must_be_complete() -> None:
    refused = _refused(_run(_android(service_apk=None), None))

    assert refused.issue is not None and refused.issue.code == "capability_setting_missing"
    assert str(refused) == (
        "environment[0].capabilities[0].service_apk: required, and neither the run "
        "configuration nor a benchmark writes it."
    )


def test_a_missing_table_is_refused_as_missing_data(tmp_path: Path) -> None:
    raw = _run(_android(), tmp_path / "empty")

    with pytest.raises(BenchmarkDataError, match="has no AndroidWorld open_app table"):
        ConfigComposer.for_config(raw).validate(raw)


def test_a_table_from_another_commit_is_refused(tmp_path: Path) -> None:
    raw = _run(_android(), _table(tmp_path, commit="0" * 40))

    with pytest.raises(BenchmarkDataError, match="was read from AndroidWorld 0000"):
        ConfigComposer.for_config(raw).validate(raw)


def test_a_root_that_starts_no_environment_merges_nothing(tmp_path: Path) -> None:
    """An export names the benchmark for its data: nothing to merge, and no table is read."""
    raw = _run(
        _android(),
        tmp_path / "no-table-here",
        {"capability_id": "android", "backend": {"avd": "AW"}},
        environment=False,
    )

    config = ConfigComposer.for_config(raw).validate(raw)

    assert config.model_dump(mode="json")["benchmark"][0]["capability"] == [
        {"capability_id": "android", "backend": {"avd": "AW"}}
    ]


def test_the_branch_sweeps_task_source_takes_no_capability_part() -> None:
    raw: dict[str, JsonValue] = {
        "benchmark": [
            {
                "benchmark_id": "trace_branches",
                "task_loader": {
                    "benchmark_name": "trace_branches",
                    "step_selector": {"selector_id": "all"},
                },
                "capability": [{"capability_id": "browser"}],
            }
        ]
    }

    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    assert [(error["type"], error["loc"][-1]) for error in refused.value.errors()] == [
        ("extra_forbidden", "capability")
    ]


def test_a_list_the_run_writes_in_a_top_level_section_is_refused_against_a_part(
    tmp_path: Path,
) -> None:
    """The block takes the top-level ``action:`` pool when it leaves ``actions`` out: that pool,
    not the default list, is the run's, and a part may not widen it or override its options."""
    pool: list[JsonValue] = [
        {"action_id": "android.click_coordinate"},
        {"action_id": "android.open_app", "apps": {"Clock": "com.google.android.deskclock"}},
    ]
    alone = {**_run(_android(), None), "action": pool}
    capability = ConfigComposer.for_config(alone).compose(alone).single("capability")
    assert isinstance(capability, AndroidCapability)
    assert [action.action_id for action in capability.actions] == [
        "android.click_coordinate",
        "android.open_app",
    ], "without a benchmark the run has the two actions of its pool"

    refused = _refused({**_run(_android(), _table(tmp_path)), "action": pool})

    assert refused.issue is not None and refused.issue.code == "capability_setting_conflict"
    assert refused.issue.path == "environment[0].capabilities[0].actions"
    assert "takes the run configuration's top-level 'action' section" in str(refused)
    assert "write the actions in the capability block" in str(refused)


def _browser_run(part_matcher: JsonValue) -> dict[str, JsonValue]:
    """A browser run whose block and benchmark both write the archive source's matcher settings."""
    browser: Block = {
        "capability_id": "browser",
        "backend": {"provider_id": "local_chrome"},
        "dom_cleaner": {"cleaner_type": "browser_use"},
        "dom_serializer": {"serializer_type": "text"},
        "network_source": {"source_id": "archive", "matcher": {"fuzzy_threshold": 0.7}},
    }
    part: Block = {
        "capability_id": "browser",
        "network_source": {"source_id": "archive", "matcher": part_matcher},
    }
    benchmark: Block = {
        "benchmark_id": "online_mind2web",
        "task_loader": {"benchmark_name": "online_mind2web"},
        "capability": [part],
    }
    return {
        "environment": [{"env_type": "browser", "capabilities": [browser]}],
        "benchmark": [benchmark],
    }


def test_a_block_of_settings_both_write_is_merged_field_by_field() -> None:
    """``matcher`` is typed as a settings block: each side's fields are taken, as for a strategy."""
    raw = _browser_run({"levels": ["exact"]})

    dumped = ConfigComposer.for_config(raw).validate(raw).model_dump(mode="json")

    matcher = dumped["environment"][0]["capabilities"][0]["network_source"]["matcher"]
    assert (matcher["levels"], matcher["fuzzy_threshold"]) == (["exact"], 0.7)


def test_a_setting_inside_such_a_block_two_sides_write_differently_is_refused_at_its_field() -> (
    None
):
    refused = _refused(_browser_run({"fuzzy_threshold": 0.4}))

    assert refused.issue is not None and refused.issue.code == "capability_setting_conflict"
    assert refused.issue.path == (
        "environment[0].capabilities[0].network_source.matcher.fuzzy_threshold"
    )


def _digest(raw: dict[str, JsonValue]) -> str:
    """The run's ``config_hash`` over its resolved configuration, as the trace hook computes it."""
    config = ConfigComposer.for_config(raw).validate(raw)
    resolved = redact_config(config, record_secrets=True)
    return TraceConfig(agent_id="a", loop_id="react", resolved=resolved).configuration_digest(
        resolved=resolved
    )


def test_where_a_setting_is_written_never_moves_the_config_hash(tmp_path: Path) -> None:
    """The resolved config records the benchmark's part and the merged block; the hash reads one."""
    data = _table(tmp_path)
    avd: Block = {"provider_id": "local_emulator", "avd": "AW"}
    in_the_run = _run(_android(backend=avd), data)
    in_the_benchmark = _run(
        _android(backend=None), data, {"capability_id": "android", "backend": avd}
    )
    elsewhere = _run(_android(backend={**avd, "avd": "Other"}), data)

    assert _digest(in_the_run) == _digest(in_the_benchmark)
    assert _digest(elsewhere) != _digest(in_the_run), "the merged blocks are still hashed"
