"""Record an index-action flow on the fixture site, then branch it in a real Chrome (M31).

The live acceptance of state branching (``docs/architecture_design.md`` §4.1
``BranchLoop``, §4.10 ``trace_branches``, §4.14 "恢复：重定位与校验") with no
LLM spend, building on M30's ``replay_driver``:

- **The recording** (:data:`RECORDING`, M30's :func:`~replay_driver.record_config`):
  ``browser_use`` in ``dom_vision`` mode, ``replayability: pinned``, the three
  browser archivers, on the mapped fixture site's ``branch`` pages — and,
  unlike M30's flows, **every step an index action**: a link, a text input, a
  radio button, a link, a link. The scripted model picks each index out of
  the prompt's DOM text (:func:`element_index`), because a fresh browser
  numbers the same element anew. (``browser_use`` offers no
  ``select_dropdown``, so the size is a radio button; a form *submission* is
  avoided — see the fixture page's note.)
- **The branches** (:func:`branch_config`): the same file as M30's replay
  (``network_source: archive``, ``base_time: from_archive``, no fixture
  routes) with ``agent_loop: branch`` (``prefix_replay``, ``strict``) and a
  ``trace_branches`` task loader over the recording's trace root. Each branch
  task runs through the real loader and :class:`BenchmarkRunner`
  (:func:`run_branch_task`), so the task context — setup, ``replay_archive``,
  ``checkpoint``, provenance — is the loader's, as in a sweep.

:func:`restoration_rate` is PRD §9's 恢复校验率: the share of restoration
checks (one per restored step, plus the checkpoint step's) that passed, with
the failed ones counted by reason.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spectragent.core.benchmark_runner import BenchmarkRunner, TaskOutcome
from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.reader import RunTrace
from spectragent.scripts.generate_all import _collect_registrars
from tests._helpers import replay_driver as driver
from tests._helpers.scripted_llm import Reply, ScriptedLLMServer

#: The recording's task text.
TASK = "Put a large blue kettle in the cart, then check the saved state."
#: What the recording types into the item field.
ITEM = "blue kettle"
#: The step the branches fork at: the cart page, before the recording opened
#: the check page. Steps 0..3 (link, type, radio, link) are restored.
CHECKPOINT = 4


# ── the scripted model: indexes read from the prompt ─────────────────


def _last_user_text(body: dict[str, Any]) -> str:
    """The text of the request's last user message — this step's browser state."""
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(
            part.get("text", "") for part in content or [] if part.get("type") == "text"
        )
    raise LookupError("the request carries no user message")


def element_index(body: dict[str, Any], element_id: str) -> int:
    """The ``[index]`` the prompt's DOM gives the element whose ``id`` is *element_id*.

    The text serializer prints ``[N]<tag id=… />``; ``N`` is the element's
    handle in this browser, so it is read from every prompt anew.
    """
    text = _last_user_text(body)
    pattern = re.compile(
        rf"\[(\d+)\]<[A-Za-z][\w-]*\b[^\n]*?\bid=[\"']?{re.escape(element_id)}[\"']?(?=[\s/>])"
    )
    match = pattern.search(text)
    if match is None:
        dom = text[text.find("DOM:") :] if "DOM:" in text else text[-2000:]
        raise LookupError(f"no element id={element_id} in the prompt's DOM:\n{dom}")
    return int(match.group(1))


def click(element_id: str) -> Reply:
    return lambda body: driver.reply(
        {"browser.click": {"index": element_index(body, element_id)}}, goal=f"click #{element_id}"
    )


def type_into(element_id: str, text: str) -> Reply:
    return lambda body: driver.reply(
        {"browser.type": {"index": element_index(body, element_id), "text": text}},
        goal=f"type into #{element_id}",
    )


#: The recorded flow: open the form, type the item, pick the size, add to cart, check, done.
RECORDING: list[Reply] = [
    click("open-form"),
    type_into("item", ITEM),
    click("size-large"),
    click("add-to-cart"),
    click("check"),
    driver.done("checked"),
]
#: A teacher that remembers (cookie + localStorage, from JS), then looks.
REMEMBER_THEN_CHECK: list[Reply] = [click("mark"), click("check")]
#: A teacher that only looks.
CHECK: list[Reply] = [click("check"), driver.done("checked")]


# ── the branch configuration and runs ────────────────────────────────


def branch_config(
    record_raw: dict[str, Any],
    *,
    llm: ScriptedLLMServer,
    trace_dir: Path,
    sources_root: Path,
    step: int = CHECKPOINT,
    branches_per_step: int = 1,
    continue_steps: int = 1,
) -> dict[str, Any]:
    """*record_raw* as a branch sweep over its own recordings (see the module docstring)."""
    raw = driver.to_replay_config(record_raw, llm=llm, trace_dir=trace_dir)
    agent = raw["agent"][0]
    agent["agent_loop"] = {
        **agent["agent_loop"],
        "loop_id": "branch",
        "continue_steps": continue_steps,
        "restorer": {"restorer_id": "prefix_replay", "verify": "strict"},
    }
    raw["task_loader"] = [
        {
            "benchmark_name": "trace_branches",
            "data_dir": str(sources_root),
            "step_selector": {"selector_id": "explicit", "steps": [step]},
            "branches_per_step": branches_per_step,
        }
    ]
    return raw


def run_branch_task(raw: dict[str, Any], task_id: str, *, timeout: float = 240) -> TaskOutcome:
    """Run one branch task of *raw* the way ``spectragent-bench`` does: its loader, its runner."""
    composer = ConfigComposer(registrars=_collect_registrars())
    loader = composer.compose(raw).single("task_loader")
    runner = BenchmarkRunner(loader=loader, compose=lambda: composer.compose(raw), timeout=timeout)
    return asyncio.run(runner.run_task(task_id))


def require_ran(outcome: TaskOutcome) -> RunTrace:
    """The branch's trace — or a failure saying how the task ended."""
    result = outcome.result
    if outcome.error is not None or outcome.trace_dir is None or result is None:
        raise AssertionError(f"branch task {outcome.task_id} did not run: {outcome.error}")
    trace = RunTrace(outcome.trace_dir)
    diverged = [
        (
            record.step,
            record.error_phase,
            record.restoration.reasons if record.restoration else None,
        )
        for record in trace.step_records
        if record.error_phase
    ]
    if result.agent_done not in ("done", "max_steps") or diverged:
        raise AssertionError(
            f"branch task {outcome.task_id} ended {result.agent_done}/{result.env_status}: "
            f"error={result.error!r} failed steps={diverged}"
        )
    return trace


