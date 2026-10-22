"""``judge-forks``: is a branch's decision at its checkpoint the same as the source's, and is it sound?

Every branch run is paired, through its recorded provenance, with the source
run's step at the checkpoint: the branch's first policy step and the step the
source took at the same screen. Only a fork whose checkpoint restored (the
restorer's check of the checkpoint observation is ``ok``) and whose branch made
a decision is judged; the others are listed with their reasons and kept out of
every statistic and of the review queue.

**Rules** compare what was recorded, and decide only on strong evidence:
action types (``click`` = ``click_coordinate``, ``type`` = ``type_coordinate``;
waits are ignored); the element a pointer hit (``same`` only for the same role
and readable name at nearby points; ``different`` only when both hits are
named interactive targets, since a click on a card's image and one on its
title may be the same choice); typed text together with the field it went
into; a scroll's direction, a navigation's URL; and the next observation's URL
(a different page decides; a difference only in volatile query parameters is
neutral; any other query difference leaves it ``unknown``). ``no_decision``
when either side ended in an error without an action.

**The judge model** is asked two separate requests, so neither answer is
coloured by the other side. ``reasonable`` and ``executed_as_intended``: the
branch's task, its checkpoint screen with its pointer actions ringed, its
actions and reply, and its next screen, with no information about the source
run, the same for same-task and task-variant forks. ``same_intent`` (same-task
forks whose source decided): both runs' actions on the same screen and both
next screens. Each answer is ``yes`` / ``no`` / ``uncertain`` with a reason.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from math import dist
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from lookahead import imaging
from lookahead.extract import restoration_summary, step_rows
from lookahead.judge import Judge, JudgeOutcome, JudgeRequest, image_part, text_part
from lookahead.output import write_lines, write_text
from lookahead.queues import item_title, observation_image
from lookahead.report import crosstab, share, table
from lookahead.rows import ActionRow, NextObservation, RestorationSummary, StepRow
from lookahead.runs import READ_ERRORS, LookaheadInputError, Run, RunSet, unreadable
from spectragent.annotation import (
    QUEUE_FORMAT,
    GuideSection,
    ItemField,
    ItemLink,
    LabelChoice,
    LabelSpec,
    MarkSource,
    Question,
    QueueImage,
    QueueItem,
    ReviewQueue,
    Suggestion,
    SuggestionDisplay,
    write_queue,
)
from spectragent.inspection.marks import action_lines, observation_geometry, step_marks
from spectragent.schemas.restoration import BranchTaskVariant

RuleVerdictValue = Literal["same", "different", "unknown", "no_decision"]
ForkKind = Literal["same_task", "task_variant"]
ForkStatus = Literal["judged", "rules_only", "unpaired", "checkpoint_diverged", "no_decision"]
#: Forks that count: paired, restored at the checkpoint, and decided by the branch.
COUNTED: frozenset[str] = frozenset({"judged", "rules_only"})
INTENT_PURPOSE = "fork_intent"
QUALITY_PURPOSE = "fork_quality"
JUDGMENT_FORMAT = "lookahead-fork-judgment-2"
#: Action types the rules treat as one.
FAMILY = {"click_coordinate": "click", "type_coordinate": "type"}
#: Action types the rules leave out of the comparison.
FILLER = frozenset({"wait"})
#: Roles of elements a person aims a click at; a hit on anything else may be inside one.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "slider",
        "spinbutton",
        "listbox",
        "treeitem",
    }
)
#: Two pointer actions at most this far apart (CSS px) aim at the same place.
NEAR_PX = 48.0
#: Query parameters that carry no choice: cache busters, tracking and signatures.
VOLATILE_PARAMETERS = frozenset(
    {
        "_",
        "t",
        "ts",
        "vt",
        "r",
        "rand",
        "random",
        "nonce",
        "cb",
        "time",
        "timestamp",
        "sid",
        "session",
        "sessionid",
        "spm",
        "spm_id_from",
        "from_source",
        "from_spmid",
        "search_source",
        "ref",
        "referrer",
        "source",
        "src",
        "trackid",
        "track_id",
        "traceid",
        "trace_id",
        "requestid",
        "request_id",
        "w_rid",
        "wts",
        "sign",
        "signature",
        "token",
        "xsec_token",
        "xsec_source",
        "gclid",
        "fbclid",
    }
)
_VOLATILE_VALUE = re.compile(
    r"\d{8,}|[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
THOUGHT_CHARS = 1500
#: The judge model's ring colours: the branch (run B, or the only run) red, the source blue.
RED = "#e8202a"
BLUE = "#1f6feb"

SYSTEM = (
    "You review the decisions of a web-browsing agent. You see screenshots of a browser and "
    "the actions the agent issued, each with the effect the browser measured. Judge only from "
    "what the screenshots and the action descriptions show, and reply with JSON only."
)


# ── the pair ──────────────────────────────────────────────────────────


@dataclass
class Fork:
    """One branch run, what it is compared with, and whether it can be judged."""

    branch: Run
    source: Run | None
    checkpoint: int
    kind: ForkKind
    variant: BranchTaskVariant | None
    branch_row: StepRow | None = None
    source_row: StepRow | None = None
    #: The restorer's check of the checkpoint observation, as the branch recorded it.
    checkpoint_check: RestorationSummary | None = None
    #: ``unpaired``, ``checkpoint_diverged`` or ``no_decision``; ``None`` for a fork to judge.
    excluded: ForkStatus | None = None
    reason: str | None = None

    @property
    def counted(self) -> bool:
        return self.excluded is None


def step_error(row: StepRow) -> str | None:
    """What the step ended in, when it ended in an error (``phase: message``)."""
    if not row.error_phase and not row.error:
        return None
    return f"{row.error_phase or 'error'}: {row.error or 'no message recorded'}"


def decided(row: StepRow) -> bool:
    """Whether the step made a decision: it acted, or it did not end in an error."""
    return bool(row.actions) or step_error(row) is None


def _check_text(check: RestorationSummary) -> str:
    reasons = ", ".join(check.reasons) or "no reason recorded"
    where = f" (restored URL {check.restored_url})" if check.restored_url else ""
    return f"{check.verdict}: {reasons}{where}"


def forks(runs: RunSet) -> list[Fork]:
    """Every branch run under the root, paired with its source's step at the checkpoint."""
    cache: dict[str, dict[int, StepRow] | Exception] = {}

    def rows_of(run: Run) -> dict[int, StepRow]:
        if run.key not in cache:
            try:
                cache[run.key] = {row.step: row for row in step_rows(run, runs)}
            except READ_ERRORS as exc:
                runs.skip(run, exc)
                cache[run.key] = exc
        found = cache[run.key]
        if isinstance(found, Exception):
            raise found
        return found

    def classify(run: Run) -> Fork:
        branch = run.config.branch
        assert branch is not None
        source = runs.source_of(run)
        fork = Fork(
            branch=run,
            source=source,
            checkpoint=branch.checkpoint_step,
            kind="task_variant" if branch.task_variant is not None else "same_task",
            variant=branch.task_variant,
        )
        candidate = rows_of(run).get(branch.checkpoint_step)
        if candidate is not None and candidate.origin == "policy":
            fork.branch_row = candidate
            fork.checkpoint_check = candidate.restoration
        if fork.branch_row is None:
            stopped = next(
                (
                    record.restoration
                    for step, record in sorted(run.records.items())
                    if record.restoration is not None and record.restoration.verdict != "ok"
                ),
                None,
            )
            if stopped is not None:
                check = restoration_summary(stopped)
                assert check is not None
                fork.checkpoint_check = check
                fork.excluded = "checkpoint_diverged"
                fork.reason = (
                    f"restoration stopped at step {stopped.step} before the checkpoint: "
                    f"{_check_text(check)}"
                )
            else:
                fork.excluded = "unpaired"
                fork.reason = (
                    f"the branch has no policy step at its checkpoint {branch.checkpoint_step} "
                    f"(it ended before deciding there)"
                )
            return fork
        if fork.checkpoint_check is None or fork.checkpoint_check.verdict != "ok":
            fork.excluded = "checkpoint_diverged"
            fork.reason = (
                "no restoration check was recorded for the checkpoint"
                if fork.checkpoint_check is None
                else f"the checkpoint did not restore: {_check_text(fork.checkpoint_check)}"
            )
            return fork
        if source is None:
            fork.excluded = "unpaired"
            fork.reason = f"its source run {branch.source_run_id!r} is not among the runs read"
            return fork
        try:
            fork.source_row = rows_of(source).get(branch.checkpoint_step)
        except READ_ERRORS as exc:
            fork.excluded = "unpaired"
            fork.reason = f"its source run does not read back: {unreadable(source.path, exc)}"
            return fork
        if fork.source_row is None:
            fork.excluded = "unpaired"
            fork.reason = f"the source run has no step {branch.checkpoint_step} with an observation"
            return fork
        error = step_error(fork.branch_row)
        if not decided(fork.branch_row):
            fork.excluded = "no_decision"
            fork.reason = f"the branch made no decision at the checkpoint ({error})"
        return fork

    branches = [run for run in runs.runs if run.config.branch is not None]
    return [fork for _, fork in runs.each(classify, branches)]


