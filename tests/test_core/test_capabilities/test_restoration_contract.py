"""Every shipped capability that verifies restorations names its own evidence type.

A capability's restoration evidence is read back from a trace by its
``capability_id`` through the capability registry (``docs/architecture_design.md``
§4.9). A capability that verifies restorations but forgets its
``RestorationEvidence`` class attribute would write branch runs that no reader
accepts, and nothing else notices until a trace is read. So for every shipped
class with a callable ``verify_restoration``: it names a subclass of
``RestorationEvidence`` that declares its own checks and judges them, and that
pins ``capability_id`` to the capability's registry key.
"""

from __future__ import annotations

from typing import ClassVar, Literal, get_args

from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.schemas.restoration import ACTION_REASONS, RestorationEvidence


def contract_problems(key: str, capability: type) -> list[str]:
    """What *capability*, registered under *key*, gets wrong about its restoration evidence."""
    evidence = getattr(capability, "RestorationEvidence", None)
    if not (
        isinstance(evidence, type)
        and issubclass(evidence, RestorationEvidence)
        and evidence is not RestorationEvidence
    ):
        return [f"{key}: {capability.__name__} names no RestorationEvidence subclass"]
    problems: list[str] = []
    reasons = evidence.REASONS
    if not reasons or len(set(reasons)) != len(reasons):
        problems.append(f"{key}: {evidence.__name__}.REASONS is empty or repeats a check")
    if set(reasons) & set(ACTION_REASONS):
        problems.append(f"{key}: {evidence.__name__}.REASONS reuses the framework's action reasons")
    if evidence.failed_checks is RestorationEvidence.failed_checks:
        problems.append(f"{key}: {evidence.__name__} does not judge its checks (failed_checks)")
    pinned = evidence.model_fields["capability_id"]
    if get_args(pinned.annotation) != (key,) or pinned.default != key:
        problems.append(f"{key}: {evidence.__name__}.capability_id is not pinned to {key!r}")
    return problems


def test_each_restoring_capability_names_its_own_evidence() -> None:
    # Shipped classes only: other test modules register throwaway capabilities
    # into the same global registry (``test_receptor_hygiene.py``).
    restoring = [
        (key, cls)
        for key in sorted(CapabilityRegistrar.keys())
        if (cls := CapabilityRegistrar.get(key)).__module__.startswith("spectragent.")
        and callable(getattr(cls, "verify_restoration", None))
    ]
    assert restoring, "no shipped capability verifies restorations: the guard would pass vacuously"
    problems = [problem for key, cls in restoring for problem in contract_problems(key, cls)]
    assert not problems, problems


def test_the_guard_names_each_defect() -> None:
    """A guard nobody has seen fail proves nothing: one defect of each kind, planted."""

    class Unjudged(RestorationEvidence):
        REASONS: ClassVar[tuple[str, ...]] = ("action_failed", "action_failed")

    class Forgetful:
        async def verify_restoration(self) -> None: ...

    class Careless(Forgetful):
        RestorationEvidence = Unjudged

    class Judged(RestorationEvidence):
        REASONS: ClassVar[tuple[str, ...]] = ("drifted",)
        capability_id: Literal["elsewhere"] = "elsewhere"

        def failed_checks(self) -> list[str]:
            return []

    class Misnamed(Forgetful):
        RestorationEvidence = Judged

    assert contract_problems("probe", Forgetful) == [
        "probe: Forgetful names no RestorationEvidence subclass"
    ]
    assert contract_problems("probe", Careless) == [
        "probe: Unjudged.REASONS is empty or repeats a check",
        "probe: Unjudged.REASONS reuses the framework's action reasons",
        "probe: Unjudged does not judge its checks (failed_checks)",
        "probe: Unjudged.capability_id is not pinned to 'probe'",
    ]
    assert contract_problems("probe", Misnamed) == [
        "probe: Judged.capability_id is not pinned to 'probe'"
    ]
