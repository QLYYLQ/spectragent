"""Android's restoration evidence: what it measures between two screens, and its checks.

``AndroidCapability`` names :class:`AndroidRestorationEvidence` as its
``RestorationEvidence`` (``docs/architecture_design.md`` §4.9, §11.2
"分支回放"): ``verify_restoration`` returns one per restored observation
(``restoration.py::measure``), and the trace reader reads a recorded one back
as this type by its ``capability_id``, as it reads an action's ``Effect``. The
measures are the app and window in front, the device clocks and the clock
alignment done before the restored capture, the UI tree's elements without the
status bar, the keyboard and the input focus, and the screenshot distance with
the status bar masked; the checks are :data:`AndroidRestorationReason`, judged
by :meth:`AndroidRestorationEvidence.failed_checks` against the limits the
capability's own knobs set (``screenshot_max_distance``,
``clock_max_difference_s``), which the evidence records with what it measured.

Pydantic models only: a reader of recorded evidence imports this module
without the device side.
"""

from typing import ClassVar, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.restoration import RestorationEvidence

#: ``AndroidCapability``'s registry key: the ``capability_id`` every Android evidence carries.
ANDROID: Final = "android"

#: Android's checks, in the order they are reported: another app or window in front
#: explains every difference after it; a device clock elsewhere explains the elements
#: and pixels that show the time.
AndroidRestorationReason = Literal[
    "foreground_mismatch",
    "clock_difference",
    "ui_tree_mismatch",
    "keyboard_focus_mismatch",
    "screenshot_distance",
]

#: What pinning the device clock before a restored capture came to. ``aligned``: the
#: clock was set to the recorded capture's; ``failed``: setting it raised (``detail``
#: says what); ``unknown``: the recorded capture has no device clock to pin to.
ClockAlignmentOutcome = Literal["aligned", "failed", "unknown"]


class ClockAlignment(BaseModel):
    """What pinning the device clock did before one restored capture.

    ``target_ms`` is the recorded capture's device clock (``None``: it has
    none); the clock was set to it after the wait for a still screen, the
    screen was let settle again, and the clock was set to it once more right
    before the capture. ``detail`` says why, in words, when it was not
    aligned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ClockAlignmentOutcome
    target_ms: int | None = None
    detail: str = ""


class InputState(BaseModel):
    """The keyboard and the input focus, as one capture's UI tree shows them.

    ``keyboard_shown``: the tree has an input method's window.
    ``focused``: the node with input focus, as its class, resource id and
    bounds (``EditText #com.android.contacts:id/name at [0,300][1080,450]``),
    which tell two fields of one id apart; ``None``: no node has it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    keyboard_shown: bool
    focused: str | None = None


class AndroidRestorationEvidence(RestorationEvidence):
    """What Android measured between a recorded and a restored screen, and the limits it judges by.

    Every measure is ``None`` when Android does not measure it or could not
    this time (no UI tree in this run, a capture without its device record,
    say); a ``None`` never takes part in a check.

    - ``recorded_*`` / ``restored_*`` ``package``, ``activity``, ``window``:
      the app, its activity and the window with input focus, from each
      capture's device record; ``foreground_match`` compares all three.
    - ``recorded_clock_ms`` / ``restored_clock_ms``: each capture's device
      clock; ``clock_difference_ms`` is the restored one less the recorded
      one. ``clock_alignment`` is what pinning the clock did before the
      restored capture (``None``: nothing was asked to align).
    - ``ui_tree_match``: the multisets of the two trees' element signatures
      (class, resource id, text, description, checked, selected; never the
      index or the bounds), with the status bar's elements left out on both
      sides; ``ui_tree_missing`` / ``ui_tree_extra`` count the recorded
      elements the restored tree lacks and the reverse, and
      ``ui_tree_differences`` names the first of them.
    - ``recorded_input`` / ``restored_input``: the keyboard and the input
      focus (:class:`InputState`).
    - ``screenshot_distance``: the Hamming distance of the two screenshots'
      difference hashes with the status bar painted out of both
      (``status_bar``: the box painted, in screen pixels; ``None``: neither
      tree showed one, and the whole images were compared).

    ``screenshot_max_distance`` and ``clock_max_difference_s`` are the limits
    the checks apply: the ``AndroidCapability`` knobs of the same names when
    it measured. ``capability_id`` is always ``android``.
    """

    REASONS: ClassVar[tuple[str, ...]] = get_args(AndroidRestorationReason)

    #: Pinned to ``AndroidCapability``'s registry key: an Android evidence can
    #: name no other capability, and the trace reader finds this type by it.
    capability_id: Literal["android"] = ANDROID
    foreground_match: bool | None = None
    recorded_package: str | None = None
    restored_package: str | None = None
    recorded_activity: str | None = None
    restored_activity: str | None = None
    recorded_window: str | None = None
    restored_window: str | None = None
    recorded_clock_ms: int | None = None
    restored_clock_ms: int | None = None
    clock_difference_ms: int | None = None
    clock_max_difference_s: float = Field(ge=0)
    clock_alignment: ClockAlignment | None = None
    ui_tree_match: bool | None = None
    ui_tree_missing: int = Field(default=0, ge=0)
    ui_tree_extra: int = Field(default=0, ge=0)
    ui_tree_differences: list[str] = Field(default_factory=list)
    recorded_input: InputState | None = None
    restored_input: InputState | None = None
    screenshot_distance: int | None = Field(default=None, ge=0)
    screenshot_max_distance: int = Field(ge=0, le=64)
    status_bar: tuple[int, int, int, int] | None = None

    def failed_checks(self) -> list[str]:
        """Android's checks this measurement does not pass, in :attr:`REASONS` order.

        - ``foreground_mismatch`` — another app, activity or focused window;
        - ``clock_difference`` — the device clocks lie further apart than
          ``clock_max_difference_s``;
        - ``ui_tree_mismatch`` — the elements differ, the status bar aside;
        - ``keyboard_focus_mismatch`` — the keyboard is shown on one side
          only, or another node has input focus;
        - ``screenshot_distance`` — the screenshots, status bar painted out,
          lie further apart than ``screenshot_max_distance``.
        """
        failed: list[str] = []
        if self.foreground_match is False:
            failed.append("foreground_mismatch")
        if (
            self.clock_difference_ms is not None
            and abs(self.clock_difference_ms) > self.clock_max_difference_s * 1000
        ):
            failed.append("clock_difference")
        if self.ui_tree_match is False:
            failed.append("ui_tree_mismatch")
        if (
            self.recorded_input is not None
            and self.restored_input is not None
            and self.recorded_input != self.restored_input
        ):
            failed.append("keyboard_focus_mismatch")
        if (
            self.screenshot_distance is not None
            and self.screenshot_distance > self.screenshot_max_distance
        ):
            failed.append("screenshot_distance")
        return failed


__all__ = [
    "ANDROID",
    "AndroidRestorationEvidence",
    "AndroidRestorationReason",
    "ClockAlignment",
    "ClockAlignmentOutcome",
    "InputState",
]
