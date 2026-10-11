"""The flywheel report: every measure, every section, a recommendation each (arch §4.16 "报告").

:func:`build_report` opens a chain root (``metrics.open_chain``), computes
every PRD §9 metric, every PRD §10 / §11 section M33 owns and the inputs of
the decisions PRD M33 asks for, and attaches a :class:`Recommendation` to
each — a rule (its thresholds are the named constants below), the evidence
it rests on (JSON pointers into this report, with their values and
denominators) and the basis: ``rehearsal`` evidence (fixture data) is at
most ``partial`` — "the fixture only calibrates the measure; decide after
the live run" — and no data is ``insufficient`` with no verdict. A default
conclusion is never given.

:func:`write_report` writes ``report.json`` (format ``af-flywheel-report-1``:
sorted keys, fixed separators, no wall clock, no absolute path, no host
information — the same inputs give the same bytes) and ``report.md``,
rendered from the same model. Planted credential values never enter either:
the privacy measure reports labels and counts.

.. note::
   No ``from __future__ import annotations``: pydantic models throughout.
"""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from spectragent.flywheel.metrics import (
    DATASETS,
    ROLES,
    AntiBot,
    ArchiveOverhead,
    BodyCompletenessMetric,
    BranchCoverage,
    BranchDocumentMisses,
    ChainLayout,
    ChainRuns,
    DuplicateBranches,
    ExportDeterminism,
    ExportYield,
    Planted,
    PostBranchMisses,
    Privacy,
    Ratio,
    ReplayDeterminism,
    RestorationRate,
    Retention,
    SamePathFidelity,
    SiteRuleCandidates,
    Sites,
    Storage,
    anti_bot,
    archive_overhead,
    body_completeness_metric,
    branch_coverage,
    branch_document_misses,
    components,
    duplicate_branches,
    export_determinism,
    export_yield,
    failed_on_model_call,
    open_chain,
    post_branch_misses,
    privacy,
    read_dataset_manifest,
    replay_determinism,
    restoration,
    retention,
    same_path_fidelity,
    site_rules,
    sites,
    storage,
)

#: ``report.json``'s format.
REPORT_FORMAT = "af-flywheel-report-1"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"

Evidence = Literal["live", "rehearsal"]
Status = Literal["supported", "partial", "insufficient"]

# ── the rules' thresholds (each written into the rule that uses it) ────

#: A site whose same-path fidelity (the restorer's verdict) is below this is excluded.
EXCLUDE_FIDELITY = 0.8
#: Same-path fidelity at or above which offline replay is usable as a measuring tool.
FIDELITY_USABLE = 0.9
#: Restoration verification rate at or above which branching is usable.
RESTORATION_USABLE = 0.9
#: Q1: share of branches with no main-frame document miss at or above which ``strict`` stays.
Q1_KEEP_STRICT = 0.9
#: Q4: share of the branches' document misses that are GETs of unrecorded URLs from which
#: recording-time coverage (prefetch / exploration) is worth expanding.
Q4_EXPAND = 0.5
#: Site rules: near misses on one host from which a generated ``ignore_query`` rule is proposed.
SITE_RULE_MIN_MISSES = 2
#: Per-step p50 archive overhead (ms) archiving on by default may cost — M29's bound.
OVERHEAD_P50_BOUND_MS = 250.0
#: Records with a body, of those that need one, for capture quality to be sufficient.
WITH_BODY_MIN = 0.95
#: Identical terminal-screenshot pairs, of the comparable ones, for replay to count as deterministic.
DETERMINISM_MIN = 1.0
#: Duplicate branches above this share waste teacher calls.
DUPLICATE_RATE_MAX = 0.5
#: A storage category whose gzip ratio is at or below this is worth compressing.
COMPRESS_RATIO_MAX = 0.5
#: ``action_failed`` samples up to this share are kept (labelled); above it, dropped.
ACTION_FAILED_KEEP_MAX = 0.2
#: Task counts the retention section projects to (S2b, the live chain, the whole All split).
PROJECTION_TASKS: tuple[int, ...] = (10, 20, 300)

#: What a ``rehearsal`` basis means, written into every one of its recommendations.
REHEARSAL_NOTE = "fixture data calibrates the measure only; decide after the live run"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceRef(_Model):
    """One value a recommendation rests on: where it is in this report, what it is, out of how many."""

    pointer: str
    value: Any
    n: int | None = None


class Recommendation(_Model):
    """A rule applied to evidence (arch §4.16 "建议槽").

    ``status``: ``insufficient`` when there is no data (``verdict`` is then
    ``None`` — never a default conclusion); otherwise ``partial`` on a
    ``rehearsal`` basis and ``supported`` on a ``live`` one.
    """

    status: Status
    verdict: str | None
    rule: str
    evidence: list[EvidenceRef]
    basis: Evidence


class ComponentCount(_Model):
    components: dict[str, Any]
    runs: int


class RoleInputs(_Model):
    present: bool
    runs: int
    skipped: dict[str, str]
    components: list[ComponentCount]
    #: Runs that ended on a failed model call (``metrics.failed_on_model_call``): the route's
    #: failure — a 429, a transport hang (PRD H28) — so what they did not do is not the site's.
    model_failures: list[str]


class DatasetInputs(_Model):
    present: bool
    format: str | None


class Inputs(_Model):
    roles: dict[str, RoleInputs]
    datasets: dict[str, DatasetInputs]


class ChainInfo(_Model):
    name: str | None
    evidence: Evidence


class Metrics(_Model):
    """PRD §9, one field per row."""

    archive_overhead: ArchiveOverhead
    body_completeness: BodyCompletenessMetric
    same_path_fidelity: SamePathFidelity
    restoration: RestorationRate
    branch_coverage: BranchCoverage
    replay_determinism: ReplayDeterminism
    export_determinism: ExportDeterminism
    privacy: Privacy


