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
    """Instantiate an Environment subclass with its capabilities.

    ``capabilities`` is a real ``EnvironmentBase.__init__`` parameter —
    the same path :class:`ConfigComposer` takes — so this helper no
    longer needs a setattr workaround. Keys are capability *instance*
    ids; action prefix routing reads ``capability_id`` off the values.
    """
    return cls(capabilities=caps_map)  # type: ignore[call-arg]


AGENT_ID = "agent#0"
ENV_ID = "env#0"


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
    agents/envs/LLMs without touching Pydantic validation, while
    mirroring what the Composer does: loop / llm / memory are injected
    onto the *agent*, because that is where
    :meth:`AgentRuntime.for_agent` reads them from.

    Use :data:`AGENT_ID` / :data:`ENV_ID` with
    ``AgentRuntime.for_agent(run, AGENT_ID)``.
    """
    from spectragent.core.composer import ComposedRun

    # Mirror Composer's injection pass: setattr on the agent overrides
    # the WiredField descriptor so ``agent.llm`` / ``agent.memory`` /
    # ``agent.agent_loop`` return concrete instances, not raise.
    agent.agent_loop = loop
    agent.llm = llm
    agent.memory = memory

    by_layer: dict[str, dict[str, Any]] = {
        "agent": {AGENT_ID: agent},
        "agent_loop": {"loop#0": loop},
        "llm": {"llm#0": llm},
        "memory": {"memory#0": memory},
    }
    if env is not None:
        by_layer["environment"] = {ENV_ID: env}
    if hooks:
        by_layer["hook"] = {f"hook#{i}": h for i, h in enumerate(hooks)}
    return ComposedRun(by_layer=by_layer)
