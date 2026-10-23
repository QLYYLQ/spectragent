"""``health``: whether each recorded run is usable as data, with named reasons and evidence.

A recording whose page never rendered, or whose content requests failed
while it was recorded, still yields steps, forks and queue items; its forks
even restore well, because nothing on the screen changes. Such a run gives
no data worth labeling and has to be recorded again. Each ``record`` run
gets a verdict, ``usable`` or ``unusable``, and every signal that fired is
a named reason with its evidence:

- ``never_rendered`` (from the recording): a stretch of consecutive policy
  steps whose screenshots are blank and unchanged from one to the next,
  while the agent waited or reloaded. A screen is blank when the capture's
  settle wait did not see content in the viewport (``capped`` waiting for
  ``content``, or no settle record) and the image agrees: almost none of its
  pixels stand out from its most common colour (skeleton placeholders are
  painted a few levels off the background);
- ``degraded_at_load`` (from the recording): in one of the first steps,
  a share of the content requests (``Document``, ``Fetch``, ``XHR``) failed
  live — Chrome reported a network failure, not the page cancelling them —
  or the start page's document failed (a network failure, an HTTP error
  status, or Chrome's own error page in an early observation);
- ``answer_states_failure`` (from the agent's answer): the final answer
  says the task was not done, by rules over its text. This is the agent's
  own statement, not a measurement of the recording; the agent's
  ``done(success=...)`` flag is never read (UI-TARS's ``finished`` verb
  always reports success).

The verdict is ``unusable`` when a reason measured on the recording fired.
``answer_states_failure`` alone is reported but leaves the run ``usable``: it
is the task's failure, not the recording's, and the pages the agent saw are
still real data (user ruling 2026-10-23). Branch and replay runs are not
judged: a branch follows its source recording's verdict.

Every fact is read through the public readers: screenshots and settle records
through ``RunTrace``, the network index lines through the browser package's
``iter_index_lines``, how a request ended through its ``source_outcome`` (the
replay loader's own reading), the answer from ``result.json``. Outputs:
``health.json`` (every run's measures, verdict and reasons), ``report.md``, and
``exclusions.json``, the ``run-exclusions-1`` file of the unusable runs, built
with the loader's own model: the ``trace_branches`` loader's ``exclude_runs``
and every lookahead command's ``--exclude-runs`` read it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from lookahead.imaging import decode, difference, ink_share
from lookahead.output import write_json, write_text
from lookahead.report import share, table
from lookahead.runs import Run, RunSet
from lookahead.stability import DEFAULT_MIN_CHANGE_AREA
from spectragent.core.capabilities.browser.replay.archive_index import (
    BodyLine,
    ExchangeLine,
    IndexEntry,
    PendingLine,
    iter_index_lines,
    source_outcome,
)
from spectragent.core.data_loader.task.run_exclusions import (
    RUN_EXCLUSIONS_FORMAT,
    RunExclusion,
    RunExclusionsFile,
)
from spectragent.flywheel.diagnostics import DifferenceMethod
from spectragent.schemas.action import Action

HEALTH_FORMAT = "lookahead-run-health-1"
#: The ``run-exclusions-1`` file ``health`` writes in its output directory.
EXCLUSIONS_NAME = "exclusions.json"

# ── thresholds: never rendered ────────────────────────────────────────

#: A pixel stands out ("ink") when its largest RGB channel differs from the
#: screen's most common colour by more than this. The skeleton placeholders of
#: the 2026-10-23 iqiyi recordings stay within it of their background (under
#: 0.6 % of their pixels differ by even 16 levels); text, icons and pictures
#: are far over it.
INK_LEVEL = 48
#: A screen is blank when less than this share of its pixels is ink. Of the
#: 809 policy screens of the two 2026-10-23 recording sets, 83 had no ink at all
#: (80 in the three recordings whose page never rendered, 3 single loading
#: screens elsewhere); the least inked of the other 726 had 0.25 %.
BLANK_INK_SHARE = 0.001
#: Consecutive blank, unchanged policy screens that make a run "never
#: rendered". A page that is still loading is blank in one observation (the
#: longest such stretch in the other 27 recordings of 2026-10-23 was 1); five
#: screens are at least 25 s of capped settle waits (5 s each) plus the
#: agent's own waiting.
MIN_BLANK_STEPS = 5
#: The share of those steps in which the agent waited or reloaded: it saw the
#: page was not there, so the blank screen is not one it meant to look at
#: (19 of 25, 22 of 29 and 25 of 26 in the three never-rendered recordings).
MIN_STUCK_SHARE = 0.5
#: Keys that reload the page, as ``send_keys`` writes them (lower case,
#: ``control`` / ``cmd`` read as ``ctrl`` / ``meta``).
RELOAD_CHORDS = frozenset(
    frozenset(chord.split("+"))
    for chord in (
        "f5",
        "shift+f5",
        "ctrl+f5",
        "ctrl+r",
        "meta+r",
        "ctrl+shift+r",
        "meta+shift+r",
    )
)
_KEY_ALIASES = {"control": "ctrl", "cmd": "meta", "command": "meta"}
WAIT_ACTION = "browser.wait"
SEND_KEYS_ACTION = "browser.send_keys"
NAVIGATE_ACTION = "browser.navigate"

# ── thresholds: degraded at load ──────────────────────────────────────

#: The steps whose commits hold the run's load: step 0's commit takes the
#: start page's requests, step 1's the ones still finishing and the first
#: action's. A request belongs to the step whose commit took its index line.
LOAD_STEPS = 2
#: Requests that carry a page's content: its documents and its data calls.
#: Images, scripts, styles, media and pings are left out: a failed ad pixel
#: does not degrade the page.
CONTENT_RESOURCE_TYPES = frozenset({"Document", "Fetch", "XHR"})
#: A load step is degraded when at least this share of its content requests
#: failed live. In steps 0 and 1 of the other 29 recordings of 2026-10-23 at
#: most 1.5 % did when more than 15 requests were made (1 of 67; the failures
#: were third-party ad and sync calls and one iqiyi telemetry call); on the
#: degraded Bilibili home page 9 % did (11 of 119, all on api.bilibili.com).
MIN_FAILED_SHARE = 0.05
#: ...and at least this many failed: a step of a few requests is not decided by
#: one or two (one step of 15 had 1 failure, 6.7 %; no other step had over 3).
MIN_FAILED_REQUESTS = 5
#: A start document answered with a status at or above this is an error page.
DOCUMENT_ERROR_STATUS = 400
#: The URL scheme of Chrome's own error page (the document did not load).
ERROR_PAGE_SCHEME = "chrome-error"
#: How a request ended, as the replay loader reads it, that counts as failed
#: live: Chrome reported a network failure. The page cancelling a request
#: (``net::ERR_ABORTED``) is not one: navigating away cancels requests in
#: healthy recordings too (up to 15 of a step's 118 content requests).
FAILED_LIVE = "network_failed"
CANCELLED_BY_PAGE = frozenset({"page_cancelled_before_response", "page_cancelled_during_body"})

# ── rules: the final answer ───────────────────────────────────────────

#: Statements that the task as a whole was not done, by name. Only task-level
#: statements: "未能读取播放量" (one field could not be read) is a partial
#: answer, not a failure.
ANSWER_FAILURE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("zh_could_not_complete", re.compile(r"(无法|未能|不能|没能|没法|没有能够)完成")),
    ("zh_task_failed", re.compile(r"任务(失败|无法进行|无法执行)")),
    (
        "en_could_not_complete",
        re.compile(
            r"\b(unable|not able|could not|couldn't|cannot|can't|failed)\s+to\s+"
            r"(complete|finish|accomplish)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "en_task_cannot_be_completed",
        re.compile(
            r"\btask\s+(cannot|could not|can't|couldn't)\s+be\s+"
            r"(completed|done|finished|accomplished)\b",
            re.IGNORECASE,
        ),
    ),
    ("en_not_achievable", re.compile(r"\bnot\s+achievable\b", re.IGNORECASE)),
)
#: Characters of the answer kept in the evidence.
ANSWER_EXCERPT = 200

Verdict = Literal["usable", "unusable"]
ReasonCode = Literal["never_rendered", "degraded_at_load", "answer_states_failure"]
REASON_CODES: tuple[ReasonCode, ...] = (
    "never_rendered",
    "degraded_at_load",
    "answer_states_failure",
)
#: What a reason rests on: a measurement of the recording, or the agent's own answer.
Basis = Literal["recording", "answer"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Screen(_Model):
    """One policy step's screen, as the never-rendered signal reads it."""

    step: int
    url: str | None = None
    #: The share of the screenshot's pixels that stand out (``None``: it did not decode).
    ink_share: float | None
    settle_outcome: str | None
    #: The settle wait saw content in the viewport (``settled``, or capped
    #: waiting only for something else).
    content_seen: bool
    blank: bool
    #: Unchanged from the previous policy screen (``None``: the first one, or not comparable).
    unchanged: bool | None
    waited_or_reloaded: bool


