"""``mouse-gan-generator/1`` forward pass: the vendored copy against the trainer's, and its contract.

Parity runs against mouse-gan-torch ``mouse_gan/infer_numpy.py`` (numpy only,
loaded straight from the trainer checkout) on a tiny export and the real smoke
export; it is skipped where the trainer project is absent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from spectragent.core.capabilities.browser.input import _mouse_gan_v1 as v1
from spectragent.core.capabilities.browser.input._learned_export import ExportInfo

from .conftest import TINY_LATENT, TINY_SEQ_LEN, TRAINER_ROOT, V1_SMOKE_EXPORT

REFERENCE = TRAINER_ROOT / "mouse_gan" / "infer_numpy.py"


@pytest.fixture(scope="module")
def reference() -> ModuleType:
    if not REFERENCE.is_file():
        pytest.skip(f"trainer reference forward pass absent: {REFERENCE}")
    spec = importlib.util.spec_from_file_location("mouse_gan_reference_infer_numpy", REFERENCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _meta(export: Path) -> dict[str, Any]:
    return json.loads((export / "meta.json").read_text())


def _conds(batch: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    offsets = rng.uniform(-0.9, 0.9, (batch, 2))
    button = np.tile([[109 / 800, 34 / 800]], (batch, 1))
    return np.concatenate([offsets, button], axis=1).astype(np.float32)


@pytest.mark.parametrize("which", ["tiny", "smoke"])
def test_forward_matches_the_trainer(reference: ModuleType, v1_export: Path, which: str) -> None:
    export = v1_export if which == "tiny" else V1_SMOKE_EXPORT
    if not (export / "generator.npz").is_file():
        pytest.skip(f"trainer export absent: {export}")
    ours = v1.load(export, _meta(export))
    theirs = reference.load_generator(export)
    noise = np.random.default_rng(0).standard_normal((8, ours.latent_dim)).astype(np.float32)
    cond = _conds(8, 1)
    assert np.array_equal(ours.forward(noise, cond), theirs.forward(noise, cond))


def test_required_arrays_are_what_the_exporter_writes() -> None:
    if not (V1_SMOKE_EXPORT / "generator.npz").is_file():
        pytest.skip(f"trainer export absent: {V1_SMOKE_EXPORT}")
    with np.load(V1_SMOKE_EXPORT / "generator.npz") as npz:
        assert sorted(v1.REQUIRED_ARRAYS) == sorted(npz.files)


def test_inspect_reports_no_stop_signal(v1_export: Path) -> None:
    assert v1.inspect(v1_export, _meta(v1_export)) == ExportInfo(kind=None, models_stop=False)


def test_draw_is_one_latent_per_row_and_no_lengths(v1_export: Path) -> None:
    generator = v1.load(v1_export, _meta(v1_export))
    cond = _conds(5, 2)
    drawn = generator.draw(cond, np.random.default_rng(4))
    assert drawn.lengths is None
    assert drawn.moves.shape == (5, TINY_SEQ_LEN, 2)
    noise = np.random.default_rng(4).standard_normal((5, TINY_LATENT), dtype=np.float32)
    assert np.array_equal(drawn.moves, generator.forward(noise, cond))


def test_generator_names_missing_arrays_and_mismatched_dims(v1_export: Path) -> None:
    with np.load(v1_export / "generator.npz") as npz:
        weights = {name: npz[name] for name in npz.files}
    meta = _meta(v1_export)
    partial = {name: value for name, value in weights.items() if name != "bn2.gamma"}
    with pytest.raises(ValueError, match=r"missing arrays: \['bn2.gamma'\]"):
        v1.NumpyGenerator(partial, meta)
    with pytest.raises(ValueError, match="dense1.weight takes 12 inputs"):
        v1.NumpyGenerator(weights, {**meta, "latent_dim": TINY_LATENT + 1})
