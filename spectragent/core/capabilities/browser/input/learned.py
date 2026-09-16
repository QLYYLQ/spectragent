"""LearnedTrajectory — cursor paths sampled from a model trained on human mouse data.

``weights_dir`` names an export directory (``generator.npz`` + ``meta.json``)
written by mouse-gan-torch. Which model it holds is read from that
``meta.json`` — its ``format`` and ``kind`` — never from YAML, so a config
cannot disagree with its weights (:mod:`._learned_export`):

- ``mouse-gan-generator/1`` — the faithful port of mouse-ai's conditional LSTM
  GAN. It has no stop signal.
- ``mouse-gan-generator/2``, kind ``mdn`` — autoregressive MDN-RNN over integer
  pixel moves with a stop head; the chosen v2 model (held-out realism 0.133
  against a real-vs-real floor of 0.12).
- ``mouse-gan-generator/2``, kind ``gan_ar`` — a length head, then an
  autoregressive GAN.

Any other format or kind fails at construction, naming it. Inference is the
vendored numpy code of :mod:`._mouse_gan_v1` / :mod:`._mouse_gan_v2`; numpy
comes from the optional ``learned-mouse`` extra and is imported when a
``LearnedTrajectory`` is constructed, never at module import, so registration
and config generation work without it.

Every model maps a condition ``(offset_x, offset_y, btn_w, btn_h)``, normalised
by the 800 px training window, to per-tick deltas at the recorder's tick rate.
Turning that into waypoints:

1. **Candidates.** ``num_candidates`` paths are sampled in one batch, and each
   is cut where the click happens.

   *Exports that model stopping* (format /2) end each path where the model
   does. A training path runs up to and including its click tick, so a sampled
   length already holds the approach, any corrective submovement and the pause
   inside the target before the click; nothing is added or trimmed. The first
   candidate in batch order whose end lands inside the target wins, else the
   one ending closest. Where the end comes from a per-tick stop head (``mdn``),
   a candidate that used all ``max_seq_len`` ticks is one whose stop head never
   fired — about 0.2 % of the exported model's samples, and those paths wander
   (around 50 idle ticks, reaching the target at tick 86-99 if at all) — so it
   is considered only when no candidate stopped. ``gan_ar`` draws its length up
   front from a head capped at the horizon, so there a full-length path is an
   ordinary draw and competes with the rest.

   Arrival truncation is not applied to these exports, not even as a fallback.
   Its sampled dwell would count the pre-click pause twice and replace the
   model's timing with a fitted constant, and it cannot repair the one real
   failure, a path that does not stop, whose arrival — if any — comes at the
   end of the horizon anyway. A stopped path that ends outside the target
   (about 8 % of ``mdn`` samples; 6 % of the human training paths do too) is
   what the endpoint correction below absorbs.

   *Exports without a stop signal* (format /1) are cut by the strategy:

   - *Arrival truncation* (``truncate_at_arrival``, on by default). The model
     does not stop on its own — every path of the trained generator runs all
     ``max_seq_len`` ticks (2 s at 50 Hz, against a human median of 26), with
     its speed peak at 6-9 % of the path instead of ~35 %. So a click is placed
     the way mouse-ai's own ``inference.py`` places it: ``k`` dwell ticks after
     the cursor is inside the target, with ``k`` drawn once per move from the
     human "ticks inside the button before the click" fit
     (:func:`sample_dwell_ticks`). "Inside" is the element box when the caller
     passes one, else within ``arrival_radius_px`` of the target point. A
     candidate that stays inside for its ``k`` dwell ticks (mouse-ai's click
     rule — leaving resets the count) is preferred and is cut at that click
     tick; otherwise a candidate that merely arrives is cut ``k`` ticks after
     its first inside tick, whatever it does in between.
   - *Fallback* — no candidate arrives, or truncation is off: each candidate
     is trimmed to its effective end (last tick moving ``>= stop_px``); the
     first whose trimmed end lands inside the target box wins, else the one
     ending closest to the target.

   Within a tier the *first* candidate in batch order wins, not the closest,
   which keeps the human spread of landing points instead of always picking a
   dead-centre path. Without an element box, "inside the target" for choosing
   between ends means inside the button the model was conditioned on.
2. **Exact endpoint.** The residual ``target - end`` is added along the path
   with weight ``w(u) = smoothstep(u ** correction_power)``, where ``u`` is
   progress through the ticks **that move** (``k / n`` when they all do).
   ``w(1) == 1`` makes the last point the target exactly; ``w'(0) == w'(1) == 0``
   leaves the model's launch and final deceleration untouched, and
   ``correction_power > 1`` pushes the correction into the late, corrective
   part of the movement. ``u`` holding still while the model does is what keeps
   a still tick still: it takes the same shift as the tick before it, so the
   settle inside the target before the click — and any mid-path pause — stays
   exactly as sampled instead of being smeared into 1 px micro-moves. The
   endpoint is never clamped (``HumanMouse`` clamps only intermediate points),
   so off-viewport targets stay exact.
3. **Timing.** One waypoint per tick, ``dt_ms = 1000 / hz`` (optionally
   jittered), replayed against absolute deadlines so the path takes the time
   the model asked for. A tick that holds the previous pixel stays in the list
   as a repeated waypoint: ``HumanMouse`` waits it out without dispatching an
   event, so still ticks — including the pause resting on the target — keep
   their time. ``hz`` and the training window are the export's, not the
   config's: both come from ``meta.json`` when it declares them, and a
   ``mouse_trajectory`` block that names a different value is rejected at
   construction rather than silently replaying the model at the wrong rate or
   scale.

**Dwell vs. HumanMouse.** ``HumanMouse`` has a ``dwell_ms`` pre-click pause,
but it defaults to 0 and nothing in the package sets it, so the pause inside
the path is the only one before the click and nothing is counted twice. It is
part of the path, so it also precedes a drag release or a type/select after
the cursor approach, where a short settle is just as human. Setting
``dwell_ms`` on top would add a second, constant pause.

**Long moves.** The training offsets stay inside the 800 px window
(``|offset|`` p99 = 0.95, max 1.09), while a 1280x720 viewport diagonal is
1.84. An offset longer than ``max_offset`` is conditioned on the same direction
shrunk to ``max_offset`` and the generated path is stretched back by the same
factor: the tick count stays that of an in-range move and the speed scales with
distance. That is the isochrony of human aiming movements, and the training
data shows it (median effective ticks 22 at 50-200 px vs 30 at 600-800 px,
while median peak speed nearly doubles), which is why this was chosen over
chaining sub-segments — a chain puts a full stop at every joint.

**Button size.** Every training path targeted the same 109x34 px button, so
any other size in the condition is out of distribution. ``button_size:
training`` (default) always conditions on that constant; ``target`` passes the
real element box when the caller knows it — only useful with a model trained
on varied sizes.

**Knobs of exports without a stop signal.** ``truncate_at_arrival``,
``arrival_radius_px`` and ``stop_px`` only say how a path that never stops is
cut. Left unset they take the defaults above; set on an export that models
stopping, construction fails naming them. They would have no effect there, and
a config asking to truncate a model that stops on its own disagrees with its
weights — the same reason a populated config block with no receptor, or a
flag that could veto the configured strategy, is an error in af rather than a
silent no-op.

**Cost.** Generation runs synchronously inside ``HumanMouse.move_to`` on the
event loop, once per move, before the first tick. Measured at the default 8
candidates on an M-series laptop: format /1 about 36 ms per move (BiLSTMs over
all 100 ticks); ``mdn`` and ``gan_ar`` about 9 ms median and under 25 ms at
worst, because an ``mdn`` rollout ends once every candidate has stopped and a
``gan_ar`` one at the longest sampled length. Next to a headless Chrome sharing
the CPU that became 45-60 ms and 14-30 ms. Weights load once, on the first
move.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field as PydanticField

from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    TrajectoryPoint,
)

if TYPE_CHECKING:
    import numpy as np

    from spectragent.core.capabilities.browser.input._learned_export import Export, Sampler

MISSING_NUMPY_MESSAGE = (
    "The 'learned' mouse trajectory needs numpy, which spectragent does not install "
    "by default. Install the optional extra: `uv sync --extra learned-mouse` "
    "(or `pip install 'spectragent[learned-mouse]'`)."
)

#: The single button size in the released training data (px).
TRAINING_BUTTON_PX: tuple[float, float] = (109.0, 34.0)
#: Tick rate of the recorder that produced the training data.
DEFAULT_HZ = 50.0
#: Training window the released data was normalised by (px).
DEFAULT_PX_PER_UNIT = 800.0
#: Defaults of the knobs that only apply to exports without a stop signal.
DEFAULT_TRUNCATE_AT_ARRIVAL = True
DEFAULT_ARRIVAL_RADIUS_PX = 12.0
DEFAULT_STOP_PX = 0.5

#: ``scipy.stats.gamma.fit`` of the human "ticks inside the button until the
#: click" counts (outliers above 24 ticks removed), from mouse-ai
#: ``src/ticks_inside_btn_until_click.py``: mean ~7.5 ticks, ~150 ms at 50 Hz.
DWELL_GAMMA_SHAPE = 10.53369
DWELL_GAMMA_SCALE = 0.80039
DWELL_GAMMA_LOC = -0.93502


def _declared_px_per_unit(export: Export) -> float | None:
    """The training window ``meta.json`` records, or None if it records none."""
    window = export.meta.get("norm_reference_window")
    if window is None:
        return None
    try:
        width, height = (float(side) for side in window)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{export.meta_path} declares norm_reference_window {window!r}; the learned "
            "trajectory reads a [width, height] pair of pixel sizes"
        ) from exc
    if width != height:
        raise ValueError(
            f"{export.meta_path} declares a {width}x{height} training window; the learned "
            "trajectory scales both axes by one px_per_unit and cannot replay a model "
            "normalised per axis"
        )
    return width


def _from_meta(
    export: Export,
    name: str,
    configured: float | None,
    declared: float | None,
    default: float,
    *,
    meta_key: str | None = None,
) -> float:
    """The export's value for ``name``, with a config that names another rejected.

    ``meta.json`` records what the weights were made with, so it decides: a
    configured value that agrees is redundant, and one that disagrees is the
    error it looks like rather than a silent reinterpretation of the model —
    replaying a 50 Hz model at 60 Hz, or unnormalising it by the wrong window,
    changes exactly the speed the model was chosen for. Config only decides
    when the meta is silent.
    """
    if declared is None:
        return default if configured is None else configured
    if configured is not None and configured != declared:
        raise ValueError(
            f"{name}={configured} contradicts {export.meta_path}, which records "
            f"{meta_key or name} {declared} for {export.label}; remove it from the "
            "mouse_trajectory block, or correct the export's meta if the value it "
            "declares is wrong"
        )
    return declared


def _require_numpy() -> None:
    """The one place the missing extra is named."""
    try:
        import numpy  # noqa: F401
    except ImportError as exc:
        raise ImportError(MISSING_NUMPY_MESSAGE) from exc


def sample_dwell_ticks(rng: np.random.Generator) -> int:
    """Ticks spent inside the target before clicking, drawn from the human fit.

    Same transform as mouse-ai's ``random_ticks_inside_btn``:
    ``max(0, round(gamma(shape, scale) + loc))`` (Python's ``round``).
    """
    draw = float(rng.gamma(DWELL_GAMMA_SHAPE, DWELL_GAMMA_SCALE)) + DWELL_GAMMA_LOC
    return max(0, round(draw))


def effective_ticks(moves_px: np.ndarray, stop_px: float) -> np.ndarray:
    """Per path, the tick count up to the last tick moving ``>= stop_px`` pixels.

    ``moves_px`` is ``(B, T, 2)`` in pixels; returns ``(B,)`` ints (0 = never moves).
    """
    import numpy as np

    moving = np.linalg.norm(moves_px, axis=-1) >= stop_px  # (B, T)
    steps = moving.shape[1]
    last = steps - np.argmax(moving[:, ::-1], axis=1)  # index of last True, +1
    return np.where(moving.any(axis=1), last, 0)


def _smoothstep(u: np.ndarray) -> np.ndarray:
    return u * u * (3.0 - 2.0 * u)


def _first_true(mask: np.ndarray) -> np.ndarray:
    """Per row of a ``(B, T)`` bool array, the first True index, or -1."""
    import numpy as np

    return np.where(mask.any(axis=1), np.argmax(mask, axis=1), -1)


class LearnedTrajectory(MouseTrajectoryBase):
    """Human-like path sampled from an exported learned model (numpy inference)."""

    __registry_key__ = "learned"

    def __init__(
        self,
        weights_dir: Annotated[
            str,
            PydanticField(
                description=(
                    "Export directory holding generator.npz and meta.json, as "
                    "written by mouse-gan-torch (`mouse_gan.export` or "
                    "`mouse_gan.v2.export_v2`). The model — format /1 GAN, /2 "
                    "`mdn` or /2 `gan_ar` — is read from meta.json. `~` is "
                    "expanded; a relative path is resolved against the working "
                    "directory. Weights load lazily on the first move."
                ),
            ),
        ],
        seed: Annotated[
            int | None,
            PydanticField(
                description=(
                    "Seed for this instance's numpy Generator (model noise, "
                    "arrival dwell and timing jitter). None draws fresh entropy."
                ),
            ),
        ] = None,
        hz: Annotated[
            float | None,
            PydanticField(
                gt=0,
                description=(
                    "Tick rate the model's per-tick deltas are replayed at. "
                    "The export fixes it: None takes `hz` from meta.json, and a "
                    "value that contradicts the meta is rejected (only an "
                    "export whose meta declares no rate takes this one, else "
                    "50 — the rate the released training data was recorded at)."
                ),
            ),
        ] = None,
        px_per_unit: Annotated[
            float | None,
            PydanticField(
                gt=0,
                description=(
                    "Pixels per model unit — the training window size, kept in "
                    "CSS pixels and never normalised by the viewport so speeds "
                    "stay human. The export fixes it too: None takes "
                    "`norm_reference_window` from meta.json, a contradicting "
                    "value is rejected, and an export that declares no window "
                    "takes this one, else 800."
                ),
            ),
        ] = None,
        button_size: Annotated[
            Literal["training", "target"],
            PydanticField(
                description=(
                    "Button size fed to the model. `training` always uses the "
                    "109x34 px button every training path targeted (other sizes "
                    "are out of distribution); `target` uses the element box "
                    "when the caller provides one, falling back to the training "
                    "size."
                ),
            ),
        ] = "training",
        num_candidates: Annotated[
            int,
            PydanticField(
                ge=1,
                description=(
                    "Paths sampled per move in one batch. The first whose end "
                    "lands inside the target wins (for a model that stops on its "
                    "own, among the candidates that stopped), else the closest."
                ),
            ),
        ] = 8,
        max_offset: Annotated[
            float,
            PydanticField(
                gt=0,
                description=(
                    "Longest start-to-target distance, in model units, the "
                    "model is conditioned on (training p99 is 0.95). A longer "
                    "move is generated at this length and stretched: same "
                    "duration, proportionally faster."
                ),
            ),
        ] = 0.95,
        min_distance_px: Annotated[
            float,
            PydanticField(
                ge=0,
                description=(
                    "Moves shorter than this skip the model and take one tick "
                    "(below 1 px: a single waypoint, no sleep)."
                ),
            ),
        ] = 3.0,
        correction_power: Annotated[
            float,
            PydanticField(
                gt=0,
                description=(
                    "Shape of the endpoint correction weight "
                    "smoothstep(u ** p). 1 spreads it symmetrically; larger "
                    "values move it later in the path."
                ),
            ),
        ] = 1.5,
        jitter: Annotated[
            float,
            PydanticField(
                ge=0,
                lt=1,
                description=(
                    "Relative per-tick timing jitter, uniform in "
                    "[-jitter, +jitter] (same convention as windmouse/bezier). "
                    "0 replays the model's fixed tick exactly."
                ),
            ),
        ] = 0.0,
        truncate_at_arrival: Annotated[
            bool | None,
            PydanticField(
                description=(
                    "Exports without a stop signal (format /1) only; an error "
                    "on one that stops. Cut each path where a human would "
                    "click: a sampled number of dwell ticks (mouse-ai's human "
                    "fit, mean ~7.5) after the cursor is inside the target; "
                    "paths that never arrive fall back to trimming at the last "
                    "moving tick. None means on."
                ),
            ),
        ] = None,
        arrival_radius_px: Annotated[
            float | None,
            PydanticField(
                gt=0,
                description=(
                    "Exports without a stop signal (format /1) only; an error "
                    "on one that stops. With no element box (a raw-coordinate "
                    "click) the cursor has arrived once within this many pixels "
                    "of the target point. None means 12, about half a small "
                    "control."
                ),
            ),
        ] = None,
        stop_px: Annotated[
            float | None,
            PydanticField(
                ge=0,
                description=(
                    "Exports without a stop signal (format /1) only; an error "
                    "on one that stops. Fallback end of a path that never "
                    "arrives (or with truncation off): its last tick moving at "
                    "least this many pixels. None means 0.5."
                ),
            ),
        ] = None,
    ) -> None:
        # Fail at construction — before a browser launches — rather than at
        # the first click: missing numpy (named once, here), a wrong
        # directory, an unknown format or kind, missing arrays, and knobs the
        # export's model cannot use. Only the array data loads lazily.
        _require_numpy()
        import numpy as np

        from spectragent.core.capabilities.browser.input._learned_export import open_export

        self._export = open_export(weights_dir)
        if self._export.models_stop:
            set_knobs = [
                name
                for name, value in (
                    ("truncate_at_arrival", truncate_at_arrival),
                    ("arrival_radius_px", arrival_radius_px),
                    ("stop_px", stop_px),
                )
                if value is not None
            ]
            if set_knobs:
                one = len(set_knobs) == 1
                raise ValueError(
                    f"{' and '.join(set_knobs)} only appl{'ies' if one else 'y'} to "
                    f"exports without a stop signal, but {self._export.directory} holds "
                    f"{self._export.label}, whose paths end where the model stops; "
                    f"remove {'it' if one else 'them'} from the mouse_trajectory block"
                )

        declared_hz = self._export.meta.get("hz")
        resolved_hz = _from_meta(
            self._export,
            "hz",
            hz,
            None if declared_hz is None else float(declared_hz),
            DEFAULT_HZ,
        )
        if resolved_hz <= 0:
            raise ValueError(f"hz must be > 0, got {resolved_hz}")
        if num_candidates < 1:
            raise ValueError(f"num_candidates must be >= 1, got {num_candidates}")

        self._hz = resolved_hz
        self._px_per_unit = _from_meta(
            self._export,
            "px_per_unit",
            px_per_unit,
            _declared_px_per_unit(self._export),
            DEFAULT_PX_PER_UNIT,
            meta_key="norm_reference_window",
        )
        self._button_size = button_size
        self._num_candidates = num_candidates
        self._max_offset = max_offset
        self._min_distance_px = min_distance_px
        self._correction_power = correction_power
        self._jitter = jitter
        self._truncate_at_arrival = (
            DEFAULT_TRUNCATE_AT_ARRIVAL if truncate_at_arrival is None else truncate_at_arrival
        )
        self._arrival_radius_px = (
            DEFAULT_ARRIVAL_RADIUS_PX if arrival_radius_px is None else arrival_radius_px
        )
        self._stop_px = DEFAULT_STOP_PX if stop_px is None else stop_px
        # Instance-owned, never the global numpy RNG.
        self._rng = np.random.default_rng(seed)
        self._sampler: Sampler | None = None

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def hz(self) -> float:
        return self._hz

    @property
    def tick_ms(self) -> float:
        return 1000.0 / self._hz

    @property
    def model(self) -> str:
        """The model named by the export's meta.json: ``mouse-gan-generator/2 kind 'mdn'``."""
        return self._export.label

    # ── protocol ──────────────────────────────────────────────────────────
    def generate(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
    ) -> list[TrajectoryPoint]:
        import numpy as np

        ex, ey = round(end_x), round(end_y)
        dx, dy = float(end_x) - float(start_x), float(end_y) - float(start_y)
        distance = math.hypot(dx, dy)
        if distance < 1.0:
            return [TrajectoryPoint(ex, ey, 0.0)]

        start = (round(start_x), round(start_y))
        if distance < self._min_distance_px:
            return self._timed([start, (ex, ey)])

        offsets_px = self._sample_path(dx, dy, target_w, target_h)  # (n, 2), last == (dx, dy)
        absolute = np.rint(offsets_px + np.array([start_x, start_y], dtype=np.float64))
        pixels = [start, *((int(x), int(y)) for x, y in absolute[:-1]), (ex, ey)]
        return self._timed(pixels)

    # ── internals ─────────────────────────────────────────────────────────
    def _load(self) -> Sampler:
        if self._sampler is None:
            self._sampler = self._export.load()
        return self._sampler

    def _sample_path(self, dx: float, dy: float, target_w: float, target_h: float) -> np.ndarray:
        """Best candidate path as cumulative px offsets from the start, endpoint-corrected."""
        import numpy as np

        sampler = self._load()
        unit = self._px_per_unit
        target = np.array([dx, dy], dtype=np.float64)

        # Long move: condition on the in-range length, stretch the output back.
        scale = max(1.0, math.hypot(dx, dy) / unit / self._max_offset)
        known_box = target_w > 0 and target_h > 0
        if self._button_size == "target" and known_box:
            btn_w, btn_h = target_w / scale, target_h / scale
        else:
            btn_w, btn_h = TRAINING_BUTTON_PX
        cond_row = [dx / unit / scale, dy / unit / scale, btn_w / unit, btn_h / unit]

        cond = np.tile(np.asarray(cond_row, dtype=np.float32), (self._num_candidates, 1))
        draw = sampler.draw(cond, self._rng)
        moves = draw.moves.astype(np.float64) * (unit * scale)  # (k, T, 2) px

        finite = np.isfinite(moves).all(axis=(1, 2))
        if not finite.any():
            raise ValueError(
                f"learned trajectory model {self._export.label} at "
                f"{self._export.directory} produced non-finite output for cond {cond_row}"
            )

        cumulative = np.cumsum(moves, axis=1)  # (k, T, 2) positions after each tick
        known_half = np.array([target_w, target_h]) / 2.0 if known_box else None
        # Box an end must land in when choosing between ends: the element's,
        # else the button the model was conditioned on, stretched with the path.
        end_half = known_half if known_half is not None else np.array([btn_w, btn_h]) * scale / 2.0

        if draw.lengths is not None:
            best, n = self._model_cut(cumulative, draw.lengths, target, end_half, finite)
        else:
            chosen = self._arrival_cut(cumulative, target, known_half, finite)
            if chosen is None:
                chosen = self._trimmed_cut(moves, cumulative, target, end_half, finite)
            best, n = chosen

        return self._corrected(cumulative[best, :n], moves[best, :n], target)

    def _corrected(self, path: np.ndarray, moves: np.ndarray, target: np.ndarray) -> np.ndarray:
        """``path`` shifted onto the target along its moving ticks.

        The residual ``target - end`` is spread with
        ``w(u) = smoothstep(u ** correction_power)``, where ``u`` is progress
        through the ticks **that move**: it stands still while the model does,
        so a still tick takes exactly the same shift as the tick before it and
        therefore stays exactly as still. Spreading over every sampled tick
        instead (``u = k / n``) gave consecutive still ticks different weights
        and smeared 1 px of correction onto each — turning the model's settle
        inside the target, which the /2 training paths record right up to the
        click, into micro-moves, and a mid-path pause into a drift.

        Over a path whose every tick moves — every format /1 path, and any /2
        path without a pause — ``u`` is ``k / n`` again, so this is the same
        correction that was tuned there: ``w(1) == 1`` puts the end exactly on
        the target, ``w'(0) == w'(1) == 0`` leaves launch and final
        deceleration alone. A path that never moves at all has no stillness to
        protect and falls back to ``k / n``.
        """
        import numpy as np

        n = len(path)
        moved = np.cumsum(np.any(moves != 0.0, axis=1))  # moving ticks so far
        total = int(moved[-1])
        # No moving tick at all: nothing to protect, progress is k / n.
        progress = moved.astype(np.float64) if total else np.arange(1, n + 1, dtype=np.float64)
        weight = _smoothstep((progress / (total or n)) ** self._correction_power)
        path = path + weight[:, None] * (target - path[-1])
        path[-1] = target
        return path

    def _model_cut(
        self,
        cumulative: np.ndarray,
        lengths: np.ndarray,
        target: np.ndarray,
        end_half: np.ndarray,
        finite: np.ndarray,
    ) -> tuple[int, int]:
        """``(candidate, ticks)`` ending where the model stopped (exports that model stopping)."""
        import numpy as np

        k, steps = cumulative.shape[0], cumulative.shape[1]
        lengths = np.clip(np.asarray(lengths, dtype=np.int64), 1, steps)
        ends = cumulative[np.arange(k), lengths - 1]  # (k, 2)
        eligible = finite
        if self._export.stop_is_per_tick:
            # This model ends a path by firing a stop head at some tick, so one
            # that used the whole horizon never stopped (and those wander);
            # only fall back to them when no candidate stopped. A model that
            # draws its length up front has no such failure — a draw equal to
            # the horizon is the length head's capped draw, not a runaway.
            stopped = finite & (lengths < steps)
            if stopped.any():
                eligible = stopped
        inside = eligible & np.all(np.abs(ends - target) <= end_half, axis=1)
        if inside.any():
            best = int(np.argmax(inside))
        else:
            errors = np.where(eligible, np.linalg.norm(ends - target, axis=1), np.inf)
            best = int(np.argmin(errors))
        return best, int(lengths[best])

    def _arrival_cut(
        self,
        cumulative: np.ndarray,
        target: np.ndarray,
        known_half: np.ndarray | None,
        finite: np.ndarray,
    ) -> tuple[int, int] | None:
        """``(candidate, ticks)`` cut at the click after arrival, or None if none arrives."""
        import numpy as np

        if not self._truncate_at_arrival:
            return None
        rel = cumulative - target
        if known_half is not None:
            inside = np.all(np.abs(rel) <= known_half, axis=-1)
        else:
            inside = np.linalg.norm(rel, axis=-1) <= self._arrival_radius_px
        inside &= finite[:, None]  # (k, T)
        steps = inside.shape[1]
        dwell = sample_dwell_ticks(self._rng)

        # mouse-ai's click rule: dwell + 1 consecutive inside ticks.
        window = dwell + 1
        if window <= steps:
            runs = np.cumsum(inside, axis=1, dtype=np.int64)
            runs[:, window:] -= runs[:, :-window].copy()
            settled = _first_true(runs[:, window - 1 :] == window)
            if (settled >= 0).any():
                best = int(np.argmax(settled >= 0))
                return best, int(settled[best]) + window  # click tick index + 1

        arrived = _first_true(inside)
        if (arrived >= 0).any():
            best = int(np.argmax(arrived >= 0))
            return best, min(int(arrived[best]) + 1 + dwell, steps)
        return None

    def _trimmed_cut(
        self,
        moves: np.ndarray,
        cumulative: np.ndarray,
        target: np.ndarray,
        end_half: np.ndarray,
        finite: np.ndarray,
    ) -> tuple[int, int]:
        """``(candidate, ticks)`` trimmed at the last moving tick (no-arrival fallback)."""
        import numpy as np

        k = moves.shape[0]
        lengths = np.maximum(effective_ticks(moves, self._stop_px), 1)
        ends = cumulative[np.arange(k), lengths - 1]  # (k, 2)
        inside = finite & np.all(np.abs(ends - target) <= end_half, axis=1)
        if inside.any():
            best = int(np.argmax(inside))
        else:
            errors = np.where(finite, np.linalg.norm(ends - target, axis=1), np.inf)
            best = int(np.argmin(errors))
        return best, int(lengths[best])

    def _timed(self, pixels: list[tuple[int, int]]) -> list[TrajectoryPoint]:
        """One waypoint per tick after the first; still ticks stay as repeats."""
        ticks = len(pixels) - 1
        dts = [self.tick_ms] * ticks
        if self._jitter > 0.0 and ticks:
            factors = self._rng.uniform(-self._jitter, self._jitter, size=ticks)
            dts = [dt * (1.0 + float(f)) for dt, f in zip(dts, factors, strict=True)]
        x0, y0 = pixels[0]
        return [
            TrajectoryPoint(x0, y0, 0.0),
            *(TrajectoryPoint(x, y, dt) for (x, y), dt in zip(pixels[1:], dts, strict=True)),
        ]
