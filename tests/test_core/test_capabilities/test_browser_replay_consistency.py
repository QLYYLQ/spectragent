"""Source run ↔ this replay's configuration (§4.14 "一致性检查与回放指纹").

The source fingerprints here are the real ``BrowserFingerprint`` documents
(measured against a scripted recorder from a real ``ReplayabilityPlan``), so
the check reads exactly what a recording writes. Each refusal names both
sides; the tolerated mismatches come back as warnings.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spectragent.core.capabilities.browser.archive import NetworkArchiver, StorageArchiver
from spectragent.core.capabilities.browser.archive.fingerprint import BrowserFingerprint
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.backends.chrome_switches import (
    NETWORK_ISOLATION_ARGS,
)
from spectragent.core.capabilities.browser.client_hints import ClientHints
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay import NoneReplayability, PinnedReplayability
from spectragent.core.capabilities.browser.replay.archive_index import (
    ArchiveIndex,
    RecordedArchive,
)
from spectragent.core.capabilities.browser.replay.consistency import (
    browser_default_problems,
    client_hint_problems,
    consistency_notes,
    consistency_problems,
    replay_t0,
    version_warnings,
)
from tests._helpers.archive_recorder import ScriptedRecorder
from tests._helpers.fake_cdp import FAKE_CLIENT_HINTS

T0 = 1_760_000_000_000
BASE_FLAGS = ["--headless=new", "--user-data-dir=<user-data-dir>"]
FIXTURE_MAP = "--host-resolver-rules=MAP shop.test 127.0.0.1:9000, MAP * ~NOTFOUND"
VERSION = {"product": "Chrome/155.0", "revision": "@abc", "userAgent": "ScriptedChrome/1.0"}
#: What the recording's browser fell back to (measured right after its launch).
DEFAULTS = BrowserDefaults(timezone="UTC", locale="zh-CN")
#: The recording browser's own client hints (Chrome 155), and a Chrome identifier they match.
HINTS = ClientHints.from_page(FAKE_CLIENT_HINTS)
IDENTIFIER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)


class _Version(ScriptedRecorder):
    async def browser_version(self) -> dict[str, Any]:
        return dict(VERSION)


def _source(
    tier: Any = None,
    *,
    flags: list[str] | None = None,
    viewport: tuple[int, int] = (1280, 720),
    network: dict[str, Any] | None = None,
    network_files: int = 1,
    archivers: tuple[Any, ...] = (NetworkArchiver(require_replayable=False),),
    defaults: BrowserDefaults | None = DEFAULTS,
    hints: ClientHints | None = None,
    edit: Any = None,
) -> RecordedArchive:
    """A source run's archive (in memory) carrying a real fingerprint document."""
    tier = tier if tier is not None else PinnedReplayability(random_seed=7)
    plan = tier.plan(
        t0_ms=T0, browser_user_agent=VERSION["userAgent"], client_hints=hints, color_scheme="dark"
    )
    document = asyncio.run(
        BrowserFingerprint().measure(
            recorder=_Version(),
            tabs=[RecorderTarget(session_id="s", target_id="t", type="page")],
            plan_fingerprint=plan.fingerprint(),
            viewport=viewport,
            session_metadata={"launch_flags": flags if flags is not None else [*BASE_FLAGS]},
            archivers=archivers,
            fresh_profile=True,
            network_fingerprint=network,
            browser_defaults=defaults,
            input_platform="macos",
        )
    )
    if edit is not None:
        edit(document)
    return RecordedArchive(
        run_id="run-src",
        fingerprint=document,
        fingerprint_sha256="f" * 64,
        t0_ms=plan.t0_ms,
        index=ArchiveIndex([]),
        network_files=network_files,
    )


def _problems(recorded: RecordedArchive, tier: Any = None, **kwargs: Any) -> list[str]:
    options: dict[str, Any] = {
        "replayability": tier if tier is not None else PinnedReplayability(random_seed=7),
        "viewport": (1280, 720),
        "planned_launch_flags": [*BASE_FLAGS, *NETWORK_ISOLATION_ARGS],
    }
    options.update(kwargs)
    return consistency_problems(recorded, **options)


def _names_both_sides(problem: str) -> bool:
    return "source run 'run-src'" in problem and "↔" in problem


# ── what is refused ───────────────────────────────────────────────────


def test_a_matching_source_has_no_problem() -> None:
    assert _problems(_source()) == []
    assert _problems(_source(NoneReplayability()), NoneReplayability()) == []


def test_a_source_without_a_fingerprint_is_refused() -> None:
    recorded = _source()
    recorded.fingerprint = None
    (problem,) = _problems(recorded)
    assert _names_both_sides(problem) and "no environment fingerprint" in problem


def test_a_source_without_network_evidence_is_refused() -> None:
    (problem,) = _problems(_source(network_files=0))
    assert _names_both_sides(problem) and "browser.network" in problem
    (bound,) = _problems(_source(archivers=(StorageArchiver(),)))
    assert "recorded no browser.network" in bound


