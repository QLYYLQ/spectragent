"""``judge-forks``: is a branch's decision at its checkpoint the same as the source's, and is it sound?

Every branch run is paired, through its recorded provenance, with the source
run's step at the checkpoint: the branch's first policy step and the step the
source took at the same screen. Two layers judge the pair.

**Rules** compare what was recorded: action types (``click`` and
``click_coordinate`` are one type, waits are ignored), the element the
pointer hit (role and readable name), the typed text, a scroll's direction, a
navigation's URL, and the URL of the next observation. ``same`` when every
action matches and the next URLs agree, ``different`` when one difference is
certain, ``unknown`` otherwise (an unnamed element, an unmeasured hit, a
different number of actions with a common prefix).

**The judge model** sees the checkpoint screen (pointer actions ringed: the
branch's in red, the source's in blue), the branch's next screen and, for a
same-task fork, the source's next screen, and answers ``same_intent`` (same-task
forks only), ``reasonable`` and ``executed_as_intended``, each ``yes`` / ``no`` /
``uncertain`` with a one-sentence reason.

The report's agreement table compares the two layers on same-task forks.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from lookahead import imaging
from lookahead.extract import step_rows
from lookahead.judge import Judge, JudgeOutcome, JudgeRequest, image_part, text_part
from lookahead.queues import observation_image
from lookahead.report import crosstab, share, table
from lookahead.rows import ActionRow, NextObservation, StepRow
from lookahead.runs import Run, RunSet
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

RuleVerdictValue = Literal["same", "different", "unknown"]
ForkKind = Literal["same_task", "task_variant"]
PURPOSE = "fork_judgment"
JUDGMENT_FORMAT = "lookahead-fork-judgment-1"
#: Action types the rules treat as one.
FAMILY = {"click_coordinate": "click", "type_coordinate": "type"}
#: Action types the rules leave out of the comparison.
FILLER = frozenset({"wait"})
THOUGHT_CHARS = 1500

SYSTEM = (
    "You review the decisions of a web-browsing agent. You see screenshots of a browser and "
    "the actions the agent issued, each with the effect the browser measured. Judge only from "
    "what the screenshots and the action descriptions show, and reply with JSON only."
)


# ── the pair ──────────────────────────────────────────────────────────


@dataclass
class Fork:
    """One branch run and what it is compared with."""

    branch: Run
    source: Run | None
    checkpoint: int
    kind: ForkKind
    variant: BranchTaskVariant | None
    branch_row: StepRow | None
    source_row: StepRow | None
    unpaired: str | None = None


def forks(runs: RunSet) -> list[Fork]:
    """Every branch run under the root, paired with its source's step at the checkpoint."""
    cache: dict[str, dict[int, StepRow]] = {}

    def rows_of(run: Run) -> dict[int, StepRow]:
        if run.key not in cache:
            cache[run.key] = {row.step: row for row in step_rows(run, runs)}
        return cache[run.key]

    found: list[Fork] = []
    for run in runs.runs:
        branch = run.config.branch
        if branch is None:
            continue
        source = runs.source_of(run)
        variant = branch.task_variant
        fork = Fork(
            branch=run,
            source=source,
            checkpoint=branch.checkpoint_step,
            kind="task_variant" if variant is not None else "same_task",
            variant=variant,
            branch_row=None,
            source_row=None,
        )
        candidate = rows_of(run).get(branch.checkpoint_step)
        if candidate is not None and candidate.origin == "policy":
            fork.branch_row = candidate
        if source is not None:
            fork.source_row = rows_of(source).get(branch.checkpoint_step)
        if fork.branch_row is None:
            fork.unpaired = (
                f"the branch has no policy step at its checkpoint {branch.checkpoint_step} "
                f"(it ended before deciding there)"
            )
        elif source is None:
            fork.unpaired = f"its source run {branch.source_run_id!r} is not among the runs read"
        elif fork.source_row is None:
            fork.unpaired = (
                f"the source run has no step {branch.checkpoint_step} with an observation"
            )
        found.append(fork)
    return found


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


