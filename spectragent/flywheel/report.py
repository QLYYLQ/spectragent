"""The flywheel report: every measure, every section, a recommendation each (arch §4.16 "报告").

:func:`build_report` opens a chain root (``metrics.open_chain``), computes
every metric, every section and the inputs of the pending decisions, and
attaches a :class:`Recommendation` to each — a rule (its thresholds are the
named constants below), the evidence it rests on (JSON pointers into this
report, with their values and denominators), the number of distinct sites
that evidence came from, and the basis. No data is ``insufficient`` with no
verdict; otherwise a recommendation is ``supported`` only on ``live``
evidence from at least :data:`SUPPORTED_MIN_SITES` sites, and ``partial``
below that ("insufficient sample") or on ``rehearsal`` evidence — "the
fixture only calibrates the measure; decide after the live run". The reason
for a status below ``supported`` is written next to it
(``status_reason``). A default conclusion is never given.

The thresholds stay in code until a live run over enough sites calibrates
them; each is written into the rule that uses it, by name and value. The
per-step archive overhead budget is not defined here but in
``core/archive/budget.py``, which the browser's overhead benchmark reads too.

:func:`write_report` writes ``report.json`` (format ``af-flywheel-report-10``:
sorted keys, fixed separators, no wall clock, no absolute path, no host
information — the same inputs give the same bytes) and ``report.md``,
rendered from the same model, every measure's skipped runs included.
Planted credential values never enter either: the privacy measure reports
labels and counts.

**Token use.** ``token_use`` counts every role's recorded tokens from the
traces (``metrics.role_token_use``, never ``chain.json``) against the chain
spec's ``token_limit``, which only a spec gives (``report --root`` has none:
the limit is ``null``, with the reason). When the runs reached it, the chain
stopped there — no stage starts once the limit is reached — and ``report.md``
says so first: everything after covers only the runs and datasets that exist.
"""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from spectragent.core.archive import ARCHIVE_MAX_P50_OVERHEAD_MS
from spectragent.flywheel.metrics import (
    DATASETS,
    NO_SITE,
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
    NearMissEndpoint,
    NearMissFieldSet,
    Planted,
    PostBranchMisses,
    Privacy,
    Ratio,
    ReplayDeterminism,
    RestorationRate,
    Retention,
    SamePathFidelity,
    SettleWait,
    SiteRuleCandidates,
    Sites,
    Storage,
    TokenUse,
    anti_bot,
    archive_overhead,
    body_completeness_metric,
    branch_coverage,
    branch_document_misses,
    components,
    duplicate_branches,
    export_determinism,
    export_yield,
    exported_sites,
    failed_on_model_call,
    open_chain,
    post_branch_misses,
    privacy,
    read_dataset_manifest,
    replay_determinism,
    restoration,
    retention,
    role_token_use,
    same_path_fidelity,
    site_rules,
    sites,
    storage,
)
from spectragent.schemas._types import UNLIMITED, Unlimited
from spectragent.schemas.dataset import DatasetManifest

#: ``report.json``'s format.
REPORT_FORMAT = "af-flywheel-report-10"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"

Evidence = Literal["live", "rehearsal"]
Status = Literal["supported", "partial", "insufficient"]

# ── the rules' thresholds (each written into the rule that uses it) ────
#
# Named <what it bounds>_MIN_<quantity> or <what it bounds>_MAX_<quantity>. Starting points
# chosen before any live run; they stay here until a live run over enough sites calibrates them.

#: Distinct sites a recommendation's evidence must come from to be ``supported`` (live only).
SUPPORTED_MIN_SITES = 10
#: A site whose same-path fidelity (the restorer's verdict) is below this is excluded.
SITE_MIN_FIDELITY = 0.8
#: Same-path fidelity at or above which offline replay is usable as a measuring tool.
REPLAY_MIN_FIDELITY = 0.9
#: Restoration verification rate at or above which branching is usable.
RESTORATION_MIN_RATE = 0.9
#: Share of the branches that acted with no main-frame document miss at or above which the
#: ``strict`` miss policy is enough.
STRICT_MIN_IN_ARCHIVE_SHARE = 0.9
#: Share of the branches' main-frame document misses that are GETs of URLs the recording never
#: fetched, from which expanding recording-time coverage (prefetch, exploration) is worth it.
EXPAND_COVERAGE_MIN_UNRECORDED_SHARE = 0.5
#: Near misses on one host from which a generated ``ignore_query`` site rule is proposed.
SITE_RULE_MIN_NEAR_MISSES = 2
#: Records with a body, of those that need one, for capture quality to be sufficient.
CAPTURE_MIN_BODY_SHARE = 0.95
#: Identical terminal-screenshot pairs, of the comparable ones, for replay to count as deterministic.
REPLAY_MIN_IDENTICAL_SHARE = 1.0
#: Duplicate branches above this share of the branches compared waste teacher calls.
BRANCH_MAX_DUPLICATE_SHARE = 0.5
#: A storage category whose gzip ratio is at or below this is worth compressing.
COMPRESS_MAX_GZIP_RATIO = 0.5
#: ``action_failed`` samples up to this share are kept (labelled); above it, dropped.
ACTION_FAILED_MAX_KEPT_SHARE = 0.2
#: Task counts the retention section projects to: a first live run, a full live chain, and
#: the whole ``online_mind2web`` ``All`` subset. Not a threshold.
RETENTION_PROJECTION_TASKS: tuple[int, ...] = (10, 20, 300)

