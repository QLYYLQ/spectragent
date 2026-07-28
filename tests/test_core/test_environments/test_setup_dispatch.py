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

        assert browser._negotiated_fragments == frozenset(
            {"f8s_dockerish.terminal"}
        )
        assert docker._negotiated_fragments == frozenset(
            {"f8s_dockerish.terminal"}
        )

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

        await env.setup(
            {"start_url": "https://a.example", "f8s_dockerish": {"timeout": 600}}
        )

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
