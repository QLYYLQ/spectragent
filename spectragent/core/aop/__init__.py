"""AOP (Aspect-Oriented Programming) engine — M04.

True method-level interception: Hooks declare which Protocol methods to
intercept via ``__intercepts__``, the Weaver wraps target objects at runtime.
Loop code has zero hook awareness.

Two-phase design:
- Discovery at definition time: conscribe metaclass registers Hooks + intercepts
- Weaving at runtime: Weaver wraps methods per YAML config

See docs/architecture_design.md §4.6 and docs/execution_dag.md M04.
"""

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.errors import (
    MissingHandlerError,
    UnclaimedRoleError,
    UnsupportedAdviceError,
)
from spectragent.core.aop.weaver import Weaver

__all__ = [
    "AdviceType",
    "Intercept",
    "MissingHandlerError",
    "UnclaimedRoleError",
    "UnsupportedAdviceError",
    "Weaver",
]
