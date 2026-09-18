"""Shared fixtures for the browser input layer.

The virtual-clock harness is here rather than in one test module because the
mouse and the keyboard play against the *same* scheduler
(``input/_schedule.py``): giving each a clock of its own is how the two would
drift apart.

Tiny, randomly initialised exports in each format the ``learned`` strategy
reads, with the exact array names, layer wiring and meta keys of the
trainer's exporters (``mouse_gan.export`` for ``mouse-gan-generator/1``,
``mouse_gan.v2.export_v2`` for ``/2``) and much smaller widths, so no test
depends on the real 1-10 MB weights. ``test_mouse_gan_v2`` checks the names
against the real exports whenever the trainer project is on the machine.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

TINY_LATENT = 8
TINY_SEQ_LEN = 30

#: The trainer project, when this machine has it (real exports + reference samplers).
TRAINER_ROOT = Path("/Users/qly/QLY/code/study/mouse-gan-torch")
V1_SMOKE_EXPORT = TRAINER_ROOT / "runs" / "smoke" / "export"
V2_MDN_EXPORT = TRAINER_ROOT / "runs" / "v2_mdn_h128_d3" / "export"
V2_GAN_AR_EXPORT = TRAINER_ROOT / "runs" / "v2_ar_gate" / "export"


def _write(
    directory: Path,
    arrays: dict[str, np.ndarray],
    meta: dict[str, Any] | None,
    drop_arrays: Iterable[str],
) -> Path:
    for name in drop_arrays:
        del arrays[name]
    directory.mkdir(parents=True, exist_ok=True)
    np.savez(directory / "generator.npz", **arrays)  # type: ignore[arg-type]
    if meta is not None:
        (directory / "meta.json").write_text(json.dumps(meta))
    return directory


class _Init:
    def __init__(self, seed: int) -> None:
        self.rng = np.random.default_rng(seed)

    def mat(self, rows: int, cols: int, scale: float = 1.0) -> np.ndarray:
        return (self.rng.standard_normal((rows, cols)) * scale / np.sqrt(cols)).astype(np.float32)

    def vec(self, n: int, scale: float = 0.1) -> np.ndarray:
        return (self.rng.standard_normal(n) * scale).astype(np.float32)


def write_tiny_v1_export(
    directory: Path,
    *,
    meta: dict[str, Any] | None = None,
    write_meta: bool = True,
    drop_arrays: Iterable[str] = (),
    seed: int = 0,
) -> Path:
    """A ``mouse-gan-generator/1`` export (``generator.npz`` + ``meta.json``)."""
    init = _Init(seed)
    cond_dim, h1, h2, u, u1, u2, d = 4, 16, 16, 8, 8, 8, 8
    arrays: dict[str, np.ndarray] = {
        "dense1.weight": init.mat(h1, TINY_LATENT + cond_dim),
        "dense1.bias": init.vec(h1),
        "bn1.gamma": np.ones(h1, np.float32),
        "bn1.beta": np.zeros(h1, np.float32),
        "bn1.moving_mean": np.zeros(h1, np.float32),
        "bn1.moving_var": np.ones(h1, np.float32),
        "dense2.weight": init.mat(h2, h1),
        "dense2.bias": init.vec(h2),
        "bn2.gamma": np.ones(2 * u, np.float32),
        "bn2.beta": np.zeros(2 * u, np.float32),
        "bn2.moving_mean": np.zeros(2 * u, np.float32),
        "bn2.moving_var": np.ones(2 * u, np.float32),
        "td_dense.weight": init.mat(d, u2),
        "td_dense.bias": init.vec(d),
        "out.weight": init.mat(2, d) * 0.05,
        "out.bias": init.vec(2, 0.01),
    }
    for prefix, units, inputs in (
        ("bilstm.fw", u, h2),
        ("bilstm.bw", u, h2),
        ("lstm1", u1, 2 * u),
        ("lstm2", u2, u1),
    ):
        arrays[f"{prefix}.w_ih"] = init.mat(4 * units, inputs)
        arrays[f"{prefix}.w_hh"] = init.mat(4 * units, units)
        arrays[f"{prefix}.b"] = init.vec(4 * units)

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
        "dtype": "float32",
    }
    full_meta.update(meta or {})
    return _write(directory, arrays, full_meta if write_meta else None, drop_arrays)


def _v2_meta(kind: str, model_args: dict[str, Any], sampling: dict[str, Any]) -> dict[str, Any]:
    """The keys ``mouse_gan.v2.export_v2.base_meta`` writes (prose fields abridged)."""
    return {
        "format": "mouse-gan-generator/2",
        "kind": kind,
        "model_args": model_args,
        "hz": 50,
        "max_seq_len": TINY_SEQ_LEN,
        "dtype": "float32",
        "norm_reference_window": [800, 800],
        "cond": {"shape": "(B, 4) float32", "order": ["offset_x", "offset_y", "btn_w", "btn_h"]},
        "output": {"moves": "(B, T, 2) / 800", "lengths": "(B,)", "cut_rule": kind},
        "sampling": sampling,
        "noise": {},
    }


def write_tiny_mdn_export(
    directory: Path,
    *,
    hidden: int = 8,
    layers: int = 2,
    K: int = 3,
    stop_bias: float = -2.0,
    sampling: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    drop_arrays: Iterable[str] = (),
    seed: int = 0,
) -> Path:
    """A ``mouse-gan-generator/2`` kind ``mdn`` export.

    ``stop_bias`` is the stop logit's bias: -2 stops after ~8 ticks on
    average, a large negative value never stops.
    """
    init = _Init(seed)
    h = hidden
    arrays: dict[str, np.ndarray] = {
        "G.inp.weight": init.mat(h, 13),
        "G.inp.bias": init.vec(h),
    }
    for layer in range(layers):
        arrays[f"G.lstm.weight_ih_l{layer}"] = init.mat(4 * h, h)
        arrays[f"G.lstm.weight_hh_l{layer}"] = init.mat(4 * h, h)
        arrays[f"G.lstm.bias_ih_l{layer}"] = init.vec(4 * h)
        arrays[f"G.lstm.bias_hh_l{layer}"] = init.vec(4 * h)
    head2_bias = init.vec(6 * K + 1, 0.2)
    head2_bias[-1] = stop_bias
    arrays.update(
        {
            "G.head1.weight": init.mat(h, h),
            "G.head1.bias": init.vec(h),
            "G.head2.weight": init.mat(6 * K + 1, h, scale=0.5),
            "G.head2.bias": head2_bias,
        }
    )
    full_meta = _v2_meta(
        "mdn",
        {"hidden": h, "layers": layers, "K": K, "dropout": 0.0, "mdn_round": True},
        sampling if sampling is not None else {"temperature": 1.0, "rounding": True},
    )
    full_meta.update(meta or {})
    return _write(directory, arrays, full_meta, drop_arrays)


def write_tiny_gan_ar_export(
    directory: Path,
    *,
    hidden: int = 8,
    nz: int = 4,
    ne: int = 3,
    idle_gate: bool = True,
    len_K: int = 2,
    len_hidden: int = 8,
    mean_length: float = 10.0,
    round_positions: bool = False,
    meta: dict[str, Any] | None = None,
    drop_arrays: Iterable[str] = (),
    seed: int = 0,
) -> Path:
    """A ``mouse-gan-generator/2`` kind ``gan_ar`` export (length head included)."""
    init = _Init(seed)
    h = hidden
    arrays: dict[str, np.ndarray] = {
        "G.init.weight": init.mat(2 * h, 7 + nz + 2),
        "G.init.bias": init.vec(2 * h),
        "G.inp.weight": init.mat(h, 2 + 2 + 1 + 3 + ne + 7),
        "G.inp.bias": init.vec(h),
    }
    for cell in ("G.cell1", "G.cell2"):
        arrays[f"{cell}.weight_ih"] = init.mat(3 * h, h)
        arrays[f"{cell}.weight_hh"] = init.mat(3 * h, h)
        arrays[f"{cell}.bias_ih"] = init.vec(3 * h)
        arrays[f"{cell}.bias_hh"] = init.vec(3 * h)
    arrays.update(
        {
            "G.head1.weight": init.mat(h, h),
            "G.head1.bias": init.vec(h),
            "G.head2.weight": init.mat(2, h),
            "G.head2.bias": init.vec(2),
        }
    )
    if idle_gate:
        arrays["G.gate.weight"] = init.mat(1, h, scale=0.5)
        arrays["G.gate.bias"] = np.array([0.6], np.float32)
    # Length head: mixture of discretised logistics around ``mean_length``.
    len_out_bias = np.zeros(3 * len_K, np.float32)
    len_out_bias[len_K : 2 * len_K] = math.log(mean_length) + np.linspace(-0.2, 0.2, len_K)
    len_out_bias[2 * len_K :] = math.log(0.15)
    arrays.update(
        {
            "len.fc1.weight": init.mat(len_hidden, 7),
            "len.fc1.bias": init.vec(len_hidden),
            "len.fc2.weight": init.mat(len_hidden, len_hidden),
            "len.fc2.bias": init.vec(len_hidden),
            "len.out.weight": init.mat(3 * len_K, len_hidden, scale=0.1),
            "len.out.bias": len_out_bias,
        }
    )
    full_meta = _v2_meta(
        "gan_ar",
        {
            "nz": nz,
            "ne": ne,
            "hidden": h,
            "st_round": round_positions,
            "idle_gate": idle_gate,
            "len_K": len_K,
            "len_hidden": len_hidden,
        },
        {"round_positions": round_positions},
    )
    full_meta.update(meta or {})
    return _write(directory, arrays, full_meta, drop_arrays)


WRITERS: dict[str, Callable[..., Path]] = {
    "v1": write_tiny_v1_export,
    "mdn": write_tiny_mdn_export,
    "gan_ar": write_tiny_gan_ar_export,
}


@pytest.fixture
def v1_export(tmp_path: Path) -> Path:
    return write_tiny_v1_export(tmp_path / "v1_export")


@pytest.fixture
def mdn_export(tmp_path: Path) -> Path:
    return write_tiny_mdn_export(tmp_path / "mdn_export")


@pytest.fixture
def gan_ar_export(tmp_path: Path) -> Path:
    return write_tiny_gan_ar_export(tmp_path / "gan_ar_export")


@pytest.fixture
def make_export(tmp_path: Path) -> Callable[..., Path]:
    """``make_export("v1" | "mdn" | "gan_ar", **writer_kwargs)`` in a fresh directory."""
    counter = iter(range(1_000_000))

    def make(kind: str, **kwargs: Any) -> Path:
        return WRITERS[kind](tmp_path / f"{kind}_export_{next(counter)}", **kwargs)

    return make


class VirtualClock:
    """The clock and the sleeps the input layer schedules against, under control.

    Real time never passes in these tests: only a sleep, or a CDP round trip
    that was given a cost, advances ``now``. That makes the deadline schedule
    exactly reproducible — and lets a test say "this dispatch takes 50 ms"
    without a browser.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordCDP:
    """Records every ``send`` call; returns empty results.

    Structurally satisfies ``agent_cdp.CDPCommandProtocol`` — including
    the positional ``session_id`` and the event-subscription half — so
    ``HumanMouse`` / ``HumanKeyboard`` can be constructed with it without a
    type error.

    ``cost_ms`` is how long a round trip takes on the virtual clock, the
    latency the deadline schedule has to absorb.
    """

    is_connected = True

    def __init__(self, clock: VirtualClock | None = None, cost_ms: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._clock = clock
        self._cost_ms = cost_ms

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        if self._clock is not None and self._cost_ms:
            self._clock.advance(self._cost_ms / 1000.0)
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    def mouse_events(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "Input.dispatchMouseEvent"]

    def types(self) -> list[str]:
        return [p["type"] for p in self.mouse_events()]

    def key_events(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "Input.dispatchKeyEvent"]

    def inserts(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "Input.insertText"]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    """Give the input layer's one scheduler a virtual clock and virtual sleeps.

    Patched on ``input/_schedule.py``, which is where the deadline loop
    lives: the mouse and the keyboard share it, so they share this harness
    too rather than growing a second clock each.
    """
    virtual = VirtualClock()
    monkeypatch.setattr(
        "spectragent.core.capabilities.browser.input._schedule.asyncio.sleep",
        virtual.sleep,
    )
    monkeypatch.setattr(
        "spectragent.core.capabilities.browser.input._schedule.monotonic",
        virtual.monotonic,
    )
    return virtual


@pytest.fixture
def no_sleep(clock: VirtualClock) -> list[float]:
    """The durations slept, which on the virtual clock cost no real time."""
    return clock.sleeps
