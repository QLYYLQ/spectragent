"""The benchmark registry: one registered component per benchmark (``docs/architecture_design.md`` §4.10.1).

A benchmark owns where its tasks come from — its task loader, and the eval
loader that gives verdicts their reference answers — the verifiers that judge
its runs, and which of their verdicts is its brief result: its primary verdict
and success rule (§4.10.1 "主判定"). It may own its own setup of each task, a
setup component a capability hosts (``setup``,
:mod:`spectragent.benchmarks._setup_registrar`), and facts of the environment
its tasks need: its part of a capability's configuration, written in its
block's ``capability`` list or computed in code (:meth:`BenchmarkBase.computed_capability`),
which the Composer merges with the run configuration's
(:mod:`spectragent.core.capability_merge`). All of it is written inside its
block, or declared by its class, never as top-level sections, and a benchmark
YAML holds that one block under ``benchmark:``. A run selects it with
``--benchmark <alias|path>``; the run configuration never holds one
(:mod:`spectragent.benchmarks.selection`).

Each benchmark is a class in its own package, ``spectragent/benchmarks/<benchmark_id>/``,
imported only when a configuration names it (§4.2.1), and writes its complete
``__wiring__``: the task loader keys, the eval loader keys, the verifier kinds
and the setup component keys it takes. A verifier of its own lives in its
package too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import BaseModel, Field, JsonValue

from spectragent.benchmarks._setup_registrar import BenchmarkSetup, BenchmarkSetupBase
from spectragent.core.data_loader.eval._registrar import EvalLoaderBase, EvalLoaderRegistrar
from spectragent.core.data_loader.eval.base import BaseEvalLoader
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.data_loader.task.base import BaseTaskLoader
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.verdict import Verdict
from spectragent.verifiers._registrar import VerifierBase
from spectragent.verifiers.base import LiveVerifier, OfflineVerifier, attachment_of


@runtime_checkable
class Benchmark(Protocol):
    """A benchmark, as the entry points that run and score its tasks see it."""

    @property
    def benchmark_id(self) -> str: ...

    @property
    def task_loader(self) -> BaseTaskLoader: ...

    @property
    def setup(self) -> BenchmarkSetup | None:
        """Its own setup of each task, hosted by a capability; ``None`` when it has none."""
        ...

    def reference_loader(self) -> BaseEvalLoader | None:
        """The reference data its verdicts are scored against, or ``None`` when it has none."""
        ...

    def offline_verifiers(self) -> list[OfflineVerifier]:
        """Its verifiers that judge a run from the recorded trace, in the order its block lists them."""
        ...

    def live_verifiers(self) -> list[LiveVerifier]:
        """Its verifiers that judge a run against the live environment, at the scoring point, in its block's order."""
        ...

    @property
    def primary_verifier(self) -> str | None:
        """The verifier kind whose verdict is its brief result; ``None`` when it declares none."""
        ...

    def primary_verdict(self, verdicts: Mapping[str, Verdict]) -> Verdict | None:
        """Its primary verdict among a run's *verdicts* (slug → verdict); ``None`` when there is none."""
        ...

    def success(self, verdict: Verdict, result: AgentResult | None) -> float | None:
        """Its success rule: how far the run succeeded by its primary *verdict*, from 0 to 1."""
        ...


BenchmarkRegistrar = create_registrar(
    "benchmark",
    Benchmark,
    discriminator_field="benchmark_id",
)