#: What a ``rehearsal`` basis means, written into every one of its recommendations.
REHEARSAL_NOTE = "fixture data calibrates the measure only; decide after the live run"
#: Why live evidence from fewer than :data:`SUPPORTED_MIN_SITES` sites is at most ``partial``.
INSUFFICIENT_SAMPLE = "insufficient sample"


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
    ``None`` — never a default conclusion); ``supported`` on ``live``
    evidence from at least :data:`SUPPORTED_MIN_SITES` sites; ``partial``
    otherwise. ``sites``: the distinct sites the evidence came from.
    ``status_reason``: why the status is below ``supported`` (``None`` when
    it is ``supported``).
    """

    status: Status
    verdict: str | None
    rule: str
    evidence: list[EvidenceRef]
    basis: Evidence
    sites: int
    status_reason: str | None


class ComponentCount(_Model):
    components: dict[str, Any]
    runs: int


class RoleInputs(_Model):
    present: bool
    runs: int
    skipped: dict[str, str]
    components: list[ComponentCount]
    #: Runs that ended on a failed model call (``metrics.failed_on_model_call``): the route's
    #: failure — a 429, a timeout — so what they did not do is not the site's.
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


class TokenUseReport(_Model):
    """What the chain's runs used, against its token limit (arch §4.16 "token 上限").

    ``limit`` is the chain spec's ``token_limit`` (a number or
    ``unlimited``); ``None`` when the report was computed without a spec,
    ``reason`` then says so. ``tokens`` adds up every role's counted runs.
    ``limit_reached``: ``tokens`` at or over a numeric limit — the chain
    stopped there, no stage starting once the limit is reached; ``False``
    under ``unlimited``; ``None`` when the limit is not known.
    """

    limit: int | Unlimited | None
    tokens: int
    limit_reached: bool | None
    reason: str | None
    roles: dict[str, TokenUse]


class Metrics(_Model):
    """The metrics (arch §4.16 "度量"), one field per row."""

    archive_overhead: ArchiveOverhead
    body_completeness: BodyCompletenessMetric
    same_path_fidelity: SamePathFidelity
    restoration: RestorationRate
    branch_coverage: BranchCoverage
    replay_determinism: ReplayDeterminism
    export_determinism: ExportDeterminism
    privacy: Privacy


class Sections(_Model):
    """The sections (arch §4.16 "各节与建议"), one field per item."""

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
    """The pending decisions, each with an evidence-backed recommendation.

    ``miss_policy``: is ``strict`` enough, or do documents need a fallback?
    ``recording_coverage``: expand coverage at recording time? ``site_rules``:
    generate ``ignore_query`` rules automatically? ``retention``: how much
    disk a task takes, for a retention budget.
    """

    miss_policy: Recommendation
    recording_coverage: Recommendation
    site_rules: Recommendation
    retention: Recommendation


class FlywheelReport(_Model):
    """The report (``report.json``)."""

    format: Literal["af-flywheel-report-10"] = REPORT_FORMAT
    chain: ChainInfo
    token_use: TokenUseReport
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
    sites: int,
    no_data: str = "no data",
) -> Recommendation:
    """The recommendation of *rule* on *evidence* from *sites* sites (the status rule above).

    *no_data* is the status reason when *has_data* is false.
    """
    if not has_data:
        return Recommendation(
            status="insufficient",
            verdict=None,
            rule=rule,
            evidence=list(evidence),
            basis=basis,
            sites=sites,
            status_reason=no_data,
        )
    reasons: list[str] = []
    if basis == "rehearsal":
        reasons.append(f"rehearsal evidence: {REHEARSAL_NOTE}")
    if sites < SUPPORTED_MIN_SITES:
        reasons.append(
            f"{INSUFFICIENT_SAMPLE}: data from {sites} site(s); supported needs at least "
            f"{SUPPORTED_MIN_SITES} (SUPPORTED_MIN_SITES)"
        )
    return Recommendation(
        status="partial" if reasons else "supported",
        verdict=verdict,
        rule=rule,
        evidence=list(evidence),
        basis=basis,
        sites=sites,
        status_reason="; ".join(reasons) if reasons else None,
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
            if p50 <= ARCHIVE_MAX_P50_OVERHEAD_MS
            else f"archiving costs {p50:+.0f} ms per step at p50: keep it opt-in"
        )
    # The comparison is as broad as its narrower group.
    sites = (
        min(metric.archived.sites, metric.control.sites)
        if metric.control is not None
        else metric.archived.sites
    )
    return _recommend(
        basis,
        f"p50 overhead (archived minus control, policy steps from step 1) ≤ "
        f"{ARCHIVE_MAX_P50_OVERHEAD_MS:.0f} ms (ARCHIVE_MAX_P50_OVERHEAD_MS, the per-step budget "
        f"the browser's overhead benchmark asserts too): affordable",
        evidence,
        verdict,
        has_data=p50 is not None,
        sites=sites,
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
            if rate >= CAPTURE_MIN_BODY_SHARE
            else "too many records lack a body: review too_large / incomplete before relying on replay"
        )
    return _recommend(
        basis,
        f"records with a body ≥ {CAPTURE_MIN_BODY_SHARE} of those needing one "
        f"(CAPTURE_MIN_BODY_SHARE)",
        evidence,
        verdict,
        has_data=metric.records > 0,
        sites=metric.sites,
    )


def _rec_fidelity(metric: SamePathFidelity, basis: Evidence) -> Recommendation:
    base = ("metrics", "same_path_fidelity", "overall")
    evidence = [
        _ratio_ref(metric.overall.restorer, *base, "restorer"),
        _ratio_ref(metric.overall.strict, *base, "strict"),
        _ratio_ref(metric.overall.exact_screenshot, *base, "exact_screenshot"),
        _ratio_ref(metric.overall.url_coverage, *base, "url_coverage"),
        _ratio_ref(metric.overall.signature_coverage, *base, "signature_coverage"),
        _ratio_ref(metric.overall.media_coverage, *base, "media_coverage"),
        _ratio_ref(metric.overall.runs_reaching_checkpoint, *base, "runs_reaching_checkpoint"),
        _ratio_ref(metric.overall.runs_passing, *base, "runs_passing"),
    ]
    value = metric.overall.restorer.value
    verdict = None
    if value is not None:
        verdict = (
            "recorded step checks meet the threshold; inspect evidence coverage and the full-path rate before use"
            if value >= REPLAY_MIN_FIDELITY
            else "same-path replay diverges: exclude the low-fidelity sites (sections.sites) first"
        )
    return _recommend(
        basis,
        f"restorer-verified steps that passed ≥ {REPLAY_MIN_FIDELITY} (REPLAY_MIN_FIDELITY); a "
        f"site below {SITE_MIN_FIDELITY} is excluded (SITE_MIN_FIDELITY)",
        evidence,
        verdict,
        has_data=metric.overall.verified_steps > 0,
        sites=metric.sites,
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
            if metric.rate >= RESTORATION_MIN_RATE
            else "restoration fails too often: fix the leading reason before branching at scale"
        )
    return _recommend(
        basis,
        f"passed restoration checks ≥ {RESTORATION_MIN_RATE} (RESTORATION_MIN_RATE)",
        evidence,
        verdict,
        has_data=metric.total > 0,
        sites=metric.sites,
    )


def _rec_coverage(metric: BranchCoverage, basis: Evidence) -> Recommendation:
    evidence = [
        _ratio_ref(metric.zero_document_miss, "metrics", "branch_coverage", "zero_document_miss"),
        EvidenceRef(
            pointer=pointer("metrics", "branch_coverage", "empty", "total"),
            value=metric.empty.total,
            n=metric.branches,
        ),
        EvidenceRef(
            pointer=pointer("metrics", "branch_coverage", "other_misses", "total"),
            value=metric.other_misses.total,
            n=metric.zero_document_miss.denominator,
        ),
    ]
    value = metric.zero_document_miss.value
    verdict = None
    if value is not None:
        verdict = (
            "branches stay inside the archive (see decisions.miss_policy)"
            if value >= STRICT_MIN_IN_ARCHIVE_SHARE
            else "branches leave the archive often (see decisions.miss_policy)"
        )
    return _recommend(
        basis,
        f"branches that acted with no main-frame document miss from the checkpoint on ≥ "
        f"{STRICT_MIN_IN_ARCHIVE_SHARE} (STRICT_MIN_IN_ARCHIVE_SHARE); an empty branch (no action "
        f"from the checkpoint on: a failed teacher call, a reply that ran no action) is left out "
        f"and counted in empty",
        evidence,
        verdict,
        has_data=metric.zero_document_miss.denominator > 0,
        sites=metric.sites,
        no_data=_no_branch_data(metric),
    )


def _no_branch_data(coverage: BranchCoverage) -> str:
    """Why a branch-outcome rule has no data: no branch at all, or only empty ones."""
    if not coverage.branches:
        return "no branch run"
    return f"every branch was empty ({coverage.empty.total}: no action from the checkpoint on)"


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
            if share >= REPLAY_MIN_IDENTICAL_SHARE
            else f"{pairs - identical} of {pairs} same-input pairs ended differently: investigate before "
            f"trusting branch outcomes"
        )
    return _recommend(
        basis,
        f"identical terminal screenshots ≥ {REPLAY_MIN_IDENTICAL_SHARE} of comparable same-input "
        f"pairs (REPLAY_MIN_IDENTICAL_SHARE)",
        evidence,
        verdict,
        has_data=pairs > 0,
        sites=metric.sites,
    )


def _rec_export(metric: ExportDeterminism, basis: Evidence, *, sites: int) -> Recommendation:
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
        sites=sites,
    )


def _rec_privacy(metric: Privacy, basis: Evidence) -> Recommendation:
    gates = (
        ("model_requests", "sent to the model", metric.model_requests),
        ("datasets", "in the datasets", metric.datasets),
    )
    evidence: list[EvidenceRef] = []
    found: list[str] = []
    for key, where, gate in gates:
        evidence += [
            EvidenceRef(
                pointer=pointer("metrics", "privacy", key, "planted", index, "occurrences"),
                value=item.occurrences,
                n=gate.items,
            )
            for index, item in enumerate(gate.planted)
        ]
        evidence.append(
            EvidenceRef(
                pointer=pointer("metrics", "privacy", key, "shapes"),
                value=gate.shapes,
                n=gate.items,
            )
        )
        leaked = [item.label for item in gate.planted if item.occurrences]
        shapes = sum(gate.shapes.values())
        if leaked or shapes:
            found.append(
                f"{where}: {', '.join(leaked) or 'no planted value'}, {shapes} credential-shaped "
                "string(s)"
            )
    planted = metric.model_requests.planted
    unsearched = [item.label for item in planted if item.occurrences is None]
    scanned = metric.model_requests.items + metric.datasets.items
    verdict = None
    if found:
        # A leak found is a conclusion whatever else could not be searched for.
        verdict = f"leak — {'; '.join(found)}: fix before the next run or export"
        if unsearched:
            verdict += f"; not searched: {', '.join(unsearched)}"
    elif scanned and planted and not unsearched:
        verdict = (
            f"no planted credential ({len(planted)} searched) and no credential shape in what "
            f"was sent to the model ({metric.model_requests.items} request(s)) or in the "
            f"datasets ({metric.datasets.items} file(s))"
        )
    if verdict is not None and metric.skipped:
        verdict += f"; {len(metric.skipped)} run(s) not read (see skipped)"
    if not scanned:
        no_data = "no recorded model request and no file under datasets/"
    elif not planted:
        no_data = (
            "no planted credential to search for (the chain spec plants none, or there is no spec)"
        )
    else:
        no_data = f"not searched: {', '.join(unsearched)}"
    return _recommend(
        basis,
        "every planted credential occurs 0 times and no SECRET_PATTERNS shape matches at either "
        "gate: what was sent to the model (the messages of every recorded request, inline image "
        "data aside) and the exported datasets (every file under datasets/); env/ and the rest "
        "of a trace stay on this machine and are not searched; 'no leak' needs every planted "
        "credential searched for (one whose environment variable is unset, or a report without "
        "a spec, leaves the planted measure unevaluated)",
        evidence,
        verdict,
        has_data=verdict is not None,
        sites=metric.sites,
        no_data=no_data,
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
        f"{SITE_MIN_FIDELITY} (SITE_MIN_FIDELITY), or whose recording's environment failed at "
        f"step 0 (an observation or an action; a failed model call never excludes a site)",
        evidence,
        verdict,
        has_data=bool(section.by_site),
        sites=len(set(section.by_site) - {NO_SITE}),
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
        sites=section.sites,
    )


def _rec_duplicates(section: DuplicateBranches, basis: Evidence) -> Recommendation:
    evidence = [
        _ratio_ref(section.rate, "sections", "duplicate_branches", "rate"),
        EvidenceRef(
            pointer=pointer("sections", "duplicate_branches", "not_compared"),
            value=section.not_compared,
            n=section.branches,
        ),
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
            if value <= BRANCH_MAX_DUPLICATE_SHARE
            else "most branches repeat an earlier one: sample the teacher more diversely or "
            "lower branches_per_step"
        )
    return _recommend(
        basis,
        f"duplicate branches ≤ {BRANCH_MAX_DUPLICATE_SHARE} of the branches compared "
        f"(BRANCH_MAX_DUPLICATE_SHARE); a branch whose checkpoint step ran no action is not "
        f"compared",
        evidence,
        verdict,
        has_data=section.rate.denominator > 0,
        sites=section.sites,
        no_data=(
            f"no branch whose checkpoint step ran an action ({section.not_compared} not compared)"
            if section.branches
            else "no branch run"
        ),
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
            f"offline under strict; max_document_misses: 0 keeps them out of exports until the "
            f"miss-policy decision (decisions.miss_policy) is made"
            if section.document_misses
            else "no branch submitted a POST the archive could not serve"
        )
    return _recommend(
        basis,
        "a POST miss from the checkpoint on is counted per branch: under strict, a branch that "
        "submits a POST the recording never made cannot complete offline",
        evidence,
        verdict,
        has_data=section.branches > 0,
        sites=section.sites,
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
            if category.ratio is not None and category.ratio <= COMPRESS_MAX_GZIP_RATIO:
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
        f"an uncompressed category whose gzip (level 6) ratio is ≤ {COMPRESS_MAX_GZIP_RATIO} is "
        f"worth compressing (COMPRESS_MAX_GZIP_RATIO)",
        evidence,
        verdict,
        has_data=has_data,
        sites=section.sites,
    )


def _retention_verdict(section: Retention) -> str | None:
    if section.per_task is None:
        return None
    projected = "; ".join(f"{p.tasks} tasks ≈ {_bytes(p.bytes)}" for p in section.projections)
    return f"plan {_bytes(section.per_task)} per task ({projected}); keep manual cleanup until a live run confirms it"


def _rec_retention(section: Retention, basis: Evidence, *, sites: int) -> Recommendation:
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
        f"per run, plus the dataset's; projected to {', '.join(map(str, RETENTION_PROJECTION_TASKS))} "
        "tasks (RETENTION_PROJECTION_TASKS)",
        evidence,
        _retention_verdict(section),
        has_data=section.per_task is not None,
        sites=sites,
    )


def _rec_yield(section: ExportYield, basis: Evidence, *, sites: int) -> Recommendation:
    evidence = [
        _ratio_ref(section.action_failed, "sections", "export_yield", "action_failed"),
        EvidenceRef(pointer=pointer("sections", "export_yield", "samples"), value=section.samples),
    ]
    value = section.action_failed.value
    verdict = None
    if value is not None:
        verdict = (
            "keep action_failed samples, labelled (labels.step_status)"
            if value <= ACTION_FAILED_MAX_KEPT_SHARE
            else "drop or down-weight action_failed samples: they are a large share"
        )
    return _recommend(
        basis,
        f"action_failed samples ≤ {ACTION_FAILED_MAX_KEPT_SHARE} of all samples are kept "
        f"(ACTION_FAILED_MAX_KEPT_SHARE)",
        evidence,
        verdict,
        has_data=section.action_failed.denominator > 0,
        sites=sites,
    )


# ── the decisions ─────────────────────────────────────────────────────


def _decide_miss_policy(metrics: Metrics, sections: Sections, basis: Evidence) -> Recommendation:
    coverage = metrics.branch_coverage
    post = sections.post_branch_misses
    evidence = [
        _ratio_ref(coverage.zero_document_miss, "metrics", "branch_coverage", "zero_document_miss"),
        EvidenceRef(
            pointer=pointer("metrics", "branch_coverage", "empty", "total"),
            value=coverage.empty.total,
            n=coverage.branches,
        ),
        EvidenceRef(
            pointer=pointer("metrics", "branch_coverage", "other_misses", "total"),
            value=coverage.other_misses.total,
            n=coverage.zero_document_miss.denominator,
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
        acted = coverage.zero_document_miss.denominator
        empty = (
            f"; {coverage.empty.total} empty branch(es) left out" if coverage.empty.total else ""
        )
        verdict = (
            f"keep strict: {clean} of {acted} branches served every main-frame document from the "
            f"archive{empty}"
            if value >= STRICT_MIN_IN_ARCHIVE_SHARE
            else f"evaluate a fallback for documents: only {clean} of {acted} branches stayed "
            f"inside the archive ({post.document_misses} POST document miss(es){empty})"
        )
    return _recommend(
        basis,
        f"keep `strict` while the share of the branches that acted with no main-frame document "
        f"miss from the checkpoint on is ≥ {STRICT_MIN_IN_ARCHIVE_SHARE} "
        f"(STRICT_MIN_IN_ARCHIVE_SHARE); below it, evaluate a fallback for documents — a new "
        f"network source, not a miss policy (arch §4.14)",
        evidence,
        verdict,
        has_data=coverage.zero_document_miss.denominator > 0,
        sites=coverage.sites,
        no_data=_no_branch_data(coverage),
    )


def _decide_recording_coverage(inputs: DecisionInputs, basis: Evidence) -> Recommendation:
    misses = inputs.branch_document_misses
    evidence = [
        _ratio_ref(
            misses.get_no_match, "decision_inputs", "branch_document_misses", "get_no_match"
        ),
        EvidenceRef(
            pointer=pointer("decision_inputs", "branch_document_misses", "document_misses"),
            value=misses.document_misses,
            n=misses.branches,
        ),
    ]
    verdict = None
    if misses.branches:
        value = misses.get_no_match.value
        if value is None:
            verdict = "do not expand coverage: no branch missed a main-frame document"
        elif value >= EXPAND_COVERAGE_MIN_UNRECORDED_SHARE:
            verdict = (
                f"expand coverage at recording time (prefetch or exploration): "
                f"{misses.get_no_match.numerator} of {misses.document_misses} document misses were "
                f"GETs of URLs the recording never fetched"
            )
        else:
            verdict = (
                f"do not expand coverage: {misses.get_no_match.numerator} of "
                f"{misses.document_misses} document misses were GETs of unrecorded URLs; merge "
                f"recordings of one task instead (the default)"
            )
    return _recommend(
        basis,
        f"expand recording-time coverage when ≥ {EXPAND_COVERAGE_MIN_UNRECORDED_SHARE} of the "
        f"main-frame document misses of the branches that acted are GETs no recorded request "
        f"matched (EXPAND_COVERAGE_MIN_UNRECORDED_SHARE)",
        evidence,
        verdict,
        has_data=misses.branches > 0,
        sites=misses.sites,
    )


def _field_set_text(fields: NearMissFieldSet) -> str:
    """``query a, b; body c`` — one combination of differing fields, as a rule would name them."""
    parts = []
    if fields.query:
        parts.append("query " + ", ".join(fields.query))
    if fields.body:
        parts.append("body " + ", ".join(fields.body))
    if fields.opaque_body:
        parts.append("body differs (digest only)")
    return "; ".join(parts)


def _decide_site_rules(inputs: DecisionInputs, basis: Evidence) -> Recommendation:
    found = inputs.site_rule_candidates
    evidence = [
        EvidenceRef(
            pointer=pointer("decision_inputs", "site_rule_candidates", "endpoints"),
            value=[endpoint.model_dump(mode="json") for endpoint in found.endpoints],
            n=found.runs,
        ),
    ]
    frequent = [e for e in found.endpoints if e.misses >= SITE_RULE_MIN_NEAR_MISSES]
    verdict = None
    if found.runs:
        verdict = (
            "propose endpoint rules for review: "
            + "; ".join(
                f"{e.host} {e.method} {e.path} [{_field_set_text(e.field_sets[0])}]"
                for e in frequent
            )
            if frequent
            else f"keep hand-written rules: no endpoint reached {SITE_RULE_MIN_NEAR_MISSES} "
            f"near misses"
        )
    return _recommend(
        basis,
        f"propose a rule for review (ignore_query / ignore_body_paths, scoped to the endpoint) "
        f"for an endpoint (host, method, path) with ≥ {SITE_RULE_MIN_NEAR_MISSES} misses whose "
        f"nearest recorded request differs only in query parameters or body fields, naming its "
        f"most frequent combination of differing fields (SITE_RULE_MIN_NEAR_MISSES)",
        evidence,
        verdict,
        has_data=found.runs > 0,
        sites=found.sites,
    )


# ── building ──────────────────────────────────────────────────────────


def _inputs(chain: ChainRuns, manifest: DatasetManifest | None) -> Inputs:
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
        datasets[name] = DatasetInputs(
            present=present, format=found.format if found is not None else None
        )
    return Inputs(roles=roles, datasets=datasets)


#: ``token_use.reason`` when the report was computed without a chain spec.
NO_SPEC_LIMIT = "computed without a chain spec, so the token limit is not known"


def _token_use(chain: ChainRuns, limit: int | Unlimited | None) -> TokenUseReport:
    roles = {role: role_token_use(chain.role(role)) for role in ROLES}
    tokens = sum(use.tokens for use in roles.values())
    if limit is None:
        reached, reason = None, NO_SPEC_LIMIT
    else:
        reached, reason = (limit != UNLIMITED and tokens >= limit), None
    return TokenUseReport(
        limit=limit, tokens=tokens, limit_reached=reached, reason=reason, roles=roles
    )


def build_report(
    layout: ChainLayout,
    *,
    evidence: Evidence,
    planted: Sequence[Planted] = (),
    name: str | None = None,
    token_limit: int | Unlimited | None = None,
) -> FlywheelReport:
    """Compute the report of the chain rooted at *layout* (module docstring).

    Reads only the root's committed runs and published datasets; never
    raises for a damaged run (it is skipped with its reason). *token_limit*
    is the chain spec's; ``None`` when there is no spec (``report --root``).

    Raises:
        DatasetFormatError: A published dataset does not read back as this
            build's format (``metrics.read_dataset_manifest``,
            ``metrics.dataset_samples``) — never read as zeros.
    """
    chain = open_chain(layout)
    manifest = read_dataset_manifest(layout)
    fidelity = same_path_fidelity(chain)
    blocked = anti_bot(chain.role("record"))
    stored = storage(chain)
    samples = manifest.samples if manifest is not None else None
    export_sites = exported_sites(chain, manifest)
    metrics = Metrics(
        archive_overhead=archive_overhead(
            chain.role("record"),
            chain.role("control"),
            roles=[chain.role(role) for role in ROLES],
        ),
        body_completeness=body_completeness_metric(chain.role("record")),
        same_path_fidelity=fidelity,
        restoration=restoration(chain.role("branch")),
        branch_coverage=branch_coverage(chain.role("branch")),
        replay_determinism=replay_determinism(chain),
        export_determinism=export_determinism(layout),
        privacy=privacy(chain, planted),
    )
    sections = Sections(
        sites=sites(
            chain, fidelity=fidelity, blocked=blocked.blocked_sites, exclude_below=SITE_MIN_FIDELITY
        ),
        anti_bot=blocked,
        duplicate_branches=duplicate_branches(chain.role("branch"), manifest),
        post_branch_misses=post_branch_misses(chain.role("branch")),
        storage=stored,
        retention=retention(chain, stored, samples, RETENTION_PROJECTION_TASKS),
        export_yield=export_yield(layout, manifest),
    )
    inputs = DecisionInputs(
        branch_document_misses=branch_document_misses(chain.role("branch")),
        site_rule_candidates=site_rules(chain),
    )
    retention_recommendation = _rec_retention(sections.retention, evidence, sites=stored.sites)
    recommendations = Recommendations(
        metrics=MetricRecommendations(
            archive_overhead=_rec_overhead(metrics.archive_overhead, evidence),
            body_completeness=_rec_body(metrics.body_completeness, evidence),
            same_path_fidelity=_rec_fidelity(metrics.same_path_fidelity, evidence),
            restoration=_rec_restoration(metrics.restoration, evidence),
            branch_coverage=_rec_coverage(metrics.branch_coverage, evidence),
            replay_determinism=_rec_determinism(metrics.replay_determinism, evidence),
            export_determinism=_rec_export(
                metrics.export_determinism, evidence, sites=export_sites
            ),
            privacy=_rec_privacy(metrics.privacy, evidence),
        ),
        sections=SectionRecommendations(
            sites=_rec_sites(sections.sites, evidence),
            anti_bot=_rec_anti_bot(sections.anti_bot, evidence),
            duplicate_branches=_rec_duplicates(sections.duplicate_branches, evidence),
            post_branch_misses=_rec_post(sections.post_branch_misses, evidence),
            storage=_rec_storage(sections.storage, evidence),
            retention=retention_recommendation,
            export_yield=_rec_yield(sections.export_yield, evidence, sites=export_sites),
        ),
    )
    decisions = Decisions(
        miss_policy=_decide_miss_policy(metrics, sections, evidence),
        recording_coverage=_decide_recording_coverage(inputs, evidence),
        site_rules=_decide_site_rules(inputs, evidence),
        retention=retention_recommendation,
    )
    return FlywheelReport(
        chain=ChainInfo(name=name, evidence=evidence),
        token_use=_token_use(chain, token_limit),
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
    """Write ``report.json`` and ``report.md`` into *out_dir* (created); their paths.

    Raises:
        OSError: *out_dir* cannot be created or written (the caller names it).
    """
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
    "sites": "站点与剔除名单 — sites and the exclusion list",
    "anti_bot": "anti-bot / Cloudflare",
    "duplicate_branches": "重复分叉率 — duplicate branches",
    "post_branch_misses": "POST 分叉的 miss — POST branch misses",
    "storage": "存储增长与压缩评估 — storage growth and compression",
    "retention": "保留期与容量 — retention and capacity",
    "export_yield": "导出产出 — export yield",
}
_DECISION_TITLES: dict[str, str] = {
    "miss_policy": "Miss policy — is strict enough?",
    "recording_coverage": "Recording coverage — expand it at recording time?",
    "site_rules": "Site rules — generate them automatically?",
    "retention": "Retention — how much disk does a task take?",
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


def _settle_md(wait: SettleWait) -> list[str]:
    """The settle wait of one role's observations, and the runs it could not read."""
    return [
        f"- settle wait before each observation ({wait.role}): p50 {_fmt(wait.p50_ms)} ms, "
        f"p95 {_fmt(wait.p95_ms)} ms over {wait.observations} observation(s) of {wait.runs} "
        f"run(s); ended by the cap: {_ratio_md(wait.capped)}; page state unknown: "
        f"{wait.unknown}; sites: {wait.sites}",
        *_skipped_md(wait.skipped, indent="  "),
    ]


