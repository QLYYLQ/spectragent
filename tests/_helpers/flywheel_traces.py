"""Chain roots for the flywheel metrics, committed by the real WAL with known answers (M33).

Every run is committed the way production commits it (``trace_builder.py``'s
rule — nothing here spells a trace path or a JSON shape): ``WALWriter`` for
steps and fragments, M29's own ``browser.network`` session for a recording's
network evidence (``replay_archive.RecordingScript``) and its replay session
for a replay's log (``ReplayLogSession`` over a real ``ReplayLog``, folding
included), ``encode_archive_file`` for every archive file. The datasets are
written as the files an export publishes (the metrics read them as files).

:func:`known_chain` writes one chain root whose every measure has a known
answer, stated next to the run that produces it:

- **record** — ``rec-shop`` (shop.test, 3 steps: documents, a stylesheet
  written twice — the second a WARC revisit —, a large script streamed twice
  — one content-addressed blob —, a ``too_large`` and a failed request;
  environment time 1000 and 1200 ms on steps 1 and 2), ``rec-blocked``
  (blocked.test: one 403 document with Cloudflare's challenge marks) and
  ``rec-broken`` (broken.test: step 0's observation failed, status error);
- **control** — ``ctl-shop`` at the same tier, no archive: 600 and 700 ms;
- **same_path** — ``sp-shop-b0`` / ``sp-shop-b1`` (rec-shop at step 2: every
  check ok; b1's step-1 screenshot differs from the source's, so the strict
  measure fails there; both end on the same terminal screenshot) and
  ``sp-blocked-b0`` (rec-blocked at step 0: ``warn`` — document miss,
  screenshot distance);
- **branch** — rec-shop at step 1, three branches: ``br-b0`` clicks (100,
  200), meets one subresource near miss, and its terminal observation's
  collection one more (a late script); ``br-b1`` clicks (300, 400) — a POST
  document miss — after a restored step whose retarget was not found, and its
  close-out folds three image misses; ``br-b2`` repeats b0 (a duplicate) after
  a document miss *before* the checkpoint (restoration's, not coverage's);
  plus ``broken``, a run whose manifest does not read back;
- **datasets** — ``first`` and ``second``, byte-identical: 5 samples, one
  ``action_failed``, one duplicate flagged.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.capabilities.browser.archive import NetworkArchiver
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayCandidate,
    ReplayLog,
    ReplayLogEntry,
)
from spectragent.core.trace.encoding import EncodedFragment, encode_archive_file, encode_fragment
from spectragent.core.trace.models import RunStatus, TaskSetupRecord, TraceConfig, TraceNegotiation
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.restoration import (
    BranchProvenance,
    RestorationCheck,
    RestorationEvidence,
    RestoredAction,
)
from spectragent.schemas.step_record import StepRecord
from tests._helpers.archive_recorder import open_session
from tests._helpers.replay_archive import RecordingScript
from tests._helpers.trace_builder import agent_result

SHOP, BLOCKED, BROKEN = "shop.test", "blocked.test", "broken.test"
#: The tier every run of :func:`known_chain` records in its resolved configuration.
PINNED: dict[str, Any] = {
    "environment": [
        {
            "env_type": "browser",
            "capabilities": [
                {"capability_id": "browser", "replayability": {"replayability_id": "pinned"}}
            ],
        }
    ]
}
#: Cloudflare's marks on a challenge page.
CHALLENGE = {"Server": "cloudflare", "CF-RAY": "8a1b2c3d4e5f-AMS", "cf-mitigated": "challenge"}
#: A stylesheet the shop serves twice (the second is a revisit record). Incompressible, so
#: the revisit saves most of the full record's stored length (the WARC is gzip).
STYLE = b"".join(hashlib.sha256(str(i).encode()).hexdigest().encode() for i in range(40))
#: A script over the inline tier, streamed twice (one content-addressed blob).
BIG = b"/* big */" * 600


def png(color: int) -> bytes:
    """An 8x8 grey PNG — the colour is the screenshot's identity."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (color, color, color)).save(buffer, format="PNG")
    return buffer.getvalue()


