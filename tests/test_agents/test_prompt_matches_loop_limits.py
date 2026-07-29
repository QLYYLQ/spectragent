"""The prompt must advertise exactly what the loop executes — §6.1.5.

The loop owns ``max_actions_per_step`` while ``BrowserUseAgent`` used to
carry its own default and render *that* into the system prompt. The
model was told it could emit more actions than the loop would run,
the loop dropped everything past the first with no log and no feedback
message, and the model re-issued steps it believed had run.

The fix has two halves and this file pins **both**: the agent renders the
limit it is handed, and ``AgentRuntime`` is what hands it over. An
earlier version of this test called ``agent.prepare(...,
max_actions_per_step=loop.max_actions_per_step)`` itself, so it stayed
green with ``AgentRuntime._loop_policy_for`` disabled — it proved only
that the agent can render a number someone else looked up.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.runtime import AgentRuntime
from spectragent.scripts.generate_all import _collect_registrars


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


async def test_runtime_carries_the_loops_limit_into_the_prompt() -> None:
    """End-to-end through ``execute()``: real agent, real loop, real runtime.

    The loop's limit is set to a value the agent would never pick on its
    own (its own default is 1) and that the shipped config does not already
    name, so deleting ``_loop_policy_for`` — or its call site in
    ``execute()`` — makes the prompt advertise 1 while the loop permits the
    probe value, and this fails.
    """
    composer = ConfigComposer(registrars=_collect_registrars())
    raw = Path("configs/browser_use_local.yaml").read_text()
    run = composer.compose(yaml.safe_load(raw))

    agent_id = run.ids("agent")[0]
    agent = run.instance("agent", agent_id)
    loop = agent.agent_loop
    shipped = loop.max_actions_per_step  # whatever the shipped config asks for

    # The agent must not own a second copy of the limit.
    assert not hasattr(agent, "max_actions_per_step")
    assert not hasattr(agent, "max_steps")

    # Diverge from every default in play so only the runtime hand-off can
    # produce the expected number: the probe must differ from the agent's own
    # fallback of 1 *and* from the shipped value, or the override below is a
    # no-op and a broken hand-off would still read as correct. Deriving it
    # keeps the gate honest when the shipped config is retuned — pinning the
    # shipped value to a literal made an ordinary config edit look like a
    # test failure.
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

    system_prompt = _message_text(memory.messages[0])
    advertised = {int(n) for n in re.findall(r"maximum of (\d+) actions", system_prompt)}
    assert advertised == {probe}, (
        f"prompt advertises {advertised}, loop enforces {loop.max_actions_per_step} "
        f"— AgentRuntime is not carrying the loop's limit to the agent"
    )


def test_agent_renders_whatever_limit_it_is_handed() -> None:
    """The agent half, in isolation: no hidden default of its own."""
    composer = ConfigComposer(registrars=_collect_registrars())
    raw = Path("configs/browser_use_local.yaml").read_text()
    run = composer.compose(yaml.safe_load(raw))

    agent = run.instance("agent", run.ids("agent")[0])
    seed = agent.prepare(task="t", action_descriptors=[], max_actions_per_step=7)
    text = _message_text(seed[0])
    assert {int(n) for n in re.findall(r"maximum of (\d+) actions", text)} == {7}
