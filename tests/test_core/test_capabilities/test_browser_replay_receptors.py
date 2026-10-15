"""The two replay receptors: ``network_source`` and its ``miss_policy`` (§4.14 "回放").

Registered keys, Protocol conformance, the generated three-level union
(capability → ``network_source`` → ``miss_policy``, with a typed ``matcher``),
what validation refuses, the Python defaults, and that the Composer injects
the *configured* instances — knobs and all — rather than a name.
"""

from __future__ import annotations

import copy
import typing
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.replay import (
    ArchiveNetworkSource,
    LiveNetworkSource,
    MissPolicyBase,
    MissPolicyProtocol,
    MissPolicyRegistrar,
    NetworkSourceProtocol,
    NetworkSourceRegistrar,
    ReplayMiss,
    StrictMissPolicy,
    default_miss_policy,
    default_network_source,
)
from spectragent.core.capabilities.browser.replay.archive_index import RecordedArchive
from spectragent.core.capabilities.browser.replay.archive_source import MAX_FULFILL_BODY_BYTES
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import collect_registrars
from tests._helpers.trace_builder import new_wal

_CAPABILITY: dict[str, Any] = {
    "capability_id": "browser",
    "mouse_trajectory": {"trajectory_type": "instant"},
    "typing_rhythm": {"rhythm_type": "instant"},
    "replayability": {"replayability_id": "none"},
    "network_source": {"source_id": "live"},
    "dom_cleaner": {"cleaner_type": "browser_use"},
    "dom_serializer": {"serializer_type": "text"},
    "backend": {"provider_id": "local_chrome"},
}

_ARCHIVE: dict[str, Any] = {
    "source_id": "archive",
    "isolation": "none_acknowledged",
    "shift_dates": "all",
    "matcher": {"levels": ["exact"], "fuzzy_threshold": 0.25, "site_rules": None},
    "max_fulfill_body_bytes": 1000,
    "fulfill_timeout_s": 2.5,
    "log_repeat_limit": 7,
    "log_mode": "summary",
    "miss_policy": {"miss_policy_id": "strict", "extra_benign_hosts": ["stats.test"]},
}


def _capability_config(network_source: Any) -> Any:
    from spectragent.generated._composed import BrowserCapabilityConfig

    block = {**copy.deepcopy(_CAPABILITY), "network_source": network_source}
    block.pop("capability_id")
    return BrowserCapabilityConfig.model_validate(block)


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(registrars=collect_registrars())


def _compose(composer: ConfigComposer, network_source: Any) -> BrowserCapability:
    raw = {"capability": [{**copy.deepcopy(_CAPABILITY), "network_source": network_source}]}
    capability = composer.compose(raw).single("capability")
    assert isinstance(capability, BrowserCapability)
    return capability


class TestRegistries:
    def test_the_keys(self) -> None:
        assert sorted(NetworkSourceRegistrar.keys()) == ["archive", "live"]
        assert NetworkSourceRegistrar.get("live") is LiveNetworkSource
        assert NetworkSourceRegistrar.get("archive") is ArchiveNetworkSource
        assert sorted(MissPolicyRegistrar.keys()) == ["strict"]
        assert MissPolicyRegistrar.get("strict") is StrictMissPolicy

    def test_every_strategy_satisfies_its_protocol(self) -> None:
        for source in (LiveNetworkSource(), ArchiveNetworkSource()):
            assert isinstance(source, NetworkSourceProtocol)
        assert isinstance(StrictMissPolicy(), MissPolicyProtocol)

    def test_both_registrars_are_composer_layers_leaves_first(self) -> None:
        layers = list(collect_registrars())
        assert layers.index("miss_policy") < layers.index("network_source")
        assert layers.index("replayability") < layers.index("miss_policy")
        assert layers.index("network_source") < layers.index("capability")