def compare_actions(branch: ActionRow, source: ActionRow) -> tuple[RuleVerdictValue, str]:
    """One branch action against the source action in the same position."""
    kind, other = action_type(branch.name), action_type(source.name)
    if kind != other:
        return "different", f"action types differ: {kind} vs {other}"
    if kind in ("click", "drag"):
        hit, other_hit = branch.facts.get("hit"), source.facts.get("hit")
        if isinstance(hit, dict) and isinstance(other_hit, dict):
            if (hit.get("role"), hit.get("name")) != (other_hit.get("role"), other_hit.get("name")):
                return "different", (
                    f"the pointer hit different elements: {_element_text(hit)} vs "
                    f"{_element_text(other_hit)}"
                )
            if hit.get("name"):
                return "same", f"the pointer hit the same element: {_element_text(hit)}"
            return "unknown", f"both hit an unnamed {hit.get('role')}; which one is unknown"
        return "unknown", (
            f"the hit element is not known on both sides ({_element_text(hit)} vs "
            f"{_element_text(other_hit)})"
        )
    if kind == "type":
        text, other_text = branch.facts.get("text"), source.facts.get("text")
        if text is None or other_text is None:
            return "unknown", "the typed text is not recorded on both sides"
        if _norm(text) == _norm(other_text):
            return "same", "typed the same text"
        return "different", f"typed different text: {text!r} vs {other_text!r}"
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


def compare_urls(url: str | None, other: str | None) -> tuple[RuleVerdictValue | None, str]:
    """The next observations' URLs: a different page is ``different``; a different query only
    (a site's random or tracking parameter as often as a real choice) decides nothing."""
    if not url or not other:
        return None, "a next URL is not known"
    if url == other:
        return "same", "the next URLs are the same"
    left, right = urlsplit(url), urlsplit(other)
    if (left.scheme, left.netloc, left.path) == (right.scheme, right.netloc, right.path):
        return "unknown", f"the next URLs differ only after the path: {url} vs {other}"
    return "different", f"the next URLs differ: {url} vs {other}"


def rule_verdict(
    branch: list[ActionRow],
    source: list[ActionRow],
    branch_next: NextObservation,
    source_next: NextObservation,
) -> RuleVerdict:
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
    values = [value for value, _ in outcomes]
    url_value, url_reason = compare_urls(branch_next.url, source_next.url)
    if url_value is not None:
        values.append(url_value)
        reasons.append(url_reason)
    if len(mine) != len(theirs):
        reasons.append(f"{len(mine)} vs {len(theirs)} actions besides waiting")
    if "different" in values:
        return RuleVerdict(verdict="different", reasons=reasons)
    if len(mine) == len(theirs) and all(value == "same" for value in values):
        return RuleVerdict(verdict="same", reasons=reasons)
    return RuleVerdict(verdict="unknown", reasons=reasons)


# ── the judge model ───────────────────────────────────────────────────


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no", "uncertain"]
    reason: str = Field(min_length=1)


class SameTaskJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    same_intent: Answer
    reasonable: Answer
    executed_as_intended: Answer


class VariantJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasonable: Answer
    executed_as_intended: Answer


QUESTIONS = {
    "same_intent": (
        "Do the branch's actions pursue the same intent as the original run's actions at this "
        "screen: the same target and the same purpose, even if executed a little differently?"
    ),
    "reasonable": (
        "Is the branch's batch of actions a reasonable next step toward the branch's own task, "
        "given the screen at the decision point?"
    ),
    "executed_as_intended": (
        "Does the branch's next screen show the effect its actions intended, without an error, "
        "a block (a login wall, a captcha, access denied) or missing content?"
    ),
}


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


def _markers(row: StepRow, prefix: str, color: str) -> list[imaging.Marker]:
    width, view = row.observation.image_width, row.observation.viewport_width
    height, view_h = row.observation.image_height, row.observation.viewport_height
    scale_x = width / view if width and view else 1.0
    scale_y = height / view_h if height and view_h else 1.0
    markers: list[imaging.Marker] = []
    for action in row.actions:
        point = action.facts.get("point")
        if isinstance(point, list) and len(point) == 2:
            markers.append(
                imaging.Marker(
                    f"{prefix}{action.index + 1}", point[0] * scale_x, point[1] * scale_y, color
                )
            )
    return markers


