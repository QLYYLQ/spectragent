"""Configuration failure stages and the public contracts that rejected a run."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from conscribe import CircularWiringError, WiringResolutionError

from spectragent.core.composer import ComposeError
from spectragent.core.loops.validation import (
    IncompatibleError,
    IncompatibleLoopInputError,
    MissingEnvironmentError,
)
from spectragent.core.negotiation.action import IncompatibleActionError
from spectragent.core.negotiation.archive import IncompatibleArchiveError
from spectragent.core.negotiation.observation import IncompatibleFragmentError


class ConfigInspectionError(Exception):
    """A failed stage, preserving the exception produced by its actual owner."""

    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"Configuration inspection failed during {stage}")


@contextmanager
def inspection_stage(name: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        raise ConfigInspectionError(name, exc) from exc


def contract_details(exc: Exception) -> dict[str, Any]:
    """Read documented exception data, never infer a contract from its message."""
    if isinstance(exc, ComposeError) and exc.issue is not None:
        return {
            "contract": "configuration_assembly",
            "message": exc.issue.message,
            "issues": [asdict(exc.issue)],
        }
    if isinstance(exc, IncompatibleError):
        return {
            "contract": "agent_slots",
            "agent": exc.agent_name,
            "loop": exc.loop_id,
            "missing_slots": list(exc.missing_slots),
            "needed_for": exc.needed_for,
        }
    if isinstance(exc, IncompatibleLoopInputError):
        return {
            "contract": "loop_inputs",
            "loop": exc.loop_id,
            "missing": list(exc.missing),
            "unknown": list(exc.unknown),
            "mistyped": dict(exc.mistyped),
        }
    if isinstance(exc, MissingEnvironmentError):
        return {"contract": "requires_environment", "loop": exc.loop_id}
    if isinstance(
        exc, IncompatibleActionError | IncompatibleFragmentError | IncompatibleArchiveError
    ):
        contract = (
            "actions"
            if isinstance(exc, IncompatibleActionError)
            else "observations"
            if isinstance(exc, IncompatibleFragmentError)
            else "archive"
        )
        result = {
            "contract": contract,
            "missing": sorted(exc.missing),
            "provided": sorted(exc.provided),
        }
        if isinstance(exc, IncompatibleFragmentError | IncompatibleArchiveError):
            result["requested_by"] = {key: list(value) for key, value in exc.requested_by.items()}
        return result
    if isinstance(exc, CircularWiringError):
        return {"contract": "acyclic_wiring", "cycle": list(exc.cycle)}
    if isinstance(exc, WiringResolutionError):
        return {
            "contract": "wiring_declaration",
            "issues": [
                {
                    "owner": problem.owner,
                    "field": problem.param_name,
                    "registry": problem.registry_name,
                    "code": problem.kind,
                    "message": problem.detail,
                }
                for problem in exc.problems
            ],
        }
    return {}