class TestTheStrategies:
    def test_live_intercepts_nothing_and_opens_nothing(self) -> None:
        live = LiveNetworkSource()
        assert (live.source_id, live.intercepts, live.isolation) == ("live", False, None)
        with pytest.raises(NotImplementedError, match="'live' intercepts nothing"):
            live.open_replay(None)  # type: ignore[arg-type]
        with pytest.raises(NotImplementedError, match="'live'"):
            live.serve(None, None)  # type: ignore[arg-type]

    def test_archive_intercepts_and_isolates_by_default(self) -> None:
        archive = ArchiveNetworkSource()
        assert (archive.source_id, archive.intercepts, archive.isolation) == (
            "archive",
            True,
            "required",
        )
        assert archive.shift_dates == "none"
        assert archive.matcher == MatcherConfig()
        assert archive.matcher.levels == ("exact", "normalized")
        assert (archive.max_fulfill_body_bytes, archive.fulfill_timeout_s) == (64 * 2**20, 10.0)
        assert archive.log_repeat_limit == 3
        assert archive.log_mode == "full"
        assert isinstance(archive.miss_policy, StrictMissPolicy)

    def test_isolation_is_the_knob_itself(self) -> None:
        assert ArchiveNetworkSource(isolation="none_acknowledged").isolation == "none_acknowledged"

    def test_matcher_arrives_as_a_dict_or_as_the_config(self) -> None:
        as_dict = ArchiveNetworkSource(matcher={"levels": ["exact"], "fuzzy_threshold": 0.3})  # type: ignore[arg-type]
        as_config = ArchiveNetworkSource(
            matcher=MatcherConfig(levels=("exact",), fuzzy_threshold=0.3)
        )
        assert as_dict.matcher == as_config.matcher
        assert isinstance(as_dict.matcher, MatcherConfig)

    @pytest.mark.parametrize(
        "knobs",
        [
            {"max_fulfill_body_bytes": MAX_FULFILL_BODY_BYTES + 1},
            {"max_fulfill_body_bytes": 0},
            {"fulfill_timeout_s": 0},
            {"log_repeat_limit": 0},
            {"log_mode": "automatic"},
            {"isolation": "off"},
            {"shift_dates": "cookies"},
        ],
    )
    def test_a_hand_built_source_is_checked_too(self, knobs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="archive:"):
            ArchiveNetworkSource(**knobs)

    def test_the_fulfil_cap_stays_under_one_cdp_message(self) -> None:
        """base64 (4/3) of the largest body plus an envelope fits in 100 MiB (measured, §4.14)."""
        assert MAX_FULFILL_BODY_BYTES * 4 / 3 + 1024 * 1024 < 100 * 2**20
        # The schema's bound is spelled out in the annotation (the stub cannot
        # see module constants); it must be the same number.
        from spectragent.generated._composed import ArchiveNetwork_SourceConfig

        field = ArchiveNetwork_SourceConfig.model_fields["max_fulfill_body_bytes"]
        assert [m.le for m in field.metadata if hasattr(m, "le")] == [MAX_FULFILL_BODY_BYTES]

    def test_the_policy_base_has_no_answer_of_its_own(self) -> None:
        miss = ReplayMiss(
            method="GET",
            url="http://a.test/x",
            resource_type="XHR",
            is_document=False,
            frame=None,
            content_type=None,
            reason="no_match",
        )
        with pytest.raises(NotImplementedError, match="'strict' must decide"):
            MissPolicyBase.decide(StrictMissPolicy(), miss)

    def test_the_defaults_come_from_the_factories(self) -> None:
        assert isinstance(default_network_source(), LiveNetworkSource)
        assert isinstance(default_miss_policy(), StrictMissPolicy)
        assert isinstance(BrowserCapability().network_source, LiveNetworkSource)

    def test_open_replay_keys_with_the_configured_site_rules(self, tmp_path: Path) -> None:
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "shop.test.yaml").write_text(
            "rules:\n  - id: shop\n    scope: {host: shop.test}\n    exhaustion: repeat_last\n    ignore_query: [session]\n"
        )
        wal = new_wal(tmp_path / "runs", "run-src")
        from spectragent.core.archive.source import open_archive

        recorded = ArchiveNetworkSource(matcher=MatcherConfig(site_rules=str(rules))).open_replay(
            open_archive(wal.run_dir)
        )
        assert isinstance(recorded, RecordedArchive)
        assert recorded.run_id == "run-src"
        assert recorded.rules.require_for_request("GET", "http://shop.test/").id == "shop"
        assert recorded.rules.sha256 is not None


