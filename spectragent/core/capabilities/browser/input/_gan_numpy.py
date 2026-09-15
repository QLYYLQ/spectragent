"""Numpy-only forward pass of an exported mouse-path GAN generator.

Vendored from ``mouse-gan-torch`` (``mouse_gan/infer_numpy.py``), the PyTorch
port of the conditional LSTM GAN in SaluRamos/mouse-ai ``train_gan.py``. The
numpy-vs-torch parity test lives upstream and is not repeated here; this copy
only tightens typing and validates the export before the first forward pass,
so a truncated or foreign ``generator.npz`` fails with the missing array's
name instead of a ``KeyError`` deep inside an LSTM step.

Only :mod:`spectragent.core.capabilities.browser.input.gan` imports this
module, and only lazily, so the package — and config generation — keeps
working without numpy installed.

Tensor contract (everything normalised by the 800x800 training window):

* ``noise``: ``(B, latent_dim)``, standard normal.
* ``cond``:  ``(B, 4)`` = ``(offset_x, offset_y, btn_w, btn_h)`` where
  ``offset = (button_centre - cursor) / 800`` and the button size is ``/ 800``.
* output: ``(B, max_seq_len, 2)`` per-tick cursor deltas ``/ 800``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MISSING_NUMPY_MESSAGE = (
    "The 'gan' mouse trajectory needs numpy, which spectragent does not install "
    "by default. Install the optional extra: `uv sync --extra mouse-gan` "
    "(or `pip install 'spectragent[mouse-gan]'`)."
)

try:
    import numpy as np
except ImportError as exc:  # the only place the missing extra is named
    raise ImportError(MISSING_NUMPY_MESSAGE) from exc

EXPORT_FORMAT = "mouse-gan-generator/1"
WEIGHTS_FILE = "generator.npz"
META_FILE = "meta.json"

_LSTM_PREFIXES: tuple[str, ...] = ("bilstm.fw", "bilstm.bw", "lstm1", "lstm2")

REQUIRED_ARRAYS: tuple[str, ...] = (
    "dense1.weight",
    "dense1.bias",
    "bn1.gamma",
    "bn1.beta",
    "bn1.moving_mean",
    "bn1.moving_var",
    "dense2.weight",
    "dense2.bias",
    *(f"{prefix}.{part}" for prefix in _LSTM_PREFIXES for part in ("w_ih", "w_hh", "b")),
    "bn2.gamma",
    "bn2.beta",
    "bn2.moving_mean",
    "bn2.moving_var",
    "td_dense.weight",
    "td_dense.bias",
    "out.weight",
    "out.bias",
)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return np.exp(-np.logaddexp(0.0, -x)).astype(x.dtype, copy=False)


def _leaky_relu(x: np.ndarray, slope: float) -> np.ndarray:
    return np.where(x >= 0, x, x * slope)


def _dense(x: np.ndarray, w: np.ndarray, b: np.ndarray) -> np.ndarray:
    return x @ w.T + b


def _batch_norm(
    x: np.ndarray,
    gamma: np.ndarray,
    beta: np.ndarray,
    mean: np.ndarray,
    var: np.ndarray,
    eps: float,
) -> np.ndarray:
    inv = gamma / np.sqrt(var + eps)
    return x * inv + (beta - mean * inv)


def _lstm(
    x: np.ndarray, w_ih: np.ndarray, w_hh: np.ndarray, b: np.ndarray, reverse: bool
) -> np.ndarray:
    """Single-direction LSTM, gate order (i, f, g, o), zero initial state, all steps."""
    batch, steps, _ = x.shape
    units = w_hh.shape[1]
    xw = x @ w_ih.T + b  # (B, T, 4u)
    h = np.zeros((batch, units), dtype=x.dtype)
    c = np.zeros((batch, units), dtype=x.dtype)
    out = np.empty((batch, steps, units), dtype=x.dtype)
    for t in range(steps - 1, -1, -1) if reverse else range(steps):
        z = xw[:, t] + h @ w_hh.T
        i = _sigmoid(z[:, :units])
        f = _sigmoid(z[:, units : 2 * units])
        g = np.tanh(z[:, 2 * units : 3 * units])
        o = _sigmoid(z[:, 3 * units :])
        c = f * c + i * g
        h = o * np.tanh(c)
        out[:, t] = h
    return out


class NumpyGenerator:
    """Eval-mode generator: Dense256-leaky, BN, Dense512-leaky, repeat, BiLSTM256,
    BN, LSTM128, LSTM64, per-tick Dense64-leaky, per-tick Dense2-linear."""

    def __init__(self, weights: Mapping[str, np.ndarray], meta: Mapping[str, Any]) -> None:
        missing = [name for name in REQUIRED_ARRAYS if name not in weights]
        if missing:
            raise ValueError(f"GAN generator weights are missing arrays: {missing}")
        self._w = {k: np.asarray(v, dtype=np.float32) for k, v in weights.items()}
        self.latent_dim = int(meta.get("latent_dim", 100))
        self.cond_dim = int(meta.get("cond_dim", 4))
        self.seq_len = int(meta.get("max_seq_len", 100))
        self.feature_dim = int(meta.get("feature_dim", 2))
        self._slope = float(meta.get("leaky_relu_negative_slope", 0.2))
        self._bn_eps = float(meta.get("bn_epsilon", 1e-3))

        expected_in = self.latent_dim + self.cond_dim
        got_in = self._w["dense1.weight"].shape[1]
        if got_in != expected_in:
            raise ValueError(
                f"dense1.weight takes {got_in} inputs but meta declares "
                f"latent_dim + cond_dim = {expected_in}"
            )
        if self._w["out.weight"].shape[0] != self.feature_dim:
            raise ValueError(
                f"out.weight emits {self._w['out.weight'].shape[0]} features but "
                f"meta declares feature_dim = {self.feature_dim}"
            )

    def forward(self, noise: np.ndarray, cond: np.ndarray) -> np.ndarray:
        """``noise (B, latent_dim)``, ``cond (B, cond_dim)`` -> ``(B, seq_len, 2)`` float32."""
        noise = np.asarray(noise, dtype=np.float32)
        cond = np.asarray(cond, dtype=np.float32)
        if noise.ndim != 2 or noise.shape[1] != self.latent_dim:
            raise ValueError(f"noise must be (B, {self.latent_dim}), got {noise.shape}")
        if cond.shape != (noise.shape[0], self.cond_dim):
            raise ValueError(f"cond must be ({noise.shape[0]}, {self.cond_dim}), got {cond.shape}")
        w, s, eps = self._w, self._slope, self._bn_eps
        x = np.concatenate([noise, cond], axis=-1)
        x = _leaky_relu(_dense(x, w["dense1.weight"], w["dense1.bias"]), s)
        x = _batch_norm(
            x, w["bn1.gamma"], w["bn1.beta"], w["bn1.moving_mean"], w["bn1.moving_var"], eps
        )
        x = _leaky_relu(_dense(x, w["dense2.weight"], w["dense2.bias"]), s)
        x = np.repeat(x[:, None, :], self.seq_len, axis=1)
        fw = _lstm(x, w["bilstm.fw.w_ih"], w["bilstm.fw.w_hh"], w["bilstm.fw.b"], reverse=False)
        bw = _lstm(x, w["bilstm.bw.w_ih"], w["bilstm.bw.w_hh"], w["bilstm.bw.b"], reverse=True)
        x = np.concatenate([fw, bw], axis=-1)
        x = _batch_norm(
            x, w["bn2.gamma"], w["bn2.beta"], w["bn2.moving_mean"], w["bn2.moving_var"], eps
        )
        x = _lstm(x, w["lstm1.w_ih"], w["lstm1.w_hh"], w["lstm1.b"], reverse=False)
        x = _lstm(x, w["lstm2.w_ih"], w["lstm2.w_hh"], w["lstm2.b"], reverse=False)
        x = _leaky_relu(_dense(x, w["td_dense.weight"], w["td_dense.bias"]), s)
        return _dense(x, w["out.weight"], w["out.bias"])


def new_rng(seed: int | None) -> np.random.Generator:
    """A fresh, instance-owned random Generator (never the global numpy RNG)."""
    return np.random.default_rng(seed)


def read_meta(path: Path) -> dict[str, Any]:
    """Parse ``meta.json``; an absent file is an empty mapping (upstream default dims)."""
    if not path.is_file():
        return {}
    meta = json.loads(path.read_text())
    if not isinstance(meta, dict):
        raise ValueError(f"{path} is not a JSON object")
    fmt = meta.get("format")
    if fmt is not None and fmt != EXPORT_FORMAT:
        raise ValueError(f"{path} declares format {fmt!r}; expected {EXPORT_FORMAT!r}")
    return meta


def load_generator(export_dir: Path, meta: Mapping[str, Any]) -> NumpyGenerator:
    """Load ``<export_dir>/generator.npz``; ``meta`` is the already-parsed ``meta.json``."""
    with np.load(export_dir / WEIGHTS_FILE) as npz:
        weights = {name: npz[name] for name in npz.files}
    return NumpyGenerator(weights, meta)


def effective_ticks(moves_px: np.ndarray, stop_px: float) -> np.ndarray:
    """Per path, the tick count up to the last tick moving ``>= stop_px`` pixels.

    ``moves_px`` is ``(B, T, 2)`` in pixels; returns ``(B,)`` ints (0 = never moves).
    """
    moving = np.linalg.norm(moves_px, axis=-1) >= stop_px  # (B, T)
    steps = moving.shape[1]
    last = steps - np.argmax(moving[:, ::-1], axis=1)  # index of last True, +1
    return np.where(moving.any(axis=1), last, 0)