def screenshot(step: int, color: int) -> EncodedFragment:
    encoded = encode_fragment(
        step, ScreenshotObs(image_bytes=png(color), viewport_width=8, viewport_height=8)
    )
    assert encoded is not None
    return encoded


def click(x: int, y: int) -> Action:
    return Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": y})


def done(text: str = "done") -> Action:
    return Action(name="browser.done", params={"extracted_content": text, "success": True})


def check(
    step: int, *reasons: str, actions: Sequence[RestoredAction] = (), document_misses: int = 0
) -> RestorationCheck:
    """A restorer's check of *step*: ``ok`` without reasons, ``warn`` with them."""
    return RestorationCheck(
        step=step,
        verdict="warn" if reasons else "ok",
        reason=reasons[0] if reasons else None,  # type: ignore[arg-type]
        reasons=list(reasons),  # type: ignore[arg-type]
        evidence=[
            RestorationEvidence(
                capability_id="browser",
                screenshot_distance=12 if "screenshot_distance" in reasons else 0,
                document_misses=document_misses,
            )
        ],
        actions=list(actions),
    )


def miss(
    url: str,
    *,
    method: str = "GET",
    resource_type: str = "XHR",
    candidates: Sequence[str] = (),
    document: bool = False,
    reason: str = "no_match",
) -> ReplayLogEntry:
    """A replay miss as the replay server logs it (a main-frame document miss diverges)."""
    return ReplayLogEntry(
        kind="miss",
        captured_at=1.0,
        cycle=1,
        method=method,
        url=url,
        resource_type="Document" if document else resource_type,
        frame="main" if document else None,
        request_class="document" if document else "subresource",
        reason=reason,  # type: ignore[arg-type]
        answer="failed" if document else "fulfilled",
        status=None if document else 404,
        candidates=[
            ReplayCandidate(record_id=f"rec-{i}", method=method, url=c, score=0.5, consumed=False)
            for i, c in enumerate(candidates)
        ],
        diverged=document,
    )


def hit(url: str) -> ReplayLogEntry:
    return ReplayLogEntry(
        kind="hit",
        captured_at=1.0,
        cycle=1,
        method="GET",
        url=url,
        resource_type="Document",
        record_id="rec-0",
        run_id="rec-shop",
        level="exact",
        answer="fulfilled",
        status=200,
    )


@dataclass
class Step:
    """One committed step and what its commit took."""

    origin: str = "policy"
    actions: Sequence[Action] = ()
    duration_ms: int | None = None
    llm_latency_ms: int | None = None
    error_phase: str = ""
    color: int | None = None
    restoration: RestorationCheck | None = None
    replay: Sequence[ReplayLogEntry] = ()
    network: Callable[[RecordingScript], Awaitable[Any]] | None = None

    def record(self, step: int) -> StepRecord:
        return StepRecord(
            step=step,
            observation_summary=f"page {step}",
            llm_output="Thought: go\nAction: (recorded)",
            actions=list(self.actions),
            action_results=[ActionResult(success=True) for _ in self.actions],
            duration_ms=self.duration_ms,
            llm_latency_ms=self.llm_latency_ms,
            error_phase=self.error_phase,
            origin=self.origin,  # type: ignore[arg-type]
            restoration=self.restoration,
        )

    def fragments(self, step: int) -> list[EncodedFragment]:
        return [screenshot(step, self.color)] if self.color is not None else []


