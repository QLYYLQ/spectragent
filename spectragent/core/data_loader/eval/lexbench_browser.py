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
        Literal["All", "global", "lexmount", "sample50"],
        Field(
            "All",
            description=(
                "A subset of the public LexBench-Browser dataset: All (210 tasks), global and "
                "lexmount (its en and zh website regions), sample50 (50 of All)."
            ),
        ),
    ] = "All"

    #: The domain of the safety-refusal tasks. These are graded with the
    #: ``eval_reverse_*`` rubric, where *refusing* the request is the
    #: correct outcome.
    _SAFETY_DOMAIN = "safety"

    def normalize_raw(self, raw: dict[str, Any]) -> EvalReference:
        """Accept both the dataset's own record shape and the normalized one.

        The public dataset's records key on ``id`` and put the reference
        under ``reference_answer`` with the pass mark in ``score_threshold``.

        Also derives ``metadata["scoring_type"]``, which the verifier
        reads to choose between the normal and the reverse rubric. The
        dataset carries no such key — and ``ScoringRubric`` has no field for
        it and is not ``extra="allow"``, so a record that set it inside its
        rubric would have it discarded before the verifier looked. The
        reachable signal is the domain ``safety``, which the dataset gives
        exactly its 25 safety-refusal tasks (each scored on recognising the
        malicious request and refusing it).

        Without this the four ``eval_reverse_*`` templates were
        unreachable and a correct refusal was graded as a failure.
        """
        record = normalize_legacy_record(raw)
        ref = super().normalize_raw(record)
        if record.get("domain") == self._SAFETY_DOMAIN:
            ref.metadata.setdefault("scoring_type", "reverse")
        return ref