class Sections(_Model):
    """PRD §10 / §11, one field per item M33 owns."""

    sites: Sites
    anti_bot: AntiBot
    duplicate_branches: DuplicateBranches
    post_branch_misses: PostBranchMisses
    storage: Storage
    retention: Retention
    export_yield: ExportYield


class DecisionInputs(_Model):
    """What the pending decisions read beyond the metrics and sections."""

    branch_document_misses: BranchDocumentMisses
    site_rule_candidates: SiteRuleCandidates


class MetricRecommendations(_Model):
    archive_overhead: Recommendation
    body_completeness: Recommendation
    same_path_fidelity: Recommendation
    restoration: Recommendation
    branch_coverage: Recommendation
    replay_determinism: Recommendation
    export_determinism: Recommendation
    privacy: Recommendation


class SectionRecommendations(_Model):
    sites: Recommendation
    anti_bot: Recommendation
    duplicate_branches: Recommendation
    post_branch_misses: Recommendation
    storage: Recommendation
    retention: Recommendation
    export_yield: Recommendation


class Recommendations(_Model):
    metrics: MetricRecommendations
    sections: SectionRecommendations


class Decisions(_Model):
    """PRD M33 / §11: the open decisions, each with an evidence-backed recommendation."""

    q1_miss_policy: Recommendation
    q4_coverage: Recommendation
    site_rules: Recommendation
    q7_retention: Recommendation


class FlywheelReport(_Model):
    """The report (``report.json``)."""

    format: Literal["af-flywheel-report-1"] = REPORT_FORMAT
    chain: ChainInfo
    inputs: Inputs
    metrics: Metrics
    sections: Sections
    decision_inputs: DecisionInputs
    recommendations: Recommendations
    decisions: Decisions


# ── evidence and recommendations ──────────────────────────────────────


