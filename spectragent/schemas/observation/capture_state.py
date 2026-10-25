"""The platform-neutral capture state: how the environment stood when it was captured.

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``". Four
facts every Agent may consume, whatever the platform, through the relative
fragment name ``capture_state`` declared **optional**: an environment that
provides one activates it, one that does not leaves the Agent running as
before. Each capability registers its own concrete fragment under its own
prefix (``browser.capture_state`` now; ``android.capture_state``,
``desktop.capture_state`` later), subclassing :class:`CaptureStateFragment`.

The four facts and what "missing" means for each:

- **settle** (:attr:`CaptureStateFragment.settle_state`): how the wait before
  the capture ended — ``settled``, ``capped`` (the cap ended it; ``pending``
  names what had not settled yet, in the capability's own labels) or
  ``unknown``, with a short ``detail`` saying why when that is known — how
  long it took, and whether the captured surface had finished its initial
  load (:attr:`SettleState.loaded`: ``False`` is the definite evidence of a
  capture taken while the surface was still loading for the first time).
  ``None`` when the capability did not wait (its wait is turned off).
- **foreground change** (:attr:`CaptureStateFragment.foreground_change`): the
  capability moved its focus before this capture because of the
  environment's own behaviour — a newly opened surface it followed, or the
  focused surface closing itself. ``None`` when nothing moved; the Agent's own
  switching action is never one.
- **pointer** (:attr:`CaptureStateFragment.pointer`): the last confirmed
  pointer position on the captured surface, in viewport units — ``known``,
  ``unknown`` (not confirmed), or ``unavailable`` (the platform has no
  pointer). Independent of whether a screenshot draws the pointer.
- **failed visible resources**
  (:attr:`CaptureStateFragment.failed_visible_resources`): how many resources
  visible in the viewport failed to load, with short identifiers.
  ``unknown`` when the measurement failed or ran out of its budget,
  ``unavailable`` when the platform cannot measure it; a count of ``0`` only
  ever means "measured, none failed".

Nothing here names a platform. A capability whose capture already records
settle and focus moves elsewhere projects them through the two properties
rather than measuring them twice; the pointer and resource facts are fields.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.schemas.observation._registrar import FragmentBase

#: How the wait before a capture ended.
SettleOutcome = Literal["settled", "capped", "unknown"]

#: Why the capability moved its focus before a capture. ``opened``: the
#: environment opened a new surface (a tab, a window, an activity) and the
#: capability followed it. ``closed``: the focused surface closed itself.
ForegroundReason = Literal["opened", "closed"]

#: Whether a pointer position is known.
PointerStatus = Literal["known", "unknown", "unavailable"]

#: Whether the failed-resource count was measured.
MeasurementStatus = Literal["measured", "unknown", "unavailable"]

#: How many failed resources are identified by name; the count is always whole.
MAX_RESOURCE_IDENTIFIERS = 8


class SettleState(BaseModel):
    """How the wait before one capture ended.

    ``loaded`` is whether the captured surface had finished its initial load
    when it was captured (``docs/architecture_design.md`` §4.4, "初次加载是否完成"),
    the one fact consumers act on without reading the capability's ``pending``
    labels:

    - ``False``: the initial load was still in progress, so the capture shows
      a surface still loading for the first time — the definite evidence of
      an incomplete load;
    - ``True``: the initial load had completed;
    - ``None``: not known (the capability could not tell).

    It is a fact about the surface, recorded at the capture, not a reading of
    how the wait ended: no ``outcome`` implies a value (a capability may stop
    waiting on a load that never completes, and then even a ``settled`` wait
    can precede a capture of a surface still loading). Required, so every
    capability says it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SettleOutcome
    waited_ms: int = Field(ge=0)
    #: Whether the captured surface had finished its initial load (class docstring).
    loaded: bool | None
    #: What had not settled when a capped wait ended, in the capability's labels.
    pending: tuple[str, ...] = ()
    #: Why the state could not be read, for an ``unknown`` outcome, when known.
    detail: str | None = None


class ForegroundChange(BaseModel):
    """The capability moved its focus before this capture because the environment did something."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: ForegroundReason
    #: The surface focused when the observation began, in the capability's identifiers.
    from_surface: str
    #: The surface this capture shows.
    to_surface: str


class PointerState(BaseModel):
    """The last confirmed pointer position, in viewport units, or why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: PointerStatus
    x: int | None = None
    y: int | None = None

    @model_validator(mode="after")
    def _position_iff_known(self) -> Self:
        has_position = self.x is not None and self.y is not None
        if (self.status == "known") != has_position or (self.x is None) != (self.y is None):
            raise ValueError("a pointer has both coordinates exactly when its status is known")
        return self


class FailedVisibleResources(BaseModel):
    """Resources visible in the viewport that failed to load, when that was measured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: MeasurementStatus
    #: How many failed; ``None`` unless measured. ``0`` means measured and none failed.
    count: int | None = Field(default=None, ge=0)
    #: Short identifiers of the first :data:`MAX_RESOURCE_IDENTIFIERS` that failed.
    identifiers: tuple[str, ...] = ()
    #: Why the measurement is unknown or unavailable, when that is known.
    detail: str | None = None

    @model_validator(mode="after")
    def _count_iff_measured(self) -> Self:
        if (self.status == "measured") != (self.count is not None):
            raise ValueError("a failed-resource count is present exactly when it was measured")
        if self.status != "measured" and self.identifiers:
            raise ValueError("an unmeasured failed-resource fact names no resources")
        if self.count is not None and len(self.identifiers) > min(
            self.count, MAX_RESOURCE_IDENTIFIERS
        ):
            raise ValueError("more resource identifiers than failed resources")
        return self


class CaptureStateFragment(FragmentBase):
    """Abstract base of every capability's ``*.capture_state`` fragment.

    A concrete subclass registers under its capability's prefix and supplies
    :attr:`settle_state` and :attr:`foreground_change`, by its own fields or
    by projecting records its capture already keeps.
    """

    __abstract__ = True

    pointer: PointerState
    failed_visible_resources: FailedVisibleResources

    @property
    def settle_state(self) -> SettleState | None:
        """How the wait before this capture ended; ``None`` when there was no wait."""
        raise NotImplementedError(f"{type(self).__name__} must provide settle_state")

    @property
    def foreground_change(self) -> ForegroundChange | None:
        """The focus move the environment caused before this capture; ``None`` when none."""
        raise NotImplementedError(f"{type(self).__name__} must provide foreground_change")

    def summary(self) -> str:
        """``settled 640ms; pointer (12, 40); 1 failed visible resource`` — for the step summary."""
        parts: list[str] = []
        settle = self.settle_state
        if settle is not None:
            parts.append(f"{settle.outcome} {settle.waited_ms}ms")
        if self.foreground_change is not None:
            parts.append(f"foreground {self.foreground_change.reason}")
        pointer = self.pointer
        parts.append(
            f"pointer ({pointer.x}, {pointer.y})"
            if pointer.status == "known"
            else f"pointer {pointer.status}"
        )
        failed = self.failed_visible_resources
        if failed.status == "measured":
            noun = "resource" if failed.count == 1 else "resources"
            parts.append(f"{failed.count} failed visible {noun}")
        else:
            parts.append(f"failed visible resources {failed.status}")
        return "; ".join(parts)


__all__ = [
    "MAX_RESOURCE_IDENTIFIERS",
    "CaptureStateFragment",
    "FailedVisibleResources",
    "ForegroundChange",
    "ForegroundReason",
    "MeasurementStatus",
    "PointerState",
    "PointerStatus",
    "SettleOutcome",
    "SettleState",
]