def judge_request(fork: Fork) -> JudgeRequest[Any] | None:
    branch, source = fork.branch_row, fork.source_row
    if branch is None or source is None or fork.source is None:
        return None
    checkpoint = fork.branch.screenshot_bytes(branch.step) or fork.source.screenshot_bytes(
        source.step
    )
    if checkpoint is None:
        return None
    same_task = fork.kind == "same_task"
    markers = _markers(branch, "B", "#e8202a")
    if same_task:
        markers += _markers(source, "S", "#1f6feb")
    view = branch.observation
    content: list[dict[str, Any]] = [
        text_part(
            f"The branch's task: {branch.task}\n"
            + (
                ""
                if same_task
                else f"(The branch was given this task at a screen the original run reached "
                f"while working on another task: {source.task})\n"
            )
            + (
                f"\nPointer coordinates are CSS pixels of a {view.viewport_width}x"
                f"{view.viewport_height} viewport; the screenshots are {view.image_width}x"
                f"{view.image_height} pixels."
                if view.viewport_width and view.image_width
                else ""
            )
        ),
        text_part(
            "Image 1: the screen at the decision point, before the branch's actions. "
            + (
                "Red rings labelled B1, B2, … mark where the branch's pointer actions pressed; "
                "blue rings labelled S1, S2, … where the original run's pressed."
                if same_task
                else "Red rings labelled B1, B2, … mark where the branch's pointer actions pressed."
            )
        ),
        image_part(imaging.mark(checkpoint, markers)),
        text_part(
            "The branch's actions at this screen:\n"
            + describe_actions(branch, "B")
            + f"\nThe branch agent's own reply:\n{_thought(branch)}"
        ),
    ]
    sources = [f"{fork.branch.key}@{branch.step}"]
    branch_next = (
        fork.branch.screenshot_bytes(branch.next.step)
        if branch.next.exists and branch.next.step is not None
        else None
    )
    if branch_next is not None:
        content += [
            text_part("Image 2: the branch's next screen, after its actions."),
            image_part(branch_next),
        ]
        sources.append(f"{fork.branch.key}@{branch.next.step}")
    else:
        content.append(text_part("The branch recorded no next screen: its run ended at this step."))
    if same_task:
        content.append(
            text_part(
                "The original run's actions at the same screen:\n"
                + describe_actions(source, "S")
                + f"\nThe original agent's own reply:\n{_thought(source)}"
            )
        )
        source_next = (
            fork.source.screenshot_bytes(source.next.step)
            if source.next.exists and source.next.step is not None
            else None
        )
        if source_next is not None:
            content += [
                text_part(
                    f"Image {len(sources) + 1}: the original run's next screen, after its actions."
                ),
                image_part(source_next),
            ]
            sources.append(f"{fork.source.key}@{source.next.step}")
    questions = fork_questions(fork.kind)
    shape = ", ".join(
        f'"{name}": {{"answer": "yes|no|uncertain", "reason": "..."}}' for name in questions
    )
    content.append(
        text_part(
            "Answer each question with yes, no or uncertain, and give a one-sentence reason.\n"
            + "\n".join(f"- {name}: {QUESTIONS[name]}" for name in questions)
            + f"\n\nReply with only a JSON object of this shape: {{{shape}}}"
        )
    )
    return JudgeRequest(
        purpose=PURPOSE,
        item=f"{fork.branch.key}@{branch.step}",
        system=SYSTEM,
        content=content,
        reply_model=SameTaskJudgment if same_task else VariantJudgment,
        image_sources=sources,
    )


# ── output ────────────────────────────────────────────────────────────


def judgment_record(
    fork: Fork,
    rule: RuleVerdict | None,
    outcome: JudgeOutcome[Any] | None,
    model: str | None,
) -> dict[str, Any]:
    branch, source = fork.branch_row, fork.source_row
    value = outcome.value if outcome is not None else None
    llm = value.model_dump(mode="json") if value is not None else None
    if llm is not None:
        llm.setdefault("same_intent", None)
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
        "status": "unpaired" if fork.unpaired else ("judged" if llm is not None else "rules_only"),
        "unpaired_reason": fork.unpaired,
        "branch_actions": [action.model_dump(mode="json") for action in branch.actions]
        if branch
        else None,
        "source_actions": [action.model_dump(mode="json") for action in source.actions]
        if source
        else None,
        "branch_next": branch.next.model_dump(mode="json") if branch else None,
        "source_next": source.next.model_dump(mode="json") if source else None,
        "rule": rule.model_dump(mode="json") if rule is not None else None,
        "llm": llm,
        "model": model if outcome is not None else None,
        "llm_error": outcome.error if outcome is not None else None,
        "llm_calls": [attempt["call_id"] for attempt in outcome.attempts] if outcome else [],
    }


ANSWERS = [
    LabelChoice(value="yes", text="Yes"),
    LabelChoice(value="no", text="No"),
    LabelChoice(value="uncertain", text="Cannot tell"),
]
INSTRUCTIONS = (
    "A branch run resumed a recorded run at a checkpoint and decided that step again. Compare "
    "the branch's decision with the screens and answer each question the item asks; a "
    "task-variant branch (given a different task) is not asked about intent. The suggestion "
    "is the judge model's answer, with its reasons."
)


def fork_questions(kind: ForkKind) -> list[str]:
    """The question keys asked of a fork of *kind*: intent only when the task is the same."""
    keys = list(QUESTIONS)
    return keys if kind == "same_task" else [key for key in keys if key != "same_intent"]