class BlankStretch(_Model):
    """Consecutive blank policy screens, each unchanged from the one before."""

    first_step: int
    last_step: int
    steps: int
    waited_or_reloaded: int
    #: How many of them the settle wait capped while waiting for content.
    content_unseen: int
    urls: list[str]

    @property
    def stuck(self) -> bool:
        return self.waited_or_reloaded >= MIN_STUCK_SHARE * self.steps

    @property
    def fires(self) -> bool:
        return self.steps >= MIN_BLANK_STEPS and self.stuck


class LoadStep(_Model):
    """The content requests one early step's commit took, and how they ended."""

    step: int
    content_requests: int
    failed_live: int
    cancelled_by_page: int
    failed_share: float | None
    #: Error text → count, of the requests that failed live.
    errors: dict[str, int] = Field(default_factory=dict)
    #: Host → count, of the requests that failed live.
    failed_hosts: dict[str, int] = Field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        return (
            self.failed_live >= MIN_FAILED_REQUESTS
            and self.failed_share is not None
            and self.failed_share >= MIN_FAILED_SHARE
        )


class StartDocument(_Model):
    """The start page's document in the early steps, and whether it failed."""

    url: str | None
    #: The first early document request for the start URL: its step, status and error.
    step: int | None = None
    status: int | None = None
    error: str | None = None
    #: An early observation that shows Chrome's error page.
    error_page_step: int | None = None
    failed: bool = False


