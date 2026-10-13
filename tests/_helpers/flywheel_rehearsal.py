"""The metrics chain on the fixture site with scripted models — no model spend (arch §4.16 "演练").

One command runs the whole chain and writes the rehearsal report::

    uv run python -m tests._helpers.flywheel_rehearsal --out /tmp/af-rehearsal

``test_rehearsal_live.py`` calls :func:`run_rehearsal`, the function behind it, in the test
process and reads what it ran: the chain root and :meth:`Rehearsal.summary` (the command
writes that summary as ``rehearsal.json``). What it does:

- starts the fixture site (mapped hosts) and one loopback scripted model per
  run stage (:class:`Router`: UI-TARS replies routed by the task's marker in
  the prompt and the request's assistant-turn count — the step number; the
  branch stage decides the shop task's checkpoint three ways in loader order:
  as recorded, a POST form submission, as recorded again);
- writes the task data (``online_mind2web`` shape) for four tasks on three
  sites — :data:`TASKS`: a deterministic shop flow with a GET form
  submission, a sign-in page that sends planted credentials every way a page
  can, a site whose click navigates to a ``performance.timeOrigin`` URL
  (pinned does not fix it: replay must miss the document), and a page a
  Cloudflare challenge answers;
- adapts the **shipped** configurations (``configs/flywheel/``: the UI-TARS
  recording, its same-path replay, its branch sweep, the SFT export) the way
  the replay, branch and export drivers do — the model's address and key, the fixture's routing
  switches, the task data directory, absolute paths for the repository's own
  files — plus a control group (the recording with ``archive: []``); the
  export reads every run (``require_eval: null``) and decontaminates against
  nothing (the sources are the benchmark's own tasks);
- writes the chain spec (``evidence: rehearsal``, the planted credentials as
  literals) and runs it with ``spectragent.flywheel.chain.run_chain`` — the
  real ``spectragent-bench`` and ``spectragent-export`` in this process.

Nothing leaves the machine: the recordings resolve every unmapped name to
nothing (``chrome_args(hermetic=True)``), the replays are isolated, and
:func:`audit_connections` records the peer of every socket this process
connects (the scripted models, the fixture, Chrome's DevTools — loopback).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from spectragent.flywheel.chain import ChainResult, ChainSpec, run_chain
from tests._helpers.fixture_site import (
    FLYWHEEL_PASSWORD,
    FLYWHEEL_TOKEN,
    FixtureSite,
    flywheel_point,
)
from tests._helpers.scripted_llm import ScriptedLLMServer

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "configs" / "flywheel"
#: The teacher API key every stage's YAML carries — a planted credential the report searches for.
TEACHER_KEY = "sk-rehearsal-teacher-0000000000000000"
#: Where the task text names its task, for the scripted models to route on.
MARKER = re.compile(r"\[rehearsal task ([\w-]+)\]")
#: The per-task timeout every run stage gets.
TASK_TIMEOUT_S = 120


def _say(thought: str, action: str) -> str:
    return f"Thought: {thought}\nAction: {action}"


def _click(name: str, thought: str) -> str:
    x, y = flywheel_point(name)
    return _say(thought, f"click(point='({x}, {y})')")


def _type(name: str, text: str, thought: str) -> str:
    x, y = flywheel_point(name)
    return _say(thought, f"type(content='{text}', point='({x}, {y})')")


def _finished(content: str) -> str:
    return _say("Done.", f"finished(content='{content}')")


@dataclass(frozen=True)
class RehearsalTask:
    """One task: its site and entry page, its text, and the recorded replies by step."""

    task_id: str
    scenario: str
    path: str
    instruction: str
    replies: tuple[str, ...]

    @property
    def text(self) -> str:
        return f"{self.instruction} [rehearsal task {self.task_id}]"


SHOP_KETTLE = RehearsalTask(
    "fw-shop-kettle",
    "flywheel_shop",
    "/flywheel_shop/start.html",
    "Search the kettle shop for 'kettle' and report the Blue kettle's price.",
    (
        _type("query", "kettle", "Type the search term."),
        _click("search", "Submit the search form."),
        _click("product", "Open the Blue kettle."),
        _finished("Blue kettle: 30 EUR"),
    ),
)
SHOP_LOGIN = RehearsalTask(
    "fw-shop-login",
    "flywheel_shop",
    "/flywheel_shop/login.html",
    "Check that the kettle shop's sign-in page loads.",
    (_finished("signed in"),),
)
#: In the order the recording sweeps them. The sign-in task right before the
#: drift task is the order that once hung drift's step-1 model call in TCP
#: connect, at any timeout (a provider left each request's SDK client to the
#: garbage collector, whose late ``aclose()`` unregistered a reused descriptor
#: from the event loop — ``docs/architecture_design.md`` §4.7).
TASKS: tuple[RehearsalTask, ...] = (
    SHOP_KETTLE,
    SHOP_LOGIN,
    RehearsalTask(
        "fw-drift-story",
        "flywheel_drift",
        "/flywheel_drift/start.html",
        "Open today's story on the news site.",
        (_click("drift", "Open today's story."), _finished("story open")),
    ),
    RehearsalTask(
        "fw-blocked-front",
        "flywheel_blocked",
        "/flywheel_blocked/start.html",
        "Open the front page of the blocked site.",
        (_finished("blocked by a challenge"),),
    ),
)
#: The branch stage forks the shop flow at its results page (step 2) …
BRANCH_STEP = 2
#: … three times, in loader order: as recorded, the POST "quick add" form, as recorded again.
BRANCH_DECISIONS: tuple[str, ...] = (
    SHOP_KETTLE.replies[BRANCH_STEP],
    _click("quick_add", "Add it to the cart straight away."),
    SHOP_KETTLE.replies[BRANCH_STEP],
)
#: What the report searches for: the teacher's key and the sign-in page's credentials.
PLANTED: tuple[tuple[str, str], ...] = (
    ("teacher api key", TEACHER_KEY),
    ("site password", FLYWHEEL_PASSWORD),
    ("site session token", FLYWHEEL_TOKEN),
)
RUN_STAGES = ("record", "control", "same_path", "branch")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


class Router:
    """One stage's scripted model: the reply for (task marker, assistant turns so far).

    Every stage replays a task's recorded replies by step; the branch stage
    pops :data:`BRANCH_DECISIONS` for the shop flow's checkpoint, in the order
    the loader runs its branches. A request it cannot route raises — the
    scripted server records that as an error the test asserts is absent.
    """

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.tasks = {task.task_id: task for task in TASKS}
        self.branch_queue = list(BRANCH_DECISIONS)
        self.routed: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def __call__(self, body: dict[str, Any]) -> str:
        messages = body.get("messages") or []
        found = MARKER.search("\n".join(_message_text(m) for m in messages))
        if found is None:
            raise LookupError("no rehearsal task marker in the prompt")
        task = self.tasks[found.group(1)]
        turn = sum(1 for message in messages if message.get("role") == "assistant")
        with self._lock:
            self.routed.append((task.task_id, turn))
            if self.stage == "branch" and task is SHOP_KETTLE and turn == BRANCH_STEP:
                return self.branch_queue.pop(0)
        if turn >= len(task.replies):
            raise LookupError(f"{task.task_id} has no recorded reply for step {turn}")
        return task.replies[turn]


def write_task_data(directory: Path, site: FixtureSite) -> Path:
    """The tasks as an ``online_mind2web`` data directory (``data_info.json`` + the ``All`` split)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "data_info.json").write_text(json.dumps({"split": {"All": "tasks.json"}}))
    records = [
        {
            "task_id": task.task_id,
            "benchmark": "online_mind2web",
            "env_type": "browser",
            "task_text": task.text,
            "start_url": site.url(task.scenario).rsplit("/", 2)[0] + task.path,
            "difficulty": "easy",
            "metadata": {"reference_length": len(task.replies)},
        }
        for task in TASKS
    ]
    (directory / "tasks.json").write_text(json.dumps(records, indent=2))
    return directory


