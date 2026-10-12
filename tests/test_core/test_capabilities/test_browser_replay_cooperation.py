"""The replay's cooperation rows (§4.14 "协作检查一览", M30).

What an intercepting network source needs of the rest of the configuration:
the recorder connection on a browser the capability owns, the task's
``replay_archive`` (and a ``live`` source refuses one), a bound archiver that
records the replay log (found by its ``replay_log`` need: ``browser.replay_log``)
and none that records live traffic (the ``fetch_response`` need:
``browser.network``, refused naming ``browser.replay_log``), and — with
``isolation: required`` — a provider that declares ``NETWORK_ISOLATION`` and
whose knobs do not route around it. A ``live`` source refuses the replay-log
archiver: it replays nothing. The configuration-decidable rows are refused by
``bind_archivers`` at ``prepare()`` when any archiver is bound; setup checks
everything again, with the source run's consistency, before it launches
anything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive.source import ArchiveOpenError
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import (
    NetworkArchiver,
    ReplayLogArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.backends.chrome_switches import (
    NETWORK_ISOLATION_ARGS,
)
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cooperation import (
    BrowserCooperationError,
    binding_problems,
    cooperation_problems,
    launch_args,
    planned_launch_flags,
    recorder_needs,
)
from spectragent.core.capabilities.browser.replay import (
    ArchiveNetworkSource,
    PinnedReplayability,
)
from tests._helpers.browser_archive import IsolatingBrowser, OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser
from tests._helpers.replay_archive import RecordingScript

PINNED = PinnedReplayability(random_seed=7)


@pytest.fixture(scope="module")
def source_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A pinned recording whose launch flags are what the provider doubles record."""

    async def _record() -> Path:
        script = RecordingScript(
            tmp_path_factory.mktemp("source"),
            run_id="run-src",
            launch_flags=[*OwnedBrowser.BASE_FLAGS, *PINNED.launch_args()],
        )
        await script.exchange("http://shop.test/", body=b"<html>home</html>")
        await script.commit(1)
        return script.finish()

    return asyncio.run(_record())


def _replaying(
    backend: Any = None,
    *,
    source: ArchiveNetworkSource | None = None,
    archivers: tuple[Any, ...] | None = None,
    tier: Any = None,
) -> BrowserCapability:
    """A replaying capability holding *archivers* as its binding, unchecked.

    Bypassing ``bind_archivers`` keeps its own refusal (the same rows, at
    ``prepare()``) from pre-empting the setup-time row under test;
    :class:`TestBinding` covers that refusal itself.
    """
    cap = BrowserCapability()
    cap.backend = backend if backend is not None else IsolatingBrowser()  # type: ignore[assignment]
    cap.replayability = tier if tier is not None else PinnedReplayability(random_seed=7)
    cap.network_source = source if source is not None else ArchiveNetworkSource()
    cap._archivers = list(archivers if archivers is not None else (ReplayLogArchiver(),))
    return cap


def _problems(cap: BrowserCapability, **kwargs: Any) -> list[str]:
    options: dict[str, Any] = {
        "cdp_url": None,
        "injected_client": False,
        "replay_archive": "/runs/src",
    }
    options.update(kwargs)
    return cooperation_problems(cap, **options)


# ── the rows ──────────────────────────────────────────────────────────


