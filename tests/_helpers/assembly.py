"""Test helpers that mirror what :class:`ConfigComposer` does at runtime.

Unit tests instantiate a class directly and then setattr its wired
dependencies. This keeps tests decoupled from the full Composer pipeline
while matching production wiring semantics (setattr = WiredField injection).
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


def make_agent(
    cls: type[T],
    *,
    llm: Any = None,
    memory: Any = None,
    **overrides: Any,
) -> T:
    """Instantiate *cls* and inject wired dependencies via setattr.

    Any kwargs passed via ``**overrides`` go through the dataclass
    ``__init__`` (they must be real config fields). ``llm`` and
    ``memory`` are *not* init params any more — they go to setattr to
    mimic what ``ConfigComposer`` does for wired receptors.
    """
    agent = cls(**overrides)
    if llm is not None:
        agent.llm = llm  # type: ignore[attr-defined]
    if memory is not None:
        agent.memory = memory  # type: ignore[attr-defined]
    return agent


def make_env(cls: type[T], caps_map: dict[str, Any]) -> T:
    """Instantiate an Environment subclass and inject ``capabilities`` dict.

    ``EnvironmentBase.__init__`` no longer takes a ``capabilities`` arg;
    the dict is a WiredField receptor populated by the Composer. Tests
    shortcut that by setting the attribute directly.
    """
    env = cls()
    env.capabilities = caps_map  # type: ignore[attr-defined]
    return env


def make_run(
    *,
    agent: Any,
    loop: Any,
    env: Any | None,
    llm: Any,
    memory: Any,
    hooks: list[Any] | None = None,
) -> Any:
    """Build a ``ComposedRun`` from pre-assembled mock parts.

    Bypasses :class:`ConfigComposer` so unit tests can plug in mock
    agents/envs/LLMs without touching Pydantic validation. ``llm`` and
    ``memory`` are also setattr'd onto *agent* so code that reads
    ``self.memory`` / ``self.llm`` inside the agent (e.g.
    ``BrowserUseAgent.prepare``) works without going through the
    ``ComposedRun`` accessors.
    """
    from spectragent.core.composer import ComposedRun

    # Mirror Composer's injection pass: setattr on the agent overrides
    # the WiredField descriptor so ``agent.llm`` / ``agent.memory``
    # return concrete instances, not raise.
    agent.llm = llm
    agent.memory = memory

    by_layer: dict[str, dict[str, Any]] = {
        "agent": {"agent": agent},
        "agent_loop": {"loop": loop},
        "llm": {"llm": llm},
        "memory": {"memory": memory},
    }
    if env is not None:
        by_layer["environment"] = {"env": env}
    if hooks:
        by_layer["hook"] = {f"h{i}": h for i, h in enumerate(hooks)}
    return ComposedRun(by_layer=by_layer)