def _shipped(name: str) -> dict[str, Any]:
    return yaml.safe_load((SHIPPED / name).read_text())


def _point_llm(raw: dict[str, Any], llm: ScriptedLLMServer) -> None:
    raw["agent"][0]["llm"].update(
        {
            "model": llm.model,
            "base_url": llm.base_url,
            "api_key": TEACHER_KEY,
            "max_retries": 0,
        }
    )


def _capability(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["environment"][0]["capabilities"][0]


def _absolute_rules(raw: dict[str, Any]) -> None:
    matcher = _capability(raw)["network_source"].get("matcher") or {}
    if matcher.get("site_rules"):
        matcher["site_rules"] = str(REPO / matcher["site_rules"])


def stage_configs(
    site: FixtureSite, llms: dict[str, ScriptedLLMServer], data_dir: Path
) -> dict[str, dict[str, Any]]:
    """The shipped configurations, adapted to the fixture (module docstring)."""
    record = _shipped("record_ui_tars_pinned.yaml")
    _point_llm(record, llms["record"])
    _capability(record)["backend"]["extra_args"] = site.chrome_args(hermetic=True)
    record["task_loader"] = [
        {"benchmark_name": "online_mind2web", "subset": "All", "data_dir": str(data_dir)}
    ]
    control = copy.deepcopy(record)
    _point_llm(control, llms["control"])
    (writer,) = [hook for hook in control["hook"] if hook["hook_id"] == "trace_writer"]
    writer["archive"] = []  # the control group: the same tier, no evidence

    same_path = _shipped("same_path_ui_tars_archive.yaml")
    _point_llm(same_path, llms["same_path"])
    _absolute_rules(same_path)

    branch = _shipped("branch_ui_tars_archive.yaml")
    _point_llm(branch, llms["branch"])
    _absolute_rules(branch)
    (loader,) = branch["task_loader"]
    loader["step_selector"] = {"selector_id": "explicit", "steps": [BRANCH_STEP]}
    loader["branches_per_step"] = len(BRANCH_DECISIONS)

    export = _shipped("export_sft.yaml")
    (exporter,) = export["exporter"]
    exporter["filters"]["require_eval"] = None
    exporter["decontamination"] = {"against": []}
    exporter["student_run_config"] = str(REPO / exporter["student_run_config"])
    # The leak scan reads the tasks' references through the document's loaders:
    # the fixture's tasks, not the packaged split.
    loader = {"benchmark_name": "online_mind2web", "subset": "All", "data_dir": str(data_dir)}
    export["eval_loader"] = [dict(loader)]
    export["task_loader"] = [dict(loader)]
    return {
        "record": record,
        "control": control,
        "same_path": same_path,
        "branch": branch,
        "export": export,
    }


def chain_spec(out: Path, configs: dict[str, Path]) -> ChainSpec:
    """The rehearsal's chain spec (written to ``<out>/chain.yaml`` by :func:`run_rehearsal`)."""
    return ChainSpec.model_validate(
        {
            "name": "fixture-rehearsal",
            "root": out / "chain",
            "evidence": "rehearsal",
            "tasks": {
                "benchmark": "online_mind2web",
                "subset": "All",
                "task_ids": [task.task_id for task in TASKS],
            },
            **{
                stage: {"config": configs[stage], "timeout": TASK_TIMEOUT_S} for stage in RUN_STAGES
            },
            "export": {"config": configs["export"], "repeat": 2},
            "report": {"planted": [{"label": label, "value": value} for label, value in PLANTED]},
        }
    )


@dataclass
class Egress:
    """Every peer this process connected a socket to, while auditing."""

    peers: list[Any] = field(default_factory=list)

    def non_loopback(self) -> list[Any]:
        return [peer for peer in self.peers if not _is_loopback(peer)]


def _is_loopback(peer: Any) -> bool:
    if isinstance(peer, str):  # an AF_UNIX path
        return True
    host = peer[0] if isinstance(peer, tuple) and peer else peer
    return str(host) in ("127.0.0.1", "::1", "localhost") or str(host).startswith("127.")


@contextlib.contextmanager
def audit_connections() -> Iterator[Egress]:
    """Record the peer of every ``socket.connect`` / ``connect_ex`` in this process."""
    egress = Egress()
    original, original_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> Any:
        egress.peers.append(address)
        return original(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        egress.peers.append(address)
        return original_ex(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    try:
        yield egress
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]
        socket.socket.connect_ex = original_ex  # type: ignore[method-assign]


@contextlib.contextmanager
def _loopback_bypasses_the_proxy() -> Iterator[None]:
    """The scripted models are on loopback: a machine-wide proxy must not see them."""
    saved = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    for key in saved:
        os.environ[key] = "127.0.0.1,localhost"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass
class Rehearsal:
    """What a rehearsal ran: the chain's result, the fixture's request log, each model's routing."""

    out: Path
    result: ChainResult
    site_requests: list[Any]
    routed: dict[str, list[tuple[str, int]]]
    model_errors: dict[str, list[str]]
    egress: Egress

    def summary(self) -> dict[str, Any]:
        """What the run left beside the chain root (``rehearsal.json``): stages, routing, egress."""
        return {
            "chain_root": str(self.result.layout.root),
            "stages": {
                stage: {
                    "exit_codes": outcome.exit_codes,
                    "runs": outcome.runs,
                    "error": outcome.error,
                }
                for stage, outcome in self.result.stages.items()
            },
            "routed": {
                stage: [list(item) for item in items] for stage, items in self.routed.items()
            },
            "model_errors": self.model_errors,
            "egress": {
                "connections": len(self.egress.peers),
                "non_loopback": [str(peer) for peer in self.egress.non_loopback()],
            },
            "served_hosts": sorted({request.host for request in self.site_requests}),
        }


def run_rehearsal(out: Path) -> Rehearsal:
    """Run the whole chain on the fixture under *out* (which must not hold an earlier rehearsal)."""
    out = out.resolve()
    if (out / "chain").exists():
        raise FileExistsError(f"{out / 'chain'} exists — a rehearsal writes a fresh chain root")
    routers = {stage: Router(stage) for stage in RUN_STAGES}
    with contextlib.ExitStack() as stack:
        egress = stack.enter_context(audit_connections())
        stack.enter_context(_loopback_bypasses_the_proxy())
        site = stack.enter_context(FixtureSite(mapped=True))
        llms = {
            stage: stack.enter_context(
                ScriptedLLMServer([routers[stage]], model="ui-tars-scripted")
            )
            for stage in RUN_STAGES
        }
        data_dir = write_task_data(out / "data" / "online_mind2web", site)
        configs: dict[str, Path] = {}
        (out / "configs").mkdir(parents=True, exist_ok=True)
        for stage, raw in stage_configs(site, llms, data_dir).items():
            configs[stage] = out / "configs" / f"{stage}.yaml"
            configs[stage].write_text(yaml.safe_dump(raw, sort_keys=False))
        spec = chain_spec(out, configs)
        (out / "chain.yaml").write_text(
            yaml.safe_dump(json.loads(spec.model_dump_json()), sort_keys=False)
        )
        result = run_chain(spec, log_level="WARNING")
        return Rehearsal(
            out=out,
            result=result,
            site_requests=site.requests,
            routed={stage: list(router.routed) for stage, router in routers.items()},
            model_errors={stage: list(llm.errors) for stage, llm in llms.items()},
            egress=egress,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests._helpers.flywheel_rehearsal",
        description="Run the flywheel metrics chain on the fixture site with scripted models.",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="A new directory for the rehearsal."
    )
    args = parser.parse_args(argv)
    rehearsal = run_rehearsal(args.out)
    summary = json.dumps(rehearsal.summary(), indent=2, sort_keys=True)
    (rehearsal.out / "rehearsal.json").write_text(summary + "\n")
    report = rehearsal.result.layout.report_dir
    print(f"chain root: {rehearsal.result.layout.root}")
    print(f"report: {report / 'report.json'}\nreport: {report / 'report.md'}")
    outside = rehearsal.egress.non_loopback()
    print(f"python-side connections: {len(rehearsal.egress.peers)}, non-loopback: {outside}")
    errors = {stage: found for stage, found in rehearsal.model_errors.items() if found}
    if errors or outside:
        print(f"scripted-model errors: {errors}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
