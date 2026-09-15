"""Shared fixtures for the browser input layer.

``gan_export`` writes a tiny, randomly initialised generator in the
``mouse-gan-generator/1`` export format (same array names and layer wiring as
``mouse_gan.export``, much smaller widths) so the ``gan`` strategy's tests
never depend on the real ~10 MB weights.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

TINY_LATENT = 8
TINY_SEQ_LEN = 30


def write_tiny_gan_export(
    directory: Path,
    *,
    meta: dict[str, Any] | None = None,
    write_meta: bool = True,
    seed: int = 0,
) -> Path:
    """Write ``generator.npz`` (+ ``meta.json``) with tiny random weights."""
    rng = np.random.default_rng(seed)
    cond_dim, h1, h2, u, u1, u2, d = 4, 16, 16, 8, 8, 8, 8

    def mat(rows: int, cols: int) -> np.ndarray:
        return (rng.standard_normal((rows, cols)) / np.sqrt(cols)).astype(np.float32)

    def vec(n: int, scale: float = 0.1) -> np.ndarray:
        return (rng.standard_normal(n) * scale).astype(np.float32)

    arrays: dict[str, np.ndarray] = {
        "dense1.weight": mat(h1, TINY_LATENT + cond_dim),
        "dense1.bias": vec(h1),
        "bn1.gamma": np.ones(h1, np.float32),
        "bn1.beta": np.zeros(h1, np.float32),
        "bn1.moving_mean": np.zeros(h1, np.float32),
        "bn1.moving_var": np.ones(h1, np.float32),
        "dense2.weight": mat(h2, h1),
        "dense2.bias": vec(h2),
        "bn2.gamma": np.ones(2 * u, np.float32),
        "bn2.beta": np.zeros(2 * u, np.float32),
        "bn2.moving_mean": np.zeros(2 * u, np.float32),
        "bn2.moving_var": np.ones(2 * u, np.float32),
        "td_dense.weight": mat(d, u2),
        "td_dense.bias": vec(d),
        "out.weight": mat(2, d) * 0.05,
        "out.bias": vec(2, 0.01),
    }
    for prefix, units, inputs in (
        ("bilstm.fw", u, h2),
        ("bilstm.bw", u, h2),
        ("lstm1", u1, 2 * u),
        ("lstm2", u2, u1),
    ):
        arrays[f"{prefix}.w_ih"] = mat(4 * units, inputs)
        arrays[f"{prefix}.w_hh"] = mat(4 * units, units)
        arrays[f"{prefix}.b"] = vec(4 * units)

    directory.mkdir(parents=True, exist_ok=True)
    np.savez(directory / "generator.npz", **arrays)  # type: ignore[arg-type]
    if write_meta:
        full_meta: dict[str, Any] = {
            "format": "mouse-gan-generator/1",
            "latent_dim": TINY_LATENT,
            "max_seq_len": TINY_SEQ_LEN,
            "feature_dim": 2,
            "cond_dim": cond_dim,
            "hz": 50,
            "norm_reference_window": [800, 800],
            "leaky_relu_negative_slope": 0.2,
            "bn_epsilon": 0.001,
        }
        full_meta.update(meta or {})
        (directory / "meta.json").write_text(json.dumps(full_meta))
    return directory


@pytest.fixture
def gan_export(tmp_path: Path) -> Path:
    return write_tiny_gan_export(tmp_path / "export")


@pytest.fixture
def make_gan_export(tmp_path: Path) -> Callable[..., Path]:
    counter = iter(range(1_000_000))

    def make(**kwargs: Any) -> Path:
        return write_tiny_gan_export(tmp_path / f"export_{next(counter)}", **kwargs)

    return make