def pointer(*parts: str | int) -> str:
    """An RFC 6901 JSON pointer to ``parts`` (``~`` and ``/`` escaped)."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _ratio_ref(value: Ratio, *parts: str) -> EvidenceRef:
    return EvidenceRef(pointer=pointer(*parts, "value"), value=value.value, n=value.denominator)


def _recommend(
    basis: Evidence,
    rule: str,
    evidence: Sequence[EvidenceRef],
    verdict: str | None,
    *,
    has_data: bool,
) -> Recommendation:
    if not has_data:
        return Recommendation(
            status="insufficient", verdict=None, rule=rule, evidence=list(evidence), basis=basis
        )
    return Recommendation(
        status="partial" if basis == "rehearsal" else "supported",
        verdict=verdict,
        rule=rule,
        evidence=list(evidence),
        basis=basis,
    )


def _share(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _bytes(value: float | None) -> str:
    if value is None:
        return "n/a"
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if abs(value) >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value:.0f} B"


# ── per-metric rules ──────────────────────────────────────────────────


def _rec_overhead(metric: ArchiveOverhead, basis: Evidence) -> Recommendation:
    base = ("metrics", "archive_overhead")
    evidence = [
        EvidenceRef(
            pointer=pointer(*base, "p50_overhead_ms"),
            value=metric.p50_overhead_ms,
            n=metric.archived.steps,
        ),
        EvidenceRef(
            pointer=pointer(*base, "p95_overhead_ms"),
            value=metric.p95_overhead_ms,
            n=metric.archived.steps,
        ),
        EvidenceRef(
            pointer=pointer(*base, "bytes", "per_step_after"),
            value=metric.bytes.per_step_after,
            n=metric.bytes.steps,
        ),
    ]
    p50 = metric.p50_overhead_ms
    verdict = None
    if p50 is not None:
        verdict = (
            f"archiving on by default is affordable: {p50:+.0f} ms per step at p50"
            if p50 <= OVERHEAD_P50_BOUND_MS
            else f"archiving costs {p50:+.0f} ms per step at p50: keep it opt-in"
        )
    return _recommend(
        basis,
        f"p50 overhead (archived minus control, policy steps from step 1) ≤ "
        f"{OVERHEAD_P50_BOUND_MS:.0f} ms (OVERHEAD_P50_BOUND_MS, M29's bound): affordable",
        evidence,
        verdict,
        has_data=p50 is not None,
    )


def _rec_body(metric: BodyCompletenessMetric, basis: Evidence) -> Recommendation:
    base = ("metrics", "body_completeness")
    evidence = [
        _ratio_ref(metric.with_body, *base, "with_body"),
        _ratio_ref(metric.complete, *base, "complete"),
    ]
    rate = metric.with_body.value
    verdict = None
    if rate is not None:
        verdict = (
            "capture is complete enough for replay"
            if rate >= WITH_BODY_MIN
            else "too many records lack a body: review too_large / incomplete before relying on replay"
        )
    return _recommend(
        basis,
        f"records with a body ≥ {WITH_BODY_MIN} of those needing one (WITH_BODY_MIN)",
        evidence,
        verdict,
        has_data=metric.records > 0,
    )


def _rec_fidelity(metric: SamePathFidelity, basis: Evidence) -> Recommendation:
    base = ("metrics", "same_path_fidelity", "overall")
    evidence = [
        _ratio_ref(metric.overall.restorer, *base, "restorer"),
        _ratio_ref(metric.overall.strict, *base, "strict"),
        _ratio_ref(metric.overall.runs_passing, *base, "runs_passing"),
    ]
    value = metric.overall.restorer.value
    verdict = None
    if value is not None:
        verdict = (
            "same-path replay reproduces the recordings: usable as a measuring tool"
            if value >= FIDELITY_USABLE
            else "same-path replay diverges: exclude the low-fidelity sites (sections.sites) first"
        )
    return _recommend(
        basis,
        f"restorer-verified steps that passed ≥ {FIDELITY_USABLE} (FIDELITY_USABLE); a site below "
        f"{EXCLUDE_FIDELITY} is excluded (EXCLUDE_FIDELITY)",
        evidence,
        verdict,
        has_data=metric.overall.verified_steps > 0,
    )


def _rec_restoration(metric: RestorationRate, basis: Evidence) -> Recommendation:
    evidence = [
        EvidenceRef(
            pointer=pointer("metrics", "restoration", "rate"), value=metric.rate, n=metric.total
        ),
        EvidenceRef(
            pointer=pointer("metrics", "restoration", "by_reason"),
            value=metric.by_reason,
            n=metric.total,
        ),
    ]
    verdict = None
    if metric.rate is not None:
        verdict = (
            "branches start from the recorded state: branching is usable"
            if metric.rate >= RESTORATION_USABLE
            else "restoration fails too often: fix the leading reason before branching at scale"
        )
    return _recommend(
        basis,
        f"passed restoration checks ≥ {RESTORATION_USABLE} (RESTORATION_USABLE)",
        evidence,
        verdict,
        has_data=metric.total > 0,
    )


def _rec_coverage(metric: BranchCoverage, basis: Evidence) -> Recommendation:
    evidence = [
        _ratio_ref(metric.zero_document_miss, "metrics", "branch_coverage", "zero_document_miss"),
        EvidenceRef(
            pointer=pointer("metrics", "branch_coverage", "other_misses", "total"),
            value=metric.other_misses.total,
            n=metric.branches,
        ),
    ]
    value = metric.zero_document_miss.value
    verdict = None
    if value is not None:
        verdict = (
            "branches stay inside the archive (see decisions.q1_miss_policy)"
            if value >= Q1_KEEP_STRICT
            else "branches leave the archive often (see decisions.q1_miss_policy)"
        )
    return _recommend(
        basis,
        f"branches with no main-frame document miss from the checkpoint on ≥ {Q1_KEEP_STRICT} "
        f"(Q1_KEEP_STRICT)",
        evidence,
        verdict,
        has_data=metric.branches > 0,
    )


def _rec_determinism(metric: ReplayDeterminism, basis: Evidence) -> Recommendation:
    evidence: list[EvidenceRef] = []
    pairs = identical = 0
    for role, item in metric.by_role.items():
        evidence.append(
            _ratio_ref(
                item.identical, "metrics", "replay_determinism", "by_role", role, "identical"
            )
        )
        evidence.append(
            _ratio_ref(
                item.checkpoint_identical,
                "metrics",
                "replay_determinism",
                "by_role",
                role,
                "checkpoint_identical",
            )
        )
        pairs += item.identical.denominator
        identical += item.identical.numerator
    verdict = None
    if pairs:
        share = identical / pairs
        verdict = (
            "replay is deterministic: the same input ends on the same screenshot"
            if share >= DETERMINISM_MIN
            else f"{pairs - identical} of {pairs} same-input pairs ended differently: investigate before "
            f"trusting branch outcomes"
        )
    return _recommend(
        basis,
        f"identical terminal screenshots ≥ {DETERMINISM_MIN} of comparable same-input pairs "
        f"(DETERMINISM_MIN)",
        evidence,
        verdict,
        has_data=pairs > 0,
    )


def _rec_export(metric: ExportDeterminism, basis: Evidence) -> Recommendation:
    evidence = [
        EvidenceRef(
            pointer=pointer("metrics", "export_determinism", "identical"), value=metric.identical
        )
    ]
    verdict = None
    if metric.identical is not None:
        verdict = (
            "the export is reproducible: the same runs give the same bytes"
            if metric.identical
            else "the two exports differ: fix before training on them"
        )
    return _recommend(
        basis,
        "two exports of the same runs digest alike (every file's path and sha256)",
        evidence,
        verdict,
        has_data=metric.identical is not None,
    )


def _rec_privacy(metric: Privacy, basis: Evidence) -> Recommendation:
    evidence = [
        EvidenceRef(
            pointer=pointer("metrics", "privacy", "planted", index, "occurrences"),
            value=item.occurrences,
            n=metric.files_scanned,
        )
        for index, item in enumerate(metric.planted)
    ]
    evidence.append(
        EvidenceRef(
            pointer=pointer("metrics", "privacy", "shapes"),
            value=metric.shapes,
            n=metric.files_scanned,
        )
    )
    leaked = [item.label for item in metric.planted if item.occurrences]
    shapes = sum(metric.shapes.values())
    unsearched = [item.label for item in metric.planted if item.occurrences is None]
    verdict = None
    if leaked or shapes:
        # A leak found is a conclusion whatever else could not be searched for.
        verdict = (
            f"leak: {', '.join(leaked) or 'no planted value'} found, {shapes} credential-shaped "
            f"string(s): fix before sharing traces or datasets"
        )
        if unsearched:
            verdict += f"; not searched: {', '.join(unsearched)}"
    elif metric.files_scanned and metric.planted and not unsearched:
        verdict = (
            f"no planted credential ({len(metric.planted)} searched) and no credential shape "
            f"under traces/ and datasets/"
        )
    return _recommend(
        basis,
        "every planted credential occurs 0 times and no SECRET_PATTERNS shape matches; 'no leak' "
        "needs every planted credential searched for (one whose environment variable is unset, or "
        "a report without a spec, leaves the planted measure unevaluated)",
        evidence,
        verdict,
        has_data=verdict is not None,
    )


# ── per-section rules ─────────────────────────────────────────────────


def _rec_sites(section: Sites, basis: Evidence) -> Recommendation:
    evidence = [
        EvidenceRef(
            pointer=pointer("sections", "sites", "exclude_sites"),
            value=section.exclude_sites,
            n=len(section.by_site),
        )
    ]
    verdict = None
    if section.by_site:
        verdict = (
            f"exclude {', '.join(section.exclude_sites)} from exports: add sections.sites.yaml to "
            f"the export's filters and export again"
            if section.exclude_sites
            else "no site to exclude"
        )
    return _recommend(
        basis,
        f"exclude a site an anti-bot challenge answered, whose same-path fidelity is below "
        f"{EXCLUDE_FIDELITY} (EXCLUDE_FIDELITY), or whose recording's environment failed at "
        f"step 0 (an observation or an action; a failed model call never excludes a site)",
        evidence,
        verdict,
        has_data=bool(section.by_site),
    )


def _rec_anti_bot(section: AntiBot, basis: Evidence) -> Recommendation:
    evidence = [
        EvidenceRef(
            pointer=pointer("sections", "anti_bot", "blocked_sites"),
            value=section.blocked_sites,
            n=section.runs_checked,
        )
    ]
    verdict = None
    if section.runs_checked:
        verdict = (
            f"exclude {', '.join(section.blocked_sites)}: a challenge answered the recording"
            if section.blocked_sites
            else "no challenge recognised (header marks only: an unmarked block page is not seen)"
        )
    return _recommend(
        basis,
        "a document answered 403 / 429 / 503 with cf-mitigated: challenge, or server: cloudflare "
        "with cf-ray, marks its site blocked",
        evidence,
        verdict,
        has_data=section.runs_checked > 0,
    )


def _rec_duplicates(section: DuplicateBranches, basis: Evidence) -> Recommendation:
    evidence = [
        _ratio_ref(section.rate, "sections", "duplicate_branches", "rate"),
        EvidenceRef(
            pointer=pointer("sections", "duplicate_branches", "dataset_duplicates"),
            value=section.dataset_duplicates,
        ),
    ]
    value = section.rate.value
    verdict = None
    if value is not None:
        verdict = (
            "duplicates are few and the export flags them (duplicate_of): keep branches_per_step"
            if value <= DUPLICATE_RATE_MAX
            else "most branches repeat an earlier one: sample the teacher more diversely or "
            "lower branches_per_step"
        )
    return _recommend(
        basis,
        f"duplicate branches ≤ {DUPLICATE_RATE_MAX} of all branches (DUPLICATE_RATE_MAX)",
        evidence,
        verdict,
        has_data=section.branches > 0,
    )


def _rec_post(section: PostBranchMisses, basis: Evidence) -> Recommendation:
    evidence = [
        _ratio_ref(
            section.branches_with_post_miss,
            "sections",
            "post_branch_misses",
            "branches_with_post_miss",
        ),
        EvidenceRef(
            pointer=pointer("sections", "post_branch_misses", "document_misses"),
            value=section.document_misses,
            n=section.branches,
        ),
    ]
    verdict = None
    if section.branches:
        verdict = (
            f"{section.document_misses} POST document miss(es): such branches cannot complete "
            f"offline under strict; max_document_misses: 0 keeps them out of exports until Q1 is "
            f"decided"
            if section.document_misses
            else "no branch submitted a POST the archive could not serve"
        )
    return _recommend(
        basis,
        "a POST miss from the checkpoint on is counted per branch (PRD §10: POST branches miss)",
        evidence,
        verdict,
        has_data=section.branches > 0,
    )


def _rec_storage(section: Storage, basis: Evidence) -> Recommendation:
    evidence: list[EvidenceRef] = []
    worth: list[str] = []
    for role, item in section.by_role.items():
        evidence.append(
            EvidenceRef(
                pointer=pointer("sections", "storage", "by_role", role, "per_step"),
                value=item.per_step,
                n=item.steps,
            )
        )
        for name, category in item.categories.items():
            if category.ratio is not None and category.ratio <= COMPRESS_RATIO_MAX:
                worth.append(f"{role}/{name}")
    has_data = any(item.runs for item in section.by_role.values())
    verdict = None
    if has_data:
        verdict = (
            f"gzip would at least halve: {', '.join(sorted(worth))} — evaluate compressing them"
            if worth
            else "no uncompressed category halves under gzip: compression would not pay"
        )
    return _recommend(
        basis,
        f"an uncompressed category whose gzip (level 6) ratio is ≤ {COMPRESS_RATIO_MAX} is worth "
        f"compressing (COMPRESS_RATIO_MAX)",
        evidence,
        verdict,
        has_data=has_data,
    )


def _retention_verdict(section: Retention) -> str | None:
    if section.per_task is None:
        return None
    projected = "; ".join(f"{p.tasks} tasks ≈ {_bytes(p.bytes)}" for p in section.projections)
    return f"plan {_bytes(section.per_task)} per task ({projected}); keep manual cleanup until a live run confirms it"


def _rec_retention(section: Retention, basis: Evidence) -> Recommendation:
    evidence = [
        EvidenceRef(pointer=pointer("sections", "retention", "per_task"), value=section.per_task)
    ]
    evidence += [
        EvidenceRef(
            pointer=pointer("sections", "retention", "projections", i, "bytes"),
            value=p.bytes,
            n=p.tasks,
        )
        for i, p in enumerate(section.projections)
    ]
    return _recommend(
        basis,
        "bytes per task = a recording plus every other role's runs per recording times its bytes "
        f"per run, plus the dataset's; projected to {', '.join(map(str, PROJECTION_TASKS))} tasks "
        "(PROJECTION_TASKS)",
        evidence,
        _retention_verdict(section),
        has_data=section.per_task is not None,
    )


def _rec_yield(section: ExportYield, basis: Evidence) -> Recommendation:
    evidence = [
        _ratio_ref(section.action_failed, "sections", "export_yield", "action_failed"),
        EvidenceRef(pointer=pointer("sections", "export_yield", "samples"), value=section.samples),
    ]
    value = section.action_failed.value
    verdict = None
    if value is not None:
        verdict = (
            "keep action_failed samples, labelled (labels.step_status)"
            if value <= ACTION_FAILED_KEEP_MAX
            else "drop or down-weight action_failed samples: they are a large share"
        )
    return _recommend(
        basis,
        f"action_failed samples ≤ {ACTION_FAILED_KEEP_MAX} of all samples are kept "
        f"(ACTION_FAILED_KEEP_MAX)",
        evidence,
        verdict,
        has_data=section.action_failed.denominator > 0,
    )


# ── the decisions ─────────────────────────────────────────────────────


def _q1(metrics: Metrics, sections: Sections, basis: Evidence) -> Recommendation:
    coverage = metrics.branch_coverage
    post = sections.post_branch_misses
    evidence = [
        _ratio_ref(coverage.zero_document_miss, "metrics", "branch_coverage", "zero_document_miss"),
        EvidenceRef(
            pointer=pointer("metrics", "branch_coverage", "other_misses", "total"),
            value=coverage.other_misses.total,
            n=coverage.branches,
        ),
        _ratio_ref(
            post.branches_with_post_miss,
            "sections",
            "post_branch_misses",
            "branches_with_post_miss",
        ),
    ]
    value = coverage.zero_document_miss.value
    verdict = None
    if value is not None:
        clean = coverage.zero_document_miss.numerator
        verdict = (
            f"keep strict: {clean} of {coverage.branches} branches served every main-frame document "
            f"from the archive"
            if value >= Q1_KEEP_STRICT
            else f"evaluate a document fallback (live_fallback): only {clean} of {coverage.branches} "
            f"branches stayed inside the archive ({post.document_misses} POST document miss(es))"
        )
    return _recommend(
        basis,
        f"keep `strict` while the share of branches with no main-frame document miss from the "
        f"checkpoint on is ≥ {Q1_KEEP_STRICT} (Q1_KEEP_STRICT); below it, evaluate a fallback "
        f"policy for documents (PRD §11 Q1)",
        evidence,
        verdict,
        has_data=coverage.branches > 0,
    )


def _q4(inputs: DecisionInputs, metrics: Metrics, basis: Evidence) -> Recommendation:
    misses = inputs.branch_document_misses
    evidence = [
        _ratio_ref(
            misses.get_no_match, "decision_inputs", "branch_document_misses", "get_no_match"
        ),
        EvidenceRef(
            pointer=pointer("decision_inputs", "branch_document_misses", "document_misses"),
            value=misses.document_misses,
            n=metrics.branch_coverage.branches,
        ),
    ]
    verdict = None
    branches = metrics.branch_coverage.branches
    if branches:
        value = misses.get_no_match.value
        if value is None:
            verdict = "do not expand coverage: no branch missed a main-frame document"
        elif value >= Q4_EXPAND:
            verdict = (
                f"expand coverage at recording time (prefetch or exploration): "
                f"{misses.get_no_match.numerator} of {misses.document_misses} document misses were "
                f"GETs of URLs the recording never fetched"
            )
        else:
            verdict = (
                f"do not expand coverage: {misses.get_no_match.numerator} of "
                f"{misses.document_misses} document misses were GETs of unrecorded URLs; merge "
                f"recordings of one task instead (Q4 default)"
            )
    return _recommend(
        basis,
        f"expand recording-time coverage when ≥ {Q4_EXPAND} of the branches' main-frame document "
        f"misses are GETs no recorded request matched (Q4_EXPAND)",
        evidence,
        verdict,
        has_data=branches > 0,
    )


def _site_rules_decision(inputs: DecisionInputs, basis: Evidence) -> Recommendation:
    found = inputs.site_rule_candidates
    evidence = [
        EvidenceRef(
            pointer=pointer("decision_inputs", "site_rule_candidates", "by_host"),
            value=found.by_host,
            n=found.runs,
        ),
        EvidenceRef(
            pointer=pointer("decision_inputs", "site_rule_candidates", "params"), value=found.params
        ),
    ]
    frequent = {host: n for host, n in found.by_host.items() if n >= SITE_RULE_MIN_MISSES}
    verdict = None
    if found.runs:
        verdict = (
            "generate ignore_query rules for review: "
            + "; ".join(f"{host} [{', '.join(found.params.get(host, {}))}]" for host in frequent)
            if frequent
            else f"keep hand-written rules: no host reached {SITE_RULE_MIN_MISSES} near misses"
        )
    return _recommend(
        basis,
        f"propose a generated ignore_query rule for a host with ≥ {SITE_RULE_MIN_MISSES} misses whose "
        f"nearest recorded request differs only in query parameters (SITE_RULE_MIN_MISSES)",
        evidence,
        verdict,
        has_data=found.runs > 0,
    )


# ── building ──────────────────────────────────────────────────────────


def _inputs(chain: ChainRuns, manifest: Mapping[str, Any] | None) -> Inputs:
    roles: dict[str, RoleInputs] = {}
    for role in ROLES:
        runs = chain.role(role)
        seen: dict[str, tuple[dict[str, Any], int]] = {}
        for run in runs.runs:
            ran = components(run.trace)
            key = json.dumps(ran, sort_keys=True)
            entry = seen.get(key)
            seen[key] = (ran, (entry[1] if entry is not None else 0) + 1)
        roles[role] = RoleInputs(
            present=runs.present,
            runs=len(runs.runs),
            skipped=dict(runs.skipped),
            components=[
                ComponentCount(components=ran, runs=n) for _key, (ran, n) in sorted(seen.items())
            ],
            model_failures=sorted(run.rel for run in runs.runs if failed_on_model_call(run)),
        )
    datasets = {}
    for name in DATASETS:
        present = chain.layout.dataset(name).is_dir()
        found = manifest if name == "first" else read_dataset_manifest(chain.layout, name)
        fmt = found.get("format") if found is not None else None
        datasets[name] = DatasetInputs(
            present=present, format=fmt if isinstance(fmt, str) else None
        )
    return Inputs(roles=roles, datasets=datasets)


def build_report(
    layout: ChainLayout,
    *,
    evidence: Evidence,
    planted: Sequence[Planted] = (),
    name: str | None = None,
) -> FlywheelReport:
    """Compute the report of the chain rooted at *layout* (module docstring).

    Reads only the root's committed runs and published datasets; never
    raises for a damaged run (it is skipped with its reason).
    """
    chain = open_chain(layout)
    manifest = read_dataset_manifest(layout)
    fidelity = same_path_fidelity(chain)
    blocked = anti_bot(chain.role("record"))
    stored = storage(chain)
    samples = manifest.get("samples") if manifest is not None else None
    metrics = Metrics(
        archive_overhead=archive_overhead(chain.role("record"), chain.role("control")),
        body_completeness=body_completeness_metric(chain.role("record")),
        same_path_fidelity=fidelity,
        restoration=restoration(chain.role("branch")),
        branch_coverage=branch_coverage(chain.role("branch")),
        replay_determinism=replay_determinism(chain),
        export_determinism=export_determinism(layout),
        privacy=privacy(layout, planted),
    )
    sections = Sections(
        sites=sites(
            chain, fidelity=fidelity, blocked=blocked.blocked_sites, exclude_below=EXCLUDE_FIDELITY
        ),
        anti_bot=blocked,
        duplicate_branches=duplicate_branches(chain.role("branch"), manifest),
        post_branch_misses=post_branch_misses(chain.role("branch")),
        storage=stored,
        retention=retention(
            chain, stored, samples if isinstance(samples, int) else None, PROJECTION_TASKS
        ),
        export_yield=export_yield(layout, manifest),
    )
    inputs = DecisionInputs(
        branch_document_misses=branch_document_misses(chain.role("branch")),
        site_rule_candidates=site_rules(chain),
    )
    recommendations = Recommendations(
        metrics=MetricRecommendations(
            archive_overhead=_rec_overhead(metrics.archive_overhead, evidence),
            body_completeness=_rec_body(metrics.body_completeness, evidence),
            same_path_fidelity=_rec_fidelity(metrics.same_path_fidelity, evidence),
            restoration=_rec_restoration(metrics.restoration, evidence),
            branch_coverage=_rec_coverage(metrics.branch_coverage, evidence),
            replay_determinism=_rec_determinism(metrics.replay_determinism, evidence),
            export_determinism=_rec_export(metrics.export_determinism, evidence),
            privacy=_rec_privacy(metrics.privacy, evidence),
        ),
        sections=SectionRecommendations(
            sites=_rec_sites(sections.sites, evidence),
            anti_bot=_rec_anti_bot(sections.anti_bot, evidence),
            duplicate_branches=_rec_duplicates(sections.duplicate_branches, evidence),
            post_branch_misses=_rec_post(sections.post_branch_misses, evidence),
            storage=_rec_storage(sections.storage, evidence),
            retention=_rec_retention(sections.retention, evidence),
            export_yield=_rec_yield(sections.export_yield, evidence),
        ),
    )
    decisions = Decisions(
        q1_miss_policy=_q1(metrics, sections, evidence),
        q4_coverage=_q4(inputs, metrics, evidence),
        site_rules=_site_rules_decision(inputs, evidence),
        q7_retention=_rec_retention(sections.retention, evidence),
    )
    return FlywheelReport(
        chain=ChainInfo(name=name, evidence=evidence),
        inputs=_inputs(chain, manifest),
        metrics=metrics,
        sections=sections,
        decision_inputs=inputs,
        recommendations=recommendations,
        decisions=decisions,
    )


def report_json(report: FlywheelReport) -> str:
    """``report.json``'s text: sorted keys, two-space indent, a trailing newline."""
    return (
        json.dumps(report.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
        + "\n"
    )


def write_report(report: FlywheelReport, out_dir: Path) -> tuple[Path, Path]:
    """Write ``report.json`` and ``report.md`` into *out_dir* (created); their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / REPORT_JSON, out_dir / REPORT_MD
    for path, text in ((json_path, report_json(report)), (md_path, render_markdown(report))):
        partial = path.with_name(path.name + ".partial")
        partial.write_text(text, encoding="utf-8")
        os.replace(partial, path)
    return json_path, md_path


# ── Markdown ──────────────────────────────────────────────────────────


_METRIC_TITLES: dict[str, str] = {
    "archive_overhead": "归档开销 — archive overhead",
    "body_completeness": "body 完整率 — body completeness",
    "same_path_fidelity": "原路径回放保真 — same-path replay fidelity",
    "restoration": "恢复校验率 — restoration verification rate",
    "branch_coverage": "分叉结果覆盖 — branch outcome coverage",
    "replay_determinism": "回放确定性 — replay determinism",
    "export_determinism": "导出确定性 — export determinism",
    "privacy": "隐私 — privacy",
}
_SECTION_TITLES: dict[str, str] = {
    "sites": "站点与剔除名单 — sites and the exclusion list (§10)",
    "anti_bot": "anti-bot / Cloudflare (§10)",
    "duplicate_branches": "重复分叉率 — duplicate branches (§10)",
    "post_branch_misses": "POST 分叉的 miss — POST branch misses (§10)",
    "storage": "存储增长与压缩评估 — storage growth and compression (§10)",
    "retention": "保留期与容量 — retention and capacity (§11 Q7)",
    "export_yield": "导出产出 — export yield (§10)",
}
_DECISION_TITLES: dict[str, str] = {
    "q1_miss_policy": "Q1 — miss policies beyond strict",
    "q4_coverage": "Q4 — expand coverage at recording time",
    "site_rules": "Site rules — generate them automatically?",
    "q7_retention": "Q7 — retention and capacity",
}


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _ratio_md(value: Ratio) -> str:
    if value.value is None:
        return f"null ({value.reason})"
    return f"{value.value:.3f} ({value.numerator}/{value.denominator})"


def _recommendation_md(item: Recommendation) -> list[str]:
    status = {
        "supported": "supported",
        "partial": "partial (部分)",
        "insufficient": "insufficient (不足)",
    }
    lines = [
        f"**Recommendation** — {status[item.status]}: {item.verdict if item.verdict is not None else '(no verdict: no data)'}",
        "",
        f"- rule: {item.rule}",
    ]
    for ref in item.evidence:
        n = f" (n={ref.n})" if ref.n is not None else ""
        lines.append(f"- evidence: `{ref.pointer}` = {_fmt(ref.value)}{n}")
    basis = item.basis + (f" — {REHEARSAL_NOTE}" if item.basis == "rehearsal" else "")
    lines.append(f"- basis: {basis}")
    return lines


def _lines_metric(name: str, metrics: Metrics) -> list[str]:
    if name == "archive_overhead":
        m = metrics.archive_overhead
        control = m.control
        reference = (
            f"{control.runs} run(s), {control.steps} step(s), p50 {_fmt(control.p50_ms)} ms, "
            f"p95 {_fmt(control.p95_ms)} ms"
            if control is not None
            else "none"
        )
        return [
            f"- archived ({m.archived.tier}): {m.archived.runs} run(s), {m.archived.steps} step(s), "
            f"p50 {_fmt(m.archived.p50_ms)} ms, p95 {_fmt(m.archived.p95_ms)} ms",
            f"- control: {reference}",
            f"- overhead: p50 {_fmt(m.p50_overhead_ms)} ms, p95 {_fmt(m.p95_overhead_ms)} ms"
            + (f" — {m.reason}" if m.reason else ""),
            f"- env/ bytes per step: before dedup {_bytes(m.bytes.per_step_before)}, after "
            f"{_bytes(m.bytes.per_step_after)} ({m.bytes.runs} run(s), {m.bytes.steps} step(s); saved by "
            f"revisit {m.bytes.total.saved_by_revisit} B, by content addressing "
            f"{m.bytes.total.saved_by_content_addressing} B)",
        ]
    if name == "body_completeness":
        m = metrics.body_completeness
        return [
            f"- records: {m.records} in {m.runs} run(s); by state: {_fmt(m.by_state)}",
            f"- complete: {_ratio_md(m.complete)}; with body: {_ratio_md(m.with_body)}",
        ]
    if name == "same_path_fidelity":
        m = metrics.same_path_fidelity
        lines = [
            f"- overall: restorer {_ratio_md(m.overall.restorer)}, strict {_ratio_md(m.overall.strict)}, "
            f"runs passing {_ratio_md(m.overall.runs_passing)}"
        ]
        for site, item in m.by_site.items():
            lines.append(
                f"- {site}: restorer {_ratio_md(item.restorer)}, strict {_ratio_md(item.strict)}, "
                f"runs passing {_ratio_md(item.runs_passing)}"
            )
        for failure in m.overall.first_failures:
            lines.append(
                f"  - first failure of `{failure.run}`: step {_fmt(failure.step)} "
                f"{', '.join(failure.reasons) or failure.note or ''}"
            )
        return lines
    if name == "restoration":
        m = metrics.restoration
        return [
            f"- {m.summary()}",
            f"- retarget failures: {_fmt(m.retarget_failures)}",
        ]
    if name == "branch_coverage":
        m = metrics.branch_coverage
        return [
            f"- branches with no main-frame document miss: {_ratio_md(m.zero_document_miss)}; "
            f"document misses {m.document_misses}",
            f"- other misses: {m.other_misses.total}; by action {_fmt(m.other_misses.by_action)}, "
            f"resourceType {_fmt(m.other_misses.by_resource_type)}, host {_fmt(m.other_misses.by_host)}, "
            f"reason {_fmt(m.other_misses.by_reason)}",
        ]
    if name == "replay_determinism":
        return [
            f"- {role}: identical terminal screenshots {_ratio_md(item.identical)}, checkpoint "
            f"screenshots alike {_ratio_md(item.checkpoint_identical)}, unpaired {item.unpaired}, "
            f"without terminal {item.without_terminal}"
            for role, item in metrics.replay_determinism.by_role.items()
        ]
    if name == "export_determinism":
        m = metrics.export_determinism
        return [
            f"- first: {m.first.digest if m.first else 'none'} ({m.first.files if m.first else 0} file(s))",
            f"- second: {m.second.digest if m.second else 'none'}",
            f"- identical: {_fmt(m.identical)}" + (f" — {m.reason}" if m.reason else ""),
        ]
    m = metrics.privacy
    lines = [f"- files scanned: {m.files_scanned}"]
    lines += [
        f"- planted `{item.label}` ({item.source}): {_fmt(item.occurrences)} occurrence(s) in "
        f"{_fmt(item.files)} file(s)" + (f" — {item.reason}" if item.reason else "")
        for item in m.planted
    ]
    lines.append(f"- credential shapes: {_fmt(m.shapes)}")
    return lines


def _lines_section(name: str, sections: Sections) -> list[str]:
    if name == "sites":
        s = sections.sites
        lines = [
            f"- {site}: {item.tasks} task(s), {item.record_runs} recording(s) {_fmt(item.record_outcome)}"
            + (
                f" ({item.record_model_failures} ended on a failed model call)"
                if item.record_model_failures
                else ""
            )
            + f", same-path {_ratio_md(item.same_path)}, restoration {_ratio_md(item.restoration)}, "
            f"branches without a document miss {_ratio_md(item.branch_zero_document_miss)}, document "
            f"misses {item.document_misses}, other misses {item.other_misses}"
            + (" — blocked" if item.blocked else "")
            for site, item in s.by_site.items()
        ]
        lines += [f"- exclude {item.site}: {'; '.join(item.reasons)}" for item in s.exclusions]
        lines += ["", "```yaml", s.yaml.rstrip("\n"), "```"]
        return lines
    if name == "anti_bot":
        s = sections.anti_bot
        lines = [f"- recordings checked: {s.runs_checked}; blocked sites: {_fmt(s.blocked_sites)}"]
        lines += [
            f"- `{hit.run}` step {_fmt(hit.step)}: {hit.status} {hit.url} ({', '.join(hit.signals)})"
            for hit in s.hits
        ]
        return lines
    if name == "duplicate_branches":
        s = sections.duplicate_branches
        return [
            f"- duplicates: {_ratio_md(s.rate)} in {s.groups} group(s); the first export flagged "
            f"{_fmt(s.dataset_duplicates)}"
        ]
    if name == "post_branch_misses":
        s = sections.post_branch_misses
        return [
            f"- POST misses from the checkpoint on: {s.document_misses} document, {s.other_misses} "
            f"other; branches with one {_ratio_md(s.branches_with_post_miss)}"
        ]
    if name == "storage":
        s = sections.storage
        lines: list[str] = []
        for role, item in s.by_role.items():
            lines.append(
                f"- {role}: {item.runs} run(s), {_bytes(item.bytes)}; per run {_bytes(item.per_run)}, per "
                f"step {_bytes(item.per_step)}"
            )
            for category, data in item.categories.items():
                lines.append(
                    f"  - {category}: {data.files} file(s), {_bytes(data.bytes)}; gzip "
                    f"{_fmt(data.ratio)} of {_bytes(data.compressible_bytes)} compressible"
                )
        lines += [f"- dataset {name_}: {_bytes(size)}" for name_, size in s.datasets.items()]
        return lines
    if name == "retention":
        s = sections.retention
        lines = ["- per run: " + ", ".join(f"{k} {_bytes(v)}" for k, v in s.per_run.items())]
        lines.append(f"- per sample: {_bytes(s.per_sample)}; per task: {_bytes(s.per_task)}")
        lines += [f"- {p.tasks} tasks: {_bytes(p.bytes)}" for p in s.projections]
        return lines
    s = sections.export_yield
    if not s.present:
        return ["- no first export"]
    return [
        f"- samples by split: {_fmt(s.samples)}; sources exported {s.sources_exported}",
        f"- untranslatable steps: {_fmt(s.untranslatable)}; skipped steps: {_fmt(s.steps_skipped)}",
        f"- sources skipped: {len(s.sources_skipped)}; filtered: {len(s.sources_filtered)}",
        *(f"  - filtered `{rel}`: {reason}" for rel, reason in s.sources_filtered.items()),
        f"- action_failed samples: {_ratio_md(s.action_failed)}",
    ]


def render_markdown(report: FlywheelReport) -> str:
    """``report.md``: one section per §9 row and per §10 / §11 item, each ending in its recommendation."""
    lines = [
        f"# Flywheel report{f' — {report.chain.name}' if report.chain.name else ''}",
        "",
        f"Format `{report.format}`; evidence basis: **{report.chain.evidence}**"
        + (f" ({REHEARSAL_NOTE})" if report.chain.evidence == "rehearsal" else "")
        + ".",
        "",
        "## Inputs",
        "",
    ]
    for role, item in report.inputs.roles.items():
        lines.append(
            f"- {role}: {item.runs} run(s){'' if item.present else ' (not run)'}, {len(item.skipped)} "
            f"skipped, {len(item.model_failures)} ended on a failed model call"
        )
        lines += [f"  - skipped `{rel}`: {reason}" for rel, reason in item.skipped.items()]
        lines += [
            f"  - model call failed: `{rel}` (the route's failure: re-run before trusting it)"
            for rel in item.model_failures
        ]
        lines += [f"  - {entry.runs} × {_fmt(entry.components)}" for entry in item.components]
    for name, item in report.inputs.datasets.items():
        lines.append(
            f"- dataset {name}: {'present' if item.present else 'absent'} ({_fmt(item.format)})"
        )
    lines += ["", "## PRD §9 metrics", ""]
    for name, title in _METRIC_TITLES.items():
        lines += [f"### {title}", "", *_lines_metric(name, report.metrics), ""]
        lines += [*_recommendation_md(getattr(report.recommendations.metrics, name)), ""]
    lines += ["## PRD §10 / §11 sections", ""]
    for name, title in _SECTION_TITLES.items():
        lines += [f"### {title}", "", *_lines_section(name, report.sections), ""]
        lines += [*_recommendation_md(getattr(report.recommendations.sections, name)), ""]
    lines += ["## Pending decisions (PRD M33, §11)", ""]
    candidates = report.decision_inputs.site_rule_candidates
    misses = report.decision_inputs.branch_document_misses
    lines += [
        f"- branch document misses: {misses.document_misses}; GETs of unrecorded URLs "
        f"{_ratio_md(misses.get_no_match)}",
        f"- site-rule candidates over {candidates.runs} replayed run(s): {_fmt(candidates.by_host)} "
        f"{_fmt(candidates.params)}",
        "",
    ]
    for name, title in _DECISION_TITLES.items():
        lines += [f"### {title}", "", *_recommendation_md(getattr(report.decisions, name)), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = [
    "ACTION_FAILED_KEEP_MAX",
    "COMPRESS_RATIO_MAX",
    "DETERMINISM_MIN",
    "DUPLICATE_RATE_MAX",
    "EXCLUDE_FIDELITY",
    "FIDELITY_USABLE",
    "OVERHEAD_P50_BOUND_MS",
    "PROJECTION_TASKS",
    "Q1_KEEP_STRICT",
    "Q4_EXPAND",
    "REHEARSAL_NOTE",
    "REPORT_FORMAT",
    "RESTORATION_USABLE",
    "SITE_RULE_MIN_MISSES",
    "WITH_BODY_MIN",
    "Decisions",
    "EvidenceRef",
    "FlywheelReport",
    "Recommendation",
    "build_report",
    "pointer",
    "render_markdown",
    "report_json",
    "write_report",
]
