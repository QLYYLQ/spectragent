"""GanTrajectory — cursor paths sampled from a GAN trained on human mouse data.

The generator is the conditional LSTM GAN of SaluRamos/mouse-ai, trained by the
PyTorch port ``mouse-gan-torch`` and exported to ``generator.npz`` +
``meta.json``. Inference is the numpy-only forward pass vendored in
:mod:`._gan_numpy`; numpy comes from the optional ``mouse-gan`` extra and is
imported when a ``GanTrajectory`` is constructed, never at module import, so
registration and config generation work without it.

The model emits ``max_seq_len`` per-tick deltas at the recorder's tick rate
for a condition ``(offset_x, offset_y, btn_w, btn_h)`` normalised by the 800 px
training window. Turning that into waypoints:

1. **Candidates.** ``num_candidates`` noise vectors go through one batched
   forward pass, and each candidate is cut where the click would happen:

   - *Arrival truncation* (``truncate_at_arrival``, default on). The model
     does not stop on its own — every path of the trained generator runs all
     ``max_seq_len`` ticks (2 s at 50 Hz, against a human median of 26), with
     its speed peak at 6-9 % of the path instead of ~35 %. So a click is placed
     the way mouse-ai's own ``inference.py`` places it: ``k`` dwell ticks
     after the cursor is inside the target, with ``k`` drawn once per move
     from the human "ticks inside the button before the click" fit
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
   dead-centre path.
2. **Exact endpoint.** The residual ``target - end`` is added along the path
   with weight ``w(u) = smoothstep(u ** correction_power)``, ``u = k / n``.
   ``w(1) == 1`` makes the last point the target exactly; ``w'(0) == w'(1) == 0``
   leaves the model's launch and final deceleration untouched, and
   ``correction_power > 1`` pushes the correction into the late, corrective
   part of the movement. The endpoint is never clamped (``HumanMouse`` clamps
   only intermediate points), so off-viewport targets stay exact.
3. **Timing.** One waypoint per tick, ``dt_ms = 1000 / hz`` (optionally
   jittered). A tick that rounds to the previous pixel stays in the list as a
   repeated waypoint: ``HumanMouse`` sleeps through it without dispatching an
   event, so still ticks — including dwell ticks resting on the target — keep
   their time.

**Dwell vs. HumanMouse.** ``HumanMouse`` has a ``dwell_ms`` pre-click pause,
but it defaults to 0 and nothing in the package sets it, so the arrival dwell
is the only pause before the click and nothing is counted twice. The dwell
ticks are part of the path, so they also precede a drag release or a
type/select after the cursor approach, where a short settle is just as human.
Setting ``dwell_ms`` on top would add a second, constant pause.

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
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field as PydanticField

from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    TrajectoryPoint,
)

if TYPE_CHECKING:
    import numpy as np

    from spectragent.core.capabilities.browser.input._gan_numpy import NumpyGenerator

#: The single button size in the released training data (px).
TRAINING_BUTTON_PX: tuple[float, float] = (109.0, 34.0)
#: Tick rate of the recorder that produced the training data.
DEFAULT_HZ = 50.0

#: ``scipy.stats.gamma.fit`` of the human "ticks inside the button until the
#: click" counts (outliers above 24 ticks removed), from mouse-ai
#: ``src/ticks_inside_btn_until_click.py``: mean ~7.5 ticks, ~150 ms at 50 Hz.
DWELL_GAMMA_SHAPE = 10.53369
DWELL_GAMMA_SCALE = 0.80039
DWELL_GAMMA_LOC = -0.93502


def sample_dwell_ticks(rng: np.random.Generator) -> int:
    """Ticks spent inside the target before clicking, drawn from the human fit.

    Same transform as mouse-ai's ``random_ticks_inside_btn``:
    ``max(0, round(gamma(shape, scale) + loc))`` (Python's ``round``).
    """
    draw = float(rng.gamma(DWELL_GAMMA_SHAPE, DWELL_GAMMA_SCALE)) + DWELL_GAMMA_LOC
    return max(0, round(draw))


def _smoothstep(u: np.ndarray) -> np.ndarray:
    return u * u * (3.0 - 2.0 * u)


def _first_true(mask: np.ndarray) -> np.ndarray:
    """Per row of a ``(B, T)`` bool array, the first True index, or -1."""
    import numpy as np

    return np.where(mask.any(axis=1), np.argmax(mask, axis=1), -1)


class GanTrajectory(MouseTrajectoryBase):
    """Human-like path sampled from an exported GAN generator (numpy inference)."""

    __registry_key__ = "gan"

    def __init__(
        self,
        weights_dir: Annotated[
            str,
            PydanticField(
                description=(
                    "Export directory holding generator.npz (and normally "
                    "meta.json), as written by mouse-gan-torch's "
                    "`mouse_gan.export`. `~` is expanded; a relative path is "
                    "resolved against the working directory. Weights load "
                    "lazily on the first move."
                ),
            ),
        ],
        seed: Annotated[
            int | None,
            PydanticField(
                description=(
                    "Seed for this instance's numpy Generator (noise and timing "
                    "jitter). None draws fresh entropy."
                ),
            ),
        ] = None,
        hz: Annotated[
            float | None,
            PydanticField(
                description=(
                    "Tick rate the model's per-tick deltas are replayed at. "
                    "None takes `hz` from meta.json, else 50 (the rate the "
                    "released training data was recorded at)."
                ),
            ),
        ] = None,
        px_per_unit: Annotated[
            float,
            PydanticField(
                gt=0,
                description=(
                    "Pixels per model unit — the training window size. Kept in "
                    "CSS pixels, never normalised by the viewport, so speeds "
                    "stay human."
                ),
            ),
        ] = 800.0,
        button_size: Annotated[
            Literal["training", "target"],
            PydanticField(
                description=(
                    "Button size fed to the generator. `training` always uses "
                    "the 109x34 px button every training path targeted (other "
                    "sizes are out of distribution); `target` uses the element "
                    "box when the caller provides one, falling back to the "
                    "training size."
                ),
            ),
        ] = "training",
        num_candidates: Annotated[
            int,
            PydanticField(
                ge=1,
                description=(
                    "Paths sampled per move in one batch. With arrival "
                    "truncation the first that settles inside the target "
                    "wins, then the first that reaches it; otherwise the first "
                    "whose trimmed end lands inside the target, else the "
                    "closest."
                ),
            ),
        ] = 8,
        stop_px: Annotated[
            float,
            PydanticField(
                ge=0,
                description=(
                    "Fallback end of a path that never arrives (or with "
                    "truncation off): its last tick moving at least this many "
                    "pixels; the near-still tail after it is dropped."
                ),
            ),
        ] = 0.5,
        truncate_at_arrival: Annotated[
            bool,
            PydanticField(
                description=(
                    "Cut each path where a human would click: a sampled number "
                    "of dwell ticks (mouse-ai's human fit, mean ~7.5) after the "
                    "cursor is inside the target. Paths that never arrive fall "
                    "back to trimming at the last moving tick."
                ),
            ),
        ] = True,
        arrival_radius_px: Annotated[
            float,
            PydanticField(
                gt=0,
                description=(
                    "When the caller passes no element box (a raw-coordinate "
                    "click), the cursor has arrived once it is within this "
                    "many pixels of the target point — about half a small "
                    "control."
                ),
            ),
        ] = 12.0,
        max_offset: Annotated[
            float,
            PydanticField(
                gt=0,
                description=(
                    "Longest start-to-target distance, in model units, the "
                    "generator is conditioned on (training p99 is 0.95). A "
                    "longer move is generated at this length and stretched: "
                    "same duration, proportionally faster."
                ),
            ),
        ] = 0.95,
        min_distance_px: Annotated[
            float,
            PydanticField(
                ge=0,
                description=(
                    "Moves shorter than this skip the generator and take one "
                    "tick (below 1 px: a single waypoint, no sleep)."
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
    ) -> None:
        # Fail at construction — before a browser launches — rather than at
        # the first click: missing numpy (the import below names the extra),
        # a wrong directory, a foreign export. Only the weights load lazily.
        from spectragent.core.capabilities.browser.input import _gan_numpy

        self._weights_dir = Path(weights_dir).expanduser()
        weights_path = self._weights_dir / _gan_numpy.WEIGHTS_FILE
        if not weights_path.is_file():
            raise FileNotFoundError(f"GAN trajectory weights not found: {weights_path}")
        self._meta = _gan_numpy.read_meta(self._weights_dir / _gan_numpy.META_FILE)

        cond_dim = int(self._meta.get("cond_dim", 4))
        if cond_dim != 4:
            raise ValueError(
                f"GAN export at {self._weights_dir} conditions on {cond_dim} values; "
                "the gan trajectory builds (offset_x, offset_y, btn_w, btn_h)"
            )

        resolved_hz = hz if hz is not None else float(self._meta.get("hz", DEFAULT_HZ))
        if resolved_hz <= 0:
            raise ValueError(f"hz must be > 0, got {resolved_hz}")
        if num_candidates < 1:
            raise ValueError(f"num_candidates must be >= 1, got {num_candidates}")

        self._hz = resolved_hz
        self._px_per_unit = px_per_unit
        self._button_size = button_size
        self._num_candidates = num_candidates
        self._stop_px = stop_px
        self._truncate_at_arrival = truncate_at_arrival
        self._arrival_radius_px = arrival_radius_px
        self._max_offset = max_offset
        self._min_distance_px = min_distance_px
        self._correction_power = correction_power
        self._jitter = jitter
        self._rng = _gan_numpy.new_rng(seed)
        self._generator: NumpyGenerator | None = None

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def hz(self) -> float:
        return self._hz

    @property
    def tick_ms(self) -> float:
        return 1000.0 / self._hz

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
    def _load(self) -> NumpyGenerator:
        if self._generator is None:
            from spectragent.core.capabilities.browser.input._gan_numpy import load_generator

            self._generator = load_generator(self._weights_dir, self._meta)
        return self._generator

    def _sample_path(self, dx: float, dy: float, target_w: float, target_h: float) -> np.ndarray:
        """Best candidate path as cumulative px offsets from the start, endpoint-corrected."""
        import numpy as np

        gen = self._load()
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

        k = self._num_candidates
        noise = self._rng.standard_normal((k, gen.latent_dim), dtype=np.float32)
        cond = np.tile(np.asarray(cond_row, dtype=np.float32), (k, 1))
        moves = gen.forward(noise, cond).astype(np.float64) * (unit * scale)  # (k, T, 2) px

        finite = np.isfinite(moves).all(axis=(1, 2))
        if not finite.any():
            raise ValueError(
                f"GAN generator at {self._weights_dir} produced non-finite output "
                f"for cond {cond_row}"
            )

        cumulative = np.cumsum(moves, axis=1)  # (k, T, 2) positions after each tick
        known_half = np.array([target_w, target_h]) / 2.0 if known_box else None

        chosen = self._arrival_cut(cumulative, target, known_half, finite)
        if chosen is None:
            chosen = self._trimmed_cut(
                moves, cumulative, target, known_half, finite, btn_w, btn_h, scale
            )
        best, n = chosen

        path = cumulative[best, :n]
        u = np.arange(1, n + 1, dtype=np.float64) / n
        weight = _smoothstep(u**self._correction_power)
        path = path + weight[:, None] * (target - path[-1])
        path[-1] = target
        return path

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
        known_half: np.ndarray | None,
        finite: np.ndarray,
        btn_w: float,
        btn_h: float,
        scale: float,
    ) -> tuple[int, int]:
        """``(candidate, ticks)`` trimmed at the last moving tick (no-arrival fallback)."""
        import numpy as np

        from spectragent.core.capabilities.browser.input._gan_numpy import effective_ticks

        k = moves.shape[0]
        lengths = np.maximum(effective_ticks(moves, self._stop_px), 1)
        ends = cumulative[np.arange(k), lengths - 1]  # (k, 2)
        half = known_half if known_half is not None else np.array([btn_w, btn_h]) * scale / 2.0
        inside = finite & np.all(np.abs(ends - target) <= half, axis=1)
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
