"""Record a scripted browser run on the fixture site, then replay it offline (M30).

The acceptance tests of ``network_source: archive`` need a recording and its
replay that commit real steps — the replay log is attributed to the step
whose collection takes it — so they run the whole runtime twice, no LLM spend:

- **Recording** (:func:`record_config`): ``browser_use.dom_vision``
  (a DOM fragment and a screenshot each step), a loopback scripted model
  (``tests/_helpers/scripted_llm.py``) answering with ``navigate`` /
  ``click_coordinate`` / ``done`` only — never an index action: backend node
  ids differ from launch to launch, coordinates and URLs do not —
  ``replayability: pinned`` and ``browser.network`` + ``browser.storage`` +
  ``browser.dom_raw`` on the trace writer, against the mapped fixture site
  with a hermetic resolver (every unmapped name resolves to nothing).
- **Replay** (:func:`to_replay_config`): the same file with four changes —
  ``network_source: archive`` (isolation required, strict misses),
  ``base_time: from_archive`` (the recording's clock), the fixture's routing
  switches dropped from the backend (the replay's own isolation replaces
  them), and ``browser.replay_log`` in ``browser.network``'s place on the
  trace writer (a replay has no live traffic to record; it records its
  decisions). The model answers with the source run's own recorded
  completions (:func:`recorded_completions`), so the agent acts exactly as
  it did; the source run directory arrives as the task's ``replay_archive``.

:func:`step_signatures` is what "the same path" means step by step: the
page URL, the multiset of indexed elements (tag, the identifying
attributes, text — no backend node ids) and the screenshot's sha256.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.fixture_site import FixtureSite
from tests._helpers.scripted_llm import ScriptedLLMServer
from tests._helpers.trace_builder import as_chat

#: The recording's tier: seeded, stepped clock from the run's start.
RANDOM_SEED = 7
#: Attributes that identify an indexed element across launches.
SIGNATURE_ATTRIBUTES = ("id", "name", "type", "role", "aria-label", "href", "placeholder")


def reply(action: dict[str, Any], *, goal: str = "") -> str:
    """One scripted ``browser_use`` reply carrying one action."""
    return json.dumps(
        {
            "thinking": "",
            "evaluation_previous_goal": "",
            "memory": "",
            "next_goal": goal,
            "action": [action],
        }
    )


def navigate(url: str) -> str:
    return reply({"browser.navigate": {"url": url}}, goal=f"open {url}")


def click_at(x: int, y: int) -> str:
    return reply({"browser.click_coordinate": {"coordinate_x": x, "coordinate_y": y}})


def done(text: str = "done") -> str:
    return reply({"browser.done": {"extracted_content": text, "success": True}})


def record_config(
    *,
    site: FixtureSite,
    llm: ScriptedLLMServer,
    trace_dir: Path,
    max_steps: int = 30,
    clock: str | None = None,
) -> dict[str, Any]:
    """The recording run's configuration (see the module docstring).

    *clock* names pinned's clock (``shared`` / ``per_document``); ``None``
    leaves the tier's default. A replay inherits it with the rest of the block.
    """
    replayability: dict[str, Any] = {"replayability_id": "pinned", "random_seed": RANDOM_SEED}
    if clock is not None:
        replayability["clock"] = clock
    return {
        "agent": [
            {
                "agent_id": "browser_use.dom_vision",
                "id": "main",
                "use_thinking": True,
                "flash_mode": False,
                "agent_loop": {
                    "loop_id": "react",
                    "max_steps": max_steps,
                    "max_actions_per_step": 1,
                    "parse_retries_per_step": 0,
                },
                "llm": {
                    "model_type": "openai",
                    "model": llm.model,
                    "base_url": llm.base_url,
                    "api_key": "scripted",
                    "max_retries": 0,
                    "provider": {"name": "direct"},
                },
                "memory": {"memory_id": "simple", "max_messages": 200},
            }
        ],
        "environment": [
            {
                "env_type": "browser",
                "id": "browser_env",
                "capabilities": [
                    {
                        "capability_id": "browser",
                        "profile": "standard",
                        "viewport_width": 1280,
                        "viewport_height": 720,
                        # The fixture's pages are static and served from loopback: a short
                        # quiet window before each observation is enough (the default, 500 ms,
                        # is exercised by test_browser_settle_live.py and the rehearsal).
                        "settle_quiet_ms": FIXTURE_SETTLE_QUIET_MS,
                        "mouse_trajectory": {"trajectory_type": "instant"},
                        "typing_rhythm": {"rhythm_type": "instant"},
                        "replayability": replayability,
                        "network_source": {"source_id": "live"},
                        "dom_cleaner": {"cleaner_type": "browser_use"},
                        "dom_serializer": {"serializer_type": "text"},
                        "backend": {
                            "provider_id": "local_chrome",
                            "headless": True,
                            "extra_args": site.chrome_args(hermetic=True),
                        },
                    }
                ],
            }
        ],
        "hook": [
            {
                "hook_id": "trace_writer",
                "trace_dir": str(trace_dir),
                "archive": [
                    {"archiver_id": "browser.network"},
                    {"archiver_id": "browser.storage"},
                    {"archiver_id": "browser.dom_raw"},
                ],
            }
        ],
    }


def to_replay_config(
    raw: dict[str, Any],
    *,
    llm: ScriptedLLMServer,
    trace_dir: Path,
    isolation: str = "required",
) -> dict[str, Any]:
    """*raw* replaying its own recording: archive source, the recorded clock, no fixture routes.

    The trace writer records the replay log where the recording recorded its
    traffic: ``browser.replay_log`` replaces ``browser.network``, the other
    archivers stay.

    The fixture's name mapping is dropped either way, so a mapped name has
    no route to the fixture in a replay; only IP-literal and ``localhost``
    traffic has one (the ``egress`` and ``localhost`` pages), and an
    isolated replay's blackhole cuts both. ``isolation: none_acknowledged``
    — the negative control of those tests — keeps ``--no-proxy-server``, so
    it differs from an isolated replay only by the blackhole, and nothing
    reaches the machine's own proxy.
    """
    replay = copy.deepcopy(raw)
    agent = replay["agent"][0]
    agent["llm"].update({"model": llm.model, "base_url": llm.base_url})
    capability = replay["environment"][0]["capabilities"][0]
    capability["network_source"] = {
        "source_id": "archive",
        "isolation": isolation,
        "matcher": {"levels": ["exact", "normalized"], "site_rules": "tests/fixtures/replay_rules"},
        "miss_policy": {"miss_policy_id": "strict"},
    }
    capability["replayability"] = {**capability["replayability"], "base_time": "from_archive"}
    backend = capability["backend"]
    backend["extra_args"] = [
        arg
        for arg in backend.get("extra_args") or []
        if not arg.startswith(("--host-resolver-rules", "--no-proxy-server"))
    ]
    if isolation == "none_acknowledged":
        backend["extra_args"].append("--no-proxy-server")
    (writer,) = [hook for hook in replay["hook"] if hook["hook_id"] == "trace_writer"]
    writer["trace_dir"] = str(trace_dir)
    writer["archive"] = [
        {"archiver_id": "browser.replay_log"}
        if entry["archiver_id"] == "browser.network"
        else entry
        for entry in writer.get("archive") or []
    ]
    return replay


@dataclass
class Outcome:
    """One finished run: its result and its trace."""

    result: Any
    run_dir: Path

    @property
    def trace(self) -> RunTrace:
        return RunTrace(self.run_dir)

    def require_finished(self, steps: int, answer: str) -> None:
        """Fail — saying why — unless the run answered *answer* after exactly *steps* steps."""
        trace = self.trace
        result = self.result
        if result.answer == answer and trace.step_count == steps:
            return
        failures = [
            (record.step, action.name, outcome.error)
            for record in trace.step_records
            for action, outcome in zip(record.actions, record.action_results, strict=False)
            if not outcome.success
        ]
        raise AssertionError(
            f"run {self.run_dir.name} ended after {trace.step_count} of {steps} steps: "
            f"answer={result.answer!r} agent_done={result.agent_done} "
            f"env_status={result.env_status} error={result.error!r} "
            f"failed actions={failures}"
        )


async def run_task(
    raw: dict[str, Any],
    *,
    task: str,
    start_url: str | None = None,
    replay_archive: Path | None = None,
    storage_state: Any = None,
    timeout: float = 180,
) -> Outcome:
    """Compose *raw* with the real Composer and run it with the real runtime."""
    run = ConfigComposer(registrars=collect_registrars()).compose(raw)
    (writer,) = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]
    context: dict[str, Any] = {}
    if start_url:
        context["start_url"] = start_url
    if storage_state is not None:
        context["storage_state"] = storage_state
    if replay_archive is not None:
        context["replay_archive"] = str(replay_archive)
    result = await AgentRuntime.for_agent(run, "main").execute(
        task, task_id="replay--0001", timeout=timeout, env_context=context or None
    )
    assert writer.run_dir is not None, "the trace writer wrote no run"
    return Outcome(result=result, run_dir=writer.run_dir)


def recorded_completions(trace: RunTrace) -> list[str]:
    """Each step's completed model reply, as the scripted model gives it back."""
    replies: list[str] = []
    for step in range(trace.step_count):
        completed = [call for call in trace.llm_messages(step) if call.status == "completed"]
        assert completed, f"step {step} has no completed model call"
        completion = as_chat(completed[-1]).completion
        assert completion is not None
        value = completion.completion
        replies.append(value if isinstance(value, str) else json.dumps(value))
    return replies


