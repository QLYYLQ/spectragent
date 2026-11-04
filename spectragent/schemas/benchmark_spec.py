"""How a run's benchmark was selected (``docs/architecture_design.md`` §5.3 "实验身份")."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

#: The parts of a benchmark block that decide how its tasks run, each with the
#: words a refusal names it by. Its ``verifiers`` are not among them: a
#: verifier never changes a run, so one added to the YAML later is allowed.
FINGERPRINTED_PARTS: dict[str, str] = {
    "task_loader": "task source (task_loader)",
    "eval_loader": "reference data (eval_loader)",
    "setup": "setup component (setup)",
    "capability": "capability part (capability)",
}

_SHA256 = r"^[0-9a-f]{64}$"


def _digest(value: object) -> str:
    """sha256 of *value* as sorted, compact JSON; an absent or empty part hashes as ``null``."""
    payload = json.dumps(
        value or None, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BlockFingerprints(BaseModel):
    """sha256 of each part of a benchmark block that decides how its tasks run, as it was selected.

    Taken from the block as ``--benchmark`` read it, ``--subset`` written in,
    before anything is redacted: the run's ``resolved`` records the free-form
    settings in it (a ``data_dir``, a setup's ``root``, the ``capability``
    list) as ``"<omitted>"``, so it cannot tell a changed block from the same
    one. What these do not cover: the contents and version of the
    benchmark's data, a ``data_dir: null`` resolved through the environment,
    and the capability parts a benchmark computes in code from its data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_loader: str = Field(pattern=_SHA256, description="sha256 of the block's task_loader.")
    eval_loader: str = Field(pattern=_SHA256, description="sha256 of the block's eval_loader.")
    setup: str = Field(pattern=_SHA256, description="sha256 of the block's setup component.")
    capability: str = Field(
        pattern=_SHA256, description="sha256 of the block's part of the capability configuration."
    )

    @classmethod
    def of(cls, block: Mapping[str, object]) -> BlockFingerprints:
        """The fingerprints of the benchmark *block* (a raw ``benchmark:`` block)."""
        return cls(**{part: _digest(block.get(part)) for part in FINGERPRINTED_PARTS})

    def changed(self, other: BlockFingerprints) -> list[str]:
        """The parts *other* fingerprints differently, in the words a refusal names them by."""
        return [
            words
            for part, words in FINGERPRINTED_PARTS.items()
            if getattr(self, part) != getattr(other, part)
        ]


class BenchmarkSpec(BaseModel):
    """How a run's benchmark was selected: its id, the ``--benchmark`` selection and ``--subset``.

    Recorded as ``TraceConfig.benchmark_spec`` by every run of a benchmark's
    task (``spectragent bench`` and ``spectragent run --task-id``). A branch run
    records its source run's, copied from the source trace: a branch sweep is a
    task source, not a benchmark, and each branch task belongs to its source
    run's benchmark, which it finds again by this record
    (``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源"). The
    block found there must still have the fingerprints recorded here.

    What the benchmark block held (its task loader, its setup component) is in
    the run's ``resolved``; this says where the block came from and what it
    was, so it is not part of the configuration digest, as the configuration's
    source file is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: str = Field(min_length=1, description="The selected benchmark's benchmark_id.")
    selection: str = Field(
        min_length=1,
        description=(
            "How it was selected: a shipped alias (configs/benchmarks/<alias>.yaml), or the "
            "absolute path of the benchmark YAML."
        ),
    )
    subset: str | None = Field(
        default=None,
        description=(
            "The --subset written into its task loader before the run was composed; null: the "
            "benchmark YAML's own."
        ),
    )
    fingerprints: BlockFingerprints = Field(
        description=(
            "sha256 of each part of the block that decides how its tasks run (task_loader, "
            "eval_loader, setup, capability), as it was selected; its verifiers are left out."
        )
    )

    def describe(self) -> str:
        """The command-line selection this record says: ``--benchmark X [--subset Y]``."""
        subset = f" --subset {self.subset}" if self.subset is not None else ""
        return f"--benchmark {self.selection}{subset}"


__all__ = ["FINGERPRINTED_PARTS", "BenchmarkSpec", "BlockFingerprints"]