class TestTheGeneratedConfig:
    def test_capability_network_source_miss_policy_nest(self) -> None:
        from spectragent.generated import _composed as generated

        capability = generated.BrowserCapabilityConfig.model_fields["network_source"]
        arms = set(get_args(capability.annotation))
        assert arms == {generated.ArchiveNetwork_SourceConfig, generated.LiveNetwork_SourceConfig}
        assert capability.is_required()
        archive = generated.ArchiveNetwork_SourceConfig.model_fields
        assert archive["miss_policy"].annotation is generated.StrictMiss_PolicyConfig
        assert archive["miss_policy"].is_required()
        # Typed, not degraded to Any / dict: the codegen imports the knob's type.
        assert archive["matcher"].annotation is MatcherConfig
        assert archive["matcher"].annotation is not typing.Any
        assert set(archive) >= {
            "matcher",
            "isolation",
            "shift_dates",
            "max_fulfill_body_bytes",
            "fulfill_timeout_s",
            "log_repeat_limit",
            "log_mode",
        }
        assert set(generated.LiveNetwork_SourceConfig.model_fields) == {"source_id"}
        assert "extra_benign_hosts" in generated.StrictMiss_PolicyConfig.model_fields

    def test_an_archive_block_validates_with_every_knob(self) -> None:
        cfg = _capability_config(_ARCHIVE)
        source = cfg.network_source
        assert (source.source_id, source.isolation, source.shift_dates) == (
            "archive",
            "none_acknowledged",
            "all",
        )
        assert source.matcher == MatcherConfig(levels=("exact",), fuzzy_threshold=0.25)
        assert source.miss_policy.extra_benign_hosts == ["stats.test"]

    @pytest.mark.parametrize(
        ("network_source", "complaint"),
        [
            ({"source_id": "arhcive"}, "source_id"),
            ({**_ARCHIVE, "miss_policy": {"miss_policy_id": "strickt"}}, "miss_policy"),
            ({k: v for k, v in _ARCHIVE.items() if k != "miss_policy"}, "miss_policy"),
            ({**_ARCHIVE, "matcher": {"levelz": ["exact"]}}, "levelz"),
            ({**_ARCHIVE, "matcher": {"levels": ["fuzzzy"]}}, "levels"),
            ({**_ARCHIVE, "matcher": {"levels": []}}, "levels"),
            ({**_ARCHIVE, "matcher": {"levels": ["exact", "exact"]}}, "levels"),
            ({**_ARCHIVE, "matcher": {"fuzzy_threshold": 1.5}}, "fuzzy_threshold"),
            ({**_ARCHIVE, "isolation": False}, "isolation"),
            ({**_ARCHIVE, "shift_dates": "cookies"}, "shift_dates"),
            ({**_ARCHIVE, "max_fulfill_body_bytes": MAX_FULFILL_BODY_BYTES + 1}, "max_fulfill"),
            ({**_ARCHIVE, "log_repeat_limit": 0}, "log_repeat_limit"),
            ({**_ARCHIVE, "log_mode": "automatic"}, "log_mode"),
            ({"source_id": "live", "isolation": "required"}, "isolation"),
        ],
    )
    def test_what_validation_refuses(self, network_source: Any, complaint: str) -> None:
        with pytest.raises(ValidationError, match=complaint):
            _capability_config(network_source)

    def test_the_block_is_required(self) -> None:
        from spectragent.generated._composed import BrowserCapabilityConfig

        block = {
            k: v for k, v in _CAPABILITY.items() if k not in ("capability_id", "network_source")
        }
        with pytest.raises(ValidationError, match="network_source"):
            BrowserCapabilityConfig.model_validate(block)


class TestTheComposerInjectsInstances:
    def test_live(self, composer: ConfigComposer) -> None:
        capability = _compose(composer, {"source_id": "live"})
        assert isinstance(capability.network_source, LiveNetworkSource)

    def test_archive_with_every_knob_and_its_policy(self, composer: ConfigComposer) -> None:
        capability = _compose(composer, _ARCHIVE)
        source = capability.network_source
        assert isinstance(source, ArchiveNetworkSource)
        assert (source.isolation, source.shift_dates) == ("none_acknowledged", "all")
        assert (
            source.max_fulfill_body_bytes,
            source.fulfill_timeout_s,
            source.log_repeat_limit,
        ) == (
            1000,
            2.5,
            7,
        )
        assert isinstance(source.matcher, MatcherConfig)
        assert source.matcher == MatcherConfig(levels=("exact",), fuzzy_threshold=0.25)
        policy = source.miss_policy
        assert isinstance(policy, StrictMissPolicy)
        assert policy.extra_benign_hosts == ["stats.test"]
        # The configured policy, not the default the constructor started with.
        assert policy is not ArchiveNetworkSource().miss_policy
