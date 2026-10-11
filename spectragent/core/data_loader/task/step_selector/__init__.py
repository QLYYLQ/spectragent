"""The ``step_selector`` axis: at which steps of a recorded run a branch sweep forks.

A receptor of the ``trace_branches`` task loader
(``TraceBranchesTaskLoader.__wiring__["step_selector"]``,
``docs/architecture_design.md`` §4.10), so it is configured nested in the
loader's block and arrives constructed. Four strategies: ``all`` (every
candidate — the default), ``every_k`` (every k-th step from an offset),
``explicit`` (the steps named) and ``last`` (each source's last candidate —
the metrics chain's same-path replay, M33).

The loader hands a selector the source run's **candidates** — committed
policy steps whose prefix is committed and whose observation was recorded —
and owns the bookkeeping: a step a selector returns that is not a candidate
(``explicit`` naming a step past the end of a short source, say) is skipped
and recorded with its reason in ``loader.skipped``. A selector is a pure
choice: stateless, no I/O, nothing per source kept on ``self``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar


@runtime_checkable
class StepSelector(Protocol):
    """Which steps of one source run to fork at."""

    @property
    def selector_id(self) -> str: ...

    def select(self, candidates: Sequence[int]) -> list[int]:
        """The steps to fork at, ascending and without repeats.

        *candidates* are the source's branchable steps, ascending. A returned
        step outside them is skipped by the caller, with a reason.
        """
        ...


StepSelectorRegistrar = create_registrar(
    "step_selector",
    StepSelector,
    discriminator_field="selector_id",
)


@dataclass
class StepSelectorBase(metaclass=StepSelectorRegistrar.Meta):
    """Base for step selectors — subclass to auto-register.

    Each selector sets ``__registry_key__`` (the YAML-facing ``selector_id``)
    and declares its knobs as ``Annotated`` dataclass fields.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def selector_id(self) -> str:
        return self.__registry_key__

    def select(self, candidates: Sequence[int]) -> list[int]:
        raise NotImplementedError(f"step selector {self.selector_id!r} must implement select()")


def default_step_selector() -> StepSelector:
    """The selector used when no configuration named one: ``all``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for a hand-built loader.
    """
    from spectragent.core.data_loader.task.step_selector.all_steps import AllSteps

    return AllSteps()


# Registration: importing the package registers every shipped selector.
from spectragent.core.data_loader.task.step_selector.all_steps import AllSteps  # noqa: E402
from spectragent.core.data_loader.task.step_selector.every_k import EveryKSteps  # noqa: E402
from spectragent.core.data_loader.task.step_selector.explicit import ExplicitSteps  # noqa: E402
from spectragent.core.data_loader.task.step_selector.last import LastStep  # noqa: E402

__all__ = [
    "AllSteps",
    "EveryKSteps",
    "ExplicitSteps",
    "LastStep",
    "StepSelector",
    "StepSelectorBase",
    "StepSelectorRegistrar",
    "default_step_selector",
]