def test_a_source_that_is_itself_a_replay_is_refused() -> None:
    replayed = {"source_id": "archive", "source_run_id": "run-original"}
    (problem,) = _problems(_source(network=replayed))
    assert _names_both_sides(problem) and "itself a replay" in problem
    assert "run-original" in problem


def test_another_tier_is_refused() -> None:
    (problem,) = _problems(_source(NoneReplayability()))
    assert _names_both_sides(problem) and "'none'" in problem and "'pinned'" in problem


def test_tier_params_must_match_except_base_time() -> None:
    (problem,) = _problems(_source(), PinnedReplayability(random_seed=8))
    assert _names_both_sides(problem) and "random_seed=7" in problem and "random_seed=8" in problem
    from_archive = PinnedReplayability(random_seed=7, base_time="from_archive")
    assert _problems(_source(), from_archive) == [], "base_time is what a replay changes"


def test_another_shim_is_refused() -> None:
    def _old_shim(document: dict[str, Any]) -> None:
        document["replayability"]["shim_sha256"] = "0" * 64

    (problem,) = _problems(_source(edit=_old_shim))
    assert _names_both_sides(problem) and "shim_sha256" in problem


def test_another_viewport_is_refused() -> None:
    (problem,) = _problems(_source(), viewport=(800, 600))
    assert _names_both_sides(problem) and "1280x720" in problem and "800x600" in problem


def test_launch_flags_compare_without_routing_switches() -> None:
    """The recording's fixture MAP and the replay's isolation never count; anything else does."""
    recorded = _source(flags=[*BASE_FLAGS, FIXTURE_MAP, "--no-proxy-server"])
    assert _problems(recorded) == []
    (problem,) = _problems(recorded, planned_launch_flags=[*BASE_FLAGS, "--lang=fr"])
    assert _names_both_sides(problem) and "launch_flags_sha256" in problem


def test_from_archive_needs_a_recorded_t0() -> None:
    from_archive = PinnedReplayability(random_seed=7, base_time="from_archive")
    recorded = _source()
    recorded.t0_ms = None
    (problem,) = _problems(recorded, from_archive)
    assert _names_both_sides(problem) and "T0" in problem and "from_archive" in problem


def test_t0_comes_from_the_source() -> None:
    recorded = _source()
    from_archive = PinnedReplayability(random_seed=7, base_time="from_archive")
    assert replay_t0(recorded, from_archive, now_ms=5) == T0
    assert replay_t0(recorded, PinnedReplayability(random_seed=7), now_ms=5) == 5
    recorded.t0_ms = None
    with pytest.raises(ValueError, match="run-src"):
        replay_t0(recorded, from_archive, now_ms=5)


def test_everything_is_reported_at_once() -> None:
    problems = _problems(
        _source(network_files=0), PinnedReplayability(random_seed=9), viewport=(1, 1)
    )
    assert len(problems) == 3 and all(_names_both_sides(p) for p in problems)


# ── the browser's defaults: what a frame no override reaches falls back to ──


def test_the_recorded_defaults_are_in_the_fingerprint() -> None:
    document = _source().fingerprint
    assert document is not None
    assert document["browser_defaults"] == {"timezone": "UTC", "locale": "zh-CN"}


def test_defaults_read_before_launch_must_be_the_recordings() -> None:
    """Strict, with no switch: another default locale or time zone refuses at ``prepare()``."""
    recorded = _source()
    assert _problems(recorded, browser_defaults=DEFAULTS) == []
    (problem,) = _problems(recorded, browser_defaults=BrowserDefaults("UTC", "en-US"))
    assert _names_both_sides(problem)
    assert "locale='zh-CN'" in problem and "locale='en-US'" in problem
    assert "read from this host before launch" in problem
    (zone,) = _problems(recorded, browser_defaults=BrowserDefaults("Asia/Tokyo", "zh-CN"))
    assert "timezone='UTC'" in zone and "timezone='Asia/Tokyo'" in zone


def test_a_default_the_os_cannot_say_is_compared_after_launch() -> None:
    """The locale off macOS: unknown before launch, decided by Chrome's own answer after it."""
    recorded = _source()
    assert _problems(recorded, browser_defaults=BrowserDefaults("UTC", None)) == []
    assert browser_default_problems(recorded, DEFAULTS) == []
    (problem,) = browser_default_problems(recorded, BrowserDefaults("UTC", "fr-FR"))
    assert _names_both_sides(problem) and "measured after launch" in problem
    assert "locale='zh-CN'" in problem and "locale='fr-FR'" in problem
    # A value Chrome did not report is a difference too.
    (missing,) = browser_default_problems(recorded, BrowserDefaults("UTC", None))
    assert "locale=None" in missing