def config(
    *,
    task_id: str,
    site: str,
    loop: str = "react",
    branch: BranchProvenance | None = None,
    archived: bool = True,
    resolved: dict[str, Any] | None = None,
) -> TraceConfig:
    return TraceConfig(
        agent_id="ui_tars",
        loop_id=loop,
        env_type="browser",
        llm_model="gpt-4o",
        benchmark="online_mind2web",
        subset="All",
        task=f"task {task_id}",
        task_id=task_id,
        resolved=resolved if resolved is not None else PINNED,
        negotiation=TraceNegotiation(
            active_fragments=["browser.screenshot"],
            active_actions=["browser.click_coordinate", "browser.done"],
            active_archivers=["browser.network"] if archived else [],
            archive_bindings={"browser.network": "browser"} if archived else {},
        ),
        env_evidence_recorded=archived,
        task_setup=TaskSetupRecord(
            context={"start_url": f"http://{site}/"}, redactions=RedactionLedger()
        ),
        branch=branch,
    )


async def write_recorded(
    directory: Path,
    run_id: str,
    steps: Sequence[Step],
    *,
    task_id: str,
    site: str,
    status: RunStatus = RunStatus.COMPLETED,
) -> Path:
    """A recording: each step's network evidence from M29's session, then its commit."""
    script = RecordingScript(directory, run_id, config=config(task_id=task_id, site=site))
    for index, spec in enumerate(steps):
        if spec.network is not None:
            await spec.network(script)
        await script.commit(index, spec.record(index), spec.fragments(index))
    return script.finish(status)


def write_plain(
    directory: Path,
    run_id: str,
    steps: Sequence[Step],
    *,
    task_id: str,
    site: str,
    resolved: dict[str, Any] | None = None,
) -> Path:
    """A run without an archive (a control group's recording)."""
    wal = WALWriter(directory, run_id)
    wal.init_run(config(task_id=task_id, site=site, archived=False, resolved=resolved))
    for index, spec in enumerate(steps):
        wal.commit_step(index, spec.record(index), fragments=spec.fragments(index))
    wal.write_result(agent_result())
    wal.finalize(RunStatus.COMPLETED)
    return wal.run_dir


async def write_replayed(
    directory: Path,
    run_id: str,
    steps: Sequence[Step],
    *,
    task_id: str,
    site: str,
    branch: BranchProvenance,
    trailing: Sequence[ReplayLogEntry] = (),
    repeat_limit: int = 100,
) -> Path:
    """A replayed run (a branch): each step's replay log through the real replay session."""
    wal = WALWriter(directory, run_id)
    wal.init_run(config(task_id=task_id, site=site, loop="branch", branch=branch))
    log = ReplayLog(repeat_limit=repeat_limit)
    session, _ = open_session(NetworkArchiver(require_replayable=False), replay_log=log)
    for index, spec in enumerate(steps):
        for entry in spec.replay:
            log.append(entry)
        files = await session.collect()
        wal.commit_step(
            index,
            spec.record(index),
            fragments=spec.fragments(index),
            extra_files=[encode_archive_file(item, step=index) for item in files],
        )
    if trailing:
        for entry in trailing:
            log.append(entry)
        files = await session.collect()
        wal.commit_trailing(extra_files=[encode_archive_file(item, trailing=0) for item in files])
    wal.write_result(agent_result())
    wal.finalize(RunStatus.COMPLETED)
    return wal.run_dir


def provenance(source: str, checkpoint: int, index: int, task_id: str) -> BranchProvenance:
    return BranchProvenance(
        source_run_id=source,
        source_trace=source,
        source_benchmark="online_mind2web",
        source_subset="All",
        source_task_id=task_id,
        checkpoint_step=checkpoint,
        branch_index=index,
        branch_group_id=f"{source}/s{checkpoint}",
    )