class TestRows:
    def test_a_replay_on_an_isolating_browser_cooperates(self) -> None:
        assert _problems(_replaying()) == []

    def test_an_intercepting_source_needs_the_recorder(self) -> None:
        cap = BrowserCapability()
        assert recorder_needs(cap) == []
        cap.network_source = ArchiveNetworkSource()
        assert recorder_needs(cap) == ["network_source: archive"]

    def test_replay_archive_goes_with_an_archive_source_only(self) -> None:
        (missing,) = _problems(_replaying(), replay_archive=None)
        assert missing.startswith("network_source: archive ↔ task context: no replay_archive")
        live = BrowserCapability()
        (unwanted,) = _problems(live, replay_archive="/runs/src")
        assert unwanted.startswith("network_source: live ↔ replay_archive='/runs/src'")

    def test_ownership_rows_name_the_source(self) -> None:
        cap = _replaying(archivers=())
        explicit = _problems(cap, cdp_url="ws://127.0.0.1:1/x")
        assert any(p.startswith("network_source: archive ↔ cdp_url") for p in explicit)
        injected = _problems(cap, injected_client=True)
        assert any(
            p.startswith("network_source: archive ↔ injected control client") for p in injected
        )

    def test_unsupported_provider_needs_acknowledgement(self) -> None:
        (problem,) = _problems(_replaying(OwnedBrowser()))
        assert problem.startswith(
            "network_source: archive isolation=required ↔ backend provider_id='fake_local'"
        )
        assert "NETWORK_ISOLATION" in problem and "none_acknowledged" in problem
        acknowledged = _replaying(
            OwnedBrowser(), source=ArchiveNetworkSource(isolation="none_acknowledged")
        )
        assert _problems(acknowledged) == []

    def test_isolation_conflicts_are_reported_knob_by_knob(self) -> None:
        backend = IsolatingBrowser(conflicts=["proxy_server='http://p:1' …", "startup_url='x' …"])
        problems = _problems(_replaying(backend))
        assert problems == [
            "network_source: archive isolation=required ↔ backend provider_id='fake_isolating' "
            "knob proxy_server='http://p:1' …",
            "network_source: archive isolation=required ↔ backend provider_id='fake_isolating' "
            "knob startup_url='x' …",
        ]
        acknowledged = _replaying(
            backend, source=ArchiveNetworkSource(isolation="none_acknowledged")
        )
        assert _problems(acknowledged) == []

    def test_the_real_local_chrome_names_its_routing_knobs(self, monkeypatch) -> None:
        monkeypatch.delenv("CHROME_USER_DATA_DIR", raising=False)
        cap = _replaying(LocalChromeProvider(proxy_server="http://127.0.0.1:7897"))
        (problem,) = _problems(cap)
        assert "provider_id='local_chrome' knob proxy_server='http://127.0.0.1:7897'" in problem

    def test_without_any_archiver_setup_still_refuses(self) -> None:
        (problem,) = _problems(_replaying(archivers=()))
        assert problem.startswith("network_source: archive ↔ bound archivers []")
        assert "bind browser.replay_log" in problem

    def test_setup_refuses_the_network_archiver_in_a_replay(self) -> None:
        problems = _problems(_replaying(archivers=(NetworkArchiver(), ReplayLogArchiver())))
        (problem,) = problems
        assert problem.startswith("network_source: archive ↔ archiver browser.network")
        assert "bind browser.replay_log instead" in problem


class TestBinding:
    def test_binding_without_a_replay_log_archiver_is_refused(self) -> None:
        cap = _replaying(archivers=())  # the source is set: binding decides its rows now
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([StorageArchiver()])
        (problem,) = info.value.problems
        assert problem.startswith("network_source: archive ↔ bound archivers ['browser.storage']")
        assert "the 'replay_log' need: browser.replay_log" in problem
        assert cap.bound_archivers == (), "nothing changed"
        cap.bind_archivers([ReplayLogArchiver(), StorageArchiver()])
        assert [a.archiver_id for a in cap.bound_archivers] == [
            "browser.replay_log",
            "browser.storage",
        ]

    def test_the_network_archiver_is_refused_naming_the_replay_log(self) -> None:
        """``browser.network`` always records live traffic; a replay has none."""
        cap = _replaying(archivers=())
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([NetworkArchiver(), ReplayLogArchiver()])
        (problem,) = info.value.problems
        assert problem.startswith("network_source: archive ↔ archiver browser.network")
        assert "records live traffic" in problem
        assert "bind browser.replay_log instead" in problem
        assert cap.bound_archivers == (), "nothing changed"

        with pytest.raises(BrowserCooperationError) as alone:
            cap.bind_archivers([NetworkArchiver()])
        network, missing = alone.value.problems
        assert network.startswith("network_source: archive ↔ archiver browser.network")
        assert missing.startswith("network_source: archive ↔ bound archivers ['browser.network']")

    def test_a_live_source_refuses_the_replay_log_archiver(self) -> None:
        cap = BrowserCapability()
        cap.backend = IsolatingBrowser()  # type: ignore[assignment]
        cap.replayability = PINNED
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([NetworkArchiver(), ReplayLogArchiver()])
        (problem,) = info.value.problems
        assert problem.startswith("network_source: live ↔ archiver browser.replay_log")
        assert "replays nothing" in problem and "network_source: archive" in problem
        cap.bind_archivers([NetworkArchiver()])
        assert [a.archiver_id for a in cap.bound_archivers] == ["browser.network"]

    def test_binding_refuses_an_unsupported_provider_at_prepare(self) -> None:
        cap = _replaying(OwnedBrowser(), archivers=())
        with pytest.raises(BrowserCooperationError, match="isolation=required"):
            cap.bind_archivers([ReplayLogArchiver()])

    def test_binding_nothing_decides_nothing_yet(self) -> None:
        cap = _replaying(OwnedBrowser(), archivers=())
        assert binding_problems(cap, [], cdp_url=None, injected_client=False) == []