# ── rules ─────────────────────────────────────────────────────────────


class RuleVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: RuleVerdictValue
    reasons: list[str] = Field(default_factory=list)


def action_type(name: str) -> str:
    local = name.rpartition(".")[2]
    return FAMILY.get(local, local)


def _norm(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def _element_text(fact: Any) -> str:
    if isinstance(fact, dict):
        return f'{fact.get("role")} "{fact.get("name", "")}"'
    return str(fact)


def action_point(action: ActionRow) -> tuple[float, float] | None:
    """Where a pointer or coordinate action aimed, in CSS px: its measured point, else its parameters."""
    point = action.facts.get("point")
    if isinstance(point, list) and len(point) == 2:
        return float(point[0]), float(point[1])
    for x_key, y_key in (("coordinate_x", "coordinate_y"), ("start_x", "start_y")):
        x, y = action.params.get(x_key), action.params.get(y_key)
        if isinstance(x, int | float) and isinstance(y, int | float):
            return float(x), float(y)
    return None


def _near(branch: ActionRow, source: ActionRow) -> bool | None:
    here, there = action_point(branch), action_point(source)
    if here is None or there is None:
        return None
    return dist(here, there) <= NEAR_PX


def _compare_pointer(branch: ActionRow, source: ActionRow) -> tuple[RuleVerdictValue, str]:
    hit, other = branch.facts.get("hit"), source.facts.get("hit")
    if not (isinstance(hit, dict) and isinstance(other, dict)):
        return "unknown", (
            f"the hit element is not known on both sides ({_element_text(hit)} vs "
            f"{_element_text(other)})"
        )
    same_element = (hit.get("role"), hit.get("name")) == (other.get("role"), other.get("name"))
    near = _near(branch, source)
    if same_element:
        if not hit.get("name"):
            return "unknown", f"both hit an unnamed {hit.get('role')}; which one is unknown"
        if near is False:
            return "unknown", (
                f"both hit {_element_text(hit)}, but far apart: possibly two items of that name"
            )
        return "same", f"the pointer hit the same element: {_element_text(hit)}"
    targets = all(
        fact.get("role") in INTERACTIVE_ROLES and fact.get("name") for fact in (hit, other)
    )
    if targets:
        return "different", (
            f"the pointer hit different targets: {_element_text(hit)} vs {_element_text(other)}"
        )
    return "unknown", (
        f"the hits differ ({_element_text(hit)} vs {_element_text(other)}), but one is not a "
        f"named interactive target: both may be inside the same item"
    )


def _compare_typing(branch: ActionRow, source: ActionRow) -> tuple[RuleVerdictValue, str]:
    text, other_text = branch.facts.get("text"), source.facts.get("text")
    if text is None or other_text is None:
        return "unknown", "the typed text is not recorded on both sides"
    if _norm(text) != _norm(other_text):
        return "different", f"typed different text: {text!r} vs {other_text!r}"
    field, other_field = branch.facts.get("target"), source.facts.get("target")
    if isinstance(field, dict) and isinstance(other_field, dict) and field.get("name"):
        if (field.get("role"), field.get("name")) == (
            other_field.get("role"),
            other_field.get("name"),
        ):
            if _near(branch, source) is False:
                return "unknown", "typed the same text into fields of one name, far apart"
            return "same", f"typed the same text into the same field: {_element_text(field)}"
        if other_field.get("name"):
            return "different", (
                f"typed the same text into different fields: {_element_text(field)} vs "
                f"{_element_text(other_field)}"
            )
    if _near(branch, source):
        return "same", "typed the same text at the same place"
    return "unknown", "typed the same text; whether into the same field is not known"


def compare_actions(branch: ActionRow, source: ActionRow) -> tuple[RuleVerdictValue, str]:
    """One branch action against the source action in the same position."""
    kind, other = action_type(branch.name), action_type(source.name)
    if kind != other:
        return "different", f"action types differ: {kind} vs {other}"
    if kind in ("click", "drag"):
        return _compare_pointer(branch, source)
    if kind == "type":
        return _compare_typing(branch, source)
    keys = {
        "scroll": "direction",
        "navigate": "url",
        "send_keys": "keys",
        "switch_tab": "target_id",
    }
    if kind in keys:
        key = keys[kind]
        value, other_value = branch.params.get(key), source.params.get(key)
        if value is None or other_value is None:
            return "unknown", f"{kind}: {key} is not recorded on both sides"
        if _norm(value) == _norm(other_value):
            return "same", f"{kind} with the same {key}"
        return "different", f"{kind} with different {key}: {value!r} vs {other_value!r}"
    if kind == "select_dropdown":
        chosen = branch.facts.get("selected")
        other_chosen = source.facts.get("selected")
        if isinstance(chosen, dict) and isinstance(other_chosen, dict):
            if chosen.get("index") == other_chosen.get("index"):
                return "same", "selected the same option"
            return "different", "selected different options"
        return "unknown", "the selected option is not known on both sides"
    if kind == "done":
        answer = branch.params.get("extracted_content")
        other_answer = source.params.get("extracted_content")
        if answer is not None and other_answer is not None and _norm(answer) == _norm(other_answer):
            return "same", "finished with the same answer"
        return "unknown", "both finished; whether the answers mean the same is not a rule's call"
    if branch.params == source.params:
        return "same", f"{kind} with the same parameters"
    return "unknown", f"{kind} with different parameters"


def volatile_parameter(name: str, value: str) -> bool:
    """A query parameter that carries no choice: a cache buster, a tracker, a signature."""
    lowered = name.lower()
    return (
        lowered in VOLATILE_PARAMETERS
        or lowered.startswith("utm_")
        or _VOLATILE_VALUE.fullmatch(value) is not None
    )


def compare_urls(url: str | None, other: str | None) -> tuple[RuleVerdictValue | None, str]:
    """The next observations' URLs: ``None`` when they decide nothing.

    A different page (scheme, host or path) is ``different``. URLs that differ
    only in volatile query parameters are neutral; any other difference after
    the path leaves the pair ``unknown``.
    """
    if not url or not other:
        return None, "a next URL is not known"
    if url == other:
        return "same", "the next URLs are the same"
    left, right = urlsplit(url), urlsplit(other)
    if (left.scheme, left.netloc, left.path) != (right.scheme, right.netloc, right.path):
        return "different", f"the next URLs differ: {url} vs {other}"
    kept = [
        sorted(
            (k, v)
            for k, v in parse_qsl(side.query, keep_blank_values=True)
            if not volatile_parameter(k, v)
        )
        for side in (left, right)
    ]
    if kept[0] == kept[1] and left.fragment == right.fragment:
        return None, "the next URLs differ only in volatile query parameters"
    return "unknown", f"the next URLs differ after the path: {url} vs {other}"


def rule_verdict(
    branch: list[ActionRow],
    source: list[ActionRow],
    branch_next: NextObservation,
    source_next: NextObservation,
    *,
    branch_error: str | None = None,
    source_error: str | None = None,
) -> RuleVerdict:
    """The rules' verdict on two decisions at the same screen (module docstring)."""
    undecided = [
        f"the {side} ended in an error without an action ({error})"
        for side, actions, error in (
            ("branch", branch, branch_error),
            ("source", source, source_error),
        )
        if error is not None and not actions
    ]
    if undecided:
        return RuleVerdict(verdict="no_decision", reasons=undecided)
    mine = [action for action in branch if action_type(action.name) not in FILLER]
    theirs = [action for action in source if action_type(action.name) not in FILLER]
    if not mine and not theirs:
        return RuleVerdict(
            verdict="same", reasons=["neither side issued an action besides waiting"]
        )
    if not mine or not theirs:
        side = "branch" if not mine else "source"
        return RuleVerdict(
            verdict="different", reasons=[f"the {side} issued no action besides waiting"]
        )
    outcomes = [compare_actions(a, b) for a, b in zip(mine, theirs, strict=False)]
    reasons = [f"action {index + 1}: {why}" for index, (_, why) in enumerate(outcomes)]
    values: list[RuleVerdictValue] = [value for value, _ in outcomes]
    url_value, url_reason = compare_urls(branch_next.url, source_next.url)
    reasons.append(url_reason)
    if url_value is not None:
        values.append(url_value)
    if len(mine) != len(theirs):
        reasons.append(f"{len(mine)} vs {len(theirs)} actions besides waiting")
    if "different" in values:
        return RuleVerdict(verdict="different", reasons=reasons)
    if len(mine) == len(theirs) and all(value == "same" for value in values):
        return RuleVerdict(verdict="same", reasons=reasons)
    return RuleVerdict(verdict="unknown", reasons=reasons)


def fork_rule(fork: Fork) -> RuleVerdict:
    assert fork.branch_row is not None and fork.source_row is not None
    return rule_verdict(
        fork.branch_row.actions,
        fork.source_row.actions,
        fork.branch_row.next,
        fork.source_row.next,
        branch_error=step_error(fork.branch_row),
        source_error=step_error(fork.source_row),
    )


# ── the judge model ───────────────────────────────────────────────────


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no", "uncertain"]
    reason: str = Field(min_length=1)


class IntentJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    same_intent: Answer


class QualityJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasonable: Answer
    executed_as_intended: Answer


#: The questions, as the model and the reviewer read them.
QUESTIONS = {
    "same_intent": (
        "Do the two runs' actions at this screen pursue the same intent: the same target and "
        "the same purpose, even if executed a little differently?"
    ),
    "reasonable": (
        "Is the agent's batch of actions a reasonable next step toward its task, given the "
        "screen at the decision point?"
    ),
    "executed_as_intended": (
        "Does the agent's next screen show the effect its actions intended, without an error, "
        "a block (a login wall, a captcha, access denied) or missing content?"
    ),
}
QUALITY_QUESTIONS = ("reasonable", "executed_as_intended")


def describe_actions(row: StepRow, label: str) -> str:
    lines: list[str] = []
    for action in row.actions:
        params = json.dumps(action.params, ensure_ascii=False)
        line = f"{label}{action.index + 1}. {action.name} {params}"
        if action.effect_text:
            line += f" — measured: {action.effect_text}"
        if action.success is False:
            line += f" — it failed: {action.error or 'no reason recorded'}"
        lines.append(line)
    return "\n".join(lines) or "(no action)"


def _thought(row: StepRow) -> str:
    text = row.llm_output.strip()
    return text if len(text) <= THOUGHT_CHARS else text[: THOUGHT_CHARS - 3] + "..."


def markers(
    image: Run, image_step: int, run: Run, step: int, prefix: str, color: str
) -> list[imaging.Marker]:
    """A ring per action of *run*'s *step* that aimed at a point (clicks, drags, coordinate
    typing), in pixels of *image*'s screenshot at *image_step* (``inspection/marks.py``)."""
    record = run.records.get(step)
    if record is None:
        return []
    geometry = observation_geometry(image.trace, image_step)
    return [
        imaging.Marker(f"{prefix}{mark.number}", mark.x, mark.y, color)
        for mark in step_marks(record, geometry)
        if mark.kind in ("click", "drag", "type") and mark.x is not None and mark.y is not None
    ]


def _geometry(row: StepRow) -> str:
    view = row.observation
    if not (view.viewport_width and view.image_width):
        return ""
    return (
        f"\nPointer coordinates are CSS pixels of a {view.viewport_width}x{view.viewport_height} "
        f"viewport; the screenshots are {view.image_width}x{view.image_height} pixels."
    )


def _answer_shape(questions: tuple[str, ...]) -> str:
    shape = ", ".join(
        f'"{name}": {{"answer": "yes|no|uncertain", "reason": "..."}}' for name in questions
    )
    return (
        "Answer each question with yes, no or uncertain, and give a one-sentence reason.\n"
        + "\n".join(f"- {name}: {QUESTIONS[name]}" for name in questions)
        + f"\n\nReply with only a JSON object of this shape: {{{shape}}}"
    )


def quality_request(fork: Fork) -> JudgeRequest[Any] | None:
    """``reasonable`` and ``executed_as_intended``, from the branch alone (no source information)."""
    branch = fork.branch_row
    assert branch is not None
    checkpoint = fork.branch.screenshot_bytes(branch.step)
    if checkpoint is None:
        return None
    content: list[dict[str, Any]] = [
        text_part(f"The agent's task: {branch.task}{_geometry(branch)}"),
        text_part(
            "Image 1: the screen at the decision point, before the agent's actions. Red rings "
            "labelled A1, A2, … mark where its pointer actions pressed or typed."
        ),
        image_part(
            imaging.mark(
                checkpoint, markers(fork.branch, branch.step, fork.branch, branch.step, "A", RED)
            )
        ),
        text_part(
            "The agent's actions at this screen:\n"
            + describe_actions(branch, "A")
            + f"\nThe agent's own reply:\n{_thought(branch)}"
        ),
    ]
    sources = [f"{fork.branch.key}@{branch.step}"]
    following = (
        fork.branch.screenshot_bytes(branch.next.step)
        if branch.next.exists and branch.next.step is not None
        else None
    )
    if following is not None:
        content += [
            text_part("Image 2: the agent's next screen, after its actions."),
            image_part(following),
        ]
        sources.append(f"{fork.branch.key}@{branch.next.step}")
    else:
        content.append(text_part("No next screen was recorded: the run ended at this step."))
    content.append(text_part(_answer_shape(QUALITY_QUESTIONS)))
    return JudgeRequest(
        purpose=QUALITY_PURPOSE,
        item=f"{fork.branch.key}@{branch.step}",
        system=SYSTEM,
        content=content,
        reply_model=QualityJudgment,
        image_sources=sources,
    )


def intent_request(fork: Fork) -> JudgeRequest[Any] | None:
    """``same_intent``: the source's and the branch's decisions at the same screen, side by side."""
    branch, source = fork.branch_row, fork.source_row
    assert branch is not None and source is not None and fork.source is not None
    checkpoint = fork.branch.screenshot_bytes(branch.step)
    if checkpoint is None:
        return None
    rings = markers(fork.branch, branch.step, fork.source, source.step, "A", BLUE) + markers(
        fork.branch, branch.step, fork.branch, branch.step, "B", RED
    )
    content: list[dict[str, Any]] = [
        text_part(
            "Two runs of an agent with the same task reached the same screen, and each issued "
            f"actions there.\nTask: {branch.task}{_geometry(branch)}"
        ),
        text_part(
            "Image 1: the screen at the decision point. Blue rings labelled A1, A2, … mark where "
            "run A's pointer actions pressed or typed; red rings labelled B1, B2, … run B's."
        ),
        image_part(imaging.mark(checkpoint, rings)),
        text_part(
            "Run A's actions:\n"
            + describe_actions(source, "A")
            + f"\nRun A's reply:\n{_thought(source)}\n\nRun B's actions:\n"
            + describe_actions(branch, "B")
            + f"\nRun B's reply:\n{_thought(branch)}"
        ),
    ]
    sources = [f"{fork.branch.key}@{branch.step}"]
    for label, run, row in (("run A", fork.source, source), ("run B", fork.branch, branch)):
        following = (
            run.screenshot_bytes(row.next.step)
            if row.next.exists and row.next.step is not None
            else None
        )
        if following is None:
            content.append(text_part(f"No next screen was recorded for {label}."))
            continue
        content += [
            text_part(f"Image {len(sources) + 1}: {label}'s next screen, after its actions."),
            image_part(following),
        ]
        sources.append(f"{run.key}@{row.next.step}")
    content.append(text_part(_answer_shape(("same_intent",))))
    return JudgeRequest(
        purpose=INTENT_PURPOSE,
        item=f"{fork.branch.key}@{branch.step}",
        system=SYSTEM,
        content=content,
        reply_model=IntentJudgment,
        image_sources=sources,
    )


def asks_intent(fork: Fork) -> bool:
    """Same-task forks whose source made a decision to compare with."""
    return (
        fork.kind == "same_task"
        and fork.counted
        and fork.source_row is not None
        and decided(fork.source_row)
    )


# ── output ────────────────────────────────────────────────────────────


def _answers(outcome: JudgeOutcome[Any] | None) -> dict[str, Any]:
    if outcome is None or outcome.value is None:
        return {}
    return outcome.value.model_dump(mode="json")


@dataclass(frozen=True)
class EarlierAnswers:
    """The judge model's answers an earlier ``judge-forks`` run wrote, by (branch run id, checkpoint).

    Reusing them asks no model: the queue can be rebuilt (in a new wording,
    with new fields) without paying for the same answers twice.
    """

    path: Path
    #: (branch run id, checkpoint step) to that run's ``llm`` answers and ``model``.
    answers: dict[tuple[str, int], tuple[dict[str, Any], str]]

    def of(self, fork: Fork) -> tuple[dict[str, Any], str] | None:
        return self.answers.get((fork.branch.run_id, fork.checkpoint))


def earlier_answers(path: Path) -> EarlierAnswers:
    """Read the model answers of an earlier run's ``judgments.jsonl``; a foreign line is refused."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LookaheadInputError(f"--answers-from {path} cannot be read: {exc}") from exc
    found: dict[tuple[str, int], tuple[dict[str, Any], str]] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise LookaheadInputError(f"{path} line {number} is not JSON: {exc}") from exc
        if not isinstance(record, dict) or record.get("format") != JUDGMENT_FORMAT:
            raise LookaheadInputError(
                f"{path} line {number} is not a {JUDGMENT_FORMAT} record (judgments.jsonl of "
                "judge-forks)"
            )
        if record.get("llm") and record.get("model"):
            key = (str(record["branch_run_id"]), int(record["checkpoint_step"]))
            found[key] = (record["llm"], str(record["model"]))
    return EarlierAnswers(path, found)


def judgment_record(
    fork: Fork,
    rule: RuleVerdict | None,
    intent: JudgeOutcome[Any] | None,
    quality: JudgeOutcome[Any] | None,
    model: str | None,
    earlier: EarlierAnswers | None = None,
) -> dict[str, Any]:
    branch, source = fork.branch_row, fork.source_row
    answers = {**_answers(intent), **_answers(quality)}
    reused = earlier.of(fork) if earlier is not None and fork.counted else None
    if reused is not None:
        answers = {key: value for key, value in reused[0].items() if value}
        model = reused[1]
    counted: ForkStatus = "judged" if answers else "rules_only"
    status: ForkStatus = fork.excluded if fork.excluded is not None else counted
    llm = (
        {
            "same_intent": answers.get("same_intent"),
            "reasonable": answers.get("reasonable"),
            "executed_as_intended": answers.get("executed_as_intended"),
        }
        if answers
        else None
    )
    asked = intent is not None or quality is not None or reused is not None
    return {
        "format": JUDGMENT_FORMAT,
        "branch_run": fork.branch.key,
        "branch_run_id": fork.branch.run_id,
        "source_run": fork.source.key if fork.source is not None else None,
        "source_run_id": fork.branch.config.branch.source_run_id
        if fork.branch.config.branch
        else None,
        "checkpoint_step": fork.checkpoint,
        "fork_kind": fork.kind,
        "task": fork.branch.config.task,
        "source_task": fork.source.config.task if fork.source is not None else None,
        "task_variant": fork.variant.model_dump(mode="json") if fork.variant else None,
        "status": status,
        "status_reason": fork.reason,
        "checkpoint_check": fork.checkpoint_check.model_dump(mode="json")
        if fork.checkpoint_check is not None
        else None,
        "branch_actions": [action.model_dump(mode="json") for action in branch.actions]
        if branch
        else None,
        "source_actions": [action.model_dump(mode="json") for action in source.actions]
        if source
        else None,
        "branch_error": step_error(branch) if branch else None,
        "source_error": step_error(source) if source else None,
        "branch_next": branch.next.model_dump(mode="json") if branch else None,
        "source_next": source.next.model_dump(mode="json") if source else None,
        "rule": rule.model_dump(mode="json") if rule is not None else None,
        "llm": llm,
        "model": model if asked else None,
        "llm_errors": {
            "same_intent": intent.error if intent is not None else None,
            "quality": quality.error if quality is not None else None,
        },
        "llm_calls": [
            attempt["call_id"]
            for outcome in (intent, quality)
            if outcome is not None
            for attempt in outcome.attempts
        ],
        #: The earlier judgments file the answers were taken from; ``None``: asked now.
        "answers_from": str(earlier.path) if reused is not None and earlier is not None else None,
    }


ANSWERS = [
    LabelChoice(value="yes", text="是"),
    LabelChoice(value="no", text="否"),
    LabelChoice(
        value="uncertain",
        text="看不出",
        help="图看不清，或信息不够判断；请在备注里说明。",
    ),
]
#: The questions as the reviewer reads them (the model reads ``QUESTIONS``).
REVIEW_QUESTIONS = [
    Question(
        key="same_intent",
        text="意图相同：原运行和分叉在这一屏上的目标和目的一样吗？",
        help="两边要找的、要操作的是同一个东西，目的也一样，就选“是”；做法细节可以不同"
        "（点的位置略有偏差、多一步或少一步）。看“检查点”图上两种颜色的标记和两边的动作。",
        choices=ANSWERS,
    ),
    Question(
        key="reasonable",
        text="合理：只看分叉自己，按它的任务和这一屏，这批动作是朝任务前进的合理一步吗？",
        help="不要求是最好的做法，合理就选“是”。不要拿原运行来比。",
        choices=ANSWERS,
    ),
    Question(
        key="executed_as_intended",
        text="执行如意：分叉的下一屏显示出它的动作想要的效果了吗？",
        help="下一屏出现了动作想要的结果，并且没有报错、没有被拦住（登录墙、验证码、拒绝访问）、"
        "内容也没有缺失，就选“是”。",
        choices=ANSWERS,
    ),
]
TITLE = "分叉在检查点上的决定"
INSTRUCTIONS = (
    "分叉是把一次录好的运行恢复到某一步（检查点），让模型在同一个画面上重新做一次决定。"
    "请回答条目问的每个问题。“合理”和“执行如意”只看分叉自己：它的任务、它的画面和它的动作。"
    "只有“意图相同”要和原运行比较；标着“原运行”的图、标记和字段只用于这个问题。"
    "模型的判断默认不显示，请先自己判断。"
)
GUIDE = [
    GuideSection(
        title="什么是分叉",
        text=(
            "- 先有一次完整录下来的运行（原运行）。\n"
            "- 分叉把浏览器恢复到原运行的某一步（检查点），画面和原运行当时一样，"
            "再让模型在这个画面上重新决定下一步做什么。\n"
            "- 同任务：分叉的任务和原运行一样，可以比较两边的决定是不是一个意思。\n"
            "- 换任务：同一个画面，给了一个新任务；这种条目不问“意图相同”，也不显示原运行。"
        ),
    ),
    GuideSection(
        title="图和标记",
        text=(
            "- 检查点：分叉做决定时看到的画面。分叉的动作用第一种颜色标出；同任务的条目里，"
            "原运行在同一画面上的动作用第二种颜色标出（只用于“意图相同”）。\n"
            "- 分叉的下一屏：分叉执行动作之后的画面，用来判断“执行如意”。\n"
            "- 原运行的下一屏：只用于“意图相同”。\n"
            "- 标记上的编号和右边动作列表的编号一致；点“动作标记”可以隐藏或显示。"
        ),
    ),
    GuideSection(
        title="三个问题",
        text=(
            "- 意图相同：比如两边都是点开“更多筛选”来限定发布时间，即使一边先多点了一下排序，"
            "也选“是”；一边点“最多播放”、另一边去搜索别的关键词，选“否”。\n"
            "- 合理：只看分叉的任务和画面。比如任务要找播放量最高的视频，分叉点了“最多播放”排序，"
            "选“是”；点了无关的广告，选“否”。\n"
            "- 执行如意：看分叉的下一屏。想打开的页面打开了、想输入的文字出现了，选“是”；"
            "出现登录墙、验证码、“拒绝访问”、报错页或空白页，选“否”。"
        ),
    ),
    GuideSection(
        title="模型的判断",
        text=(
            "- 每个条目都有一个模型给出的判断和理由（理由是英文原文）。\n"
            "- 它默认藏起来，免得影响你的判断。先自己选，再点“查看模型的判断”对照。\n"
            "- 模型的判断只供参考，它常常会错；和你的判断不一样时，以你的为准。"
        ),
    ),
]
KIND_TEXT = {"same_task": "同任务", "task_variant": "换任务"}
SOURCE_ONLY = "（只用于“意图相同”）"


def queue_item(record: dict[str, Any], fork: Fork) -> QueueItem | None:
    branch, source = fork.branch_row, fork.source_row
    llm = record["llm"]
    if branch is None or llm is None or not fork.counted:
        return None
    intent = asks_intent(fork) and source is not None and fork.source is not None
    asked = (["same_intent"] if intent else []) + list(QUALITY_QUESTIONS)
    branch_record = fork.branch.records[branch.step]
    marks = [MarkSource(run=fork.branch.key, step=branch.step, label="分叉的动作")]
    if intent:
        assert source is not None and fork.source is not None
        marks.append(
            MarkSource(run=fork.source.key, step=source.step, label="原运行的动作" + SOURCE_ONLY)
        )
    images: list[QueueImage] = [
        observation_image(
            "checkpoint",
            "检查点（分叉和原运行都在这一屏上做决定）"
            if intent
            else "检查点（分叉在这一屏上做决定）",
            fork.branch.key,
            branch.step,
            marks_from=marks,
        )
    ]
    if branch.next.exists and branch.next.screenshot and branch.next.step is not None:
        images.append(
            observation_image(
                "branch_next", "分叉的下一屏", fork.branch.key, branch.next.step, marks_from=[]
            )
        )
    fields = [
        ItemField(text="分叉的任务", value=record["task"]),
        ItemField(
            text="分叉类型",
            value="同任务（任务和原运行一样）"
            if fork.kind == "same_task"
            else "换任务（同一个画面，新的任务）",
        ),
        ItemField(text="分叉的动作（画在“检查点”上）", value=action_lines(branch_record)),
        ItemField(text="分叉的回复（原文）", value=_thought(branch)),
    ]
    links = [ItemLink(text="打开分叉的这一步", run=fork.branch.key, step=branch.step)]
    if intent:
        assert source is not None and fork.source is not None
        if source.next.exists and source.next.screenshot and source.next.step is not None:
            images.append(
                observation_image(
                    "source_next",
                    "原运行的下一屏" + SOURCE_ONLY,
                    fork.source.key,
                    source.next.step,
                    marks_from=[],
                )
            )
        rule = record["rule"] or {}
        fields += [
            ItemField(
                text="原运行的动作" + SOURCE_ONLY,
                value=action_lines(fork.source.records[source.step]),
            ),
            ItemField(
                text="规则判断（原运行对比）",
                value=f"{rule.get('verdict')}: " + "; ".join(rule.get("reasons") or []),
                technical=True,
            ),
        ]
        links.append(
            ItemLink(text="打开原运行的这一步" + SOURCE_ONLY, run=fork.source.key, step=source.step)
        )
    check = fork.checkpoint_check
    fields += [
        ItemField(
            text="检查点恢复检查",
            value=_check_text(check) if check is not None else "未记录",
            technical=True,
        ),
        ItemField(text="分叉运行", value=fork.branch.key, technical=True),
        ItemField(text="判断模型", value=str(record["model"]), technical=True),
    ]
    answered = {key: llm[key] for key in asked if llm.get(key)}
    return QueueItem(
        item_id=f"{fork.branch.key}@{branch.step}",
        title=item_title(
            branch.observation.url,
            fork.branch.start_url,
            fork.branch.task_id,
            f"分叉 · 检查点第 {branch.step} 步 · {KIND_TEXT[fork.kind]}",
        ),
        run=fork.branch.key,
        step=branch.step,
        images=images,
        fields=fields,
        links=links,
        questions=asked,
        suggestion=Suggestion(
            answers={key: value["answer"] for key, value in answered.items()},
            reasons={key: value["reason"] for key, value in answered.items()},
            source=str(record["model"]),
        )
        if answered
        else None,
    )


def render_report(records: list[dict[str, Any]], totals: str) -> str:
    counted = [record for record in records if record["status"] in COUNTED]
    same = [record for record in counted if record["fork_kind"] == "same_task"]
    variant = [record for record in counted if record["fork_kind"] == "task_variant"]

    def intent(record: dict[str, Any]) -> str:
        llm = record["llm"]
        answer = llm.get("same_intent") if llm else None
        if answer:
            return answer["answer"]
        return "no reply" if record["llm_errors"]["same_intent"] else "not asked"

    decisive = [
        record
        for record in same
        if record["rule"]["verdict"] in ("same", "different") and intent(record) in ("yes", "no")
    ]
    agree = sum(
        1
        for record in decisive
        if (record["rule"]["verdict"] == "same") == (intent(record) == "yes")
    )
    rule_decides = sum(1 for record in same if record["rule"]["verdict"] in ("same", "different"))
    lines = [
        "# Fork judgments",
        "",
        f"Branch runs: {len(records)}. Judged (paired, restored at the checkpoint, decided by the "
        f"branch): {len(counted)} ({len(same)} same-task, {len(variant)} task-variant). Every "
        "statistic below is over these only.",
        "",
        "## Can rules judge same-task forks?",
        "",
        f"The rules decide (same or different) {share(rule_decides, len(same))} of the "
        f"same-task forks. Where both the rules and the judge model decide, they agree on "
        f"{share(agree, len(decisive))}.",
        "",
        "Rule verdict × the judge model's same_intent:",
        "",
        crosstab(
            ((record["rule"]["verdict"], intent(record)) for record in same),
            row_title="rule",
            columns=["yes", "no", "uncertain", "no reply", "not asked"],
        ),
        "",
        "## Reasonable and executed as intended",
        "",
        "Asked from the branch alone, the same way for both kinds of fork.",
        "",
    ]

    def answer(record: dict[str, Any], question: str) -> str:
        llm = record["llm"]
        found = llm.get(question) if llm else None
        return found["answer"] if found else "no reply"

    for kind, group in (("same-task", same), ("task-variant", variant)):
        if not group:
            continue
        lines += [
            f"{kind} forks, reasonable × executed_as_intended:",
            "",
            crosstab(
                (
                    (answer(record, "reasonable"), answer(record, "executed_as_intended"))
                    for record in group
                ),
                row_title="reasonable",
                columns=["yes", "no", "uncertain", "no reply"],
            ),
            "",
        ]
    rules = Counter((record["fork_kind"], record["rule"]["verdict"]) for record in counted)
    lines += [
        "## Rule verdicts by fork kind",
        "",
        table(
            ["fork kind", "same", "different", "unknown", "no_decision"],
            [
                [
                    kind,
                    *(
                        rules[(kind, value)]
                        for value in ("same", "different", "unknown", "no_decision")
                    ),
                ]
                for kind in ("same_task", "task_variant")
            ],
        ),
        "",
        "## Not judged",
        "",
    ]
    for status, title in (
        ("checkpoint_diverged", "The checkpoint did not restore"),
        ("no_decision", "The branch made no decision"),
        ("unpaired", "Not paired with a source step"),
    ):
        group = [record for record in records if record["status"] == status]
        lines.append(f"{title}: {len(group)}.")
        reasons = Counter(record["status_reason"] for record in group)
        lines += [f"- {count} × {reason}" for reason, count in reasons.most_common()]
        lines.append("")
    lines += ["## Judge model usage", "", totals, ""]
    return "\n".join(lines)


async def judge_forks(
    runs: RunSet,
    judge: Judge | None,
    out: Path,
    *,
    annotations: Path,
    queue_id: str,
    earlier: EarlierAnswers | None = None,
) -> tuple[list[dict[str, Any]], Path | None]:
    """Judge every fork; write ``judgments.jsonl``, ``report.md`` and the review queue.

    Without a judge only the rules run, and no queue is written, unless
    *earlier* answers are reused: then no model is asked and the queue is
    written with them.
    """
    found = forks(runs)
    rules = [fork_rule(fork) if fork.counted else None for fork in found]
    intents: list[JudgeOutcome[Any] | None] = [None] * len(found)
    qualities: list[JudgeOutcome[Any] | None] = [None] * len(found)
    if judge is not None:
        asked: list[tuple[list[JudgeOutcome[Any] | None], int, JudgeRequest[Any]]] = []
        for index, fork in enumerate(found):
            if not fork.counted:
                continue
            quality = quality_request(fork)
            if quality is not None:
                asked.append((qualities, index, quality))
            if asks_intent(fork):
                request = intent_request(fork)
                if request is not None:
                    asked.append((intents, index, request))
        replies = await judge.ask_all([request for _, _, request in asked])
        for (target, index, _), reply in zip(asked, replies, strict=True):
            target[index] = reply
    model = judge.model if judge is not None else None
    records = [
        judgment_record(fork, rule, intent, quality, model, earlier)
        for fork, rule, intent, quality in zip(found, rules, intents, qualities, strict=True)
    ]
    totals = (
        judge.totals.summary()
        if judge is not None
        else f"No judge model was asked; its answers were reused from {earlier.path}."
        if earlier is not None
        else "No judge model was asked."
    )
    queue: ReviewQueue | None = None
    if judge is not None or earlier is not None:
        items = [
            item
            for record, fork in zip(records, found, strict=True)
            if (item := queue_item(record, fork)) is not None
        ]
        queue = ReviewQueue(
            format=QUEUE_FORMAT,
            queue_id=queue_id,
            title=TITLE,
            instructions=INSTRUCTIONS,
            guide=GUIDE,
            suggestion_display=SuggestionDisplay(
                title="模型的判断（仅供参考）", shown="on_request"
            ),
            label=LabelSpec(questions=REVIEW_QUESTIONS, regions=None, note=True),
            items=items,
        )
    write_lines(
        out / "judgments.jsonl", (json.dumps(record, ensure_ascii=False) for record in records)
    )
    write_text(out / "report.md", render_report(records, totals))
    return records, write_queue(annotations, queue) if queue is not None else None
