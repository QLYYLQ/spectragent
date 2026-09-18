"""Upstream LexBench record shape → normalized loader input.

``benchmark_data/browser/lexbench_browser/tasks_private.jsonl`` ships in
the upstream LexBench shape rather than the normalized one used by the
other files in that directory: it keys on an integer ``id``, calls the
instruction ``query``, the start URL ``target_website``, the reference
``reference_answer``, and leaves the login/risk-control flags flat at the
top level instead of nested under ``setup``.

This module converts that shape into the normalized dict the loader base
classes expect. It is the concrete reason ``_normalize_raw`` is an
overridable hook on both the task and the eval side.
"""

from typing import Any

#: Upstream keys consumed by the conversion below. Anything else is
#: forwarded into ``metadata`` rather than dropped.
_CONSUMED = frozenset(
    {
        "id",
        "query",
        "language",
        "target_website",
        "login_required",
        "login_type",
        "risk_control",
        "risk_control_types",
        "difficulty",
        "domain",
        "scenario_tier",
        "reasoning_type",
        "task_type",
        "reference_answer",
        "score_threshold",
    }
)


def is_legacy_record(raw: dict[str, Any]) -> bool:
    """True when ``raw`` uses the upstream LexBench shape."""
    return "task_id" not in raw and "query" in raw


def normalize_legacy_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert an upstream LexBench record to the normalized shape.

    Already-normalized records pass through untouched, so a loader can
    call this unconditionally for every split.
    """
    if not is_legacy_record(raw):
        return raw

    # Reference: upstream keeps the pass mark at the top level as
    # ``score_threshold``; normalized data nests it in the rubric.
    reference: dict[str, Any] = dict(raw.get("reference_answer") or {})
    scoring = dict(reference.get("scoring") or {})
    if scoring:
        threshold = raw.get("score_threshold")
        if threshold is not None:
            scoring.setdefault("threshold", threshold)
        reference["scoring"] = scoring

    # Tags: normalized data flattens the three upstream classifiers.
    tags = [
        value
        for value in (
            raw.get("scenario_tier"),
            raw.get("reasoning_type"),
            raw.get("task_type"),
        )
        if value
    ]

    metadata: dict[str, Any] = {key: value for key, value in raw.items() if key not in _CONSUMED}
    # Keep the upstream classifiers reachable — graders read ``task_type``.
    for key in ("task_type", "reasoning_type", "scenario_tier", "score_threshold"):
        if raw.get(key) is not None:
            metadata[key] = raw[key]

    return {
        "task_id": str(raw["id"]),
        "task_text": raw.get("query", ""),
        "language": raw.get("language", "en"),
        "start_url": raw.get("target_website"),
        "setup": {
            "login_required": bool(raw.get("login_required", False)),
            "login_type": raw.get("login_type") or None,
            "risk_control": bool(raw.get("risk_control", False)),
            "risk_control_types": raw.get("risk_control_types") or [],
        },
        "difficulty": raw.get("difficulty"),
        "domain": raw.get("domain"),
        "tags": tags,
        "reference": reference or None,
        "metadata": metadata,
    }