def _near_miss_md(endpoints: Sequence[NearMissEndpoint]) -> list[str]:
    """One line per endpoint with near misses, then one per combination of fields that differed."""
    lines: list[str] = []
    for endpoint in endpoints:
        lines.append(
            f"  - {endpoint.host} {endpoint.method} {endpoint.path}: {endpoint.misses} near "
            f"miss(es); query {_fmt(endpoint.query)}; body {_fmt(endpoint.body)}; "
            f"body differs (digest only) {endpoint.opaque_body}"
        )
        lines += [
            f"    - {fields.misses} × {_field_set_text(fields)}" for fields in endpoint.field_sets
        ]
    return lines


def _skipped_md(skipped: Mapping[str, str], indent: str = "") -> list[str]:
    """One line per run a measure left out, with why (a measure's ``skipped``)."""
    return [f"{indent}- skipped `{run}`: {reason}" for run, reason in skipped.items()]


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
    lines.append(f"- sites: {item.sites}")
    lines.append(f"- basis: {item.basis}")
    if item.status_reason is not None:
        lines.append(f"- why {item.status}: {item.status_reason}")
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
            *_skipped_md(m.bytes.skipped),
            *[line for wait in m.settle_wait for line in _settle_md(wait)],
        ]
    if name == "body_completeness":
        m = metrics.body_completeness
        return [
            f"- records: {m.records} in {m.runs} run(s); by state: {_fmt(m.by_state)}",
            f"- complete: {_ratio_md(m.complete)}; with body: {_ratio_md(m.with_body)}",
            *_skipped_md(m.skipped),
        ]
    if name == "same_path_fidelity":
        m = metrics.same_path_fidelity
        lines = [
            f"- overall: restorer {_ratio_md(m.overall.restorer)}, strict {_ratio_md(m.overall.strict)}, "
            f"runs reaching the checkpoint with complete checks {_ratio_md(m.overall.runs_reaching_checkpoint)}, "
            f"runs passing {_ratio_md(m.overall.runs_passing)}",
            f"- screenshot bytes equal {_ratio_md(m.overall.exact_screenshot)}; "
            f"URL comparison coverage {_ratio_md(m.overall.url_coverage)}; "
            f"element-signature comparison coverage {_ratio_md(m.overall.signature_coverage)}",
            f"- media-state comparison coverage {_ratio_md(m.overall.media_coverage)}; "
            f"steps failing media_mismatch: {m.overall.media_mismatches}; "
            f"media alignment by outcome: {_fmt(m.overall.media_alignment)}",
            "- strict compares equal URLs and screenshot bytes only where both were measured; "
            "runs passing covers every checked state through the last checkpoint, not task correctness.",
        ]
        for site, item in m.by_site.items():
            lines.append(
                f"- {site}: restorer {_ratio_md(item.restorer)}, strict {_ratio_md(item.strict)}, "
                f"runs passing {_ratio_md(item.runs_passing)}, "
                f"checkpoint check coverage {_ratio_md(item.runs_reaching_checkpoint)}, "
                f"screenshot bytes equal {_ratio_md(item.exact_screenshot)}, "
                f"URL coverage {_ratio_md(item.url_coverage)}, "
                f"element-signature coverage {_ratio_md(item.signature_coverage)}, "
                f"media coverage {_ratio_md(item.media_coverage)}, "
                f"media_mismatch steps {item.media_mismatches}, "
                f"media alignment {_fmt(item.media_alignment)}"
            )
        for failure in m.overall.first_failures:
            lines.append(
                f"  - first failure of `{failure.run}`: step {_fmt(failure.step)} "
                f"{', '.join(failure.reasons) or failure.note or ''}"
            )
        lines += _skipped_md(m.overall.skipped)
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
            f"- branches: {m.branches}; empty (no action from the checkpoint on, left out): "
            f"{m.empty.total} {_fmt(m.empty.by_reason)}",
            f"- branches that acted with no main-frame document miss: "
            f"{_ratio_md(m.zero_document_miss)}; document misses {m.document_misses}",
            f"- other misses: {m.other_misses.total}; by action {_fmt(m.other_misses.by_action)}, "
            f"resourceType {_fmt(m.other_misses.by_resource_type)}, host {_fmt(m.other_misses.by_host)}, "
            f"reason {_fmt(m.other_misses.by_reason)}",
            *_skipped_md(m.skipped),
        ]
    if name == "replay_determinism":
        lines = []
        for role, item in metrics.replay_determinism.by_role.items():
            lines.append(
                f"- {role}: identical terminal screenshots {_ratio_md(item.identical)}, checkpoint "
                f"screenshots alike {_ratio_md(item.checkpoint_identical)}, unpaired {item.unpaired}, "
                f"without terminal {item.without_terminal}"
            )
            lines += _skipped_md(item.skipped, indent="  ")
        return lines
    if name == "export_determinism":
        m = metrics.export_determinism
        return [
            f"- first: {m.first.digest if m.first else 'none'} ({m.first.files if m.first else 0} file(s))",
            f"- second: {m.second.digest if m.second else 'none'}",
            f"- identical: {_fmt(m.identical)}" + (f" — {m.reason}" if m.reason else ""),
        ]
    m = metrics.privacy
    lines = [
        f"- sent to the model: {m.model_requests.items} recorded request(s) of {m.runs} run(s)",
        f"- datasets: {m.datasets.items} file(s)",
        "- not searched: env/ and the rest of each trace (they stay on this machine)",
    ]
    for where, gate, unit in (
        ("sent to the model", m.model_requests, "request"),
        ("datasets", m.datasets, "file"),
    ):
        lines += [
            f"- {where}, planted `{item.label}` ({item.source}): {_fmt(item.occurrences)} "
            f"occurrence(s) in {_fmt(item.items)} {unit}(s)"
            + (f" — {item.reason}" if item.reason else "")
            for item in gate.planted
        ]
        lines.append(f"- {where}, credential shapes: {_fmt(gate.shapes)}")
    return lines + _skipped_md(m.skipped)


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
            f"misses {item.document_misses}, other misses {item.other_misses}, JSONP callback "
            f"names rewritten {item.callback_rewrites}, responses answered by an identical record "
            f"{_fmt(item.substitutions)}, responses repeated after exhaustion {item.reuses}"
            + (" — blocked" if item.blocked else "")
            for site, item in s.by_site.items()
        ]
        lines += [f"- exclude {item.site}: {'; '.join(item.reasons)}" for item in s.exclusions]
        lines += _skipped_md(s.skipped)
        lines += ["", "```yaml", s.yaml.rstrip("\n"), "```"]
        return lines
    if name == "anti_bot":
        s = sections.anti_bot
        lines = [f"- recordings checked: {s.runs_checked}; blocked sites: {_fmt(s.blocked_sites)}"]
        lines += [
            f"- `{hit.run}` step {_fmt(hit.step)}: {hit.status} {hit.url} ({', '.join(hit.signals)})"
            for hit in s.hits
        ]
        lines += _skipped_md(s.skipped)
        return lines
    if name == "duplicate_branches":
        s = sections.duplicate_branches
        return [
            f"- duplicates: {_ratio_md(s.rate)} in {s.groups} group(s); not compared (no action at "
            f"the checkpoint step) {s.not_compared}; the first export flagged "
            f"{_fmt(s.dataset_duplicates)}",
            *_skipped_md(s.skipped),
        ]
    if name == "post_branch_misses":
        s = sections.post_branch_misses
        return [
            f"- POST misses from the checkpoint on: {s.document_misses} document, {s.other_misses} "
            f"other; branches with one {_ratio_md(s.branches_with_post_miss)}",
            *_skipped_md(s.skipped),
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
            lines += _skipped_md(item.skipped, indent="  ")
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


def _token_use_md(use: TokenUseReport) -> list[str]:
    """The ``Token use`` section: the stop first, when the runs reached the limit."""
    lines: list[str] = []
    if use.limit_reached:
        lines += [
            f"**The chain stopped at its token limit.** Its runs recorded {use.tokens} tokens, "
            f"at or over the limit of {use.limit}: no stage starts once the limit is reached, "
            f"so everything below covers only the runs and datasets that exist.",
            "",
        ]
    if use.limit is None:
        limit = f"not known ({use.reason})"
    elif use.limit == UNLIMITED:
        limit = UNLIMITED
    else:
        limit = f"{use.limit} tokens ({'reached' if use.limit_reached else 'not reached'})"
    lines.append(f"- limit: {limit}; used: {use.tokens} tokens")
    for role, item in use.roles.items():
        lines.append(
            f"- {role}: {item.tokens} tokens over {item.calls} call(s) in {item.runs} run(s); "
            f"{len(item.calls_without_usage)} call(s) reported no usage"
        )
        lines += [
            f"  - no usage reported (not counted): `{call}`" for call in item.calls_without_usage
        ]
        lines += [
            f"  - incomplete recording `{run}`: {reason}" for run, reason in item.incomplete.items()
        ]
        lines += _skipped_md(item.skipped, indent="  ")
    return lines


def render_markdown(report: FlywheelReport) -> str:
    """``report.md``: one section per metric, per section and per decision, each ending in its recommendation."""
    lines = [
        f"# Flywheel report{f' — {report.chain.name}' if report.chain.name else ''}",
        "",
        f"Format `{report.format}`; evidence basis: **{report.chain.evidence}**"
        + (f" ({REHEARSAL_NOTE})" if report.chain.evidence == "rehearsal" else "")
        + f". A recommendation is supported only on live evidence from at least "
        f"{SUPPORTED_MIN_SITES} sites.",
        "",
        "## Token use",
        "",
        *_token_use_md(report.token_use),
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
    lines += ["", "## Metrics", ""]
    for name, title in _METRIC_TITLES.items():
        lines += [f"### {title}", "", *_lines_metric(name, report.metrics), ""]
        lines += [*_recommendation_md(getattr(report.recommendations.metrics, name)), ""]
    lines += ["## Sections", ""]
    for name, title in _SECTION_TITLES.items():
        lines += [f"### {title}", "", *_lines_section(name, report.sections), ""]
        lines += [*_recommendation_md(getattr(report.recommendations.sections, name)), ""]
    lines += ["## Pending decisions", ""]
    candidates = report.decision_inputs.site_rule_candidates
    misses = report.decision_inputs.branch_document_misses
    lines += [
        f"- document misses of the {misses.branches} branch(es) that acted: "
        f"{misses.document_misses}; GETs of unrecorded URLs {_ratio_md(misses.get_no_match)}",
        *_skipped_md(misses.skipped),
        f"- site-rule candidates over {candidates.runs} replayed run(s): "
        f"{len(candidates.endpoints)} endpoint(s), "
        f"{sum(e.misses for e in candidates.endpoints)} near miss(es)",
        *_near_miss_md(candidates.endpoints),
        *_skipped_md(candidates.skipped),
        "",
    ]
    for name, title in _DECISION_TITLES.items():
        lines += [f"### {title}", "", *_recommendation_md(getattr(report.decisions, name)), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = [
    "ACTION_FAILED_MAX_KEPT_SHARE",
    "BRANCH_MAX_DUPLICATE_SHARE",
    "CAPTURE_MIN_BODY_SHARE",
    "COMPRESS_MAX_GZIP_RATIO",
    "EXPAND_COVERAGE_MIN_UNRECORDED_SHARE",
    "INSUFFICIENT_SAMPLE",
    "NO_SPEC_LIMIT",
    "REHEARSAL_NOTE",
    "REPLAY_MIN_FIDELITY",
    "REPLAY_MIN_IDENTICAL_SHARE",
    "REPORT_FORMAT",
    "RESTORATION_MIN_RATE",
    "RETENTION_PROJECTION_TASKS",
    "SITE_MIN_FIDELITY",
    "SITE_RULE_MIN_NEAR_MISSES",
    "STRICT_MIN_IN_ARCHIVE_SHARE",
    "SUPPORTED_MIN_SITES",
    "Decisions",
    "EvidenceRef",
    "FlywheelReport",
    "Recommendation",
    "TokenUseReport",
    "build_report",
    "pointer",
    "render_markdown",
    "report_json",
    "write_report",
]
