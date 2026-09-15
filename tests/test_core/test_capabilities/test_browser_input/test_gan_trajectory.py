"""``gan`` trajectory strategy — af semantics around the vendored generator.

Numpy-vs-torch parity of the forward pass is proven upstream (mouse-gan-torch
``tests/test_infer_numpy.py``); these tests pin what this package adds:
registration and config surface, exact endpoints, tick timing, seeding, the
long-move and candidate-selection policies, degenerate input and the
missing-numpy error, and arrival truncation.
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
from pydantic import ValidationError

import spectragent.core.capabilities.browser.input as input_pkg
from spectragent.core.capabilities.browser.input import gan as gan_module
from spectragent.core.capabilities.browser.input.gan import (
    TRAINING_BUTTON_PX,
    GanTrajectory,
    sample_dwell_ticks,
)
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryRegistrar,
    TrajectoryPoint,
)

from .conftest import TINY_LATENT, TINY_SEQ_LEN

SMOKE_EXPORT = Path("/Users/qly/QLY/code/study/mouse-gan-torch/runs/smoke/export")


class ScriptedGenerator:
    """Stands in for the numpy generator with hand-written per-tick moves.

    ``paths_px`` is one list of per-tick pixel deltas per candidate row; rows
    are padded with zeros to ``seq_len``. Records every condition it sees.
    """

    latent_dim = TINY_LATENT

    def __init__(self, paths_px: list[list[tuple[float, float]]], *, unit: float = 800.0) -> None:
        self.seq_len = TINY_SEQ_LEN
        self.unit = unit
        self.paths_px = paths_px
        self.conds: list[np.ndarray] = []

    def forward(self, noise: np.ndarray, cond: np.ndarray) -> np.ndarray:
        self.conds.append(np.array(cond))
        out = np.zeros((noise.shape[0], self.seq_len, 2), dtype=np.float32)
        for row in range(noise.shape[0]):
            path = self.paths_px[row % len(self.paths_px)]
            if path:
                out[row, : len(path)] = np.asarray(path, dtype=np.float32) / self.unit
        return out


def _straight(dx: float, dy: float, ticks: int) -> list[tuple[float, float]]:
    return [(dx / ticks, dy / ticks)] * ticks


def _scripted(traj: GanTrajectory, generator: ScriptedGenerator) -> GanTrajectory:
    traj._generator = generator  # type: ignore[assignment]  # test seam: skip weight loading
    return traj


# ── registration + config surface ───────────────────────────────────────


def test_registered_under_gan(gan_export: Path) -> None:
    assert MouseTrajectoryRegistrar.get("gan") is GanTrajectory
    assert GanTrajectory(weights_dir=str(gan_export)).trajectory_type == "gan"


def _capability_config(mouse_trajectory: dict[str, Any]) -> Any:
    from spectragent.generated._composed import BrowserCapabilityConfig

    return BrowserCapabilityConfig.model_validate(
        {"mouse_trajectory": mouse_trajectory, "backend": {"provider_id": "local_chrome"}}
    )


def test_generated_config_accepts_gan_block() -> None:
    cfg = _capability_config(
        {"trajectory_type": "gan", "weights_dir": "/w", "seed": 3, "num_candidates": 4}
    )
    mt = cfg.mouse_trajectory
    assert mt.trajectory_type == "gan"
    assert (mt.weights_dir, mt.seed, mt.num_candidates) == ("/w", 3, 4)
    assert mt.button_size == "training"
    assert mt.px_per_unit == 800.0


@pytest.mark.parametrize(
    "bad",
    [
        {"trajectory_type": "gan"},  # weights_dir is required
        {"trajectory_type": "gan", "weights_dir": "/w", "num_candidates": 0},
        {"trajectory_type": "gan", "weights_dir": "/w", "button_size": "huge"},
        {"trajectory_type": "gan", "weights_dir": "/w", "px_per_unit": 0},
        {"trajectory_type": "gan", "weights_dir": "/w", "gravity": 9.0},  # windmouse knob
    ],
)
def test_generated_config_rejects_bad_gan_block(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _capability_config(bad)


def test_shipped_yaml_with_a_gan_block_composes_a_gan_strategy(gan_export: Path) -> None:
    import yaml

    from spectragent.core.composer import ConfigComposer
    from spectragent.scripts.generate_all import _collect_registrars

    shipped = Path(__file__).resolve().parents[4] / "configs" / "browser_use_local.yaml"
    raw = yaml.safe_load(shipped.read_text())
    capability = raw["environment"][0]["capabilities"][0]
    capability["mouse_trajectory"] = {
        "trajectory_type": "gan",
        "weights_dir": str(gan_export),
        "seed": 1,
        "num_candidates": 2,
    }
    run = ConfigComposer(registrars=_collect_registrars()).compose(raw)
    (cap,) = run.instances("environment")[0].capabilities.values()
    assert isinstance(cap.mouse_trajectory, GanTrajectory)
    assert cap.mouse_trajectory._num_candidates == 2


def test_construction_checks_the_export_but_loads_weights_lazily(
    tmp_path: Path, gan_export: Path
) -> None:
    with pytest.raises(FileNotFoundError, match="generator.npz"):
        GanTrajectory(weights_dir=str(tmp_path / "nope"))

    traj = GanTrajectory(weights_dir=str(gan_export), seed=0)
    assert traj._generator is None
    traj.generate(100, 100, 500, 300)
    loaded = traj._generator
    assert loaded is not None
    traj.generate(500, 300, 100, 100)
    assert traj._generator is loaded  # once per instance


def test_foreign_export_format_is_rejected(make_gan_export: Any) -> None:
    export = make_gan_export(meta={"format": "something-else/9"})
    with pytest.raises(ValueError, match="format"):
        GanTrajectory(weights_dir=str(export))


def test_missing_numpy_names_the_extra(monkeypatch: pytest.MonkeyPatch, gan_export: Path) -> None:
    monkeypatch.setitem(sys.modules, "numpy", None)  # type: ignore[arg-type]
    monkeypatch.delitem(
        sys.modules, "spectragent.core.capabilities.browser.input._gan_numpy", raising=False
    )
    monkeypatch.delattr(input_pkg, "_gan_numpy", raising=False)
    with pytest.raises(ImportError, match=r"mouse-gan"):
        GanTrajectory(weights_dir=str(gan_export))


# ── endpoint + timing invariants on the real (tiny) forward pass ────────

_GRID = [
    ((640, 360), (900, 500)),
    ((10, 10), (200, 90)),
    ((1270, 710), (5, 5)),  # long diagonal, beyond the training range
    ((100, 600), (450, 820)),  # off-viewport target on a 1280x720 page
    ((640, 360), (-30, 360)),  # off-viewport to the left
    ((3.4, 7.6), (321.5, 99.49)),  # float inputs
]


@pytest.mark.parametrize("start,end", _GRID)
def test_endpoint_is_exact(
    gan_export: Path, start: tuple[float, float], end: tuple[float, float]
) -> None:
    traj = GanTrajectory(weights_dir=str(gan_export), seed=11)
    pts = traj.generate(*start, *end, target_w=60, target_h=24)
    assert pts
    assert (pts[-1].x, pts[-1].y) == (round(end[0]), round(end[1]))
    assert (pts[0].x, pts[0].y, pts[0].dt_ms) == (round(start[0]), round(start[1]), 0.0)


def test_timestamps_are_monotone_multiples_of_the_tick(gan_export: Path) -> None:
    traj = GanTrajectory(weights_dir=str(gan_export), seed=5)
    assert traj.hz == 50.0
    pts = traj.generate(100, 100, 700, 400)
    assert len(pts) > 2
    assert pts[0].dt_ms == 0.0
    # One waypoint per tick at exactly 1/hz; still ticks are repeated
    # waypoints, never folded, so the time of every tick survives.
    assert [p.dt_ms for p in pts[1:]] == [20.0] * (len(pts) - 1)
    assert len(pts) - 1 <= TINY_SEQ_LEN


def test_hz_resolution(make_gan_export: Any) -> None:
    assert GanTrajectory(weights_dir=str(make_gan_export(meta={"hz": 60}))).hz == 60.0
    assert GanTrajectory(weights_dir=str(make_gan_export(meta={"hz": 60})), hz=50).hz == 50.0
    no_meta = make_gan_export(write_meta=False)
    # Without meta.json the upstream default dims apply; only hz is checked here.
    assert GanTrajectory(weights_dir=str(no_meta)).hz == 50.0
    meta_without_hz = make_gan_export()
    meta = json.loads((meta_without_hz / "meta.json").read_text())
    del meta["hz"]
    (meta_without_hz / "meta.json").write_text(json.dumps(meta))
    assert GanTrajectory(weights_dir=str(meta_without_hz)).hz == 50.0


def test_jitter_perturbs_ticks_within_bounds(gan_export: Path) -> None:
    generator = ScriptedGenerator([_straight(400, 0, 20)])
    traj = _scripted(
        GanTrajectory(weights_dir=str(gan_export), seed=1, jitter=0.2, truncate_at_arrival=False),
        generator,
    )
    pts = traj.generate(100, 100, 500, 100)
    dts = [p.dt_ms for p in pts[1:]]
    assert len(dts) == 20
    assert all(20.0 * 0.8 - 1e-9 <= dt <= 20.0 * 1.2 + 1e-9 for dt in dts)
    assert len(set(dts)) > 1


def test_determinism_with_seed(gan_export: Path) -> None:
    def run(seed: int) -> list[TrajectoryPoint]:
        traj = GanTrajectory(weights_dir=str(gan_export), seed=seed, jitter=0.1)
        return traj.generate(50, 60, 800, 500) + traj.generate(800, 500, 200, 650)

    assert run(42) == run(42)
    assert run(42) != run(43)


# ── policies, with a scripted generator ─────────────────────────────────


def _no_truncation(gan_export: Path, **kwargs: Any) -> GanTrajectory:
    return GanTrajectory(weights_dir=str(gan_export), truncate_at_arrival=False, **kwargs)


def test_perfect_path_is_replayed_untouched(gan_export: Path) -> None:
    generator = ScriptedGenerator([_straight(300, 120, 12)])
    traj = _scripted(_no_truncation(gan_export, seed=0), generator)
    pts = traj.generate(100, 100, 400, 220)
    assert [(p.x, p.y) for p in pts] == [
        (round(100 + 25 * k), round(100 + 10 * k)) for k in range(13)
    ]
    assert [p.dt_ms for p in pts] == [0.0] + [20.0] * 12


def test_endpoint_correction_is_smooth_and_eases_into_target(gan_export: Path) -> None:
    # The model undershoots by 100 px; the correction must not add a jump.
    generator = ScriptedGenerator([_straight(300, 0, 20)])
    traj = _scripted(_no_truncation(gan_export, seed=0), generator)
    pts = traj.generate(0, 300, 400, 300)
    xs = [p.x for p in pts]
    assert xs[-1] == 400
    steps = np.diff(xs)
    assert (steps >= 0).all()  # still monotone toward the target
    assert steps.max() <= 15 + 100 * 1.5 * 1.5 / 20 + 1  # 15 px/tick + peak correction slope
    # Final step stays near the model's 15 px: a linear spread would add 5 px
    # there and a (k/n)**2 spread ~10 px — a visible lunge into the target.
    assert steps[-1] <= 18


def test_long_move_is_conditioned_in_range_and_stretched(gan_export: Path) -> None:
    max_offset = 0.95
    # The scripted model lands exactly on whatever in-range offset it is given.
    in_range_px = (0.95 * 800 * 0.6, 0.95 * 800 * 0.8)  # |offset| == 0.95 units
    generator = ScriptedGenerator([_straight(*in_range_px, 16)])
    traj = _scripted(_no_truncation(gan_export, seed=0, max_offset=max_offset), generator)
    start, dx, dy = (10.0, 10.0), 1200.0 * 0.6, 1200.0 * 0.8  # 1200 px == 1.5 units
    pts = traj.generate(start[0], start[1], start[0] + dx, start[1] + dy)

    cond = generator.conds[-1]
    assert np.linalg.norm(cond[0, :2]) == pytest.approx(max_offset, rel=1e-6)
    assert cond[0, :2] / np.linalg.norm(cond[0, :2]) == pytest.approx([0.6, 0.8], rel=1e-6)
    assert tuple(cond[0, 2:]) == pytest.approx((109 / 800, 34 / 800))
    # Same tick count as the in-range move; each tick stretched by 1.5/0.95.
    assert len(pts) == 17
    stretched_step = math.hypot(*in_range_px) / 16 * (1.5 / 0.95)
    assert math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y) == pytest.approx(
        stretched_step, abs=1.5
    )
    assert sum(p.dt_ms for p in pts) == pytest.approx(16 * 20.0)


def test_in_range_move_is_not_rescaled(gan_export: Path) -> None:
    generator = ScriptedGenerator([_straight(400, 0, 10)])
    traj = _scripted(GanTrajectory(weights_dir=str(gan_export), seed=0), generator)
    traj.generate(100, 100, 500, 100)
    assert generator.conds[-1][0, :2] == pytest.approx([0.5, 0.0])


def test_button_size_policy(gan_export: Path) -> None:
    cases: list[tuple[Literal["training", "target"], tuple[float, float], tuple[float, float]]] = [
        ("training", (60.0, 24.0), TRAINING_BUTTON_PX),
        ("target", (60.0, 24.0), (60.0, 24.0)),
        ("target", (0.0, 0.0), TRAINING_BUTTON_PX),  # unknown box falls back
    ]
    for policy, box, expected in cases:
        generator = ScriptedGenerator([_straight(400, 0, 10)])
        traj = _scripted(GanTrajectory(weights_dir=str(gan_export), button_size=policy), generator)
        traj.generate(100, 100, 500, 100, target_w=box[0], target_h=box[1])
        got = tuple(generator.conds[-1][0, 2:] * 800)
        assert got == pytest.approx(expected, rel=1e-5), policy


def test_fallback_candidates_first_inside_wins_else_closest(gan_export: Path) -> None:
    far = _straight(100, 200, 10)  # misses by ~200 px
    inside_off_centre = _straight(410, 8, 10)  # inside a 60x24 box, not centred
    dead_centre = _straight(400, 0, 10)
    near_miss = _straight(380, 40, 10)

    generator = ScriptedGenerator([far, inside_off_centre, dead_centre])
    traj = _scripted(_no_truncation(gan_export, num_candidates=3), generator)
    pts = traj.generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert generator.conds[-1].shape == (3, 4)
    # The chosen path is recognisable by its own tick-1 position.
    assert (pts[1].x, pts[1].y) == (41, 1)

    generator = ScriptedGenerator([far, near_miss])
    traj = _scripted(_no_truncation(gan_export, num_candidates=2), generator)
    pts = traj.generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert pts[1].x == pytest.approx(38, abs=1)  # the near miss, corrected


def test_still_ticks_stay_as_repeated_waypoints(gan_export: Path) -> None:
    path = [(30.0, 0.0)] * 5 + [(0.6, 0.0)] + [(0.1, 0.0)] * 3 + [(30.0, 0.0)] * 5
    generator = ScriptedGenerator([path])
    traj = _scripted(_no_truncation(gan_export, stop_px=0.05), generator)
    end_x = sum(dx for dx, _ in path)
    pts = traj.generate(0, 0, end_x, 0)
    assert (pts[-1].x, pts[-1].y) == (round(end_x), 0)
    assert len(pts) == len(path) + 1
    assert sum(p.dt_ms for p in pts) == pytest.approx(len(path) * 20.0)
    assert any((a.x, a.y) == (b.x, b.y) for a, b in zip(pts, pts[1:], strict=False))


# ── arrival truncation ──────────────────────────────────────────────────


def _mouse_ai_reference_dwell(rng: random.Random) -> int:
    """mouse-ai ``random_ticks_inside_btn``, verbatim."""
    return max(0, round(rng.gammavariate(10.53369, 0.80039) + -0.93502))


def test_dwell_ticks_follow_the_mouse_ai_fit() -> None:
    rng = np.random.default_rng(7)
    ours = np.array([sample_dwell_ticks(rng) for _ in range(20_000)])
    ref_rng = random.Random(7)
    reference = np.array([_mouse_ai_reference_dwell(ref_rng) for _ in range(20_000)])

    assert ours.min() >= 0
    assert ours.dtype.kind == "i"
    # Gamma(10.53, 0.80) - 0.94: mean ~7.5 ticks (150 ms at 50 Hz), sd ~2.6.
    assert ours.mean() == pytest.approx(7.5, abs=0.1)
    assert ours.std() == pytest.approx(2.6, abs=0.15)
    for q in (10, 50, 90):
        assert abs(np.percentile(ours, q) - np.percentile(reference, q)) <= 1
    # Seeded: the same generator state gives the same draws.
    a, b = np.random.default_rng(3), np.random.default_rng(3)
    assert [sample_dwell_ticks(a) for _ in range(50)] == [sample_dwell_ticks(b) for _ in range(50)]


@pytest.fixture
def dwell_3(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(gan_module, "sample_dwell_ticks", lambda rng: 3)
    return 3


def _never_stopping(step_x: float, ticks: int = TINY_SEQ_LEN) -> list[tuple[float, float]]:
    return [(step_x, 0.0)] * ticks


def test_truncation_cuts_a_never_stopping_path_after_the_dwell(
    gan_export: Path, dwell_3: int
) -> None:
    # 40 px/tick along x; the target at +400 px is reached on tick index 9
    # and passed through (the run continues for all 30 ticks).
    generator = ScriptedGenerator([_never_stopping(40.0)])
    on = _scripted(GanTrajectory(weights_dir=str(gan_export), seed=0), generator)
    off = _scripted(_no_truncation(gan_export, seed=0), generator)

    long = off.generate(100, 100, 500, 100)
    short = on.generate(100, 100, 500, 100)
    assert len(long) == TINY_SEQ_LEN + 1
    assert len(short) == 1 + (9 + 1 + dwell_3)  # arrival tick + dwell, then the click
    assert (short[-1].x, short[-1].y) == (500, 100)
    assert (long[-1].x, long[-1].y) == (500, 100)
    assert sum(p.dt_ms for p in short) == pytest.approx((10 + dwell_3) * 20.0)


def test_settling_candidate_is_preferred_and_rests_on_the_target(
    gan_export: Path, dwell_3: int
) -> None:
    passes_through = _never_stopping(40.0)
    settles = _straight(400, 0, 8)  # reaches +400 on tick index 7, then zeros
    generator = ScriptedGenerator([passes_through, settles])
    traj = _scripted(GanTrajectory(weights_dir=str(gan_export), num_candidates=2), generator)
    pts = traj.generate(100, 100, 500, 100)
    assert (pts[1].x, pts[1].y) == (150, 100)  # the settling candidate (50 px/tick)
    # Arrival on tick 7, then dwell_3 more ticks inside: 11 ticks in all, the
    # last four resting on the target as repeated waypoints.
    assert len(pts) == 1 + 8 + dwell_3
    assert [(p.x, p.y) for p in pts[-(dwell_3 + 1) :]] == [(500, 100)] * (dwell_3 + 1)
    assert all(p.dt_ms == 20.0 for p in pts[1:])


def test_arrival_uses_the_element_box_when_given(gan_export: Path, dwell_3: int) -> None:
    # Passes 20 px below the target: outside the 12 px radius, inside a 60x60 box.
    # (Box check: |dx| <= 30 and |dy| <= 30 from tick index 9 on.)
    path = [(40.0, 2.0)] * 10 + [(40.0, 0.0)] * 20
    generator = ScriptedGenerator([path])
    traj = _scripted(GanTrajectory(weights_dir=str(gan_export), seed=0), generator)

    no_box = traj.generate(100, 100, 500, 100)
    boxed = traj.generate(100, 100, 500, 100, target_w=60, target_h=60)
    assert len(no_box) == TINY_SEQ_LEN + 1  # never within 12 px -> fallback
    # Inside the box from tick index 9 on (at +400, +20): cut after the dwell.
    assert len(boxed) == 1 + (9 + 1 + dwell_3)
    assert (boxed[-1].x, boxed[-1].y) == (500, 100)


def test_no_arrival_falls_back_to_trimming(gan_export: Path) -> None:
    misses = [(40.0, 10.0)] * 10  # ends 100 px below the target, then stops
    on = _scripted(GanTrajectory(weights_dir=str(gan_export), seed=4), ScriptedGenerator([misses]))
    off = _scripted(_no_truncation(gan_export, seed=4), ScriptedGenerator([misses]))
    assert on.generate(100, 100, 500, 100) == off.generate(100, 100, 500, 100)


def test_arrival_is_detected_on_the_stretched_long_move(gan_export: Path, dwell_3: int) -> None:
    # In-range, the scripted model moves 760/16 px per tick and never stops.
    # Stretched by 1.5/0.95 it reaches the 1200 px target on tick index 15;
    # unstretched it would only get there on tick index 25.
    in_range_step = 0.95 * 800 / 16
    generator = ScriptedGenerator([_never_stopping(in_range_step)])
    traj = _scripted(GanTrajectory(weights_dir=str(gan_export), seed=0), generator)
    pts = traj.generate(20, 300, 1220, 300)

    assert np.linalg.norm(generator.conds[-1][0, :2]) == pytest.approx(0.95, rel=1e-6)
    assert len(pts) == 1 + (15 + 1 + dwell_3)
    assert (pts[-1].x, pts[-1].y) == (1220, 300)
    stretched = in_range_step * 1.5 / 0.95
    assert pts[1].x - pts[0].x == pytest.approx(stretched, abs=1)


# ── degenerate input ────────────────────────────────────────────────────


def test_zero_distance_is_a_single_point(gan_export: Path) -> None:
    traj = GanTrajectory(weights_dir=str(gan_export))
    assert traj.generate(42, 42, 42, 42) == [TrajectoryPoint(42, 42, 0.0)]
    assert traj.generate(42.2, 42, 42.6, 42.3) == [TrajectoryPoint(43, 42, 0.0)]
    assert traj._generator is None  # never touched the model


def test_tiny_move_is_one_tick(gan_export: Path) -> None:
    traj = GanTrajectory(weights_dir=str(gan_export))
    assert traj.generate(100, 100, 102, 101) == [
        TrajectoryPoint(100, 100, 0.0),
        TrajectoryPoint(102, 101, 20.0),
    ]
    assert traj._generator is None


def test_model_that_never_moves_still_lands(gan_export: Path) -> None:
    generator = ScriptedGenerator([[]])  # all-zero output
    traj = _scripted(GanTrajectory(weights_dir=str(gan_export)), generator)
    assert traj.generate(10, 10, 300, 200) == [
        TrajectoryPoint(10, 10, 0.0),
        TrajectoryPoint(300, 200, 20.0),
    ]


def test_non_finite_output_is_an_error(gan_export: Path) -> None:
    class NanGenerator(ScriptedGenerator):
        def forward(self, noise: np.ndarray, cond: np.ndarray) -> np.ndarray:
            return np.full((noise.shape[0], self.seq_len, 2), np.nan, dtype=np.float32)

    traj = _scripted(GanTrajectory(weights_dir=str(gan_export)), NanGenerator([[]]))
    with pytest.raises(ValueError, match="non-finite"):
        traj.generate(10, 10, 300, 200)


# ── the development export, when this machine has it ────────────────────


@pytest.mark.skipif(not (SMOKE_EXPORT / "generator.npz").is_file(), reason="smoke export absent")
def test_smoke_export_generates_exact_viewport_paths() -> None:
    traj = GanTrajectory(weights_dir=str(SMOKE_EXPORT), seed=0, hz=50)
    for (sx, sy), (ex, ey) in (((640, 360), (1200, 90)), ((20, 20), (1260, 700))):
        pts = traj.generate(sx, sy, ex, ey, target_w=80, target_h=30)
        assert (pts[-1].x, pts[-1].y) == (ex, ey)
        assert 1 < len(pts) <= 101