# ── what a branch trace says ─────────────────────────────────────────


@dataclass
class RestorationRate:
    """PRD §9 恢复校验率 over a set of branch runs."""

    ok: int = 0
    total: int = 0
    #: Failed checks by reason (a check with several reasons counts for each).
    by_reason: Counter[str] = field(default_factory=Counter)

    @property
    def rate(self) -> float:
        return self.ok / self.total if self.total else 0.0

    def summary(self) -> str:
        reasons = ", ".join(f"{reason}={n}" for reason, n in sorted(self.by_reason.items()))
        return f"{self.ok}/{self.total} checks passed ({self.rate:.0%}){'; ' + reasons if reasons else ''}"


def restoration_rate(trace_dirs: Iterable[Path]) -> RestorationRate:
    """Every restoration check the branch runs recorded, and how many passed."""
    rate = RestorationRate()
    for trace_dir in trace_dirs:
        for record in RunTrace(trace_dir).step_records:
            check = record.restoration
            if check is None:
                continue
            rate.total += 1
            if check.verdict == "ok":
                rate.ok += 1
            rate.by_reason.update(check.reasons)
    return rate


def page_text(trace: RunTrace, step: int) -> str:
    """The serialized DOM a step observed (what the agent read)."""
    dom = trace.observation_fragment(step, "browser.dom")
    tree = getattr(dom, "element_tree", None)
    return str(getattr(tree, "serialized_dom", ""))


def storage_names(trace: RunTrace) -> set[str]:
    """Every cookie name and ``localStorage`` key the run's ``browser.storage`` evidence holds."""
    names: set[str] = set()
    for _step, record in trace.iter_archive("browser.storage", include_trailing=True):
        document = json.loads(trace.read_extra(record.path))
        names.update(str(cookie.get("name")) for cookie in document.get("cookies") or [])
        for origin in document.get("local") or []:
            names.update(str(key) for key in (origin.get("items") or {}))
    return names


def components(trace: RunTrace) -> dict[str, Any]:
    """The components the trace says ran: agent, loop, cleaner, serializer, backend, model route."""
    resolved = trace.config.resolved
    agent = (resolved.get("agent") or [{}])[0]
    capability = ((resolved.get("environment") or [{}])[0].get("capabilities") or [{}])[0]
    llm = agent.get("llm") or {}
    return {
        "agent": trace.config.agent_id,
        "loop": trace.config.loop_id,
        "restorer": (agent.get("agent_loop") or {}).get("restorer", {}).get("restorer_id"),
        "cleaner": (capability.get("dom_cleaner") or {}).get("cleaner_type"),
        "serializer": (capability.get("dom_serializer") or {}).get("serializer_type"),
        "backend": (capability.get("backend") or {}).get("provider_id"),
        "network_source": (capability.get("network_source") or {}).get("source_id"),
        "route": f"{llm.get('model_type')}/{(llm.get('provider') or {}).get('name')} "
        f"model={trace.config.llm_model} base_url={llm.get('base_url')}",
    }
