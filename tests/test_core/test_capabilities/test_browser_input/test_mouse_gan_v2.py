"""``mouse-gan-generator/2`` sampler: the vendored copy against the trainer's, and its contract.

Parity runs against mouse-gan-torch ``mouse_gan/v2/infer_numpy_v2.py`` — numpy
only, loaded straight from the trainer checkout — on tiny exports covering
every option the vendored kinds read, and on the real exports; it is skipped
where the trainer project is absent. The contract tests (where a path ends,
what the stop head decides, what construction and loading reject) need nothing
but the tiny fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from spectragent.core.capabilities.browser.input import _mouse_gan_v2 as v2
from spectragent.core.capabilities.browser.input._learned_export import ExportInfo

from .conftest import TINY_SEQ_LEN, TRAINER_ROOT, V2_GAN_AR_EXPORT, V2_MDN_EXPORT

REFERENCE = TRAINER_ROOT / "mouse_gan" / "v2" / "infer_numpy_v2.py"

#: Tiny exports covering each option the vendored kinds read from meta.json.
VARIANTS: dict[str, tuple[str, dict[str, Any]]] = {
    "mdn": ("mdn", {}),
    "mdn-continuous-temperature": (
        "mdn",
        {"sampling": {"temperature": 0.8, "rounding": False}},
    ),
    "mdn-one-layer-more-components": ("mdn", {"layers": 1, "K": 5}),
    "mdn-long-rollouts": ("mdn", {"stop_bias": -6.0}),
    "gan_ar-idle-gate": ("gan_ar", {}),
    "gan_ar-no-gate": ("gan_ar", {"idle_gate": False}),
    "gan_ar-round-positions": ("gan_ar", {"round_positions": True}),
    "gan_ar-no-tick-noise": ("gan_ar", {"ne": 0}),
}


@pytest.fixture(scope="module")
def reference() -> ModuleType:
    if not REFERENCE.is_file():
        pytest.skip(f"trainer reference sampler absent: {REFERENCE}")
    spec = importlib.util.spec_from_file_location("mouse_gan_reference_infer_numpy_v2", REFERENCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _meta(export: Path) -> dict[str, Any]:
    return json.loads((export / "meta.json").read_text())


def _load(export: Path) -> v2.MdnSampler | v2.GanArSampler:
    meta = _meta(export)
    v2.inspect(export, meta)
    return v2.load(export, meta)


def _conds(batch: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    offsets = rng.uniform(-0.9, 0.9, (batch, 2))
    button = np.tile([[109 / 800, 34 / 800]], (batch, 1))
    return np.concatenate([offsets, button], axis=1).astype(np.float32)


def _assert_matches_reference(
    ours: v2.MdnSampler | v2.GanArSampler, theirs: Any, cond: np.ndarray
) -> None:
    batch = len(cond)
    # The same noise, drawn the same way from the same Generator state ...
    our_noise = ours.noise(batch, np.random.default_rng(3))
    their_noise = theirs.draw_noise(batch, np.random.default_rng(3))
    assert list(our_noise) == list(their_noise)
    for name, value in our_noise.items():
        assert value.dtype == their_noise[name].dtype
        assert np.array_equal(value, their_noise[name]), name
    # ... gives the same rollout, bit for bit ...
    moves, lengths = ours.forward(cond, our_noise)
    expected = theirs.forward(cond, their_noise)
    assert np.array_equal(lengths, expected.lengths)
    assert np.array_equal(moves, expected.moves)
    # ... and a seeded draw is the trainer's seeded sample.
    drawn = ours.draw(cond, np.random.default_rng(17))
    sampled = theirs.sample(cond, np.random.default_rng(17))
    assert np.array_equal(drawn.lengths, sampled.lengths)
    assert np.array_equal(drawn.moves, sampled.moves)


# ── parity with the trainer ─────────────────────────────────────────────


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_tiny_exports_match_the_trainer_sampler(
    reference: ModuleType, make_export: Callable[..., Path], variant: str
) -> None:
    kind, kwargs = VARIANTS[variant]
    export = make_export(kind, **kwargs)
    ours = _load(export)
    theirs = reference.load_generator_v2(export)
    _assert_matches_reference(ours, theirs, _conds(24, 5))


@pytest.mark.parametrize("real", [V2_MDN_EXPORT, V2_GAN_AR_EXPORT], ids=["mdn", "gan_ar"])
def test_real_exports_match_the_trainer_sampler(reference: ModuleType, real: Path) -> None:
    if not (real / "generator.npz").is_file():
        pytest.skip(f"trainer export absent: {real}")
    _assert_matches_reference(_load(real), reference.load_generator_v2(real), _conds(32, 7))


@pytest.mark.parametrize(
    "kind,real", [("mdn", V2_MDN_EXPORT), ("gan_ar", V2_GAN_AR_EXPORT)], ids=["mdn", "gan_ar"]
)
def test_tiny_fixtures_mirror_the_real_export_layout(
    make_export: Callable[..., Path], kind: str, real: Path
) -> None:
    if not (real / "generator.npz").is_file():
        pytest.skip(f"trainer export absent: {real}")
    tiny = make_export(kind)
    with np.load(tiny / "generator.npz") as ours, np.load(real / "generator.npz") as theirs:
        assert sorted(ours.files) == sorted(theirs.files)
        for name in ours.files:
            assert ours[name].ndim == theirs[name].ndim, name
            assert ours[name].dtype == theirs[name].dtype, name
        real_files = sorted(theirs.files)
    tiny_meta, real_meta = _meta(tiny), _meta(real)
    assert set(tiny_meta) <= set(real_meta)
    assert set(tiny_meta["model_args"]) == set(real_meta["model_args"])
    assert set(tiny_meta["sampling"]) == set(real_meta["sampling"])
    assert (tiny_meta["format"], tiny_meta["kind"]) == (real_meta["format"], real_meta["kind"])
    # Construction asks for exactly the arrays the exporter writes.
    assert sorted(v2.required_arrays(kind, real_meta["model_args"])) == real_files


# ── contract ────────────────────────────────────────────────────────────


def test_inspect_reports_the_kind_and_how_the_model_stops(
    mdn_export: Path, gan_ar_export: Path
) -> None:
    # Both kinds decide where a path ends, but only ``mdn`` decides it tick by
    # tick (a Bernoulli stop head); ``gan_ar`` draws the whole length up front,
    # so a length at the horizon is a capped draw rather than a runaway.
    assert v2.inspect(mdn_export, _meta(mdn_export)) == ExportInfo(
        kind="mdn", stop_is_per_tick=True
    )
    assert v2.inspect(gan_ar_export, _meta(gan_ar_export)) == ExportInfo(
        kind="gan_ar", stop_is_per_tick=False
    )


@pytest.mark.parametrize("kind", ["mdn", "gan_ar"])
def test_paths_are_zero_after_their_length(make_export: Callable[..., Path], kind: str) -> None:
    sampler = _load(make_export(kind))
    drawn = sampler.draw(_conds(64, 1), np.random.default_rng(0))
    assert drawn.moves.shape == (64, TINY_SEQ_LEN, 2)
    assert drawn.moves.dtype == np.float32
    assert drawn.lengths.dtype == np.int64
    assert ((drawn.lengths >= 1) & (drawn.lengths <= TINY_SEQ_LEN)).all()
    assert len(set(drawn.lengths.tolist())) > 1
    for row, n in enumerate(drawn.lengths):
        assert (drawn.moves[row, n:] == 0).all()
        assert (drawn.moves[row, :n] != 0).any()


def test_mdn_moves_are_integer_pixels_unless_rounding_is_off(
    make_export: Callable[..., Path],
) -> None:
    rounded = _load(make_export("mdn")).draw(_conds(16, 2), np.random.default_rng(0)).moves * 800
    assert np.allclose(rounded, np.rint(rounded), atol=1e-3)
    continuous = (
        _load(make_export("mdn", sampling={"temperature": 1.0, "rounding": False}))
        .draw(_conds(16, 2), np.random.default_rng(0))
        .moves
        * 800
    )
    assert not np.allclose(continuous, np.rint(continuous), atol=1e-3)


def test_mdn_path_ends_after_the_tick_whose_u_stop_falls_below_the_stop_probability(
    make_export: Callable[..., Path],
) -> None:
    # A stop bias of +40 makes sigmoid(stop_logit) exactly 1 in float32, so a
    # tick ends the path iff its u_stop < 1.
    sampler = _load(make_export("mdn", stop_bias=40.0))
    assert isinstance(sampler, v2.MdnSampler)
    noise = sampler.noise(3, np.random.default_rng(0))
    noise["u_stop"][:] = 0.0
    noise["u_stop"][1, :4] = 1.0  # row 1 cannot stop before tick index 4
    noise["u_stop"][2, :] = 1.0  # row 2 never stops
    moves, lengths = sampler.forward(_conds(3, 4), noise)
    assert lengths.tolist() == [1, 5, TINY_SEQ_LEN]
    assert (moves[0, 1:] == 0).all()
    assert (moves[1, 5:] == 0).all()


@pytest.mark.parametrize("stop_bias,length", [(-40.0, TINY_SEQ_LEN), (40.0, 1)])
def test_the_mdn_stop_head_alone_decides_the_length(
    make_export: Callable[..., Path], stop_bias: float, length: int
) -> None:
    drawn = _load(make_export("mdn", stop_bias=stop_bias)).draw(
        _conds(16, 2), np.random.default_rng(0)
    )
    assert (drawn.lengths == length).all()


def test_gan_ar_length_is_an_inverse_cdf_draw_from_the_length_head(gan_ar_export: Path) -> None:
    sampler = _load(gan_ar_export)
    assert isinstance(sampler, v2.GanArSampler)
    cond = _conds(4, 3)
    probs = np.exp(sampler.length_log_probs(cond).astype(np.float64))
    assert probs.shape == (4, TINY_SEQ_LEN)
    assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-5)
    cdf = np.cumsum(probs, axis=1)
    assert (cdf[:, 6] > cdf[:, 5]).all()
    assert sampler.sample_lengths(cond, np.zeros(4)).tolist() == [1] * 4
    between = (cdf[:, 5] + cdf[:, 6]) / 2  # past L=6's cumulative mass, short of L=7's
    assert sampler.sample_lengths(cond, between).tolist() == [7] * 4


def test_the_idle_gate_can_hold_the_cursor_exactly_still(make_export: Callable[..., Path]) -> None:
    export = make_export("gan_ar")
    with np.load(export / "generator.npz") as npz:
        arrays = {name: npz[name] for name in npz.files}
    arrays["G.gate.weight"] = np.zeros_like(arrays["G.gate.weight"])
    arrays["G.gate.bias"] = np.array([-1.0], np.float32)  # clip(-1, 0, 1) == 0 every tick
    np.savez(export / "generator.npz", **arrays)  # type: ignore[arg-type]
    drawn = _load(export).draw(_conds(8, 1), np.random.default_rng(0))
    assert (drawn.moves == 0).all()


@pytest.mark.parametrize("kind", ["mdn", "gan_ar"])
def test_draws_are_reproducible_from_the_generator_state(
    make_export: Callable[..., Path], kind: str
) -> None:
    sampler = _load(make_export(kind))
    cond = _conds(8, 0)
    a = sampler.draw(cond, np.random.default_rng(9))
    b = sampler.draw(cond, np.random.default_rng(9))
    c = sampler.draw(cond, np.random.default_rng(10))
    assert np.array_equal(a.moves, b.moves)
    assert np.array_equal(a.lengths, b.lengths)
    assert not np.array_equal(a.moves, c.moves)


@pytest.mark.parametrize(
    "kind,model_args,array",
    [
        # arrays written for K=3, meta claims K=4
        ("mdn", {"hidden": 8, "layers": 2, "K": 4}, "G.head2.weight"),
        # arrays written for nz=4, meta claims nz=5
        (
            "gan_ar",
            {"hidden": 8, "nz": 5, "ne": 3, "idle_gate": True, "len_K": 2},
            "G.init.weight",
        ),
    ],
)
def test_load_names_an_array_whose_shape_disagrees_with_model_args(
    make_export: Callable[..., Path], kind: str, model_args: dict[str, Any], array: str
) -> None:
    export = make_export(kind, meta={"model_args": model_args})
    meta = _meta(export)
    v2.inspect(export, meta)  # names are all there
    with pytest.raises(ValueError, match=re.escape(array) + " has shape"):
        v2.load(export, meta)