class AnswerCheck(_Model):
    """The final answer (``result.json``'s ``answer``) and the failure rule it matched."""

    agent_done: str | None
    answer: str
    rule: str | None = None
    matched: str | None = None


class Reason(_Model):
    code: ReasonCode
    basis: Basis
    summary: str


class RunHealth(_Model):
    run: str
    run_id: str
    site: str | None
    task_id: str | None
    task: str
    verdict: Verdict
    reasons: list[Reason]
    policy_screens: int
    blank_screens: int
    blank_stretches: list[BlankStretch]
    load: list[LoadStep]
    start_document: StartDocument
    answer: AnswerCheck


class HealthReport(_Model):
    format: Literal["lookahead-run-health-1"] = HEALTH_FORMAT
    thresholds: dict[str, Any]
    inputs: dict[str, str]
    counts: dict[str, int]
    runs: list[RunHealth]
    #: Runs that are not recordings, and why they are not judged.
    not_assessed: dict[str, str]
    skipped_runs: dict[str, str]


def thresholds() -> dict[str, Any]:
    return {
        "ink_level": INK_LEVEL,
        "blank_ink_share": BLANK_INK_SHARE,
        "min_change_area": DEFAULT_MIN_CHANGE_AREA,
        "min_blank_steps": MIN_BLANK_STEPS,
        "min_stuck_share": MIN_STUCK_SHARE,
        "load_steps": LOAD_STEPS,
        "content_resource_types": sorted(CONTENT_RESOURCE_TYPES),
        "min_failed_share": MIN_FAILED_SHARE,
        "min_failed_requests": MIN_FAILED_REQUESTS,
        "document_error_status": DOCUMENT_ERROR_STATUS,
        "answer_failure_rules": {name: rule.pattern for name, rule in ANSWER_FAILURE_RULES},
    }


