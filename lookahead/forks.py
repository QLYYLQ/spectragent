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
from lookahead.queues import observation_image
from lookahead.report import crosstab, share, table
from lookahead.rows import ActionRow, NextObservation, RestorationSummary, StepRow
from lookahead.runs import READ_ERRORS, Run, RunSet, unreadable
from spectragent.annotation import (
    QUEUE_FORMAT,
    ItemField,
    ItemLink,
    LabelChoice,
    LabelSpec,
    Question,
    QueueImage,
    QueueItem,
    ReviewQueue,
    Suggestion,
    write_queue,
)
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


def markers(row: StepRow, prefix: str, color: str) -> list[imaging.Marker]:
    """A ring per action that aimed at a point (clicks, drags, coordinate typing), in image pixels."""
    width, view = row.observation.image_width, row.observation.viewport_width
    height, view_h = row.observation.image_height, row.observation.viewport_height
    scale_x = width / view if width and view else 1.0
    scale_y = height / view_h if height and view_h else 1.0
    found: list[imaging.Marker] = []
    for action in row.actions:
        point = action_point(action)
        if point is not None:
            found.append(
                imaging.Marker(
                    f"{prefix}{action.index + 1}", point[0] * scale_x, point[1] * scale_y, color
                )
            )
    return found


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
        image_part(imaging.mark(checkpoint, markers(branch, "A", "#e8202a"))),
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
    rings = markers(source, "A", "#1f6feb") + markers(branch, "B", "#e8202a")
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


def judgment_record(
    fork: Fork,
    rule: RuleVerdict | None,
    intent: JudgeOutcome[Any] | None,
    quality: JudgeOutcome[Any] | None,
    model: str | None,
) -> dict[str, Any]:
    branch, source = fork.branch_row, fork.source_row
    answers = {**_answers(intent), **_answers(quality)}
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
    asked = intent is not None or quality is not None
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
    }


ANSWERS = [
    LabelChoice(value="yes", text="Yes"),
    LabelChoice(value="no", text="No"),
    LabelChoice(value="uncertain", text="Cannot tell"),
]
INSTRUCTIONS = (
    "A branch run resumed a recorded run at a checkpoint and decided that step again. Answer "
    "each question the item asks. 'Reasonable' and 'executed as intended' are about the "
    "branch alone: judge them from its task, its screens and its actions. Only the "
    "'same intent' question compares it with the original run; the fields and the image "
    "marked 'For same intent' belong to that question. The suggestion is the judge model's."
)
FOR_INTENT = "For same intent: "


def queue_item(record: dict[str, Any], fork: Fork) -> QueueItem | None:
    branch, source = fork.branch_row, fork.source_row
    llm = record["llm"]
    if branch is None or llm is None or not fork.counted:
        return None
    intent = asks_intent(fork) and source is not None and fork.source is not None
    asked = (["same_intent"] if intent else []) + list(QUALITY_QUESTIONS)
    images: list[QueueImage] = [
        observation_image(
            "checkpoint", "The screen at the decision point", fork.branch.key, branch.step
        )
    ]
    if branch.next.exists and branch.next.screenshot and branch.next.step is not None:
        images.append(
            observation_image(
                "branch_next", "The branch's next screen", fork.branch.key, branch.next.step
            )
        )
    fields = [
        ItemField(text="Task", value=record["task"]),
        ItemField(text="Branch actions", value=describe_actions(branch, "B")),
        ItemField(text="Branch reply", value=_thought(branch)),
    ]
    links = [ItemLink(text="Branch run", run=fork.branch.key, step=branch.step)]
    if intent:
        assert source is not None and fork.source is not None
        if source.next.exists and source.next.screenshot and source.next.step is not None:
            images.append(
                observation_image(
                    "source_next",
                    FOR_INTENT + "the original run's next screen",
                    fork.source.key,
                    source.next.step,
                )
            )
        rule = record["rule"] or {}
        fields += [
            ItemField(
                text=FOR_INTENT + "the original run's actions", value=describe_actions(source, "S")
            ),
            ItemField(
                text=FOR_INTENT + "rule verdict",
                value=f"{rule.get('verdict')}: " + "; ".join(rule.get("reasons") or []),
            ),
        ]
        links.append(
            ItemLink(text=FOR_INTENT + "the original run", run=fork.source.key, step=source.step)
        )
    answered = {key: llm[key] for key in asked if llm.get(key)}
    return QueueItem(
        item_id=f"{fork.branch.key}@{branch.step}",
        title=f"{fork.branch.key} checkpoint {branch.step} ({fork.kind.replace('_', ' ')})",
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
    runs: RunSet, judge: Judge | None, out: Path, *, annotations: Path, queue_id: str
) -> tuple[list[dict[str, Any]], Path | None]:
    """Judge every fork; write ``judgments.jsonl``, ``report.md`` and the review queue.

    Without a judge only the rules run, and no queue is written.
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
        judgment_record(fork, rule, intent, quality, model)
        for fork, rule, intent, quality in zip(found, rules, intents, qualities, strict=True)
    ]
    totals = judge.totals.summary() if judge is not None else "No judge model was asked."
    queue: ReviewQueue | None = None
    if judge is not None:
        items = [
            item
            for record, fork in zip(records, found, strict=True)
            if (item := queue_item(record, fork)) is not None
        ]
        queue = ReviewQueue(
            format=QUEUE_FORMAT,
            queue_id=queue_id,
            title="Branch decisions at the checkpoint",
            instructions=INSTRUCTIONS,
            label=LabelSpec(
                questions=[
                    Question(key=key, text=text, choices=ANSWERS) for key, text in QUESTIONS.items()
                ],
                regions=None,
                note=True,
            ),
            items=items,
        )
    write_lines(
        out / "judgments.jsonl", (json.dumps(record, ensure_ascii=False) for record in records)
    )
    write_text(out / "report.md", render_report(records, totals))
    return records, write_queue(annotations, queue) if queue is not None else None
