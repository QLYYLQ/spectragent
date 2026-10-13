"""``BrowserCapability.check_setup``: every setup refusal, at ``prepare()`` (§4.14 "协作检查一览").

The rows that used to be decided only when setup ran — and so only after the
trace was opened — are decided by ``check_setup``, which ``AgentRuntime.prepare()``
calls through ``Environment.check_setup`` with the task's setup context, the
observation negotiation and the archivers about to be bound:

- ``replayability: pinned`` on a provider that cannot take it (cloud), against
  the provider's own knobs, or with a persistent profile;
- the replay rows: ``replay_archive`` present or missing for the network
  source, the replay log with nothing bound, and the source run's consistency
  with this configuration — read from the source's environment fingerprint
  alone, never its index;
- a negotiated fragment the profile's watchdogs cannot resolve.

The check changes nothing and launches nothing. ``setup`` runs it again, so
the setup-level tests (``test_browser_replayability.py``,
``test_browser_replay_cooperation.py``) still hold.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from spectragent.core.archive.source import ArchiveOpenError, open_archive
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import ReplayLogArchiver, StorageArchiver
from spectragent.core.capabilities.browser.backends.capabilities import CDP
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.replay import (
    ArchiveNetworkSource,
    PinnedReplayability,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    load_archive,
    load_fingerprint,
)
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.runtime import AgentRuntime
from tests._helpers.assembly import AGENT_ID, make_env, make_run
from tests._helpers.browser_archive import IsolatingBrowser, OwnedBrowser
from tests._helpers.replay_archive import RecordingScript

PINNED = PinnedReplayability(random_seed=7)


class _CloudBrowser(OwnedBrowser):
    """A provider that launches nothing this capability owns (the cloud lite providers)."""

    __capabilities__: ClassVar[set[str]] = {CDP}
    provider_id = "fake_cloud"


class _KnobbedBrowser(OwnedBrowser):
    """A provider one of whose knobs contradicts the tier's switches."""

    def launch_arg_conflicts(self, launch_args: Any) -> list[str]:
        return ["user_agent='X' contradicts --user-agent=Y"]