def step_actions(trace: RunTrace) -> list[list[tuple[str, dict[str, Any]]]]:
    """The actions each step ran, by name and params."""
    return [
        [(action.name, dict(action.params)) for action in record.actions]
        for record in trace.step_records
    ]


@dataclass(frozen=True)
class StepSignature:
    """What a step observed: the URL, the indexed elements, the screenshot's digest."""

    url: str
    elements: tuple[tuple[str, int], ...]
    screenshot_sha256: str | None


def _text(node: Any) -> str:
    parts: list[str] = []

    def walk(item: Any) -> None:
        if getattr(item, "node_type", 1) == 3:
            parts.append(str(getattr(item, "node_value", "") or ""))
        for child in getattr(item, "children", None) or []:
            walk(child)

    walk(node)
    return " ".join(" ".join(parts).split())


def element_signature(node: Any) -> str:
    """Tag, identifying attributes and text of one indexed element — never its backend id."""
    attributes = getattr(node, "attributes", None) or {}
    picked = {name: attributes[name] for name in SIGNATURE_ATTRIBUTES if name in attributes}
    return json.dumps(
        [getattr(node, "tag_name", ""), picked, _text(node)], sort_keys=True, ensure_ascii=False
    )


def step_signatures(trace: RunTrace) -> list[StepSignature]:
    """One :class:`StepSignature` per committed step."""
    signatures: list[StepSignature] = []
    for step in range(trace.step_count):
        meta = trace.observation_fragment(step, "browser.browser_meta")
        dom = trace.observation_fragment(step, "browser.dom")
        shot = trace.screenshot(step)
        selector_map = getattr(dom, "selector_map", None) or {}
        elements = Counter(element_signature(node) for node in selector_map.values())
        signatures.append(
            StepSignature(
                url=str(getattr(meta, "url", "")),
                elements=tuple(sorted(elements.items())),
                screenshot_sha256=hashlib.sha256(shot).hexdigest() if shot else None,
            )
        )
    return signatures