def test_a_source_that_recorded_no_defaults_is_refused() -> None:
    (problem,) = _problems(_source(defaults=None), browser_defaults=DEFAULTS)
    assert _names_both_sides(problem) and "recorded no browser defaults" in problem
    assert _problems(_source(defaults=None)) == [problem], "refused whatever this host says"
    assert browser_default_problems(_source(defaults=None), DEFAULTS) == []


# ── the client hints behind the user agent ────────────────────────────


def test_a_pinned_source_recorded_before_the_client_hints_is_refused() -> None:
    def _old(document: dict[str, Any]) -> None:
        del document["replayability"]["client_hints"]

    (problem,) = _problems(_source(edit=_old))
    assert _names_both_sides(problem) and "recorded no client hints" in problem
    assert _problems(_source(NoneReplayability(), edit=_old), NoneReplayability()) == []


def test_a_pinned_identifier_s_hints_are_compared_after_launch() -> None:
    tier = PinnedReplayability(random_seed=7, user_agent=IDENTIFIER)
    recorded = _source(tier, hints=HINTS)
    document = recorded.fingerprint
    assert document is not None
    assert document["replayability"]["client_hints"] == HINTS.as_metadata()
    assert document["page"]["userAgentData"] == HINTS.low_entropy()
    assert _problems(recorded, tier) == []
    same = tier.plan(t0_ms=T0, browser_user_agent="B", client_hints=HINTS)
    assert client_hint_problems(recorded, same) == []
    chromium = ClientHints.from_page(
        {
            **FAKE_CLIENT_HINTS,
            "brands": [b for b in FAKE_CLIENT_HINTS["brands"] if "Google" not in b["brand"]],
        }
    )
    other = tier.plan(t0_ms=T0, browser_user_agent="B", client_hints=chromium)
    (problem,) = client_hint_problems(recorded, other)
    assert _names_both_sides(problem)
    assert '"Google Chrome";v="155"' in problem.split("↔")[0]
    assert '"Google Chrome"' not in problem.split("↔")[1]


def test_the_browser_s_own_identifier_s_hints_only_warn() -> None:
    tier = PinnedReplayability(random_seed=7)
    recorded = _source(tier, hints=HINTS)
    newer = ClientHints.from_page(
        {**FAKE_CLIENT_HINTS, "brands": [{"brand": "Chromium", "version": "156"}]}
    )
    plan = tier.plan(t0_ms=T0, browser_user_agent="B", client_hints=newer)
    assert client_hint_problems(recorded, plan) == []
    warnings = version_warnings(
        recorded, browser_version=VERSION, replayability=tier, client_hints=newer
    )
    assert [w.split(":")[0] for w in warnings] == [
        "browser client hints differ (the tier does not pin a user agent)"
    ]
    assert (
        version_warnings(recorded, browser_version=VERSION, replayability=tier, client_hints=HINTS)
        == []
    )


# ── what is only noted or warned ──────────────────────────────────────


def test_a_side_without_a_digest_is_skipped_and_noted() -> None:
    def _no_digest(document: dict[str, Any]) -> None:
        document["launch_flags_sha256"] = None

    recorded = _source(edit=_no_digest)
    assert _problems(recorded, planned_launch_flags=["--anything"]) == []
    assert consistency_notes(recorded, planned_launch_flags=["--x"]) == [
        "launch flags not compared: source run 'run-src' recorded no launch-flag digest"
    ]
    assert _problems(_source(), planned_launch_flags=None) == []
    assert consistency_notes(_source(), planned_launch_flags=None) == [
        "launch flags not compared: the provider plans no launch flags"
    ]
    assert consistency_notes(_source(), planned_launch_flags=BASE_FLAGS) == []


def test_version_warnings() -> None:
    recorded = _source()
    pinned = PinnedReplayability(random_seed=7)
    assert version_warnings(recorded, browser_version=VERSION, replayability=pinned) == []
    newer = {"product": "Chrome/156.0", "revision": "@def", "userAgent": "ScriptedChrome/2.0"}
    warnings = version_warnings(recorded, browser_version=newer, replayability=pinned)
    assert [w.split(":")[0] for w in warnings] == [
        "Chrome product differs",
        "Chrome revision differs",
        "browser user agent differs (the tier does not pin one)",
    ]
    ua_pinned = PinnedReplayability(random_seed=7, user_agent="Pinned/1")
    pinned_warnings = version_warnings(recorded, browser_version=newer, replayability=ua_pinned)
    assert not any("user agent" in w for w in pinned_warnings)


def test_a_changed_redaction_policy_warns() -> None:
    def _old_policy(document: dict[str, Any]) -> None:
        document["redaction"]["policy"] = "af-privacy-0+browser-0"

    warnings = version_warnings(
        _source(edit=_old_policy),
        browser_version=VERSION,
        replayability=PinnedReplayability(random_seed=7),
    )
    assert len(warnings) == 1 and warnings[0].startswith("redaction policy differs")
