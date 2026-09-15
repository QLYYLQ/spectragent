"""Numpy sampler for ``mouse-gan-generator/2`` exports: kinds ``mdn`` and ``gan_ar``.

Vendored from mouse-gan-torch ``mouse_gan/v2/infer_numpy_v2.py`` with the op
order unchanged, so ``test_mouse_gan_v2`` can demand identical lengths and
moves from that file for identical noise whenever the trainer project is on
the machine (numpy-vs-torch parity is proven upstream). Only the kinds with
exports worth running are copied:

* ``mdn`` — an LSTM over [previous integer-px move, remaining offset, t, cond
  features] emits a mixture of K bivariate Gaussians over the next move, which
  is rounded to integer pixels, and a Bernoulli stop head: the path ends after
  tick t when ``u_stop[t] < sigmoid(stop_logit_t)``. The chosen v2 model.
* ``gan_ar`` — a length head samples the tick count L first (a mixture of
  discretised logistics over log L), then two GRU cells emit moves for t < L,
  optionally through an idle gate that can hold the cursor exactly still.

The trainer's ``gan_nar`` (BiGRU) and ``gan_conv`` (dilated Conv1d) kinds are
not vendored: they were screening arms that mostly missed their targets (val
hit 0.09 and 0.63 against the human 0.94) and nothing exports them. An export
of either, or of any other kind, is rejected at construction by name.

Contract of both kinds (the same condition as format /1):

* ``cond``: ``(B, 4)`` float32 ``(offset_x, offset_y, btn_w, btn_h) / 800``.
* output: ``moves`` ``(B, max_seq_len, 2)`` float32 deltas ``/ 800``, exactly 0
  from tick ``lengths[i]`` on, and ``lengths`` ``(B,)`` int64 in
  ``1..max_seq_len``; the click follows tick ``lengths[i] - 1``. A length of
  ``max_seq_len`` means the path used the whole horizon — for ``mdn``, that the
  stop head never fired.
* noise is drawn from the caller's Generator in the trainer's order and way
  (``noise``), so a seed reproduces the trainer's ``GeneratorV2.sample``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from spectragent.core.capabilities.browser.input._learned_export import (
    META_FILE,
    WEIGHTS_FILE,
    Draw,
    ExportInfo,
    require_arrays,
)

FORMAT: str = "mouse-gan-generator/2"
#: Kinds this module samples.
KINDS: tuple[str, ...] = ("gan_ar", "mdn")
#: Kinds the trainer's sampler knows and this copy deliberately leaves out.
NOT_VENDORED: tuple[str, ...] = ("gan_conv", "gan_nar")

WINDOW = 800.0
T_MAX = 100
MOVE_TO_NORM = 50.0 / 800.0  # networks emit px / 50
LEAK = 0.2
MDN_UNIT_PX = 10.0  # the MDN's Gaussians live in px / 10
MDN_LOG_SIGMA_MIN = math.log(0.02)
COND_FEATURES = 7

_REQUIRED_MODEL_ARGS: dict[str, tuple[str, ...]] = {
    "mdn": ("hidden", "K"),
    "gan_ar": ("hidden", "nz", "ne"),
}


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return np.exp(-np.logaddexp(0.0, -x)).astype(x.dtype, copy=False)


def _logsig(x: np.ndarray) -> np.ndarray:
    return (-np.logaddexp(0.0, -x)).astype(x.dtype, copy=False)


def _leaky(x: np.ndarray) -> np.ndarray:
    return np.where(x >= 0, x, x * LEAK)


def _log_softmax(x: np.ndarray, axis: int) -> np.ndarray:
    m = x.max(axis=axis, keepdims=True)
    return x - m - np.log(np.exp(x - m).sum(axis=axis, keepdims=True))


def _logsumexp(x: np.ndarray, axis: int) -> np.ndarray:
    m = x.max(axis=axis, keepdims=True)
    return (m + np.log(np.exp(x - m).sum(axis=axis, keepdims=True))).squeeze(axis)


def cond_features(cond: np.ndarray) -> np.ndarray:
    """``(B, 4) -> (B, 7)``: ``[2*off, 2*|off|, off/max(|off|, 1e-4), 5*btn]``."""
    off, btn = cond[:, :2], cond[:, 2:4]
    dist = np.sqrt((off * off).sum(axis=1, keepdims=True))
    unit = off / np.maximum(dist, 1e-4)
    return np.concatenate([2 * off, 2 * dist, unit, 5 * btn], axis=1).astype(cond.dtype, copy=False)


def _dense_arrays(*layers: str) -> list[str]:
    return [f"{layer}.{part}" for layer in layers for part in ("weight", "bias")]


def required_arrays(kind: str, model_args: Mapping[str, Any]) -> list[str]:
    """Array names an export of ``kind`` holds (torch state_dict keys under ``G.``/``len.``)."""
    rnn_parts = ("weight_ih", "weight_hh", "bias_ih", "bias_hh")
    if kind == "mdn":
        lstm = [
            f"G.lstm.{part}_l{layer}"
            for layer in range(int(model_args.get("layers", 2)))
            for part in rnn_parts
        ]
        return [*_dense_arrays("G.inp"), *lstm, *_dense_arrays("G.head1", "G.head2")]
    cells = [f"G.{cell}.{part}" for cell in ("cell1", "cell2") for part in rnn_parts]
    gate = _dense_arrays("G.gate") if model_args.get("idle_gate", False) else []
    return [
        *_dense_arrays("G.init", "G.inp"),
        *cells,
        *_dense_arrays("G.head1", "G.head2"),
        *gate,
        *_dense_arrays("len.fc1", "len.fc2", "len.out"),
    ]


def inspect(directory: Path, meta: Mapping[str, Any]) -> ExportInfo:
    """Construction-time checks: a vendored kind, its model arguments, its arrays."""
    where = directory / META_FILE
    kind = meta.get("kind")
    supported = ", ".join(KINDS)
    if kind in NOT_VENDORED:
        raise ValueError(
            f"{where} declares {FORMAT} kind {kind!r}, which the learned trajectory "
            f"does not vendor (supported kinds: {supported})"
        )
    if not isinstance(kind, str) or kind not in KINDS:
        declared = "declares no kind" if kind is None else f"declares unknown kind {kind!r}"
        raise ValueError(f"{where} {declared}; supported {FORMAT} kinds: {supported}")
    model_args = meta.get("model_args")
    needed = _REQUIRED_MODEL_ARGS[kind]
    if not isinstance(model_args, Mapping):
        raise ValueError(f"{where} has no model_args object; kind {kind!r} needs {list(needed)}")
    missing = [name for name in needed if name not in model_args]
    if missing:
        raise ValueError(f"{where} model_args lacks {missing}, which kind {kind!r} needs")
    require_arrays(directory, required_arrays(kind, model_args), f"{FORMAT} kind {kind!r}")
    return ExportInfo(kind=kind, models_stop=True)


def load(directory: Path, meta: Mapping[str, Any]) -> MdnSampler | GanArSampler:
    """Read ``<directory>/generator.npz`` into the sampler for ``meta['kind']``."""
    with np.load(directory / WEIGHTS_FILE) as npz:
        weights = {name: npz[name] for name in npz.files}
    if meta.get("kind") == "mdn":
        return MdnSampler(weights, meta)
    return GanArSampler(weights, meta)


class _Network:
    """Weights (float32) + the dense primitive + shape checks shared by both kinds."""

    kind: str

    def __init__(self, weights: Mapping[str, np.ndarray], meta: Mapping[str, Any]) -> None:
        self.a: dict[str, Any] = dict(meta["model_args"])
        self.w = {k: np.asarray(v, dtype=np.float32) for k, v in weights.items()}
        self.T = int(meta.get("max_seq_len", T_MAX))
        self.hidden = int(self.a["hidden"])
        missing = [name for name in required_arrays(self.kind, self.a) if name not in self.w]
        if missing:
            raise ValueError(f"{FORMAT} kind {self.kind!r} weights are missing arrays: {missing}")

    def _expect(self, name: str, shape: tuple[int, ...]) -> None:
        got = tuple(self.w[name].shape)
        if got != shape:
            raise ValueError(
                f"{FORMAT} kind {self.kind!r}: {name} has shape {got}, expected {shape} "
                f"for model_args {self.a}"
            )

    def _dense(self, x: np.ndarray, name: str) -> np.ndarray:
        return x @ self.w[name + ".weight"].T + self.w[name + ".bias"]


class MdnSampler(_Network):
    """``mdn``: autoregressive LSTM mixture density network with a stop head."""

    kind = "mdn"

    def __init__(self, weights: Mapping[str, np.ndarray], meta: Mapping[str, Any]) -> None:
        super().__init__(weights, meta)
        self.layers = int(self.a.get("layers", 2))
        self.K = int(self.a["K"])
        sampling = meta.get("sampling") or {}
        self.temperature = float(sampling.get("temperature", 1.0))
        self.rounding = bool(sampling.get("rounding", True))
        h = self.hidden
        self._expect("G.inp.weight", (h, 2 + 2 + 1 + 1 + COND_FEATURES))
        for layer in range(self.layers):
            self._expect(f"G.lstm.weight_ih_l{layer}", (4 * h, h))
            self._expect(f"G.lstm.weight_hh_l{layer}", (4 * h, h))
        self._expect("G.head1.weight", (h, h))
        self._expect("G.head2.weight", (6 * self.K + 1, h))

    def noise(self, batch: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
        """``u_comp (B, T)`` U[0,1), ``eps (B, T, 2)`` N(0,1) float32, ``u_stop (B, T)`` U[0,1)."""
        return {
            "u_comp": rng.random((batch, self.T)),
            "eps": rng.standard_normal((batch, self.T, 2)).astype(np.float32),
            "u_stop": rng.random((batch, self.T)),
        }

    def _lstm_cell(
        self, x: np.ndarray, h: np.ndarray, c: np.ndarray, layer: int
    ) -> tuple[np.ndarray, np.ndarray]:
        u = h.shape[1]
        w = self.w
        zz = (
            x @ w[f"G.lstm.weight_ih_l{layer}"].T
            + h @ w[f"G.lstm.weight_hh_l{layer}"].T
            + w[f"G.lstm.bias_ih_l{layer}"]
            + w[f"G.lstm.bias_hh_l{layer}"]
        )
        i = _sigmoid(zz[:, :u])
        f = _sigmoid(zz[:, u : 2 * u])
        g = np.tanh(zz[:, 2 * u : 3 * u])
        o = _sigmoid(zz[:, 3 * u :])
        c = f * c + i * g
        return o * np.tanh(c), c

    def forward(
        self, cond: np.ndarray, noise: Mapping[str, np.ndarray]
    ) -> tuple[np.ndarray, np.ndarray]:
        """Deterministic rollout given ``noise`` -> ``(moves / 800, lengths)``."""
        cond = np.atleast_2d(np.asarray(cond, dtype=np.float32))
        u_comp = np.asarray(noise["u_comp"])
        eps = np.asarray(noise["eps"], np.float32)
        u_stop = np.asarray(noise["u_stop"])
        batch, steps, hid, K = len(cond), self.T, self.hidden, self.K
        dt = np.float32
        feats = cond_features(cond)
        prev = np.zeros((batch, 2), dt)
        pos = np.zeros((batch, 2), dt)
        hs = [np.zeros((batch, hid), dt) for _ in range(self.layers)]
        cs = [np.zeros((batch, hid), dt) for _ in range(self.layers)]
        alive = np.ones(batch, dtype=bool)
        lengths = np.full(batch, steps, dtype=np.int64)
        out = np.zeros((batch, steps, 2), dt)
        rows = np.arange(batch)
        for t in range(steps):
            rem = cond[:, :2] - pos
            x = np.concatenate(
                [
                    prev / 50,
                    2 * rem,
                    2 * np.sqrt((rem * rem).sum(1, keepdims=True)),
                    np.full((batch, 1), t / 50, dt),
                    feats,
                ],
                axis=1,
            ).astype(dt)
            x = _leaky(self._dense(x, "G.inp"))
            for layer in range(self.layers):
                hs[layer], cs[layer] = self._lstm_cell(x, hs[layer], cs[layer], layer)
                x = hs[layer]
            o = self._dense(_leaky(self._dense(x, "G.head1")), "G.head2")
            logit_pi = o[:, :K]
            mu = o[:, K : 3 * K].reshape(batch, K, 2)
            log_sig = np.clip(o[:, 3 * K : 5 * K].reshape(batch, K, 2), MDN_LOG_SIGMA_MIN, 3.0)
            rho = np.tanh(o[:, 5 * K : 6 * K]) * 0.95
            e = logit_pi - logit_pi.max(1, keepdims=True)
            pi = np.exp(e) / np.exp(e).sum(1, keepdims=True)
            k = np.minimum((np.cumsum(pi, axis=1) < u_comp[:, t : t + 1].astype(dt)).sum(1), K - 1)
            mu_k = mu[rows, k]
            sig_k = np.exp(log_sig[rows, k]) * dt(self.temperature)
            rho_k = rho[rows, k]
            e0, e1 = eps[:, t, 0], eps[:, t, 1]
            dx = mu_k[:, 0] + sig_k[:, 0] * e0
            dy = mu_k[:, 1] + sig_k[:, 1] * (rho_k * e0 + np.sqrt(1 - rho_k * rho_k) * e1)
            mv = np.stack([dx, dy], axis=1) * dt(MDN_UNIT_PX)
            if self.rounding:
                mv = np.round(mv)
            mv = mv * alive[:, None]
            out[:, t] = mv
            stop = (u_stop[:, t].astype(dt) < _sigmoid(o[:, 6 * K])) & alive
            lengths[stop] = t + 1
            alive &= ~stop
            prev, pos = mv, pos + mv / dt(800.0)
            if not alive.any():
                break
        return (out / dt(800.0)).astype(dt), lengths

    def draw(self, cond: np.ndarray, rng: np.random.Generator) -> Draw:
        moves, lengths = self.forward(cond, self.noise(len(cond), rng))
        return Draw(moves=moves, lengths=lengths)


class GanArSampler(_Network):
    """``gan_ar``: length head, then an autoregressive two-GRU-cell generator."""

    kind = "gan_ar"

    def __init__(self, weights: Mapping[str, np.ndarray], meta: Mapping[str, Any]) -> None:
        super().__init__(weights, meta)
        self.nz = int(self.a["nz"])
        self.ne = int(self.a["ne"])
        self.idle_gate = bool(self.a.get("idle_gate", False))
        self.len_K = int(self.a.get("len_K", 4))
        self.round_positions = bool((meta.get("sampling") or {}).get("round_positions", False))
        h = self.hidden
        self._expect("G.init.weight", (2 * h, COND_FEATURES + self.nz + 2))
        self._expect("G.inp.weight", (h, 2 + 2 + 1 + 3 + self.ne + COND_FEATURES))
        for cell in ("G.cell1", "G.cell2"):
            self._expect(f"{cell}.weight_ih", (3 * h, h))
            self._expect(f"{cell}.weight_hh", (3 * h, h))
        self._expect("G.head1.weight", (h, h))
        self._expect("G.head2.weight", (2, h))
        if self.idle_gate:
            self._expect("G.gate.weight", (1, h))
        len_hidden = self.w["len.fc1.weight"].shape[0]
        self._expect("len.fc1.weight", (len_hidden, COND_FEATURES))
        self._expect("len.out.weight", (3 * self.len_K, self.w["len.fc2.weight"].shape[0]))

    def noise(self, batch: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
        """``u_len (B,)`` U[0,1), ``z (B, nz)`` and ``eps (B, T, ne)`` N(0,1) float32."""
        return {
            "u_len": rng.random((batch,)),
            "z": rng.standard_normal((batch, self.nz)).astype(np.float32),
            "eps": rng.standard_normal((batch, self.T, self.ne)).astype(np.float32),
        }

    def length_log_probs(self, cond: np.ndarray) -> np.ndarray:
        """``(B, 4) -> (B, T)`` log p(L = k + 1)."""
        K = self.len_K
        dtype = np.float32
        h = _leaky(self._dense(cond_features(cond), "len.fc1"))
        h = _leaky(self._dense(h, "len.fc2"))
        p = self._dense(h, "len.out")
        logit_pi, mu, log_s = p[:, :K], p[:, K : 2 * K], np.clip(p[:, 2 * K :], -5.0, 1.0)
        k = np.arange(1, self.T + 1, dtype=dtype)
        hi = np.log(k + 0.5)
        lo = np.log(np.maximum(k - 0.5, 1e-30)).astype(dtype)
        lo[0] = -1e4
        s = np.exp(log_s)[..., None]
        a = (hi - mu[..., None]) / s
        b = (lo - mu[..., None]) / s
        log_mass = _logsig(a) + _logsig(-b) + np.log(-np.expm1(np.minimum(b - a, -1e-6)))
        log_mix = _logsumexp(_log_softmax(logit_pi, 1)[..., None] + log_mass, axis=1)
        return _log_softmax(log_mix, 1)

    def sample_lengths(self, cond: np.ndarray, u: np.ndarray) -> np.ndarray:
        """Inverse-CDF length draw: ``L = 1 + min(#(cdf < u), T - 1)``."""
        cdf = np.cumsum(np.exp(self.length_log_probs(cond).astype(np.float64)), axis=1)
        return 1 + np.minimum(
            (cdf < np.asarray(u, dtype=np.float64)[:, None]).sum(axis=1), self.T - 1
        )

    def _gru_cell(self, x: np.ndarray, h: np.ndarray, prefix: str) -> np.ndarray:
        u = h.shape[1]
        xw = x @ self.w[prefix + ".weight_ih"].T + self.w[prefix + ".bias_ih"]
        hw = h @ self.w[prefix + ".weight_hh"].T + self.w[prefix + ".bias_hh"]
        r = _sigmoid(xw[:, :u] + hw[:, :u])
        z = _sigmoid(xw[:, u : 2 * u] + hw[:, u : 2 * u])
        n = np.tanh(xw[:, 2 * u :] + r * hw[:, 2 * u :])
        return (1 - z) * n + z * h

    def _moves(
        self, cond: np.ndarray, lengths: np.ndarray, z: np.ndarray, eps: np.ndarray
    ) -> np.ndarray:
        batch, steps, hid = len(cond), self.T, self.hidden
        dtype = np.float32
        feats = cond_features(cond)
        Lf = lengths.astype(dtype)
        h = np.tanh(
            self._dense(
                np.concatenate([feats, z, (Lf / 50)[:, None], np.log(Lf)[:, None] / 4], axis=1),
                "G.init",
            )
        )
        h1, h2 = h[:, :hid], h[:, hid:]
        pos = np.zeros((batch, 2), dtype)
        prev = np.zeros((batch, 2), dtype)
        out = np.zeros((batch, steps, 2), dtype)
        for t in range(int(lengths.max())):
            act = (t < Lf).astype(dtype)[:, None]
            rem = cond[:, :2] - pos
            parts = [
                prev,
                2 * rem,
                2 * np.sqrt((rem * rem).sum(1, keepdims=True)),
                (t / Lf)[:, None],
                ((Lf - t) / 50)[:, None] * act,
                (Lf / 50)[:, None],
            ]
            if self.ne:
                parts.append(eps[:, t])
            parts.append(feats)
            x = _leaky(self._dense(np.concatenate(parts, axis=1).astype(dtype), "G.inp"))
            h1 = self._gru_cell(x, h1, "G.cell1")
            h2 = self._gru_cell(h1, h2, "G.cell2")
            m = self._dense(_leaky(self._dense(h2, "G.head1")), "G.head2") * act
            if self.idle_gate:
                m = m * np.clip(self._dense(h2, "G.gate"), 0.0, 1.0)
            out[:, t] = m
            prev = m
            pos = pos + m * MOVE_TO_NORM
        return out

    def forward(
        self, cond: np.ndarray, noise: Mapping[str, np.ndarray]
    ) -> tuple[np.ndarray, np.ndarray]:
        """Deterministic given ``noise`` -> ``(moves / 800, lengths)``."""
        cond = np.atleast_2d(np.asarray(cond, dtype=np.float32))
        lengths = self.sample_lengths(cond, noise["u_len"])
        z = np.asarray(noise["z"], np.float32)
        eps = np.asarray(noise["eps"], np.float32).reshape(len(cond), self.T, self.ne)
        moves = self._moves(cond, lengths, z, eps) * np.float32(MOVE_TO_NORM)
        if self.round_positions:
            pos = np.round(np.cumsum(moves * np.float32(WINDOW), axis=1))
            moves = np.diff(pos, axis=1, prepend=np.zeros_like(pos[:, :1])) / np.float32(WINDOW)
        return moves.astype(np.float32), lengths

    def draw(self, cond: np.ndarray, rng: np.random.Generator) -> Draw:
        moves, lengths = self.forward(cond, self.noise(len(cond), rng))
        return Draw(moves=moves, lengths=lengths)
