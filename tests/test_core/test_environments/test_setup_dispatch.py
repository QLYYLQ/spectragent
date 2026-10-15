"""§5.2.6 — ``EnvironmentBase.setup`` routes per capability.

It used to be::

    async def setup(self, **kwargs):
        for cap in self.capabilities.values():
            await cap.setup(**kwargs)

with ``CapabilityBase.setup`` and ``BrowserCapability.setup`` both ending
in ``**kwargs``. Two consequences, both pinned here:

1. Two capabilities could not be configured independently — one
   ``timeout`` reached both, and neither could have its own.
2. A misspelled key was accepted by every capability and acted on by
   none. There was no error anywhere.

The accepted key set is read off each capability's ``setup()``
signature, so the contract cannot drift from the implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import ActionResult
from tests._helpers.assembly import make_env

pytestmark = pytest.mark.usefixtures("archive_fakes")


@dataclass
class Browserish(CapabilityBase):
    """Takes a start URL and a timeout."""

    __registry_key__ = "f8s_browserish"

    def __post_init__(self) -> None:
        self.got: dict[str, Any] = {}

    async def setup(
        self,
        *,
        negotiation: Any = None,
        start_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.got = {"start_url": start_url, "timeout": timeout}
        await super().setup(negotiation=negotiation)

    async def execute_action(self, action) -> ActionResult:
        return ActionResult(success=True)


@dataclass
class Dockerish(CapabilityBase):
    """Takes an image and — deliberately — a ``timeout`` of its own."""

    __registry_key__ = "f8s_dockerish"

    def __post_init__(self) -> None:
        self.got: dict[str, Any] = {}

    async def setup(
        self,
        *,
        negotiation: Any = None,
        image: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.got = {"image": image, "timeout": timeout}
        await super().setup(negotiation=negotiation)

    async def execute_action(self, action) -> ActionResult:
        return ActionResult(success=True)


@dataclass
class Sinkish(CapabilityBase):
    """Declares nothing but a ``**kwargs`` sink — the pre-F8 shape."""

    __registry_key__ = "f8s_sinkish"

    def __post_init__(self) -> None:
        self.got: dict[str, Any] = {}

    async def setup(self, *, negotiation: Any = None, **kwargs: Any) -> None:
        self.got = dict(kwargs)
        await super().setup(negotiation=negotiation)


def _env(*caps: CapabilityBase) -> EnvironmentBase:
    return make_env(EnvironmentBase, {c.capability_id: c for c in caps})


def _pair() -> tuple[Browserish, Dockerish]:
    """A fresh pair per test — capabilities carry per-run state."""
    return Browserish(), Dockerish()


class TestFlatContextRoutesByDeclaration:
    """``TaskSetup.to_context()`` is flat; each key finds its owner."""

    async def test_each_capability_gets_only_its_own_keys(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)

        await env.setup({"start_url": "https://example.com", "image": "swe:latest"})

        assert browser.got == {"start_url": "https://example.com", "timeout": None}
        assert docker.got == {"image": "swe:latest", "timeout": None}

    async def test_negotiation_still_reaches_every_capability(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)

        await env.setup({"image": "x"}, negotiation={"f8s_dockerish.terminal"})

        assert browser._negotiated_fragments == frozenset({"f8s_dockerish.terminal"})
        assert docker._negotiated_fragments == frozenset({"f8s_dockerish.terminal"})

    async def test_empty_context_is_fine(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)
        await env.setup()
        assert browser.got == {"start_url": None, "timeout": None}


class TestUnknownKeyIsAnError:
    async def test_typo_raises_and_names_the_key(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)

        with pytest.raises(ValueError) as exc:
            await env.setup({"strat_url": "https://example.com"})

        message = str(exc.value)
        assert "strat_url" in message
        # The message has to say what *would* have worked.
        assert "start_url" in message and "image" in message

    async def test_neither_capability_ran_setup(self) -> None:
        """Fail before any capability starts, not halfway through."""
        browser, docker = _pair()
        env = _env(browser, docker)

        with pytest.raises(ValueError):
            await env.setup({"image": "ok", "nonsense": 1})

        assert browser.got == {} and docker.got == {}

    async def test_a_kwargs_sink_claims_nothing(self) -> None:
        """The pre-F8 shape no longer swallows unknown keys."""
        sink = Sinkish()
        env = _env(sink)

        with pytest.raises(ValueError, match="anything_at_all"):
            await env.setup({"anything_at_all": 1})


class TestNamespacedContextGivesIndependence:
    """The case a flat context cannot express: same option, two owners."""

    async def test_same_key_different_values_per_capability(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)

        await env.setup(
            {
                "f8s_browserish": {"timeout": 5, "start_url": "https://a.example"},
                "f8s_dockerish": {"timeout": 600, "image": "swe:latest"},
            }
        )

        assert browser.got == {"start_url": "https://a.example", "timeout": 5}
        assert docker.got == {"image": "swe:latest", "timeout": 600}

    async def test_namespaced_and_flat_compose(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)

        await env.setup({"start_url": "https://a.example", "f8s_dockerish": {"timeout": 600}})

        assert browser.got["start_url"] == "https://a.example"
        assert docker.got["timeout"] == 600

    async def test_flat_shared_key_reaches_both_declarers(self) -> None:
        """Documented fallback — both declare ``timeout``, both get it."""
        browser, docker = _pair()
        env = _env(browser, docker)

        await env.setup({"timeout": 30})

        assert browser.got["timeout"] == 30
        assert docker.got["timeout"] == 30

    async def test_unknown_key_inside_a_namespace_raises(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)

        with pytest.raises(ValueError) as exc:
            await env.setup({"f8s_browserish": {"image": "wrong-capability"}})

        message = str(exc.value)
        assert "image" in message and "f8s_browserish" in message


class TestReplayArchiveRouting:
    """M30: ``replay_archive`` is a task-context key the browser declares by name (§4.10)."""

    def test_replay_archive_routes_only_to_the_browser_capability(self) -> None:
        from spectragent.core.capabilities.browser import BrowserCapability

        browser = BrowserCapability()
        docker = Dockerish()
        env = _env(browser, docker)
        routed = env.route_setup_context({"replay_archive": "/runs/src", "image": "swe:latest"})
        assert routed[browser.capability_id] == {"replay_archive": "/runs/src"}
        assert routed[docker.capability_id] == {"image": "swe:latest"}

    def test_a_capability_that_does_not_declare_it_refuses_it(self) -> None:
        browser, docker = _pair()
        env = _env(browser, docker)
        with pytest.raises(ValueError, match="replay_archive"):
            env.route_setup_context({"replay_archive": "/runs/src"})


# ── check_setup: the same routing, at prepare(), acquiring nothing ───


@dataclass
class Checkingish(Browserish):
    """A :class:`Browserish` with a setup check that records each slice it is handed."""

    __registry_key__ = "f8s_checkingish"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.checks: list[tuple[dict[str, Any], Any, tuple[Any, ...]]] = []
        self.refusal: Exception | None = None

    def check_setup(self, context, *, negotiation=None, archivers=()) -> None:
        self.checks.append((dict(context), negotiation, tuple(archivers)))
        if self.refusal is not None:
            raise self.refusal


class TestCheckSetup:
    """``Environment.check_setup`` (§4.3): what setup would refuse, refused without setting up."""

    def test_each_checking_capability_gets_its_own_slice(self) -> None:
        checking, docker = Checkingish(), Dockerish()
        env = _env(checking, docker)

        env.check_setup(
            {
                "start_url": "https://a.example",
                "image": "swe:latest",
                "f8s_checkingish": {"timeout": 5},
            },
            negotiation={"f8s_dockerish.terminal"},
        )

        assert checking.checks == [
            ({"start_url": "https://a.example", "timeout": 5}, {"f8s_dockerish.terminal"}, ())
        ]
        assert checking.got == {} and docker.got == {}, "nothing was set up"
        assert checking._negotiated_fragments is None, "the negotiation was not applied"

    def test_an_unknown_key_is_refused_before_any_capability_checks(self) -> None:
        checking = Checkingish()
        env = _env(checking, Dockerish())
        with pytest.raises(ValueError, match="strat_url"):
            env.check_setup({"strat_url": "https://a.example"})
        assert checking.checks == []

    def test_a_capability_s_refusal_propagates(self) -> None:
        checking = Checkingish()
        checking.refusal = RuntimeError("tier ↔ provider")
        with pytest.raises(RuntimeError, match="tier ↔ provider"):
            _env(checking).check_setup()

    def test_archivers_go_to_the_capability_that_provides_them(self) -> None:
        from spectragent.core.environments.errors import UnroutableArchiverError
        from tests._helpers.archive_fakes import (
            FakeArchivingCapability,
            FakeOrphanArchiver,
            FakeStepArchiver,
        )

        class _Checking(FakeArchivingCapability):
            __abstract__ = True

            def check_setup(self, context, *, negotiation=None, archivers=()) -> None:
                self.__dict__["checked"] = tuple(archivers)

        cap, steps = _Checking(), FakeStepArchiver()
        env = _env(cap, Checkingish())
        env.check_setup(archivers=[steps])
        assert cap.__dict__["checked"] == (steps,)
        assert cap.bind_calls == 0, "checking binds nothing"
        with pytest.raises(UnroutableArchiverError):
            env.check_setup(archivers=[FakeOrphanArchiver()])