# ── never rendered ────────────────────────────────────────────────────


def _chord(keys: Any) -> frozenset[str]:
    parts = str(keys or "").lower().replace(" ", "").split("+")
    return frozenset(_KEY_ALIASES.get(part, part) for part in parts if part)


def waited_or_reloaded(actions: Iterable[Action], url: str | None) -> bool:
    """Whether the step waited, pressed a reload key, or navigated to the page it showed."""
    for action in actions:
        if action.name == WAIT_ACTION:
            return True
        if action.name == SEND_KEYS_ACTION and _chord(action.params.get("keys")) in RELOAD_CHORDS:
            return True
        if action.name == NAVIGATE_ACTION and url and action.params.get("url") == url:
            return True
    return False


def screens(run: Run, method: DifferenceMethod | None = None) -> list[Screen]:
    """Every policy step's screen that committed a screenshot, in step order."""
    method = method or DifferenceMethod()
    found: list[Screen] = []
    previous: bytes | None = None
    for step in run.steps():
        record = run.records[step]
        if record.origin != "policy" or not run.has_screenshot(step):
            continue
        data = run.screenshot_bytes(step)
        shot = run.screenshot(step)
        settle = shot.settle if shot is not None else None
        content_seen = settle is not None and (
            settle.outcome == "settled"
            or (settle.outcome == "capped" and "content" not in settle.waiting_for)
        )
        image = decode(data) if data is not None else None
        ink = ink_share(image, INK_LEVEL) if image is not None else None
        unchanged: bool | None = None
        if previous is not None and data is not None:
            compared = difference(previous, data, method)
            if compared.compared:
                unchanged = not compared.significant(DEFAULT_MIN_CHANGE_AREA)
        url = shot.source_url if shot is not None else None
        found.append(
            Screen(
                step=step,
                url=url,
                ink_share=ink,
                settle_outcome=settle.outcome if settle is not None else None,
                content_seen=content_seen,
                blank=ink is not None and ink < BLANK_INK_SHARE and not content_seen,
                unchanged=unchanged,
                waited_or_reloaded=waited_or_reloaded(record.actions, url),
            )
        )
        previous = data
    return found


def blank_stretches(found: list[Screen]) -> list[BlankStretch]:
    """Runs of consecutive blank policy screens, each unchanged from the one before."""
    stretches: list[list[Screen]] = []
    for position, screen in enumerate(found):
        if not screen.blank:
            continue
        before = found[position - 1] if position else None
        continues = (
            stretches
            and before is not None
            and before.blank
            and stretches[-1][-1] is before
            and screen.unchanged is True
        )
        if continues:
            stretches[-1].append(screen)
        else:
            stretches.append([screen])
    return [
        BlankStretch(
            first_step=members[0].step,
            last_step=members[-1].step,
            steps=len(members),
            waited_or_reloaded=sum(1 for screen in members if screen.waited_or_reloaded),
            content_unseen=sum(
                1
                for screen in members
                if screen.settle_outcome == "capped" and not screen.content_seen
            ),
            urls=sorted({screen.url for screen in members if screen.url}),
        )
        for members in stretches
    ]


# ── degraded at load ──────────────────────────────────────────────────


def _normalized(url: str | None) -> tuple[str, str, str, str] | None:
    if not url:
        return None
    parts = urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.path or "/", parts.query)


