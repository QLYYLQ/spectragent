"""The export job's nested knobs (arch §4.15 "配置").

Frozen pydantic dataclasses validate each settings block. conscribe imports
these original types into the generated config, preserving their validators.
``RequireEval`` is shared with the ``trace_branches`` loader.

"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from spectragent.core.data_loader.task.require_eval import RequireEval
from spectragent.core.trace.sources import AgentOutcomeFilter
from spectragent.schemas._types import Unlimited

SplitBy = Literal["site", "task"]
DecontaminationMatch = Literal["task_id", "site_instruction", "either"]
ImageTokens = Literal["qwen_vl", "fixed"]
#: ``thought_index_refs``: drop a teacher thought naming element indices the
#: student does not see, or keep it as written.
ThoughtIndexRefs = Literal["drop", "keep"]
#: ``wait_actions``: keep every recorded wait, or remove the waits a teacher
#: took on a capture whose settle wait was capped (arch §4.15 "等待动作").
WaitActions = Literal["keep", "drop_when_capped"]


def thought_rewrite_problem(
    *,
    wait_actions: WaitActions,
    thought_source: Literal["teacher", "none"],
    models: int,
    cache: str | None,
) -> str | None:
    """Why the thought-rewrite fields disagree with ``wait_actions``, or ``None``.

    The one statement of the rule: ``thought_rewrite_llms`` (exactly one
    model) and ``thought_rewrite_cache`` are needed exactly when waits are
    dropped and there are teacher thoughts to rewrite
    (``wait_actions: drop_when_capped`` with ``thought_source: teacher``);
    anywhere else they would be configured and never used, so they are
    refused. *models* is how many models ``thought_rewrite_llms`` holds.
    """
    needed = wait_actions == "drop_when_capped" and thought_source == "teacher"
    if needed:
        if models > 1:
            return f"thought_rewrite_llms holds at most one model, it holds {models}"
        missing = [
            what
            for what, given in (
                ("thought_rewrite_llms (the helper chat model that rewrites them)", models == 1),
                ("thought_rewrite_cache (the directory its rewrites are cached in)", bool(cache)),
            )
            if not given
        ]
        if missing:
            return (
                f"wait_actions: drop_when_capped with thought_source: teacher rewrites the "
                f"affected thoughts, so it needs {' and '.join(missing)}"
            )
        return None
    configured = [
        name
        for name, given in (
            ("thought_rewrite_llms", models > 0),
            ("thought_rewrite_cache", cache is not None),
        )
        if given
    ]
    if configured:
        reason = (
            "wait_actions is keep"
            if wait_actions == "keep"
            else "thought_source is none, so there is no thought to rewrite"
        )
        return (
            f"{' and '.join(configured)} would never be used: {reason}. Remove "
            f"{'them' if len(configured) > 1 else 'it'}, or set wait_actions: "
            f"drop_when_capped with thought_source: teacher"
        )
    return None


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class ExportFilters:
    """Which matched sources, and which of their steps, become samples (``filters``).

    A source a filter leaves out is listed in the dataset manifest's
    ``sources.filtered`` with the reason, a step one leaves out in its
    ``steps.filtered``; neither is an error. Whether a source's trace is
    closed and complete through a step is not a filter but a hard condition
    the export always applies (arch §4.15).
    """

    agent_outcome: AgentOutcomeFilter = Field(
        default="any",
        description=(
            "Keep sources whose agent ended this way, as their result.json says (done, "
            "max_steps, error, timeout, vetoed); 'any' (the default) keeps every outcome."
        ),
    )
    require_eval: RequireEval | None = Field(
        default=None,
        description=(
            "Keep sources an evaluator graded 'label' (evaluator_id, label: 1, slug: null — "
            "a branch run is judged by its source run's verdicts). None: no evaluation needed."
        ),
    )
    exclude_restoration_diverged: bool = Field(
        default=True,
        description=(
            "Leave out a run any of whose steps recorded a restoration check that did not "
            "pass: verdict warn (the branch went on from a state that is not the recorded "
            "one) or diverged (strict ended it)."
        ),
    )
    max_document_misses: Annotated[int, Field(ge=0)] | Unlimited = Field(
        default=0,
        description=(
            "Leave out a replayed run whose replay log counts more main-frame document "
            "misses than this (a run without a replay log counts 0). Default 0; "
            "'unlimited': no limit."
        ),
    )
    exclude_sites: tuple[str, ...] = Field(
        default=(),
        description=(
            "Host names whose runs are left out: a source's site key equal to an entry or "
            "ending in '.' + entry."
        ),
    )
    exclude_unloaded_steps: bool = Field(
        default=True,
        description=(
            "A step whose recorded observation was captured before it finished its initial "
            "load (its capture_state settle says loaded: false; in a browser, the main "
            "document had not reached its load event, however the wait ended) produces no "
            "sample; "
            "it stays in later samples' context as it happened. A page whose DOM never went "
            "quiet does not count, and a step with no capture_state recorded is exported. "
            "Each one left out is listed in the manifest's steps.filtered with the reason. "
            "false: no check."
        ),
    )


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class SplitSettings:
    """How samples are split into ``train`` and ``validation`` (``split``) — never by step."""

    by: SplitBy = Field(
        default="site",
        description=(
            "site: the source task's start_url host; task: <benchmark>/<subset>/<task_id>. "
            "A branch run uses its source task."
        ),
    )
    validation_fraction: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="The share of split keys hashed into validation.",
    )
    salt: str = Field(
        default="af-split-1",
        description="Mixed into the key's hash; change it to draw a different split.",
    )


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class BenchmarkRef:
    """A benchmark a student will be evaluated on: its loader key and subset."""

    benchmark: str = Field(description="The task / eval loader key (benchmark_name).")
    subset: str | None = Field(
        default=None,
        description=(
            "The subset to read: the export document's loaders of this subset (or, when it "
            "configures none for the benchmark, the registered loader built with it). None: "
            "every loader the document configures for the benchmark, else the registered "
            "loader's default subset."
        ),
    )


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class Decontamination:
    """Which sources an evaluation benchmark rules out (``decontamination``)."""

    against: tuple[BenchmarkRef, ...] = Field(
        default=(),
        description=(
            "Benchmarks the student will be evaluated on, read through the export "
            "document's eval_loader / task_loader blocks (by benchmark_name; else each "
            "benchmark's registered loader with its defaults). Only the benchmarks listed "
            "here are checked. Default []: nothing is decontaminated, and the dataset "
            "manifest says decontaminated: false."
        ),
    )
    match: DecontaminationMatch = Field(
        default="either",
        description=(
            "task_id: the source task's id is one of the benchmark's tasks; "
            "site_instruction: its start_url host and normalised instruction match one; "
            "either."
        ),
    )


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class LengthBudget:
    """The longest record a trainer may receive (``length``) — over it is an error, never a cut."""

    max_tokens: int = Field(ge=1, description="The estimated token ceiling of one record.")
    image_tokens: ImageTokens = Field(
        default="qwen_vl",
        description=(
            "qwen_vl: ceil(w/28) * ceil(h/28) + 2 per image (image pixels); fixed: "
            "fixed_image_tokens per image."
        ),
    )
    fixed_image_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Tokens per image under image_tokens: fixed, where it is required. None (the "
            "default): not set, which only image_tokens: qwen_vl accepts."
        ),
    )
    chars_per_token: float = Field(
        default=4.0, gt=0.0, description="Characters of text per estimated token."
    )

    def __post_init__(self) -> None:
        if self.image_tokens == "fixed" and self.fixed_image_tokens is None:
            raise ValueError("length: image_tokens: fixed needs fixed_image_tokens")


#: ``filters`` when the config leaves it null.
DEFAULT_FILTERS = ExportFilters()
#: ``split`` when the config leaves it null.
DEFAULT_SPLIT = SplitSettings()
#: ``decontamination`` when the config leaves it null.
DEFAULT_DECONTAMINATION = Decontamination()

__all__ = [
    "DEFAULT_DECONTAMINATION",
    "DEFAULT_FILTERS",
    "DEFAULT_SPLIT",
    "BenchmarkRef",
    "Decontamination",
    "DecontaminationMatch",
    "ExportFilters",
    "ImageTokens",
    "LengthBudget",
    "RequireEval",
    "SplitBy",
    "SplitSettings",
    "ThoughtIndexRefs",
    "WaitActions",
    "thought_rewrite_problem",
]
