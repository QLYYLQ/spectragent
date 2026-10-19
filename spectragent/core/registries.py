"""Public registrar catalog shared by composition, inspection, and generation.

This is the one collection point for YAML-configurable framework layers.
It does not instantiate components or hold run-owned mutable state.
"""

from __future__ import annotations


def collect_registrars() -> dict[str, type]:
    """Return the YAML-configurable framework registrars in their stable order.

    The explicit layer order keeps generated configurations deterministic.
    Each call returns a fresh mapping; callers may select a subset without
    changing another Composer or the generator. Concrete implementations
    are discovered by the caller's normal Composer/build preparation.
    """
    from spectragent.agents._registrar import AgentRegistrar
    from spectragent.agents.memory._registrar import MemoryRegistrar
    from spectragent.core.actions._registrar import ActionRegistrar
    from spectragent.core.archive._registrar import ArchiverRegistrar
    from spectragent.core.capabilities._registrar import CapabilityRegistrar
    from spectragent.core.capabilities.browser.backends._registrar import (
        BrowserProviderRegistrar,
    )
    from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerRegistrar
    from spectragent.core.capabilities.browser.dom.serializers import (
        DOMSerializerRegistrar,
    )
    from spectragent.core.capabilities.browser.input import (
        MouseTrajectoryRegistrar,
        TypingRhythmRegistrar,
    )
    from spectragent.core.capabilities.browser.replay import (
        MissPolicyRegistrar,
        NetworkSourceRegistrar,
        ReplayabilityRegistrar,
    )
    from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
    from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
    from spectragent.core.data_loader.task.step_selector import StepSelectorRegistrar
    from spectragent.core.environments._registrar import EnvironmentRegistrar
    from spectragent.core.hooks._registrar import HookRegistrar
    from spectragent.core.llm._registrar import LLMRegistrar
    from spectragent.core.llm.decision_registrar import DecisionModelRegistrar
    from spectragent.core.loops._registrar import AgentLoopRegistrar
    from spectragent.core.loops.restore import RestorerRegistrar
    from spectragent.evaluator._registrar import EvaluatorRegistrar
    from spectragent.export._registrar import ExporterRegistrar
    from spectragent.export.translate._registrar import ActionTranslatorRegistrar
    from spectragent.replay_review._registrar import ReplayReviewerRegistrar

    return {
        # Leaves of the wiring DAG first — not load-bearing but keeps
        # the generated union ordering stable under rename.
        "browser_provider": BrowserProviderRegistrar,
        # Browser-internal strategy axes. Excluding them left their
        # config surface untyped — most visibly ``mouse_trajectory``,
        # which was emitted as a bare ``str`` so a typo was silently
        # downgraded to the instant (non-humanized) strategy.
        "mouse_trajectory": MouseTrajectoryRegistrar,
        "typing_rhythm": TypingRhythmRegistrar,
        "dom_cleaner": DOMCleanerRegistrar,
        "dom_serializer": DOMSerializerRegistrar,
        # How far a run is pinned for replay (``none`` / ``pinned``, §4.14):
        # collected so ``pinned``'s seed / time zone / locale / DPR knobs are
        # typed in the generated union rather than lost behind a Literal.
        "replayability": ReplayabilityRegistrar,
        # Offline replay (§4.14 "回放"): the network source and, nested in its
        # ``archive`` block, the miss policy — leaves before their owners, so
        # the generated union nests capability → network_source → miss_policy
        # with every knob (matcher, isolation, shift_dates, …) typed.
        "miss_policy": MissPolicyRegistrar,
        "network_source": NetworkSourceRegistrar,
        # Environment evidence (§4.5, §4.14): the trace hook's ``archive``
        # receptor draws from it, so each archiver's knobs are typed in YAML
        # and a mistyped archiver_id is refused.
        "archiver": ArchiverRegistrar,
        "action": ActionRegistrar,
        "capability": CapabilityRegistrar,
        # How a branch run restores its checkpoint (§4.1 ``BranchLoop``): a
        # receptor of the ``branch`` loop, collected before ``agent_loop``
        # so the loop's block nests the restorer union with every knob
        # (verify, screenshot_max_distance, pace) typed.
        "restorer": RestorerRegistrar,
        "agent_loop": AgentLoopRegistrar,
        "llm": LLMRegistrar,
        "decision_model": DecisionModelRegistrar,
        "replay_reviewer": ReplayReviewerRegistrar,
        "memory": MemoryRegistrar,
        "environment": EnvironmentRegistrar,
        "hook": HookRegistrar,
        # Where a branch sweep forks (§4.10 ``trace_branches``): a receptor of
        # that task loader, collected before ``task_loader`` so the loader's
        # block nests the selector union (``explicit`` steps, ``every_k``'s k
        # and offset) typed.
        "step_selector": StepSelectorRegistrar,
        "task_loader": TaskLoaderRegistrar,
        "eval_loader": EvalLoaderRegistrar,
        # Scoring. An optional layer — a config that only runs an agent
        # omits it — but a *layer*, so the judge model, its prompts and
        # its thresholds are chosen in YAML like everything else instead
        # of living in a per-benchmark script outside the package.
        "evaluator": EvaluatorRegistrar,
        # Training export (§4.15): how a teacher's actions become a student's.
        # An optional top-level layer, and the exporter's ``translator``
        # receptor — collected before the exporter so its block nests the
        # translator union with every knob (occlusion, untranslatable) typed.
        "action_translator": ActionTranslatorRegistrar,
        # The export job itself (§4.15): sources, filters, split, the student's
        # run config and the record format. An optional top-level layer — an
        # export YAML has only this block — whose ``translator`` receptor nests
        # the union collected just above.
        "exporter": ExporterRegistrar,
        "agent": AgentRegistrar,
    }