def load(run: Run) -> tuple[list[LoadStep], StartDocument]:
    """The content requests of the first :data:`LOAD_STEPS` steps, and the start document."""
    lines = list(iter_index_lines(run.trace))
    bodies: dict[str, BodyLine] = {}
    #: Each record's last line: a record whose last line is ``pending`` was in flight at run end.
    last: dict[str, IndexEntry] = {}
    for _, line in lines:
        last[line.record_id] = line
        if isinstance(line, BodyLine):
            bodies[line.record_id] = line
    early = range(LOAD_STEPS)
    counted: dict[int, list[tuple[ExchangeLine, str, str | None]]] = {step: [] for step in early}
    start = StartDocument(url=run.start_url)
    wanted = _normalized(run.start_url)
    for step, line in lines:
        if step not in counted or not isinstance(line, ExchangeLine):
            continue
        if line.resource_type not in CONTENT_RESOURCE_TYPES:
            continue
        body = bodies.get(line.record_id)
        ended = source_outcome(
            line, body, ends_pending=isinstance(last.get(line.record_id), PendingLine)
        )
        error = (body.error_text if body is not None else None) or line.error_text
        counted[step].append((line, ended, error))
        if (
            line.resource_type == "Document"
            and start.step is None
            and wanted is not None
            and _normalized(line.url) == wanted
        ):
            start.step, start.status, start.error = step, line.status, error
            start.failed = ended == FAILED_LIVE or (
                line.status is not None and line.status >= DOCUMENT_ERROR_STATUS
            )
    for step in early:
        if step not in run.records:
            continue
        shot = run.screenshot(step)
        url = shot.source_url if shot is not None else None
        if url and urlsplit(url).scheme == ERROR_PAGE_SCHEME:
            start.error_page_step = step
            start.failed = True
            break
    steps: list[LoadStep] = []
    for step, requests in counted.items():
        failed = [(line, error) for line, ended, error in requests if ended == FAILED_LIVE]
        steps.append(
            LoadStep(
                step=step,
                content_requests=len(requests),
                failed_live=len(failed),
                cancelled_by_page=sum(1 for _, ended, _ in requests if ended in CANCELLED_BY_PAGE),
                failed_share=len(failed) / len(requests) if requests else None,
                errors=dict(Counter(error or "no error text" for _, error in failed).most_common()),
                failed_hosts=dict(
                    Counter(
                        urlsplit(line.url).hostname or line.url for line, _ in failed
                    ).most_common()
                ),
            )
        )
    return steps, start


# ── the final answer ──────────────────────────────────────────────────


def answer_check(run: Run) -> AnswerCheck:
    """The final answer and the first failure rule it matches (``None``: none)."""
    result = run.trace.result
    if result is None:
        return AnswerCheck(agent_done=None, answer="")
    answer = result.answer.strip()
    check = AnswerCheck(agent_done=str(result.agent_done), answer=_excerpt(answer, ANSWER_EXCERPT))
    for name, rule in ANSWER_FAILURE_RULES:
        found = rule.search(answer)
        if found is not None:
            check.rule, check.matched = name, found.group(0)
            break
    return check


