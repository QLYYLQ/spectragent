"""Custom Pydantic types for schema standardization."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BeforeValidator


class EnvironmentStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class AgentDoneStatus(StrEnum):
    DONE = "done"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    ERROR = "error"


# --- EnvironmentStatus normalization ---

_ENV_STATUS_ALIASES: dict[str, EnvironmentStatus] = {
    "completed": EnvironmentStatus.SUCCESS,
    "complete": EnvironmentStatus.SUCCESS,
    "ok": EnvironmentStatus.SUCCESS,
    "pass": EnvironmentStatus.SUCCESS,
    "error": EnvironmentStatus.FAILED,
    "fail": EnvironmentStatus.FAILED,
    "terminated": EnvironmentStatus.FAILED,
    "cancelled": EnvironmentStatus.FAILED,
}


def _normalize_env_status(value: Any) -> EnvironmentStatus:
    """Normalize various status strings to EnvironmentStatus.

    Handles case-insensitive matching and common aliases like
    ``"completed"`` -> ``EnvironmentStatus.SUCCESS``.
    """
    if isinstance(value, EnvironmentStatus):
        return value

    if not isinstance(value, str):
        return EnvironmentStatus(str(value))

    lower = value.strip().lower()

    # Direct enum member match
    try:
        return EnvironmentStatus(lower)
    except ValueError:
        pass

    # Alias lookup
    if lower in _ENV_STATUS_ALIASES:
        return _ENV_STATUS_ALIASES[lower]

    raise ValueError(
        f"Cannot normalize env_status {value!r}. "
        f"Expected one of {[s.value for s in EnvironmentStatus]} "
        f"or aliases {list(_ENV_STATUS_ALIASES.keys())}"
    )


# --- AgentDoneStatus normalization ---

_DONE_ALIASES: dict[str, AgentDoneStatus] = {
    "completed": AgentDoneStatus.DONE,
    "complete": AgentDoneStatus.DONE,
    "success": AgentDoneStatus.DONE,
    "ok": AgentDoneStatus.DONE,
    "pass": AgentDoneStatus.DONE,
    "failed": AgentDoneStatus.ERROR,
    "fail": AgentDoneStatus.ERROR,
    "timed_out": AgentDoneStatus.TIMEOUT,
    "terminated": AgentDoneStatus.MAX_STEPS,
    "cancelled": AgentDoneStatus.ERROR,
}


def _normalize_agent_done(value: Any) -> AgentDoneStatus:
    """Normalize various strings to AgentDoneStatus.

    Handles case-insensitive matching and common aliases.
    """
    if isinstance(value, AgentDoneStatus):
        return value

    if not isinstance(value, str):
        return AgentDoneStatus(str(value))

    lower = value.strip().lower()

    # Direct enum member match
    try:
        return AgentDoneStatus(lower)
    except ValueError:
        pass

    # Alias lookup
    if lower in _DONE_ALIASES:
        return _DONE_ALIASES[lower]

    raise ValueError(
        f"Cannot normalize agent_done {value!r}. "
        f"Expected one of {[s.value for s in AgentDoneStatus]} "
        f"or aliases {list(_DONE_ALIASES.keys())}"
    )


def _ensure_utc(value: Any) -> datetime:
    """Coerce various datetime representations to a UTC-aware datetime.

    Accepts:
    - ``int`` / ``float``: Unix epoch seconds
    - Naive ``datetime``: Assumed UTC and tagged
    - Aware ``datetime``: Converted to UTC
    - ``str``: Passed through to let Pydantic handle parsing
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # str or other types — let Pydantic's default parsing handle it
    return value


UTCDatetime = Annotated[datetime, BeforeValidator(_ensure_utc)]
NormalizedEnvironmentStatus = Annotated[EnvironmentStatus, BeforeValidator(_normalize_env_status)]
NormalizedAgentDoneStatus = Annotated[AgentDoneStatus, BeforeValidator(_normalize_agent_done)]

# Deprecated aliases for backward compatibility
AgentStatus = EnvironmentStatus
NormalizedAgentStatus = NormalizedEnvironmentStatus
_normalize_status = _normalize_env_status
_STATUS_ALIASES = _ENV_STATUS_ALIASES
