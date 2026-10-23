"""The prompt must advertise exactly what the loop executes — §6.1.5.

The loop owns ``max_actions_per_step`` while ``BrowserUseAgent`` used to
carry its own default and render *that* into the system prompt. The
model was told it could emit more actions than the loop would run,
the loop dropped everything past the first with no log and no feedback
message, and the model re-issued steps it believed had run.

The hand-off has two halves: the agent renders the typed limits it consumes,
and Runtime obtains those limits from the Loop and passes them through the
existing prepare slot. Calling prepare directly alone cannot verify that seam.

Scope, precisely: what is compared here is the *advertised* number against
``ReactLoop.max_actions_per_step``, the attribute the loop enforces. The
other half of the claim — that the loop really does stop at that number,
truncate the rest and say so — is
``test_core/test_loops/test_react_step_records.py::TestActionTruncation``,
which builds its own loop so its numbers are derived, not pinned. And the
hop before this one, YAML → loop, belongs to the shipped-config gate in
``test_core/test_composer/test_shipped_config_integration.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from tests._helpers.action_spaces import space_of
from tests._helpers.fragment_formats import dom_formats

#: Resolved from this file, not from the CWD — ``pytest tests/test_agents``
#: from anywhere but the repo root used to make these two tests vanish into
#: a ``FileNotFoundError`` that looked like a missing config.
SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "browser_use_local.yaml"


def _shipped_config(*, trace_dir: Path | None = None) -> dict[str, Any]:
    """The shipped YAML, its trace writer pointed at *trace_dir* when one is given.

    The shipped file records under ``./traces``; a test that executes a run
    must not leave a trace in the working directory.
    """
    raw = yaml.safe_load(SHIPPED_CONFIG.read_text())
    if trace_dir is not None:
        for hook in raw.get("hook") or []:
            if hook.get("hook_id") == "trace_writer":
                hook["trace_dir"] = str(trace_dir)
    return raw


class _RecordingMemory:
    """Captures the seed messages ``AgentRuntime`` writes after prepare."""

    supports_protocols = frozenset({"user_assistant"})

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, msg: Any) -> None:
        self.messages.append(msg)

    def add_messages(self, msgs: Any) -> None:
        self.messages.extend(msgs)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else str(getattr(message, "content", message))


async def test_runtime_carries_the_loops_limit_into_the_prompt(tmp_path: Path) -> None:
    """End-to-end through ``execute()``: real agent, real loop, real runtime.

    The Loop's limit differs from its shipped setting, so the prompt can
    advertise it only if Runtime passes the current typed execution limits.
    """
    traces = tmp_path / "traces"
    composer = ConfigComposer(registrars=collect_registrars())
    run = composer.compose(_shipped_config(trace_dir=traces))

    agent_id = run.ids("agent")[0]
    agent = run.instance("agent", agent_id)
    loop = agent.agent_loop
    shipped = loop.max_actions_per_step  # whatever the shipped config asks for

    # The agent must not own a second copy of the limit.
    assert not hasattr(agent, "max_actions_per_step")
    assert not hasattr(agent, "max_steps")

    # Derive a probe from the shipped value so tuning the config cannot
    # accidentally turn the assertion into a comparison against a default.
    probe = max(shipped, 1) + 2
    loop.max_actions_per_step = probe

    memory = _RecordingMemory()
    agent.memory = memory
    runtime = AgentRuntime.for_agent(run, agent_id)

    # Fail the environment at setup: the seed prompt is built and written
    # to memory *before* setup runs, so the assertion below sees the real
    # prompt without launching Chrome or calling an LLM.
    env = runtime.env
    assert env is not None

    async def _refuse(_context: Any = None, **_kw: Any) -> None:
        raise RuntimeError("env deliberately not started by this test")

    env.setup = _refuse  # type: ignore[method-assign]

    result = await runtime.execute("t", task_id="t-1", raise_on_error=False)
    assert result.agent_done.value == "error"
    # The run's trace went to the test's own directory, not ./traces.
    assert traces.is_dir() and any(traces.iterdir()), "the trace writer was not redirected"

    system_prompt = _message_text(memory.messages[0])
    advertised = {int(n) for n in re.findall(r"maximum of (\d+) actions", system_prompt)}
    assert advertised == {probe}, (
        f"prompt advertises {advertised}, loop enforces {loop.max_actions_per_step} "
        f"— AgentRuntime is not carrying the loop's limit to the agent"
    )


def test_agent_renders_whatever_limit_it_is_handed() -> None:
    """The agent half, in isolation: no hidden default of its own."""
    composer = ConfigComposer(registrars=collect_registrars())
    run = composer.compose(_shipped_config())

    agent = run.instance("agent", run.ids("agent")[0])
    # Real descriptors: browser_use compiles them into a closed output
    # schema and refuses a run that could emit nothing at all.
    from spectragent.core.actions._registrar import ActionRegistrar

    descriptors = [ActionRegistrar.get("browser.done")().descriptor()]
    seed = agent.prepare(
        task="t",
        action_space=space_of(descriptors),
        execution_limits=ExecutionLimits(max_actions_per_step=7),
        fragment_formats=dom_formats(),
    )
    text = _message_text(seed[0])
    assert {int(n) for n in re.findall(r"maximum of (\d+) actions", text)} == {7}
