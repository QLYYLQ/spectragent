"""``learned`` trajectory strategy — af semantics around the vendored samplers.

Sampler parity with the trainer's reference code lives in ``test_mouse_gan_v2``;
these tests pin what the strategy adds: registration and config surface,
reading the model from ``meta.json`` (its tick rate and training window
included, and rejecting what it cannot sample, the retired format /1 among
it), exact endpoints, an endpoint correction that leaves the model's still
ticks still, tick timing, seeding, the long-move, button-size and
candidate-selection policies, where a path ends — the model's own stop — and
degenerate input.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser.input._learned_export import Draw
from spectragent.core.capabilities.browser.input.learned import (
    TRAINING_BUTTON_PX,
    LearnedTrajectory,
)
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryRegistrar,
    TrajectoryPoint,
)

from .conftest import (
    TINY_SEQ_LEN,
    V2_GAN_AR_EXPORT,
    V2_MDN_EXPORT,
)

KINDS = ("mdn", "gan_ar")


class ScriptedSampler:
    """Stands in for a loaded model with hand-written per-tick moves.

    ``paths_px`` is one list of per-tick pixel deltas per candidate row, padded
    with zeros to ``seq_len``. ``lengths`` is where the model stops each row
    (cycled like the paths); moves from a row's length on are zeroed, as the
    real samplers do. Records every condition it sees.
    """

    def __init__(
        self,
        paths_px: list[list[tuple[float, float]]],
        *,
        lengths: list[int],
        unit: float = 800.0,
        seq_len: int = TINY_SEQ_LEN,
    ) -> None:
        self.paths_px = paths_px
        self.lengths = lengths
        self.unit = unit
        self.seq_len = seq_len
        self.conds: list[np.ndarray] = []

    def draw(self, cond: np.ndarray, rng: np.random.Generator) -> Draw:
        self.conds.append(np.array(cond))
        batch = cond.shape[0]
        out = np.zeros((batch, self.seq_len, 2), dtype=np.float32)
        for row in range(batch):
            path = self.paths_px[row % len(self.paths_px)]
            if path:
                out[row, : len(path)] = np.asarray(path, dtype=np.float32) / self.unit
        lengths = np.array([self.lengths[row % len(self.lengths)] for row in range(batch)])
        for row, n in enumerate(lengths):
            out[row, n:] = 0.0
        return Draw(moves=out, lengths=lengths)


def _straight(dx: float, dy: float, ticks: int) -> list[tuple[float, float]]:
    return [(dx / ticks, dy / ticks)] * ticks


def _scripted(traj: LearnedTrajectory, sampler: ScriptedSampler) -> LearnedTrajectory:
    traj._sampler = sampler  # test seam: skip weight loading
    return traj


def _with_sampler(export: Path, sampler: ScriptedSampler, **kwargs: Any) -> LearnedTrajectory:
    """A strategy on *export* whose model is *sampler*; seed 0 unless given."""
    knobs: dict[str, Any] = {"seed": 0, **kwargs}
    return _scripted(LearnedTrajectory(weights_dir=str(export), **knobs), sampler)


@pytest.fixture
def export_of(make_export: Callable[..., Path]) -> Callable[[str], Path]:
    """``export_of(kind)``: a default tiny export of that kind."""
    return lambda kind: make_export(kind)


# ── registration + config surface ───────────────────────────────────────


def test_registered_under_learned_and_the_gan_key_is_gone(mdn_export: Path) -> None:
    from conscribe import KeyNotFoundError

    assert MouseTrajectoryRegistrar.get("learned") is LearnedTrajectory
    assert sorted(MouseTrajectoryRegistrar.keys()) == ["bezier", "instant", "learned", "windmouse"]
    with pytest.raises(KeyNotFoundError):
        MouseTrajectoryRegistrar.get("gan")
    assert LearnedTrajectory(weights_dir=str(mdn_export)).trajectory_type == "learned"


def _capability_config(mouse_trajectory: dict[str, Any]) -> Any:
    from spectragent.generated._composed import BrowserCapabilityConfig

    return BrowserCapabilityConfig.model_validate(
        {
            "mouse_trajectory": mouse_trajectory,
            "typing_rhythm": {"rhythm_type": "instant"},
            "replayability": {"replayability_id": "none"},
            "network_source": {"source_id": "live"},
            "dom_cleaner": {"cleaner_type": "browser_use"},
            "dom_serializer": {"serializer_type": "text"},
            "backend": {"provider_id": "local_chrome"},
        }
    )


def test_generated_config_accepts_a_learned_block() -> None:
    cfg = _capability_config(
        {"trajectory_type": "learned", "weights_dir": "/w", "seed": 3, "num_candidates": 4}
    )
    mt = cfg.mouse_trajectory
    assert mt.trajectory_type == "learned"
    assert (mt.weights_dir, mt.seed, mt.num_candidates) == ("/w", 3, 4)
    assert mt.button_size == "training"
    # hz and the training window are the export's; unset unless written.
    assert (mt.hz, mt.px_per_unit) == (None, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"trajectory_type": "learned"},  # weights_dir is required
        {"trajectory_type": "learned", "weights_dir": "/w", "num_candidates": 0},
        {"trajectory_type": "learned", "weights_dir": "/w", "button_size": "huge"},
        {"trajectory_type": "learned", "weights_dir": "/w", "px_per_unit": 0},
        {"trajectory_type": "learned", "weights_dir": "/w", "gravity": 9.0},  # windmouse knob
        # The model comes from meta.json; there is no knob to name it.
        {"trajectory_type": "learned", "weights_dir": "/w", "kind": "mdn"},
        {"trajectory_type": "learned", "weights_dir": "/w", "format": "mouse-gan-generator/2"},
    ],
)
def test_generated_config_rejects_a_bad_learned_block(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _capability_config(bad)


def test_generated_config_rejects_the_old_gan_key() -> None:
    with pytest.raises(ValidationError, match="gan"):
        _capability_config({"trajectory_type": "gan", "weights_dir": "/w"})


def _shipped_run_config(mouse_trajectory: dict[str, Any]) -> dict[str, Any]:
    import yaml

    shipped = Path(__file__).resolve().parents[4] / "configs" / "browser_use_local.yaml"
    raw = yaml.safe_load(shipped.read_text())
    raw["environment"][0]["capabilities"][0]["mouse_trajectory"] = mouse_trajectory
    return raw


@pytest.mark.parametrize(
    "kind,label",
    [
        ("mdn", "mouse-gan-generator/2 kind 'mdn'"),
        ("gan_ar", "mouse-gan-generator/2 kind 'gan_ar'"),
    ],
)
def test_shipped_yaml_with_a_learned_block_composes_the_exported_model(
    export_of: Callable[[str], Path], kind: str, label: str
) -> None:
    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    raw = _shipped_run_config(
        {"trajectory_type": "learned", "weights_dir": str(export_of(kind)), "seed": 1}
    )
    run = ConfigComposer(registrars=collect_registrars()).compose(raw)
    (cap,) = run.instances("environment")[0].capabilities.values()
    assert isinstance(cap.mouse_trajectory, LearnedTrajectory)
    assert cap.mouse_trajectory._export.label == label


@pytest.mark.parametrize(
    "knob,value",
    [("truncate_at_arrival", False), ("arrival_radius_px", 20.0), ("stop_px", 1.0)],
)
def test_compose_refuses_the_knobs_of_the_retired_format(
    mdn_export: Path, knob: str, value: object
) -> None:
    # They cut the paths of format /1, which never stopped; no export read now needs them.
    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    raw = _shipped_run_config(
        {"trajectory_type": "learned", "weights_dir": str(mdn_export), knob: value}
    )
    with pytest.raises(ValidationError, match=knob):
        ConfigComposer(registrars=collect_registrars()).compose(raw)


# ── the model is read from meta.json ────────────────────────────────────


@pytest.mark.parametrize(
    "kind,label,sampler",
    [
        ("mdn", "mouse-gan-generator/2 kind 'mdn'", "MdnSampler"),
        ("gan_ar", "mouse-gan-generator/2 kind 'gan_ar'", "GanArSampler"),
    ],
)
def test_the_export_meta_picks_the_sampler(
    export_of: Callable[[str], Path], kind: str, label: str, sampler: str
) -> None:
    traj = LearnedTrajectory(weights_dir=str(export_of(kind)), seed=0)
    assert traj._export.label == label
    traj.generate(100, 100, 500, 300)
    assert type(traj._sampler).__name__ == sampler


@pytest.mark.parametrize("kind", KINDS)
def test_construction_checks_the_export_but_loads_weights_lazily(
    tmp_path: Path, export_of: Callable[[str], Path], kind: str
) -> None:
    with pytest.raises(FileNotFoundError, match="generator.npz"):
        LearnedTrajectory(weights_dir=str(tmp_path / "nope"))

    traj = LearnedTrajectory(weights_dir=str(export_of(kind)), seed=0)
    assert traj._sampler is None
    traj.generate(100, 100, 500, 300)
    loaded = traj._sampler
    assert loaded is not None
    traj.generate(500, 300, 100, 100)
    assert traj._sampler is loaded  # once per instance


def test_an_export_without_meta_is_rejected(mdn_export: Path) -> None:
    (mdn_export / "meta.json").unlink()
    with pytest.raises(FileNotFoundError, match="meta.json"):
        LearnedTrajectory(weights_dir=str(mdn_export))


def test_an_unparseable_meta_is_rejected(mdn_export: Path) -> None:
    (mdn_export / "meta.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        LearnedTrajectory(weights_dir=str(mdn_export))


@pytest.mark.parametrize(
    "meta,match",
    [
        ({"format": "something-else/9"}, r"format 'something-else/9'.*mouse-gan-generator/2"),
        # The retired GAN port, which never stopped a path, is no longer read.
        (
            {"format": "mouse-gan-generator/1"},
            r"format 'mouse-gan-generator/1'; the learned trajectory reads mouse-gan-generator/2$",
        ),
        ({"format": None}, "no format"),
    ],
)
def test_an_unknown_format_is_rejected_naming_it(
    make_export: Callable[..., Path], meta: dict[str, Any], match: str
) -> None:
    export = make_export("mdn", meta=meta)
    if meta["format"] is None:
        stored = json.loads((export / "meta.json").read_text())
        del stored["format"]
        (export / "meta.json").write_text(json.dumps(stored))
    with pytest.raises(ValueError, match=match):
        LearnedTrajectory(weights_dir=str(export))


@pytest.mark.parametrize("kind", ["gan_nar", "gan_conv"])
def test_v2_kinds_that_are_not_vendored_are_rejected_naming_them(
    make_export: Callable[..., Path], kind: str
) -> None:
    export = make_export("gan_ar", meta={"kind": kind})
    with pytest.raises(ValueError, match=rf"kind '{kind}'.*does not vendor.*gan_ar, mdn"):
        LearnedTrajectory(weights_dir=str(export))


def test_an_unknown_v2_kind_is_rejected_naming_it(make_export: Callable[..., Path]) -> None:
    with pytest.raises(ValueError, match=r"unknown kind 'diffusion'.*gan_ar, mdn"):
        LearnedTrajectory(weights_dir=str(make_export("mdn", meta={"kind": "diffusion"})))
    no_kind = make_export("mdn")
    stored = json.loads((no_kind / "meta.json").read_text())
    del stored["kind"]
    (no_kind / "meta.json").write_text(json.dumps(stored))
    with pytest.raises(ValueError, match="no kind"):
        LearnedTrajectory(weights_dir=str(no_kind))


@pytest.mark.parametrize(
    "kind,model_args,match",
    [
        ("mdn", {"hidden": 8, "layers": 2}, r"\['K'\]"),
        ("gan_ar", {"hidden": 8, "nz": 4}, r"\['ne'\]"),
    ],
)
def test_incomplete_model_args_are_rejected(
    make_export: Callable[..., Path], kind: str, model_args: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        LearnedTrajectory(weights_dir=str(make_export(kind, meta={"model_args": model_args})))


@pytest.mark.parametrize(
    "kind,kwargs,missing",
    [
        ("mdn", {"drop_arrays": ["G.lstm.bias_hh_l1"]}, "G.lstm.bias_hh_l1"),
        ("gan_ar", {"drop_arrays": ["len.out.bias"]}, "len.out.bias"),
        # meta promises an idle gate the weights do not have
        ("gan_ar", {"drop_arrays": ["G.gate.weight", "G.gate.bias"]}, "G.gate.weight"),
    ],
)
def test_missing_arrays_are_rejected_at_construction(
    make_export: Callable[..., Path], kind: str, kwargs: dict[str, Any], missing: str
) -> None:
    with pytest.raises(ValueError, match=missing.replace(".", r"\.")):
        LearnedTrajectory(weights_dir=str(make_export(kind, **kwargs)))


def test_missing_numpy_names_the_extra(monkeypatch: pytest.MonkeyPatch, mdn_export: Path) -> None:
    monkeypatch.setitem(sys.modules, "numpy", None)  # type: ignore[arg-type]
    for name in ("_learned_export", "_mouse_gan_v2"):
        monkeypatch.delitem(
            sys.modules, f"spectragent.core.capabilities.browser.input.{name}", raising=False
        )
    with pytest.raises(ImportError, match=r"learned-mouse"):
        LearnedTrajectory(weights_dir=str(mdn_export))


# ── endpoint + timing invariants on the real (tiny) samplers ────────────

_GRID = [
    ((640, 360), (900, 500)),
    ((10, 10), (200, 90)),
    ((1270, 710), (5, 5)),  # long diagonal, beyond the training range
    ((100, 600), (450, 820)),  # off-viewport target on a 1280x720 page
    ((640, 360), (-30, 360)),  # off-viewport to the left
    ((3.4, 7.6), (321.5, 99.49)),  # float inputs
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("start,end", _GRID)
def test_endpoint_is_exact(
    export_of: Callable[[str], Path],
    kind: str,
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    traj = LearnedTrajectory(weights_dir=str(export_of(kind)), seed=11)
    for box in ((60, 24), (0, 0)):
        pts = traj.generate(*start, *end, target_w=box[0], target_h=box[1])
        assert pts
        assert (pts[-1].x, pts[-1].y) == (round(end[0]), round(end[1]))
        assert (pts[0].x, pts[0].y, pts[0].dt_ms) == (round(start[0]), round(start[1]), 0.0)


@pytest.mark.parametrize("kind", KINDS)
def test_timestamps_are_ticks(export_of: Callable[[str], Path], kind: str) -> None:
    traj = LearnedTrajectory(weights_dir=str(export_of(kind)), seed=5)
    assert traj.tick_ms == 20.0
    for _ in range(5):
        pts = traj.generate(100, 100, 700, 400)
        assert pts[0].dt_ms == 0.0
        # One waypoint per tick at exactly 1/hz; still ticks are repeated
        # waypoints, never folded, so the time of every tick survives.
        assert [p.dt_ms for p in pts[1:]] == [20.0] * (len(pts) - 1)
        assert 1 <= len(pts) - 1 <= TINY_SEQ_LEN


def _without(export: Path, key: str) -> Path:
    meta = json.loads((export / "meta.json").read_text())
    del meta[key]
    (export / "meta.json").write_text(json.dumps(meta))
    return export


@pytest.mark.parametrize("kind", KINDS)
def test_the_export_fixes_hz_and_the_training_window(
    make_export: Callable[..., Path], kind: str
) -> None:
    # meta.json records what the weights were made with, so it decides: a
    # config cannot replay a 60 Hz model at 40 Hz, or unnormalise a model
    # trained on an 800 px window by 1000 px — both change exactly the speed
    # the model was selected for, silently.
    declared = LearnedTrajectory(weights_dir=str(make_export(kind, meta={"hz": 60})))
    assert declared.tick_ms == pytest.approx(1000 / 60)
    assert declared._px_per_unit == 800.0

    agreeing = LearnedTrajectory(
        weights_dir=str(make_export(kind, meta={"hz": 60})), hz=60, px_per_unit=800
    )
    assert (agreeing.tick_ms, agreeing._px_per_unit) == (pytest.approx(1000 / 60), 800.0)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "knobs,match",
    [
        ({"hz": 40}, r"^hz=40 contradicts .*meta\.json, which records hz 60\.0 for "),
        (
            {"px_per_unit": 1000},
            r"^px_per_unit=1000 contradicts .*records norm_reference_window 800\.0 for ",
        ),
    ],
    ids=["hz", "px_per_unit"],
)
def test_a_knob_contradicting_the_export_is_rejected_naming_both(
    make_export: Callable[..., Path], kind: str, knobs: dict[str, Any], match: str
) -> None:
    export = make_export(kind, meta={"hz": 60})
    with pytest.raises(ValueError, match=match):
        LearnedTrajectory(weights_dir=str(export), **knobs)


@pytest.mark.parametrize("kind", KINDS)
def test_a_silent_meta_falls_back_to_the_released_recorder(
    make_export: Callable[..., Path], kind: str
) -> None:
    # No claim to contradict: the config decides, else the released data's
    # 50 Hz recorder and 800 px window.
    bare = _without(_without(make_export(kind), "hz"), "norm_reference_window")
    assert LearnedTrajectory(weights_dir=str(bare)).tick_ms == 20.0
    assert LearnedTrajectory(weights_dir=str(bare))._px_per_unit == 800.0
    tuned = LearnedTrajectory(weights_dir=str(bare), hz=40, px_per_unit=1000)
    assert (tuned.tick_ms, tuned._px_per_unit) == (25.0, 1000.0)
    with pytest.raises(ValueError, match="hz must be > 0"):
        LearnedTrajectory(weights_dir=str(_without(make_export(kind), "hz")), hz=0)


def test_a_meta_window_that_is_not_one_square_size_is_rejected(
    make_export: Callable[..., Path],
) -> None:
    with pytest.raises(ValueError, match="800.0x600.0 training window"):
        LearnedTrajectory(
            weights_dir=str(make_export("mdn", meta={"norm_reference_window": [800, 600]}))
        )
    with pytest.raises(ValueError, match=r"norm_reference_window 800"):
        LearnedTrajectory(weights_dir=str(make_export("mdn", meta={"norm_reference_window": 800})))


def test_the_declared_window_is_what_the_path_is_scaled_by(
    make_export: Callable[..., Path],
) -> None:
    # A model normalised by a 400 px window covers half the pixels per unit.
    # Taking the default 800 instead would have doubled every step, silently.
    export = make_export("mdn", meta={"norm_reference_window": [400, 400]})
    sampler = ScriptedSampler([_straight(300, 0, 10)], lengths=[10], unit=400.0)
    traj = _with_sampler(export, sampler)
    assert traj._px_per_unit == 400.0
    pts = traj.generate(0, 0, 300, 0)
    assert sampler.conds[-1][0, 0] == pytest.approx(0.75)  # 300 px of a 400 px window
    assert (pts[1].x, pts[-1].x) == (30, 300)


def test_jitter_perturbs_ticks_within_bounds(mdn_export: Path) -> None:
    sampler = ScriptedSampler([_straight(400, 0, 20)], lengths=[20])
    traj = _with_sampler(mdn_export, sampler, seed=1, jitter=0.2)
    pts = traj.generate(100, 100, 500, 100)
    dts = [p.dt_ms for p in pts[1:]]
    assert len(dts) == 20
    assert all(20.0 * 0.8 - 1e-9 <= dt <= 20.0 * 1.2 + 1e-9 for dt in dts)
    assert len(set(dts)) > 1


@pytest.mark.parametrize("kind", KINDS)
def test_determinism_with_seed(export_of: Callable[[str], Path], kind: str) -> None:
    export = export_of(kind)

    def run(seed: int) -> list[TrajectoryPoint]:
        traj = LearnedTrajectory(weights_dir=str(export), seed=seed, jitter=0.1)
        return traj.generate(50, 60, 800, 500) + traj.generate(800, 500, 200, 650)

    assert run(42) == run(42)
    assert run(42) != run(43)


# ── policies shared by every export, with a scripted sampler ────────────


def test_perfect_path_is_replayed_untouched(mdn_export: Path) -> None:
    sampler = ScriptedSampler([_straight(300, 120, 12)], lengths=[12])
    traj = _with_sampler(mdn_export, sampler)
    pts = traj.generate(100, 100, 400, 220)
    assert [(p.x, p.y) for p in pts] == [
        (round(100 + 25 * k), round(100 + 10 * k)) for k in range(13)
    ]
    assert [p.dt_ms for p in pts] == [0.0] + [20.0] * 12


def test_endpoint_correction_is_smooth_and_eases_into_target(mdn_export: Path) -> None:
    # The model undershoots by 100 px; the correction must not add a jump.
    sampler = ScriptedSampler([_straight(300, 0, 20)], lengths=[20])
    traj = _with_sampler(mdn_export, sampler)
    pts = traj.generate(0, 300, 400, 300)
    xs = [p.x for p in pts]
    assert xs[-1] == 400
    steps = np.diff(xs)
    assert (steps >= 0).all()  # still monotone toward the target
    assert steps.max() <= 15 + 100 * 1.5 * 1.5 / 20 + 1  # 15 px/tick + peak correction slope
    # Final step stays near the model's 15 px: a linear spread would add 5 px
    # there and a (k/n)**2 spread ~10 px — a visible lunge into the target.
    assert steps[-1] <= 18


@pytest.mark.parametrize("kind", KINDS)
def test_long_move_is_conditioned_in_range_and_stretched(
    export_of: Callable[[str], Path], kind: str
) -> None:
    max_offset = 0.95
    # The scripted model lands exactly on whatever in-range offset it is given.
    in_range_px = (0.95 * 800 * 0.6, 0.95 * 800 * 0.8)  # |offset| == 0.95 units
    sampler = ScriptedSampler([_straight(*in_range_px, 16)], lengths=[16])
    traj = _with_sampler(export_of(kind), sampler, max_offset=max_offset)
    start, dx, dy = (10.0, 10.0), 1200.0 * 0.6, 1200.0 * 0.8  # 1200 px == 1.5 units
    pts = traj.generate(start[0], start[1], start[0] + dx, start[1] + dy)

    cond = sampler.conds[-1]
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


def test_in_range_move_is_not_rescaled(mdn_export: Path) -> None:
    sampler = ScriptedSampler([_straight(400, 0, 10)], lengths=[10])
    traj = _with_sampler(mdn_export, sampler)
    traj.generate(100, 100, 500, 100)
    assert sampler.conds[-1][0, :2] == pytest.approx([0.5, 0.0])


@pytest.mark.parametrize("kind", KINDS)
def test_button_size_policy(export_of: Callable[[str], Path], kind: str) -> None:
    cases: list[tuple[Literal["training", "target"], tuple[float, float], tuple[float, float]]] = [
        ("training", (60.0, 24.0), TRAINING_BUTTON_PX),
        ("target", (60.0, 24.0), (60.0, 24.0)),
        ("target", (0.0, 0.0), TRAINING_BUTTON_PX),  # unknown box falls back
    ]
    for policy, box, expected in cases:
        sampler = ScriptedSampler([_straight(400, 0, 10)], lengths=[10])
        traj = _scripted(
            LearnedTrajectory(weights_dir=str(export_of(kind)), button_size=policy), sampler
        )
        traj.generate(100, 100, 500, 100, target_w=box[0], target_h=box[1])
        got = tuple(sampler.conds[-1][0, 2:] * 800)
        assert got == pytest.approx(expected, rel=1e-5), policy


def test_every_candidate_shares_one_condition(gan_ar_export: Path) -> None:
    sampler = ScriptedSampler([_straight(400, 0, 10)], lengths=[10])
    traj = _scripted(LearnedTrajectory(weights_dir=str(gan_ar_export), num_candidates=5), sampler)
    traj.generate(0, 0, 400, 0)
    cond = sampler.conds[-1]
    assert cond.shape == (5, 4) and cond.dtype == np.float32
    assert (cond == cond[0]).all()


def test_still_ticks_stay_as_repeated_waypoints(mdn_export: Path) -> None:
    path = [(30.0, 0.0)] * 5 + [(0.6, 0.0)] + [(0.1, 0.0)] * 3 + [(30.0, 0.0)] * 5
    traj = _with_sampler(mdn_export, ScriptedSampler([path], lengths=[len(path)]))
    end_x = sum(dx for dx, _ in path)
    pts = traj.generate(0, 0, end_x, 0)
    assert (pts[-1].x, pts[-1].y) == (round(end_x), 0)
    assert len(pts) == len(path) + 1
    assert sum(p.dt_ms for p in pts) == pytest.approx(len(path) * 20.0)
    assert any((a.x, a.y) == (b.x, b.y) for a, b in zip(pts, pts[1:], strict=False))


# ── where a path ends: the model's own stop ─────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_the_model_length_is_the_path_including_its_pause(
    export_of: Callable[[str], Path], kind: str
) -> None:
    # 10 moving ticks land on the target, then the model holds still for 5
    # ticks before its stop: the pause is part of the sampled length.
    path = _straight(400, 0, 10) + [(0.0, 0.0)] * 5
    traj = _with_sampler(export_of(kind), ScriptedSampler([path], lengths=[15]))
    pts = traj.generate(100, 100, 500, 100)
    assert len(pts) == 1 + 15
    assert [(p.x, p.y) for p in pts[10:]] == [(500, 100)] * 6
    assert [p.dt_ms for p in pts] == [0.0] + [20.0] * 15


def test_trailing_still_ticks_survive_the_correction(mdn_export: Path) -> None:
    # The model walks to 40 px short of the target and then holds still for 5
    # ticks — its settle before the click, which the /2 training paths run up
    # to and including. Correcting over the whole sampled length turned that
    # settle into 1 px micro-moves; it must stay exactly still, on the target.
    path = _straight(360, 30, 10) + [(0.0, 0.0)] * 5
    traj = _with_sampler(mdn_export, ScriptedSampler([path], lengths=[15]))
    pts = traj.generate(100, 100, 500, 100)

    assert len(pts) == 1 + 15
    assert [(p.x, p.y) for p in pts[-6:]] == [(500, 100)] * 6  # last moving tick + the settle
    assert all(p.dt_ms == 20.0 for p in pts[1:])  # every still tick keeps its time
    # The moving part still eases onto the target rather than jumping.
    steps = [math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts[:10], pts[1:11], strict=True)]
    assert max(steps) <= 50


def test_leading_still_ticks_stay_at_the_start(mdn_export: Path) -> None:
    path = [(0.0, 0.0)] * 4 + _straight(360, 0, 8)
    traj = _with_sampler(mdn_export, ScriptedSampler([path], lengths=[12]))
    pts = traj.generate(100, 100, 500, 100)
    assert [(p.x, p.y) for p in pts[:5]] == [(100, 100)] * 5  # the path's own start, untouched
    assert (pts[-1].x, pts[-1].y) == (500, 100)


def test_a_mid_path_pause_stays_a_pause(mdn_export: Path) -> None:
    # The correction's progress stands still while the model does, so a pause
    # in the middle of a move does not drift either.
    path = _straight(180, 0, 6) + [(0.0, 0.0)] * 4 + _straight(180, 0, 6)
    traj = _with_sampler(mdn_export, ScriptedSampler([path], lengths=[16]))
    pts = traj.generate(100, 100, 500, 100)
    assert len({(p.x, p.y) for p in pts[7:11]}) == 1  # the four paused ticks, one position
    assert (pts[-1].x, pts[-1].y) == (500, 100)


def test_a_still_path_is_corrected_across_its_whole_length(mdn_export: Path) -> None:
    # Nothing to protect: a model that never moves has no moving span, so the
    # correction keeps its smooth ease onto the target.
    traj = _with_sampler(mdn_export, ScriptedSampler([[]], lengths=[6]))
    pts = traj.generate(100, 100, 400, 100)
    xs = [p.x for p in pts]
    assert xs == sorted(xs)
    assert len(set(xs)) > 2
    assert (pts[-1].x, pts[-1].y) == (400, 100)


def test_a_path_through_the_target_is_not_cut_at_arrival(mdn_export: Path) -> None:
    # Overshoots by 80 px (tick index 11) and comes back: the whole correction
    # is the model's, so nothing is truncated where it first crossed the target.
    path = _straight(480, 0, 12) + _straight(-80, 0, 2)
    traj = _with_sampler(mdn_export, ScriptedSampler([path], lengths=[14]))
    pts = traj.generate(100, 100, 500, 100, target_w=60, target_h=24)
    assert len(pts) == 1 + 14
    assert max(p.x for p in pts) == 580
    assert (pts[-1].x, pts[-1].y) == (500, 100)


def test_the_first_stopped_candidate_ending_inside_wins(mdn_export: Path) -> None:
    far = _straight(100, 200, 10)
    inside_off_centre = _straight(410, 8, 10)  # inside a 60x24 box, not centred
    dead_centre = _straight(400, 0, 10)
    sampler = ScriptedSampler([far, inside_off_centre, dead_centre], lengths=[10, 10, 10])
    traj = _with_sampler(mdn_export, sampler, num_candidates=3)
    pts = traj.generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert (pts[1].x, pts[1].y) == (41, 1)
    assert len(pts) == 11


def test_a_candidate_that_never_stopped_is_passed_over(mdn_export: Path) -> None:
    # Row 0 lands dead centre but used the whole horizon (length == T): the
    # stop head never fired. Row 1 stopped 40 px off the box; it wins.
    never_stopped = _straight(400, 0, 10)
    stopped_near_miss = _straight(380, 40, 10)
    sampler = ScriptedSampler([never_stopped, stopped_near_miss], lengths=[TINY_SEQ_LEN, 10])
    traj = _with_sampler(mdn_export, sampler, num_candidates=2)
    pts = traj.generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert len(pts) == 11
    assert pts[1].y == pytest.approx(4, abs=1)  # the near miss, corrected

    # Had it stopped, the dead-centre row would have won.
    sampler = ScriptedSampler([never_stopped, stopped_near_miss], lengths=[10, 10])
    pts = _with_sampler(mdn_export, sampler, num_candidates=2).generate(
        0, 0, 400, 0, target_w=60, target_h=24
    )
    assert (pts[1].x, pts[1].y) == (40, 0)


def test_without_a_candidate_inside_the_closest_end_wins(gan_ar_export: Path) -> None:
    far = _straight(100, 200, 10)
    near_miss = _straight(380, 40, 10)
    sampler = ScriptedSampler([far, near_miss], lengths=[10, 10])
    traj = _with_sampler(gan_ar_export, sampler, num_candidates=2)
    pts = traj.generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert len(pts) == 11
    assert pts[1].y == pytest.approx(4, abs=1)


def test_a_gan_ar_length_at_the_horizon_is_an_ordinary_draw(
    gan_ar_export: Path, mdn_export: Path
) -> None:
    # ``gan_ar`` draws L up front from a length head capped at max_seq_len, so
    # L == max_seq_len is a legitimate long path, not a model that never
    # stopped: it competes with the rest instead of being passed over. Only a
    # per-tick stop head (``mdn``) can fail to fire — see
    # ``test_a_candidate_that_never_stopped_is_passed_over``.
    stopped_near_miss = _straight(380, 40, 10)
    full_horizon_inside = _straight(400, 0, TINY_SEQ_LEN)
    sampler = ScriptedSampler([stopped_near_miss, full_horizon_inside], lengths=[10, TINY_SEQ_LEN])
    pts = _with_sampler(gan_ar_export, sampler, num_candidates=2).generate(
        0, 0, 400, 0, target_w=60, target_h=24
    )
    assert len(pts) == 1 + TINY_SEQ_LEN
    assert (pts[1].x, pts[1].y) == (13, 0)  # the full-horizon candidate, 400/30 px a tick

    # An mdn export sampling exactly the same batch passes it over.
    sampler = ScriptedSampler([stopped_near_miss, full_horizon_inside], lengths=[10, TINY_SEQ_LEN])
    pts = _with_sampler(mdn_export, sampler, num_candidates=2).generate(
        0, 0, 400, 0, target_w=60, target_h=24
    )
    assert len(pts) == 11


def test_when_no_candidate_stopped_the_closest_full_horizon_path_is_used(mdn_export: Path) -> None:
    far = _straight(100, 200, TINY_SEQ_LEN)
    near = _straight(390, 6, TINY_SEQ_LEN)
    sampler = ScriptedSampler([far, near], lengths=[TINY_SEQ_LEN, TINY_SEQ_LEN])
    traj = _with_sampler(mdn_export, sampler, num_candidates=2)
    pts = traj.generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert len(pts) == 1 + TINY_SEQ_LEN  # the whole horizon, as sampled
    assert (pts[1].x, pts[1].y) == (13, 0)
    assert (pts[-1].x, pts[-1].y) == (400, 0)


def test_without_an_element_box_ends_are_judged_against_the_conditioned_button(
    mdn_export: Path,
) -> None:
    # Row 0 ends 40 px past the target: inside the 109x34 training button the
    # model aimed at, outside a 60x24 element box. Row 1 ends dead centre.
    past = _straight(440, 0, 10)
    centre = _straight(400, 0, 10)
    raw_click = _with_sampler(
        mdn_export, ScriptedSampler([past, centre], lengths=[10, 10]), num_candidates=2
    ).generate(0, 0, 400, 0)
    assert raw_click[1].x == 44
    boxed = _with_sampler(
        mdn_export, ScriptedSampler([past, centre], lengths=[10, 10]), num_candidates=2
    ).generate(0, 0, 400, 0, target_w=60, target_h=24)
    assert boxed[1].x == 40


# ── degenerate input ────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_zero_distance_is_a_single_point(export_of: Callable[[str], Path], kind: str) -> None:
    traj = LearnedTrajectory(weights_dir=str(export_of(kind)))
    assert traj.generate(42, 42, 42, 42) == [TrajectoryPoint(42, 42, 0.0)]
    assert traj.generate(42.2, 42, 42.6, 42.3) == [TrajectoryPoint(43, 42, 0.0)]
    assert traj._sampler is None  # never touched the model


@pytest.mark.parametrize("kind", KINDS)
def test_tiny_move_is_one_tick(export_of: Callable[[str], Path], kind: str) -> None:
    traj = LearnedTrajectory(weights_dir=str(export_of(kind)))
    assert traj.generate(100, 100, 102, 101) == [
        TrajectoryPoint(100, 100, 0.0),
        TrajectoryPoint(102, 101, 20.0),
    ]
    assert traj._sampler is None


def test_model_that_never_moves_still_lands(mdn_export: Path) -> None:
    # The model keeps its sampled length and eases onto the target.
    held = _scripted(
        LearnedTrajectory(weights_dir=str(mdn_export)), ScriptedSampler([[]], lengths=[5])
    )
    pts = held.generate(10, 10, 300, 200)
    assert len(pts) == 6
    assert (pts[-1].x, pts[-1].y) == (300, 200)
    xs = [p.x for p in pts]
    assert xs == sorted(xs)


def test_non_finite_output_is_an_error(mdn_export: Path) -> None:
    class NanSampler(ScriptedSampler):
        def draw(self, cond: np.ndarray, rng: np.random.Generator) -> Draw:
            drawn = super().draw(cond, rng)
            return Draw(moves=np.full_like(drawn.moves, np.nan), lengths=drawn.lengths)

    traj = _scripted(LearnedTrajectory(weights_dir=str(mdn_export)), NanSampler([[]], lengths=[10]))
    with pytest.raises(ValueError, match="non-finite"):
        traj.generate(10, 10, 300, 200)


# ── the real exports, when this machine has them ────────────────────────


@pytest.mark.parametrize(
    "export",
    [V2_MDN_EXPORT, V2_GAN_AR_EXPORT],
    ids=["v2-mdn", "v2-gan_ar"],
)
def test_real_exports_generate_exact_viewport_paths(export: Path) -> None:
    # The tick rate is the export's to declare — a number written here would be
    # a test disagreeing with the weights it loads, which is exactly what the
    # strategy refuses from a config.
    if not (export / "generator.npz").is_file():
        pytest.skip(f"trainer export absent: {export}")
    hz = json.loads((export / "meta.json").read_text())["hz"]
    traj = LearnedTrajectory(weights_dir=str(export), seed=0)
    assert (traj.tick_ms, traj._px_per_unit) == (pytest.approx(1000 / hz), 800.0)
    for (sx, sy), (ex, ey) in (((640, 360), (1200, 90)), ((20, 20), (1260, 700))):
        for box in ((80, 30), (0, 0)):
            pts = traj.generate(sx, sy, ex, ey, target_w=box[0], target_h=box[1])
            assert (pts[-1].x, pts[-1].y) == (ex, ey)
            assert 1 < len(pts) <= 101
            assert all(p.dt_ms == traj.tick_ms for p in pts[1:])