@pytest.fixture(scope="module")
def source_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A pinned recording (seed 7) whose launch flags are what the provider doubles record."""

    async def _record() -> Path:
        script = RecordingScript(
            tmp_path_factory.mktemp("source"),
            run_id="run-src",
            launch_flags=OwnedBrowser().planned_launch_flags(
                PINNED.launch_args(), PINNED.launch_env()
            ),
        )
        await script.exchange("http://shop.test/", body=b"<html>home</html>")
        await script.commit(1)
        return script.finish()

    return asyncio.run(_record())


def _pinned(backend: Any, **pinned: Any) -> BrowserCapability:
    cap = BrowserCapability()
    cap.backend = backend  # type: ignore[assignment]
    cap.replayability = PinnedReplayability(**{"random_seed": 7, **pinned})
    return cap


def _replaying(backend: Any = None, *, seed: int = 7) -> BrowserCapability:
    cap = _pinned(backend if backend is not None else IsolatingBrowser(), random_seed=seed)
    cap.network_source = ArchiveNetworkSource()
    return cap


def _refused(cap: BrowserCapability, context: dict[str, Any] | None = None, **kw: Any) -> list[str]:
    with pytest.raises(BrowserCooperationError) as info:
        cap.check_setup(context or {}, **kw)
    return info.value.problems


# ── the rows that used to wait for setup ──────────────────────────────


class TestPinned:
    def test_on_a_cloud_provider(self) -> None:
        problems = _refused(_pinned(_CloudBrowser()))
        assert len(problems) == 2
        assert all(
            p.startswith("replayability: pinned ↔ backend provider_id='fake_cloud'")
            for p in problems
        )
        assert "NETWORK_CAPTURE" in problems[0] and "LAUNCH_ARGS" in problems[1]

    def test_against_the_provider_s_knobs(self) -> None:
        (problem,) = _refused(_pinned(_KnobbedBrowser()))
        assert problem == (
            "replayability: pinned ↔ backend provider_id='fake_local' knob "
            "user_agent='X' contradicts --user-agent=Y"
        )

    def test_with_a_persistent_profile(self) -> None:
        (problem,) = _refused(_pinned(OwnedBrowser(profile="user_data_dir='/home/me/chrome'")))
        assert problem.startswith("replayability: pinned ↔ backend provider_id='fake_local'")
        assert "user_data_dir='/home/me/chrome'" in problem and "fresh profile" in problem

    def test_with_an_explicit_endpoint_from_the_task_context(self) -> None:
        (problem,) = _refused(_pinned(OwnedBrowser()), {"cdp_url": "ws://127.0.0.1:1/x"})
        assert problem.startswith("replayability: pinned ↔ cdp_url")

    def test_on_its_own_browser_it_cooperates(self) -> None:
        _pinned(OwnedBrowser()).check_setup({})


class TestReplay:
    def test_an_archive_source_needs_the_task_s_replay_archive(self) -> None:
        problems = _refused(_replaying(), archivers=[ReplayLogArchiver()])
        assert problems == [
            "network_source: archive ↔ task context: no replay_archive — the source run "
            "directory to replay (run_cli --replay-archive DIR)"
        ]

    def test_a_live_source_refuses_one(self) -> None:
        (problem,) = _refused(BrowserCapability(), {"replay_archive": "/runs/src"})
        assert problem.startswith("network_source: live ↔ replay_archive='/runs/src'")

    def test_nothing_bound_means_no_replay_log(self, source_run: Path) -> None:
        problems = _refused(_replaying(), {"replay_archive": str(source_run)})
        assert [p.split(" ↔ ")[1].split(":")[0] for p in problems] == ["bound archivers []"]

    def test_the_archivers_about_to_be_bound_are_the_ones_checked(self, source_run: Path) -> None:
        cap = _replaying()
        problems = _refused(cap, {"replay_archive": str(source_run)}, archivers=[StorageArchiver()])
        assert problems[0].startswith(
            "network_source: archive ↔ bound archivers ['browser.storage']"
        )
        cap.check_setup(
            {"replay_archive": str(source_run)},
            archivers=[ReplayLogArchiver(), StorageArchiver()],
        )

    def test_the_source_run_s_consistency(self, source_run: Path) -> None:
        problems = _refused(
            _replaying(seed=8), {"replay_archive": str(source_run)}, archivers=[ReplayLogArchiver()]
        )
        assert any("random_seed=7" in p and "random_seed=8" in p for p in problems)
        assert all("source run 'run-src'" in p for p in problems)

    def test_cooperation_and_consistency_are_one_error(self, source_run: Path) -> None:
        cap = _replaying(OwnedBrowser(), seed=8)
        problems = _refused(
            cap, {"replay_archive": str(source_run)}, archivers=[ReplayLogArchiver()]
        )
        assert len(problems) == 3
        assert "isolation=required" in problems[0]
        assert "random_seed" in problems[1] and "launch_flags_sha256" in problems[2]

    def test_a_host_whose_browser_falls_back_to_another_locale_is_refused(
        self, source_run: Path
    ) -> None:
        """Strict, with no switch: the OS facts read before launch against the recording's defaults."""

        class _ElsewhereBrowser(IsolatingBrowser):
            LOCALE = "fr-FR"

        (problem,) = _refused(
            _replaying(_ElsewhereBrowser()),
            {"replay_archive": str(source_run)},
            archivers=[ReplayLogArchiver()],
        )
        assert problem.startswith("source run 'run-src' browser default locale='en-US' ↔")
        assert "locale='fr-FR' (read from this host before launch)" in problem

    def test_a_locale_the_os_cannot_say_is_left_to_after_launch(self, source_run: Path) -> None:
        class _NoLocaleBeforeLaunch(IsolatingBrowser):
            LOCALE = None

        _replaying(_NoLocaleBeforeLaunch()).check_setup(
            {"replay_archive": str(source_run)}, archivers=[ReplayLogArchiver()]
        )

    def test_only_the_fingerprint_is_read(
        self, source_run: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_index(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the check loaded the source run's index")

        monkeypatch.setattr(ArchiveNetworkSource, "open_replay", no_index)
        _replaying().check_setup(
            {"replay_archive": str(source_run)}, archivers=[ReplayLogArchiver()]
        )

    def test_an_archive_that_cannot_be_opened_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(ArchiveOpenError, match="not-a-run"):
            _replaying().check_setup(
                {"replay_archive": str(tmp_path / "not-a-run")}, archivers=[ReplayLogArchiver()]
            )


def test_the_fingerprint_read_is_the_full_load_s_minus_the_index(source_run: Path) -> None:
    light = load_fingerprint(open_archive(source_run))
    full = load_archive(open_archive(source_run))
    assert (light.run_id, light.fingerprint, light.fingerprint_sha256, light.t0_ms) == (
        full.run_id,
        full.fingerprint,
        full.fingerprint_sha256,
        full.t0_ms,
    )
    assert light.network_files == full.network_files == 1
    assert not light.index.records and light.store is None
    assert full.index.records, "the full load holds the recorded exchange"


class TestStartPage:
    """The task's start page: setup opens only one complete http(s) URL (§4.10 "起始页")."""

    def test_a_start_page_with_no_scheme_is_refused_by_name(self) -> None:
        (problem,) = _refused(BrowserCapability(), {"start_url": "www.bilibili.com"})
        assert problem.startswith("start_url 'www.bilibili.com' ↔ browser setup: ")
        assert "(it has no scheme)" in problem

    def test_text_or_several_sites_are_refused_by_name(self) -> None:
        (problem,) = _refused(
            BrowserCapability(), {"start_url": "www.xiachufang.com或其他美食网站"}
        )
        assert problem.startswith("start_url 'www.xiachufang.com或其他美食网站' ↔ browser setup")
        (several,) = _refused(BrowserCapability(), {"start_url": "jd.com + tmall.com"})
        assert "it contains whitespace" in several

    def test_a_complete_url_cooperates(self) -> None:
        BrowserCapability().check_setup({"start_url": "https://www.bilibili.com"})
        BrowserCapability().check_setup({})

    def test_it_is_reported_with_every_other_problem_at_once(self) -> None:
        problems = _refused(_pinned(_CloudBrowser()), {"start_url": "www.bilibili.com"})
        assert len(problems) == 3
        assert sum(p.startswith("start_url 'www.bilibili.com'") for p in problems) == 1

    async def test_setup_refuses_it_before_launching_anything(self) -> None:
        backend = OwnedBrowser()
        cap = BrowserCapability()
        cap.backend = backend  # type: ignore[assignment]
        try:
            with pytest.raises(BrowserCooperationError, match="start_url 'www.bilibili.com'"):
                await cap.setup(start_url="www.bilibili.com")
        finally:
            await cap.teardown()
        assert backend.open_calls == []


class TestProfile:
    def test_a_negotiated_fragment_no_installed_watchdog_resolves(self) -> None:
        cap = BrowserCapability()  # negotiation read the standard profile's fragments
        cap.profile = "vision_only"
        (problem,) = _refused(cap, negotiation=frozenset({"browser.dom", "browser.screenshot"}))
        assert problem.startswith("profile 'vision_only' (watchdogs_exclude ['dom', 'security'])")
        assert "negotiated fragment(s) ['browser.dom']" in problem

    def test_fragments_of_other_capabilities_are_not_its_business(self) -> None:
        cap = BrowserCapability()
        cap.profile = "vision_only"
        cap.check_setup({}, negotiation=frozenset({"browser.screenshot", "docker.terminal"}))


def test_the_check_changes_nothing() -> None:
    backend = OwnedBrowser()
    cap = _pinned(backend)
    cap.check_setup(
        {}, negotiation=frozenset({"browser.screenshot"}), archivers=[StorageArchiver()]
    )
    assert cap.bound_archivers == ()
    assert cap.provided_fragments == cap.declared_fragments, "the negotiation was not applied"
    assert backend.open_calls == [] and cap._cdp is None


# ── through the runtime ───────────────────────────────────────────────


class _ScreenshotAgent:
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot"]),
        "action": ("action", ["done"]),
    }

    def build_react_prompt(self, obs: Any) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []


class _LLM:
    model = "setup-check-llm"


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def clear(self) -> None:
        return None


def _run(cap: BrowserCapability) -> Any:
    from spectragent.core.loops import ReactLoop

    return make_run(
        agent=_ScreenshotAgent(),
        loop=ReactLoop(max_steps=1),
        env=make_env(BrowserEnvironment, {"browser": cap}),
        llm=_LLM(),
        memory=_Memory(),
    )


def test_prepare_refuses_pinned_on_a_cloud_provider() -> None:
    backend = _CloudBrowser()
    with pytest.raises(BrowserCooperationError, match="fake_cloud"):
        AgentRuntime.for_agent(_run(_pinned(backend)), AGENT_ID).prepare()
    assert backend.open_calls == []


def test_prepare_refuses_a_replay_against_its_source(source_run: Path) -> None:
    runtime = AgentRuntime.for_agent(_run(_replaying(seed=8)), AGENT_ID)
    with pytest.raises(BrowserCooperationError, match="random_seed=8"):
        runtime.prepare(env_context={"replay_archive": str(source_run)})


def test_prepare_refuses_a_start_page_that_is_not_a_url() -> None:
    backend = OwnedBrowser()
    cap = BrowserCapability()
    cap.backend = backend  # type: ignore[assignment]
    runtime = AgentRuntime.for_agent(_run(cap), AGENT_ID)
    with pytest.raises(BrowserCooperationError) as info:
        runtime.prepare(env_context={"start_url": "www.xiachufang.com或其他美食网站"})
    assert info.value.problems == [
        "start_url 'www.xiachufang.com或其他美食网站' ↔ browser setup: the task's start page "
        "is not an http(s) URL (it has no scheme); setup opens the start page before the "
        "agent's first observation and opens only one complete http:// or https:// URL, so "
        "correct the task's start page"
    ]
    assert backend.open_calls == []
    runtime.prepare(env_context={"start_url": "https://www.xiachufang.com"})