def write_dataset(directory: Path, *, samples: Sequence[str], duplicates: int) -> Path:
    """A published dataset's files: ``manifest.json`` and one split's samples (as an export writes)."""
    directory.mkdir(parents=True)
    lines = [
        json.dumps(
            {"sample": {"sample_id": f"run/s{i}", "labels": {"step_status": status}}, "render": {}},
            sort_keys=True,
        )
        for i, status in enumerate(samples)
    ]
    (directory / "train.samples.jsonl").write_text("\n".join(lines) + "\n")
    (directory / "train.jsonl").write_text("\n".join('{"messages":[]}' for _ in samples) + "\n")
    manifest = {
        "format": "af-sft-1",
        "samples": len(samples),
        "split": {"samples": {"train": len(samples)}},
        "steps": {"untranslatable": {"no_coordinate": 1}, "skipped": {}},
        "sources": {
            "exported": [{"path": "record/rec-shop"}, {"path": "branch/br-b0"}],
            "skipped": {},
            "filtered": {"branch/br-b1": "1 main-frame document miss(es) in replay"},
        },
        "duplicates": duplicates,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return directory


@dataclass
class KnownChain:
    root: Path
    runs: dict[str, Path] = field(default_factory=dict)


async def _shop_step0(script: RecordingScript) -> None:
    await script.exchange(f"http://{SHOP}/", resource_type="Document", body=b"<p>shop</p>")
    await script.exchange(f"http://{SHOP}/style.css", body=STYLE)


async def _shop_step1(script: RecordingScript) -> None:
    await script.exchange(f"http://{SHOP}/results?q=kettle", resource_type="Document")
    await script.exchange(f"http://{SHOP}/style2.css", body=STYLE)  # a revisit of style.css
    await script.streamed_later(f"http://{SHOP}/app.js", BIG)


async def _shop_step2(script: RecordingScript) -> None:
    await script.streamed_later(f"http://{SHOP}/app2.js", BIG)  # the same blob again
    await script.too_large(f"http://{SHOP}/huge")
    script.failed(f"http://{SHOP}/broken")


async def _blocked_step0(script: RecordingScript) -> None:
    await script.exchange(
        f"http://{BLOCKED}/",
        status=403,
        resource_type="Document",
        headers=CHALLENGE,
        body=b"<p>Just a moment...</p>",
    )


async def known_chain(root: Path) -> KnownChain:
    """Write :func:`known_chain`'s root (module docstring) under *root*."""
    chain = KnownChain(root=root)
    record, control = root / "traces" / "record", root / "traces" / "control"
    same_path, branch = root / "traces" / "same_path", root / "traces" / "branch"

    chain.runs["rec-shop"] = await write_recorded(
        record,
        "rec-shop",
        [
            Step(
                actions=[click(1, 1)],
                duration_ms=4000,
                llm_latency_ms=500,
                color=10,
                network=_shop_step0,
            ),
            Step(
                actions=[click(2, 2)],
                duration_ms=1300,
                llm_latency_ms=300,
                color=20,
                network=_shop_step1,
            ),
            Step(
                actions=[done()],
                duration_ms=1500,
                llm_latency_ms=300,
                color=30,
                network=_shop_step2,
            ),
        ],
        task_id="shop-1",
        site=SHOP,
    )
    chain.runs["rec-blocked"] = await write_recorded(
        record,
        "rec-blocked",
        [
            Step(
                actions=[done("blocked")],
                duration_ms=3000,
                llm_latency_ms=200,
                color=70,
                network=_blocked_step0,
            )
        ],
        task_id="blocked-1",
        site=BLOCKED,
    )
    chain.runs["rec-broken"] = await write_recorded(
        record,
        "rec-broken",
        [Step(duration_ms=2000, error_phase="observe")],
        task_id="broken-1",
        site=BROKEN,
        status=RunStatus.ERROR,
    )
    chain.runs["ctl-shop"] = write_plain(
        control,
        "ctl-shop",
        [
            Step(actions=[click(1, 1)], duration_ms=3000, llm_latency_ms=500),
            Step(actions=[click(2, 2)], duration_ms=900, llm_latency_ms=300),
            Step(actions=[done()], duration_ms=1000, llm_latency_ms=300),
        ],
        task_id="shop-1",
        site=SHOP,
    )

    near = miss(
        f"http://{SHOP}/data.json?session=2", candidates=[f"http://{SHOP}/data.json?session=1"]
    )
    for index, middle in ((0, 20), (1, 21)):
        chain.runs[f"sp-shop-b{index}"] = await write_replayed(
            same_path,
            f"sp-shop-b{index}",
            [
                Step(
                    "restored",
                    [click(1, 1)],
                    color=10,
                    restoration=check(0),
                    replay=[hit(f"http://{SHOP}/")],
                ),
                Step("restored", [click(2, 2)], color=middle, restoration=check(1)),
                Step("policy", [done()], color=30, restoration=check(2), replay=[near]),
                Step("terminal", color=40),
            ],
            task_id="shop-1",
            site=SHOP,
            branch=provenance("rec-shop", 2, index, "shop-1"),
        )
    chain.runs["sp-blocked-b0"] = await write_replayed(
        same_path,
        "sp-blocked-b0",
        [
            Step(
                "policy",
                [done("blocked")],
                color=71,
                restoration=check(0, "document_miss", "screenshot_distance", document_misses=1),
                replay=[
                    miss(f"http://{BLOCKED}/", document=True, candidates=[f"http://{BLOCKED}/"])
                ],
            ),
            Step("terminal", color=72),
        ],
        task_id="blocked-1",
        site=BLOCKED,
        branch=provenance("rec-blocked", 0, 0, "blocked-1"),
    )

    not_found = RestoredAction(
        recorded=click(1, 1),
        executed=None,
        method="geometry_signature",
        failure="not_found",
        recorded_success=True,
    )
    chain.runs["br-b0"] = await write_replayed(
        branch,
        "br-b0",
        [
            Step("restored", [click(1, 1)], color=10, restoration=check(0)),
            Step(
                "policy",
                [click(100, 200)],
                color=20,
                restoration=check(1),
                replay=[
                    hit(f"http://{SHOP}/results?q=kettle"),
                    miss(
                        f"http://{SHOP}/data.json?session=3",
                        candidates=[f"http://{SHOP}/data.json?session=1"],
                    ),
                ],
            ),
            # A script the page the click opened asks for while the terminal observation waits.
            Step(
                "terminal",
                color=50,
                replay=[miss(f"http://{SHOP}/late.js", resource_type="Script")],
            ),
        ],
        task_id="shop-1",
        site=SHOP,
        branch=provenance("rec-shop", 1, 0, "shop-1"),
    )
    pixel = miss("http://ads.test/pixel.gif", resource_type="Image")
    chain.runs["br-b1"] = await write_replayed(
        branch,
        "br-b1",
        [
            Step(
                "restored",
                [click(1, 1)],
                color=11,
                restoration=check(
                    0, "retarget_not_found", "screenshot_distance", actions=[not_found]
                ),
            ),
            Step(
                "policy",
                [click(300, 400)],
                color=20,
                restoration=check(1),
                replay=[miss(f"http://{SHOP}/cart", method="POST", document=True)],
            ),
            Step("terminal", color=60),
        ],
        task_id="shop-1",
        site=SHOP,
        branch=provenance("rec-shop", 1, 1, "shop-1"),
        trailing=[pixel, pixel, pixel],
        repeat_limit=1,
    )
    chain.runs["br-b2"] = await write_replayed(
        branch,
        "br-b2",
        [
            Step(
                "restored",
                [click(1, 1)],
                color=10,
                restoration=check(0),
                replay=[miss(f"http://{SHOP}/results?q=kettle", document=True)],
            ),
            Step(
                "policy",
                [click(100, 200)],
                color=20,
                restoration=check(1, "document_miss", document_misses=1),
            ),
            Step("terminal", color=50),
        ],
        task_id="shop-1",
        site=SHOP,
        branch=provenance("rec-shop", 1, 2, "shop-1"),
    )
    broken = branch / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json")
    chain.runs["broken"] = broken

    samples = ["ok", "ok", "action_failed", "ok", "ok"]
    for name in ("first", "second"):
        write_dataset(root / "datasets" / name, samples=samples, duplicates=1)
    return chain