def _excerpt(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── the verdict ───────────────────────────────────────────────────────


def _steps_text(stretch: BlankStretch) -> str:
    if stretch.first_step == stretch.last_step:
        return f"step {stretch.first_step}"
    return f"steps {stretch.first_step}-{stretch.last_step}"


def reasons(
    stretches: list[BlankStretch], steps: list[LoadStep], start: StartDocument, answer: AnswerCheck
) -> list[Reason]:
    """Every signal that fired, as a named reason with a one-line summary."""
    found: list[Reason] = []
    firing = [stretch for stretch in stretches if stretch.fires]
    if firing:
        longest = max(firing, key=lambda stretch: stretch.steps)
        found.append(
            Reason(
                code="never_rendered",
                basis="recording",
                summary=(
                    f"never rendered: {_steps_text(longest)} ({longest.steps} policy steps) showed "
                    f"a blank, unchanged screen while the agent waited or reloaded in "
                    f"{longest.waited_or_reloaded} of them"
                ),
            )
        )
    degraded = [step for step in steps if step.degraded]
    parts = [
        f"step {step.step}: {step.failed_live} of {step.content_requests} content requests "
        f"({share_text(step.failed_share)}) failed live ("
        + ", ".join(f"{error} x{count}" for error, count in step.errors.items())
        + ")"
        for step in degraded
    ]
    if start.failed:
        if start.error_page_step is not None:
            parts.append(f"step {start.error_page_step} showed Chrome's error page")
        if start.step is not None and (start.error or start.status):
            parts.append(
                f"the start page's document failed ({start.error or f'status {start.status}'})"
            )
    if parts:
        found.append(
            Reason(
                code="degraded_at_load",
                basis="recording",
                summary="degraded at load: " + "; ".join(parts),
            )
        )
    if answer.rule is not None:
        found.append(
            Reason(
                code="answer_states_failure",
                basis="answer",
                summary=(
                    f"the final answer states the task was not done ({answer.rule}: "
                    f"\u201c{answer.matched}\u201d)"
                ),
            )
        )
    return found


def share_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def assess(run: Run, method: DifferenceMethod | None = None) -> RunHealth:
    """One recording's measures, reasons and verdict."""
    found = screens(run, method)
    stretches = blank_stretches(found)
    steps, start = load(run)
    answer = answer_check(run)
    why = reasons(stretches, steps, start, answer)
    return RunHealth(
        run=run.key,
        run_id=run.run_id,
        site=run.site,
        task_id=run.task_id,
        task=run.config.task,
        verdict="unusable" if any(reason.basis == "recording" for reason in why) else "usable",
        reasons=why,
        policy_screens=len(found),
        blank_screens=sum(1 for screen in found if screen.blank),
        blank_stretches=stretches,
        load=steps,
        start_document=start,
        answer=answer,
    )


def analyse(runs: RunSet) -> HealthReport:
    """Every recording's health; other runs are listed as not judged."""
    method = DifferenceMethod()
    not_assessed = {
        run.key: f"a {run.kind} run: only recordings are judged (a branch follows its source)"
        for run in runs.runs
        if run.kind != "record"
    }
    recordings = [run for run in runs.runs if run.kind == "record"]
    assessed = [health for _, health in runs.each(lambda run: assess(run, method), recordings)]
    counts = Counter(health.verdict for health in assessed)
    for health in assessed:
        counts.update(reason.code for reason in health.reasons)
    return HealthReport(
        thresholds=thresholds(),
        inputs={"root": str(runs.root), "viewer_root": str(runs.viewer_root)},
        counts={key: counts[key] for key in ("usable", "unusable", *REASON_CODES)},
        runs=assessed,
        not_assessed=not_assessed,
        skipped_runs=dict(runs.skipped),
    )


def exclusions(report: HealthReport) -> RunExclusionsFile:
    """The unusable runs as a ``run-exclusions-1`` file, built with the loader's own model."""
    return RunExclusionsFile(
        format=RUN_EXCLUSIONS_FORMAT,
        runs=[
            RunExclusion(
                run_id=health.run_id,
                reason="; ".join(reason.summary for reason in health.reasons),
                note=f"lookahead health: {health.run}",
            )
            for health in report.runs
            if health.verdict == "unusable"
        ],
    )


# ── the report ────────────────────────────────────────────────────────


def _longest(health: RunHealth) -> str:
    if not health.blank_stretches:
        return "-"
    longest = max(health.blank_stretches, key=lambda stretch: stretch.steps)
    return f"{longest.steps} ({_steps_text(longest)}; waited {longest.waited_or_reloaded})"


def _worst_load(health: RunHealth) -> str:
    measured = [step for step in health.load if step.content_requests]
    if not measured:
        return "-"
    worst = max(measured, key=lambda step: (step.failed_share or 0.0, step.failed_live))
    return f"step {worst.step}: {worst.failed_live}/{worst.content_requests}"


def render_report(report: HealthReport) -> str:
    unusable = [health for health in report.runs if health.verdict == "unusable"]
    lines = [
        "# Recording health",
        "",
        f"{len(report.runs)} recording(s): {report.counts['usable']} usable, "
        f"{report.counts['unusable']} unusable.",
        "",
        "## Signals and thresholds",
        "",
        f"- **never_rendered** (recording): at least {MIN_BLANK_STEPS} consecutive policy screens "
        f"that are blank (the settle wait did not see content, and under "
        f"{BLANK_INK_SHARE:.1%} of the pixels differ from the screen's most common colour by "
        f"more than {INK_LEVEL} levels) and unchanged from one to the next (no changed region "
        f"of {DEFAULT_MIN_CHANGE_AREA:.1%} of the image), while the agent waited or reloaded in "
        f"at least {MIN_STUCK_SHARE:.0%} of them.",
        f"- **degraded_at_load** (recording): in one of steps 0-{LOAD_STEPS - 1}, at least "
        f"{MIN_FAILED_SHARE:.0%} and at least {MIN_FAILED_REQUESTS} of the content requests "
        f"({', '.join(sorted(CONTENT_RESOURCE_TYPES))}) failed live (a network failure Chrome "
        "reported; the page cancelling a request does not count); or the start page's "
        f"document failed (a network failure, a status of {DOCUMENT_ERROR_STATUS} or more, or "
        "Chrome's error page in an early observation).",
        "- **answer_states_failure** (answer): the final answer says the task was not done "
        "(rules: " + ", ".join(name for name, _ in ANSWER_FAILURE_RULES) + "). The agent's own "
        "statement, not a measurement of the recording: reported, but on its own it leaves "
        "the run usable.",
        "",
        "## Verdicts",
        "",
        table(
            [
                "run",
                "site",
                "task",
                "verdict",
                "reasons",
                "longest blank stretch",
                "worst load step (failed live)",
                "answer rule",
            ],
            [
                [
                    health.run,
                    health.site or "-",
                    health.task_id or "-",
                    health.verdict,
                    ", ".join(reason.code for reason in health.reasons) or "-",
                    _longest(health),
                    _worst_load(health),
                    health.answer.rule or "-",
                ]
                for health in sorted(report.runs, key=lambda health: health.run)
            ],
        ),
        "",
    ]
    if unusable:
        lines += ["## Why each unusable run is unusable", ""]
        for health in sorted(unusable, key=lambda health: health.run):
            lines.append(f"### `{health.run}` ({health.site or 'no site'}, task {health.task_id})")
            lines.append("")
            lines += [f"- {reason.summary} [{reason.basis}]" for reason in health.reasons]
            if health.answer.answer:
                lines.append(f"- final answer ({health.answer.agent_done}): {health.answer.answer}")
            lines.append("")
    flagged = [health for health in report.runs if health.verdict == "usable" and health.reasons]
    if flagged:
        lines += ["## Usable runs whose answer says the task was not done", ""]
        lines += [
            f"- `{health.run}` ({health.site or 'no site'}, task {health.task_id}): "
            f"{health.answer.answer}"
            for health in sorted(flagged, key=lambda health: health.run)
        ]
        lines.append("")
    blank_total = sum(health.blank_screens for health in report.runs)
    screens_total = sum(health.policy_screens for health in report.runs)
    lines += [
        "## Measures",
        "",
        f"- Blank policy screens: {share(blank_total, screens_total)}.",
        "",
    ]
    if report.not_assessed:
        lines += [f"Not judged: {len(report.not_assessed)} run(s) that are not recordings.", ""]
    if report.skipped_runs:
        lines += ["## Runs not read", ""]
        lines += [f"- `{key}`: {why}" for key, why in sorted(report.skipped_runs.items())]
        lines.append("")
    return "\n".join(lines)


def run_health(runs: RunSet, out: Path) -> HealthReport:
    """Judge every recording; write ``health.json``, ``report.md`` and ``exclusions.json``."""
    report = analyse(runs)
    write_json(out / "health.json", report.model_dump(mode="json"))
    write_text(out / "report.md", render_report(report))
    write_json(out / EXCLUSIONS_NAME, exclusions(report).model_dump(mode="json"))
    return report