class BenchmarkBase(metaclass=BenchmarkRegistrar.Meta):
    """Common base of every benchmark: its task loader, its eval loader, its verifiers and its setup.

    A concrete benchmark sets ``__registry_key__`` and writes its complete
    ``__wiring__``, naming the task loader key it takes, the eval loader key
    when it has reference data, the verifier kinds that can judge its runs,
    and when it sets its tasks up itself, the setup component key.
    ``task_loader`` is required: the block says where the tasks come from and
    with which knobs (``subset``, ``data_dir``). ``eval_loader`` may be left
    out (:meth:`reference_loader`), and so may ``verifiers``: none judges its
    runs then. A verifier writes no setting of the run, so its block is never
    part of the run's configuration identity (``docs/architecture_design.md``
    §5.3 "实验身份"). ``setup`` is ``None`` unless the benchmark wires one; a
    benchmark that cannot run without its setup declares the receptor without
    ``None``, which makes the block required.
    """

    __abstract__ = True
    #: The ``benchmark_id`` this benchmark registers under.
    __registry_key__: ClassVar[str]
    __wiring__: ClassVar[dict[str, Any]] = {}
    #: The verifier kind whose verdict is its brief result, its primary verdict
    #: (§4.10.1 "主判定"); ``None``: it declares none. Every verdict is kept;
    #: this only says which one a summary reports.
    __primary_verifier__: ClassVar[str | None] = None

    # Receptors, annotation only: conscribe installs a ``WiredField`` and the
    # Composer injects the configured instances. ``__init__`` sets the eval
    # loader's in-Python default.
    task_loader: TaskLoaderBase
    eval_loader: EvalLoaderBase | None
    # Optional in the schema: a block that leaves it out lists none. Never
    # ``None`` on an instance: ``__init__`` sets ``[]``, and the Composer
    # replaces it only with the verifiers a block lists.
    verifiers: list[VerifierBase] | None
    setup: BenchmarkSetupBase | None

    def __init__(
        self,
        capability: Annotated[
            tuple[dict[str, JsonValue], ...],
            Field(
                description=(
                    "This benchmark's part of the configuration of the capabilities its tasks run "
                    "on: blocks in the capability schema, each with its capability_id, writing "
                    "only what the benchmark needs (the device image, ...). Each is merged field "
                    "by field into the run configuration's block of that capability: a field "
                    "both write with different values is refused, naming both; on a list of "
                    "blocks (actions) a part sets options on the entries there, matched by their "
                    "discriminator, and never adds or removes one. [] (the default): none."
                )
            ),
        ] = (),
    ) -> None:
        self.capability = capability
        self.eval_loader = None
        self.verifiers = []
        self.setup = None

    @classmethod
    def computed_capability(cls, config: BaseModel) -> list[dict[str, JsonValue]]:
        """This benchmark's part of capability configurations computed in code, given its validated *config*.

        Merged like the blocks of its ``capability`` list, and refused in the
        same way when it disagrees with them or with the run configuration.
        ``[]`` here; AndroidWorld's is its ``open_app`` name table. The
        Composer asks only for a run that starts an environment.
        """
        return []

    @property
    def benchmark_id(self) -> str:
        return self.__registry_key__

    @property
    def primary_verifier(self) -> str | None:
        return type(self).__primary_verifier__

    def reference_loader(self) -> EvalLoaderBase | None:
        """The reference data this benchmark's verdicts are scored against.

        Its ``eval_loader`` block when it has one. Without one, the eval
        loader registered under its task loader's key, built for the task
        loader's subset: the reference data of the very tasks it runs, with
        that loader's defaults (``$SPECTRAGENT_BENCHMARK_DATA``). ``None``
        when no eval loader is registered under that key (a branch sweep's
        task source has none, §4.10). Built on each call: only a caller that
        scores asks, and it asks once.
        """
        configured = self.eval_loader
        if configured is not None:
            return configured
        tasks = self.task_loader
        loader_cls = EvalLoaderRegistrar.get_or_none(tasks.benchmark_name)
        return None if loader_cls is None else loader_cls(subset=tasks.subset)

    def offline_verifiers(self) -> list[OfflineVerifier]:
        """Its verifiers attached offline (needing no live environment), in its block's order.

        They judge its runs from the committed traces: after a sweep
        (``spectragent bench``) or any time later (``spectragent eval``).
        """
        return [
            verifier
            for verifier in self.verifiers or []
            if attachment_of(verifier) == "offline" and isinstance(verifier, OfflineVerifier)
        ]

    def live_verifiers(self) -> list[LiveVerifier]:
        """Its verifiers attached live (needing the live environment), in its block's order.

        They judge at the scoring point, each hosted by the capability it names
        (``docs/architecture_design.md`` §4.9 "基准组件由 capability 承载"):
        ``AgentRuntime.prepare()`` routes and binds them as it does the setup
        component.

        Raises:
            TypeError: One needs the live environment and names no capability
                to host it.
        """
        live: list[LiveVerifier] = []
        for verifier in self.verifiers or []:
            if attachment_of(verifier) != "live":
                continue
            if not isinstance(verifier, LiveVerifier):
                raise TypeError(
                    f"verifier {verifier.verifier_id!r} needs the live environment and names no "
                    f"capability to host it (LiveVerifier)"
                )
            live.append(verifier)
        return live

    def primary_verdict(self, verdicts: Mapping[str, Verdict]) -> Verdict | None:
        """Its primary verdict among a run's *verdicts* (slug → verdict); ``None`` when there is none.

        The verdict of the first verifier its block lists of its primary kind
        (``__primary_verifier__``); ``None`` when the block lists none of that
        kind.
        """
        kind = self.primary_verifier
        if kind is None:
            return None
        listed = [v.identity().slug for v in self.verifiers or [] if v.verifier_id == kind]
        return verdicts.get(listed[0]) if listed else None

    def success(self, verdict: Verdict, result: AgentResult | None) -> float | None:
        """Its success rule: how far the run succeeded by its primary *verdict*, from 0 to 1.

        ``None`` when the verdict judged nothing, which is never a failed
        task. The default reads the verdict's label: 1 passed, 0 failed. A
        benchmark whose primary verifier gives a score, or whose own runner
        counts the agent's ending too, states its own rule; *result* is the
        run's ``result.json``, ``None`` when the run wrote none.
        """
        if verdict.status != "judged" or verdict.label is None:
            return None
        return float(verdict.label)


__all__ = ["Benchmark", "BenchmarkBase", "BenchmarkRegistrar"]