class TestLaunchArgs:
    def test_isolation_is_added_only_when_required_and_supported(self) -> None:
        tier = tuple(PINNED.launch_args())
        assert launch_args(_replaying()) == (*tier, *NETWORK_ISOLATION_ARGS)
        acknowledged = _replaying(source=ArchiveNetworkSource(isolation="none_acknowledged"))
        assert launch_args(acknowledged) == tier
        assert launch_args(_replaying(OwnedBrowser())) == tier
        live = BrowserCapability()
        live.backend = IsolatingBrowser()  # type: ignore[assignment]
        assert launch_args(live) == ()

    def test_planned_flags_are_the_providers_for_those_args(self) -> None:
        cap = _replaying()
        planned = planned_launch_flags(cap, cdp_url=None, injected_client=False)
        assert planned == [*OwnedBrowser.BASE_FLAGS, *launch_args(cap)]
        assert planned_launch_flags(cap, cdp_url="ws://x", injected_client=False) is None
        assert planned_launch_flags(cap, cdp_url=None, injected_client=True) is None


# ── setup refuses before anything starts ──────────────────────────────


class TestRefusedSetup:
    async def _refused(self, cap: BrowserCapability, **setup: Any) -> BrowserCooperationError:
        backend = cap.backend
        browser = FakeBrowser()
        cap._inject_cdp_factory(browser.client)
        with pytest.raises(BrowserCooperationError) as info:
            await cap.setup(**setup)
        assert backend.open_calls == [], "a refused replay launches nothing"  # type: ignore[attr-defined]
        assert browser.clients == [] and cap._cdp is None and cap._recorder is None
        return info.value

    async def test_a_refused_replay_setup_launches_nothing(self, source_run: Path) -> None:
        missing = await self._refused(_replaying())
        assert "no replay_archive" in missing.problems[0]
        unsupported = await self._refused(
            _replaying(OwnedBrowser()), replay_archive=str(source_run)
        )
        assert "isolation=required" in unsupported.problems[0]
        other_seed = await self._refused(
            _replaying(tier=PinnedReplayability(random_seed=8)), replay_archive=str(source_run)
        )
        assert any("random_seed=7" in p and "random_seed=8" in p for p in other_seed.problems)

    async def test_cooperation_and_consistency_are_one_error(self, source_run: Path) -> None:
        cap = _replaying(OwnedBrowser(), tier=PinnedReplayability(random_seed=8))
        error = await self._refused(cap, replay_archive=str(source_run))
        # The provider row, then the source run's: the seed, and the switch it sets.
        assert len(error.problems) == 3
        assert "isolation=required" in error.problems[0]
        assert "random_seed" in error.problems[1]
        assert "launch_flags_sha256" in error.problems[2]

    async def test_an_archive_that_cannot_be_opened_fails_before_launch(
        self, tmp_path: Path
    ) -> None:
        cap = _replaying()
        backend = cap.backend
        cap._inject_cdp_factory(FakeBrowser().client)
        with pytest.raises(ArchiveOpenError, match="not-a-run"):
            await cap.setup(replay_archive=str(tmp_path / "not-a-run"))
        assert backend.open_calls == []  # type: ignore[attr-defined]
