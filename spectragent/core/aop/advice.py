"""Advice types and Intercept declarations for the AOP engine.

An ``Intercept`` declares which method on which target role a Hook wants
to intercept, and with which advice type (before/after/around).

Hook implementations name their handler methods by convention:
``{advice}_{method}`` — e.g. ``after_observe``, ``before_ainvoke``,
``around_build_react_prompt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AdviceType(Enum):
    """When the hook handler runs relative to the target method."""

    BEFORE = "before"
    """Execute before the target method. Receives args/kwargs."""

    AFTER = "after"
    """Execute after the target method. Receives the return value."""

    AROUND = "around"
    """Wraps the entire call. Receives a ``proceed`` callable."""


@dataclass(frozen=True)
class Intercept:
    """Declaration of a single interception point.

    Attributes:
        method: Target method name (e.g. ``"observe"``, ``"ainvoke"``).
        role: Target object role (``"env"``, ``"agent"``, ``"llm"``, ``"loop"``).
        advice: When to run: before, after, or around.

    Example::

        Intercept("observe", role="env", advice="after")
        Intercept("ainvoke", role="llm", advice="around")
    """

    method: str
    role: str
    advice: AdviceType | str

    def __post_init__(self) -> None:
        if isinstance(self.advice, str):
            object.__setattr__(self, "advice", AdviceType(self.advice))

    @property
    def handler_name(self) -> str:
        """Conventional handler method name: ``{advice}_{method}``."""
        advice_val = self.advice.value if isinstance(self.advice, AdviceType) else self.advice
        return f"{advice_val}_{self.method}"