def queue_item(record: dict[str, Any], fork: Fork) -> QueueItem | None:
    branch, source = fork.branch_row, fork.source_row
    llm = record["llm"]
    if branch is None or source is None or fork.source is None or llm is None:
        return None
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
    if fork.kind == "same_task" and source.next.exists and source.next.screenshot:
        assert source.next.step is not None
        images.append(
            observation_image(
                "source_next", "The original run's next screen", fork.source.key, source.next.step
            )
        )
    asked = fork_questions(fork.kind)
    answers = {key: llm[key]["answer"] for key in asked}
    reasons = {key: llm[key]["reason"] for key in asked}
    explained = [f"{key}: {answers[key]} — {reasons[key]}" for key in asked]
    rule = record["rule"] or {}
    fields = [
        ItemField(text="Fork", value=fork.kind.replace("_", " ")),
        ItemField(text="Branch task", value=record["task"]),
        ItemField(text="Original task", value=record["source_task"] or "unknown"),
        ItemField(text="Branch actions", value=describe_actions(branch, "B")),
        ItemField(text="Original actions", value=describe_actions(source, "S")),
        ItemField(
            text="Rule verdict",
            value=f"{rule.get('verdict')}: " + "; ".join(rule.get("reasons") or []),
        ),
        ItemField(text="Judge model", value="\n".join(explained)),
    ]
    return QueueItem(
        item_id=f"{fork.branch.key}@{branch.step}",
        title=f"{fork.branch.key} checkpoint {branch.step} ({fork.kind.replace('_', ' ')})",
        run=fork.branch.key,
        step=branch.step,
        images=images,
        fields=fields,
        links=[
            ItemLink(text="Branch run", run=fork.branch.key, step=branch.step),
            ItemLink(text="Source run", run=fork.source.key, step=source.step),
        ],
        questions=asked,
        suggestion=Suggestion(answers=answers, reasons=reasons, source=str(record["model"])),
    )


def render_report(records: list[dict[str, Any]], totals: str) -> str:
    paired = [record for record in records if record["status"] != "unpaired"]
    same = [record for record in paired if record["fork_kind"] == "same_task"]
    variant = [record for record in paired if record["fork_kind"] == "task_variant"]

    def intent(record: dict[str, Any]) -> str:
        llm = record["llm"]
        if llm is None:
            return "no reply"
        answer = llm.get("same_intent")
        return answer["answer"] if answer else "not asked"

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
        f"Branch runs: {len(records)}. Paired with their source step: {len(paired)} "
        f"({len(same)} same-task, {len(variant)} task-variant).",
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
            columns=["yes", "no", "uncertain", "no reply"],
        ),
        "",
        "## Reasonable and executed as intended",
        "",
    ]

    def answer(record: dict[str, Any], question: str) -> str:
        llm = record["llm"]
        if llm is None:
            return "no reply"
        return llm[question]["answer"]

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
    rules = Counter((record["fork_kind"], record["rule"]["verdict"]) for record in paired)
    lines += [
        "## Rule verdicts by fork kind",
        "",
        table(
            ["fork kind", "same", "different", "unknown"],
            [
                [kind, rules[(kind, "same")], rules[(kind, "different")], rules[(kind, "unknown")]]
                for kind in ("same_task", "task_variant")
            ],
        ),
        "",
    ]
    unpaired = [record for record in records if record["status"] == "unpaired"]
    if unpaired:
        lines += ["## Not paired", ""]
        lines += [f"- `{record['branch_run']}`: {record['unpaired_reason']}" for record in unpaired]
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
    rules: list[RuleVerdict | None] = []
    for fork in found:
        if fork.unpaired is None:
            assert fork.branch_row is not None and fork.source_row is not None
            rules.append(
                rule_verdict(
                    fork.branch_row.actions,
                    fork.source_row.actions,
                    fork.branch_row.next,
                    fork.source_row.next,
                )
            )
        else:
            rules.append(None)
    requests = [judge_request(fork) if fork.unpaired is None else None for fork in found]
    outcomes: list[JudgeOutcome[Any] | None] = [None] * len(found)
    if judge is not None:
        asked = [(index, request) for index, request in enumerate(requests) if request is not None]
        replies = await judge.ask_all([request for _, request in asked])
        for (index, _), reply in zip(asked, replies, strict=True):
            outcomes[index] = reply
    model = judge.model if judge is not None else None
    records = [
        judgment_record(fork, rule, outcome, model)
        for fork, rule, outcome in zip(found, rules, outcomes, strict=True)
    ]
    out.mkdir(parents=True, exist_ok=True)
    with (out / "judgments.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    totals = judge.totals.summary() if judge is not None else "No judge model was asked."
    (out / "report.md").write_text(render_report(records, totals), encoding="utf-8")
    if judge is None:
        return records, None
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
    return records, write_queue(annotations, queue)
