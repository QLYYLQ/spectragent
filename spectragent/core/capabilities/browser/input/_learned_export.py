"""Learned-trajectory exports: ``meta.json`` names the model, the backend follows.

An export is a directory holding ``generator.npz`` (the weights) and
``meta.json``. The meta's ``format`` — and, from ``mouse-gan-generator/2`` on,
its ``kind`` — says which network the weights belong to and how it is sampled,
so a config names only the directory and cannot claim one model while the
weights hold another. :func:`open_export` reads the meta, picks the backend for
its format and lets that backend check the rest (kind, model arguments, array
names) at construction, before a browser launches.

Backends, each vendored from mouse-gan-torch and numpy only:

- ``mouse-gan-generator/1`` — :mod:`._mouse_gan_v1`, the faithful port of
  mouse-ai's conditional LSTM GAN. It has no stop signal: every path runs the
  whole horizon and the strategy decides where it ends.
- ``mouse-gan-generator/2`` — :mod:`._mouse_gan_v2`, kinds ``mdn`` and
  ``gan_ar``. Each path ends where the model says.

A backend module provides ``FORMAT``, ``inspect(directory, meta)`` and
``load(directory, meta)``. A new model family is a new module plus one entry
in :func:`_backends`; neither the strategy nor its consumers change.

This module itself imports numpy only inside :func:`require_arrays`, which
the backends call after the strategy has checked that numpy is installed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import numpy as np

WEIGHTS_FILE = "generator.npz"
META_FILE = "meta.json"


@dataclass(frozen=True, slots=True)
class Draw:
    """One batch of sampled paths, in model units (pixels / training window).

    ``moves`` is ``(B, T, 2)`` per-tick cursor deltas. ``lengths`` is ``(B,)``
    ticks per path (``1..T``, zero moves after) when the model decides where a
    path ends, and ``None`` when it has no stop signal and every path runs all
    ``T`` ticks.
    """

    moves: np.ndarray
    lengths: np.ndarray | None


class Sampler(Protocol):
    """A loaded model: one call samples one path per condition row."""

    def draw(self, cond: np.ndarray, rng: np.random.Generator) -> Draw:
        """``cond`` is ``(B, 4)`` float32 ``(offset_x, offset_y, btn_w, btn_h)``."""
        ...


@dataclass(frozen=True, slots=True)
class ExportInfo:
    """What a backend's ``inspect`` learned about an export."""

    #: Model within the format (``None`` when the format holds one model).
    kind: str | None
    #: Whether the model decides where each path ends (``Draw.lengths`` set).
    models_stop: bool
    #: Whether that decision is taken tick by tick, so a path that used the
    #: whole horizon is one that never ended — ``mdn``'s Bernoulli stop head.
    #: ``gan_ar`` instead draws the length up front from a head capped at
    #: ``max_seq_len``, where the full horizon is an ordinary draw.
    stop_is_per_tick: bool = False


class _Backend(Protocol):
    """Module-level contract of a backend (``_mouse_gan_v1`` / ``_mouse_gan_v2``)."""

    FORMAT: str

    def inspect(self, directory: Path, meta: Mapping[str, Any]) -> ExportInfo: ...

    def load(self, directory: Path, meta: Mapping[str, Any]) -> Sampler: ...


def _backends() -> dict[str, _Backend]:
    from spectragent.core.capabilities.browser.input import _mouse_gan_v1, _mouse_gan_v2

    modules: tuple[_Backend, ...] = (_mouse_gan_v1, _mouse_gan_v2)
    return {module.FORMAT: module for module in modules}


@dataclass(frozen=True, slots=True)
class Export:
    """A checked export directory; :meth:`load` reads its weights."""

    directory: Path
    meta: Mapping[str, Any]
    format: str
    kind: str | None
    models_stop: bool
    stop_is_per_tick: bool

    @property
    def label(self) -> str:
        """How messages name the model: ``mouse-gan-generator/2 kind 'mdn'``."""
        return self.format if self.kind is None else f"{self.format} kind {self.kind!r}"

    @property
    def meta_path(self) -> Path:
        """The ``meta.json`` :attr:`meta` was read from (messages point at it)."""
        return self.directory / META_FILE

    def load(self) -> Sampler:
        return _backends()[self.format].load(self.directory, self.meta)


def open_export(weights_dir: str) -> Export:
    """Check ``weights_dir`` and identify its model from ``meta.json``.

    Raises ``FileNotFoundError`` for a missing ``generator.npz`` or
    ``meta.json`` and ``ValueError`` naming the offending value for an unknown
    format, an unsupported kind, incomplete model arguments or missing arrays.
    Array data is not read here.
    """
    directory = Path(weights_dir).expanduser()
    weights_path = directory / WEIGHTS_FILE
    if not weights_path.is_file():
        raise FileNotFoundError(f"learned trajectory weights not found: {weights_path}")
    meta_path = directory / META_FILE
    if not meta_path.is_file():
        raise FileNotFoundError(
            f"learned trajectory export has no {META_FILE}: {meta_path} "
            "(its format and kind identify the model)"
        )
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{meta_path} is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError(f"{meta_path} is not a JSON object")

    backends = _backends()
    fmt = meta.get("format")
    if not isinstance(fmt, str) or fmt not in backends:
        declared = "declares no format" if fmt is None else f"declares format {fmt!r}"
        raise ValueError(
            f"{meta_path} {declared}; the learned trajectory reads {', '.join(sorted(backends))}"
        )
    info = backends[fmt].inspect(directory, meta)
    return Export(directory, meta, fmt, info.kind, info.models_stop, info.stop_is_per_tick)


def require_arrays(directory: Path, required: Iterable[str], label: str) -> None:
    """Fail naming every array ``label`` needs that ``generator.npz`` lacks.

    Reads only the archive's member list, not the array data.
    """
    import numpy as np

    path = directory / WEIGHTS_FILE
    with np.load(path) as npz:
        present = set(npz.files)
    missing = [name for name in required if name not in present]
    if missing:
        raise ValueError(f"{path} lacks arrays a {label} export holds: {missing}")
