"""LexBench-Browser benchmark eval loader."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from spectragent.core.data_loader._lexbench_legacy import normalize_legacy_record
from spectragent.core.data_loader.eval._registrar import EvalLoaderBase
from spectragent.schemas.eval_reference import EvalReference


@dataclass
class LexBenchBrowserEvalLoader(EvalLoaderBase):
    """Loads LexBench-Browser evaluation reference data."""

    __registry_key__ = "lexbench_browser"

    subset: Annotated[
        Literal[
            "All",
            "L1",
            "L2",
            "L3-api",
            "L3-security",
            "omw",
            "omw-20pct",
            "omw-80pct",
            "private",
        ],
        Field("All", description="LexBench data subset"),
    ] = "All"

    #: Tag that marks a safety-refusal task. These are graded with the
    #: ``eval_reverse_*`` rubric, where *refusing* the request is the
    #: correct outcome.
    _SECURITY_TAG = "L3-security"

    def _normalize_raw(self, raw: dict[str, Any]) -> EvalReference:
        """Accept both the normalized and the upstream record shape.

        Upstream records key on ``id`` and put the reference under
        ``reference_answer`` with the pass mark in ``score_threshold``.

        Also derives ``metadata["scoring_type"]``, which the evaluator
        reads to choose between the normal and the reverse rubric. No
        shipped LexBench file carries that key — and ``ScoringRubric``
        has no field for it and is not ``extra="allow"``, so even an
        upstream record that set it would have it discarded before the
        evaluator looked. The reachable signal is the ``L3-security``
        tag, which sits on exactly the 25 safety-refusal records.

        Without this the four ``eval_reverse_*`` templates were
        unreachable and a correct refusal was graded as a failure.
        """
        ref = super()._normalize_raw(normalize_legacy_record(raw))
        if self._SECURITY_TAG in ref.tags:
            ref.metadata.setdefault("scoring_type", "reverse")
        return ref
