"""Dataset checks run before anything is written (arch §4.15 "校验").

The round trip is checked while rendering (it needs the agent's state at the
step); everything else here runs on the formatted records, and any failure
fails the whole export — nothing reaches ``output_dir``:

- **images** — each record references as many images as its rendered
  context carries, and every referenced file was stored;
- **coordinates** — each point of the target actions lies in the step
  screenshot's viewport, the point the student's reply executes lies in the
  image, and the grid point it writes lies on the student's grid;
- **reference leak** — a task's reference answer (its benchmark's eval
  loader, read through the export document's loaders, ``benchmarks.py``;
  normalised, at least 8 characters) never appears in text the export itself
  puts into a prompt: the seed (system) messages and the task text. The page
  showing the answer is the world, not a leak;
- **credentials** — no credential-shaped string
  (``core/trace/secret_patterns.py``) in any text byte to be written;
- **length** — a pluggable :class:`LengthEstimator`; over ``max_tokens`` is
  an error naming the sample, never a truncation.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.params import point_params
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    SystemMessage,
)
from spectragent.core.trace.secret_patterns import find_secrets
from spectragent.export._registrar import ExportRecord
from spectragent.export.benchmarks import BenchmarkLoaders
from spectragent.export.errors import (
    CredentialLeakError,
    ExportValidationError,
    ReferenceLeakError,
    SampleTooLongError,
)
from spectragent.export.render import RenderedSample
from spectragent.export.settings import LengthBudget
from spectragent.export.split import normalized_text
from spectragent.schemas.action import Action
from spectragent.schemas.observation.geometry import CoordinateSpace, point_grid
from spectragent.schemas.observation.image import image_size

#: A reference answer shorter than this (normalised) is not scanned for: a
#: ``yes`` or a price appears in prompts by coincidence.
MIN_LEAK_CHARS = 8
#: Pixels per side of one Qwen-VL vision token patch (``qwen_vl`` estimate).
_QWEN_PATCH = 28


# ── images ────────────────────────────────────────────────────────────


def image_parts(messages: Iterable[BaseMessage]) -> list[Any]:
    """Every image part of *messages*, in order."""
    parts: list[Any] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            parts.extend(part for part in content if getattr(part, "type", None) == "image_url")
    return parts


def check_images(record: ExportRecord, rendered: RenderedSample, dataset_root: Path) -> None:
    """The record shows exactly the context's images, each stored under *dataset_root*."""
    expected = len(image_parts(rendered.context))
    if len(record.images) != expected:
        raise ExportValidationError(
            "images",
            record.sample_id,
            f"the record references {len(record.images)} image(s), the context carries {expected}",
        )
    for path in record.images:
        if not (dataset_root / path).is_file():
            raise ExportValidationError(
                "images", record.sample_id, f"{path} is referenced but was not stored"
            )


# ── coordinates ───────────────────────────────────────────────────────


def _points(action: Action) -> list[tuple[str, int | float, int | float]]:
    params_model = getattr(ActionRegistrar.get_or_none(action.name), "Params", None)
    if params_model is None:
        return []
    found: list[tuple[str, int | float, int | float]] = []
    for x_key, y_key in point_params(params_model):
        x, y = action.params.get(x_key), action.params.get(y_key)
        if isinstance(x, int | float) and isinstance(y, int | float):
            found.append((f"{action.name}.{x_key[:-2]}", x, y))
    return found


def check_coordinates(rendered: RenderedSample, space: CoordinateSpace) -> None:
    """Every target point lies in the viewport, on the grid, and executes inside the image."""
    sample = rendered.sample
    targets = [point for action in sample.actions or [] for point in _points(action)]
    executed = [point for action in rendered.target_actions for point in _points(action)]
    if not targets and not executed:
        return
    image = sample.image
    if image is None:
        raise ExportValidationError(
            "coordinates", sample.sample_id, "a point action on a step with no recorded image"
        )
    grid = point_grid(space, image)
    if grid is None:
        raise ExportValidationError(
            "coordinates", sample.sample_id, "the step's image has no known size for a point"
        )
    width, height = image.viewport_width, image.viewport_height
    for name, x, y in targets:
        if not (0 <= x < width and 0 <= y < height):
            raise ExportValidationError(
                "coordinates",
                sample.sample_id,
                f"{name} ({x}, {y}) is outside the {width}x{height} viewport",
            )
        gx, gy = grid.to_grid(x, y)
        if not (0 <= gx <= grid.width and 0 <= gy <= grid.height):
            raise ExportValidationError(
                "coordinates",
                sample.sample_id,
                f"{name} writes ({gx}, {gy}), off the {grid.width}x{grid.height} grid",
            )
    for name, x, y in executed:
        px = x * image.image_width / width
        py = y * image.image_height / height
        if not (0 <= px < image.image_width and 0 <= py < image.image_height):
            raise ExportValidationError(
                "coordinates",
                sample.sample_id,
                f"{name} executes at ({x}, {y}), outside the "
                f"{image.image_width}x{image.image_height} image",
            )


# ── reference leaks ───────────────────────────────────────────────────


class ReferenceScanner:
    """Reads each source task's reference answer from its benchmark's eval loader.

    The loaders are the export document's (``benchmarks.py``): the eval
    loaders it configures for the benchmark, searched in document order for
    the task, else the benchmark's registered eval loader built with its
    defaults and the source run's subset. A benchmark with neither is
    ``unchecked`` (counted in the manifest); a loader that cannot load, or
    has no reference for the task, fails the export.

    Attributes:
        checked: Samples whose reference was read and scanned for.
        unchecked: Benchmark → samples with no eval loader to read from.
    """

    def __init__(self, loaders: BenchmarkLoaders) -> None:
        self._loaders = loaders
        self.checked = 0
        self.unchecked: dict[str, int] = {}

    def check(self, rendered: RenderedSample, *, benchmark: str, subset: str, task_id: str) -> None:
        """Raise :class:`ReferenceLeakError` when the reference answer is in a prompt's own text."""
        sample_id = rendered.sample.sample_id
        loaders = self._eval_loaders(benchmark, subset, sample_id)
        if not loaders:
            self.unchecked[benchmark or "-"] = self.unchecked.get(benchmark or "-", 0) + 1
            return
        reference = next(
            (found for loader in loaders if (found := loader.get(task_id)) is not None), None
        )
        if reference is None:
            subsets = sorted({str(getattr(loader, "subset", None)) for loader in loaders})
            raise ExportValidationError(
                "reference_leak",
                sample_id,
                f"the {benchmark} eval loader (subset {', '.join(subsets)}) has no reference "
                f"for task {task_id!r}",
            )
        self.checked += 1
        answer = normalized_text(reference.expected_answer or "")
        if len(answer) < MIN_LEAK_CHARS:
            return
        seed = " ".join(
            message.text for message in rendered.context if isinstance(message, SystemMessage)
        )
        if answer in normalized_text(seed):
            raise ReferenceLeakError(sample_id, "seed")
        if answer in normalized_text(rendered.sample.task):
            raise ReferenceLeakError(sample_id, "task")

    def _eval_loaders(self, benchmark: str, subset: str, sample_id: str) -> tuple[Any, ...]:
        """The document's eval loaders for *benchmark*, else its registered default, else ``()``."""
        configured = self._loaders.configured("eval", benchmark)
        if configured:
            return configured
        try:
            loader = self._loaders.registered_default("eval", benchmark, subset or None)
        except Exception as exc:
            raise ExportValidationError(
                "reference_leak",
                sample_id,
                f"the {benchmark} eval loader could not load its references "
                f"({type(exc).__name__}: {exc}); point it at the data with an eval_loader "
                f"block in the export document (benchmark_name: {benchmark}, data_dir: ...)",
            ) from exc
        return () if loader is None else (loader,)


# ── credentials ───────────────────────────────────────────────────────


def check_credentials(files: Mapping[str, bytes]) -> None:
    """No credential-shaped string in any text file about to be written.

    *files* maps a dataset-relative path to its bytes; JSON lines are
    scanned one by one so the error names the sample (``id`` /
    ``sample.sample_id``). Fails closed on the first hit, never echoing it.
    """
    for path in sorted(files):
        text = files[path].decode("utf-8")
        for line in text.splitlines():
            found = find_secrets(line)
            if found:
                label, _match = found[0]
                raise CredentialLeakError(path, _sample_of(line), label)


def _sample_of(line: str) -> str:
    try:
        value = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("id"), str):
        return value["id"]
    sample = value.get("sample")
    if isinstance(sample, dict) and isinstance(sample.get("sample_id"), str):
        return sample["sample_id"]
    return ""


# ── length ────────────────────────────────────────────────────────────


@runtime_checkable
class LengthEstimator(Protocol):
    """Estimates a rendered sample's length in its trainer's tokens, offline."""

    def estimate(self, rendered: RenderedSample) -> int: ...


@dataclass(frozen=True)
class ApproxTokenEstimator:
    """Text characters / ``chars_per_token``, plus a per-image count — no tokenizer, no network.

    ``image_tokens: qwen_vl`` counts ``ceil(w/28) * ceil(h/28) + 2`` per image
    (its pixel size, read from the bytes); ``fixed`` counts
    ``fixed_image_tokens``.
    """

    chars_per_token: float = 4.0
    image_tokens: Literal["qwen_vl", "fixed"] = "qwen_vl"
    fixed_image_tokens: int = 0

    @classmethod
    def from_budget(cls, budget: LengthBudget) -> ApproxTokenEstimator:
        return cls(
            chars_per_token=budget.chars_per_token,
            image_tokens=budget.image_tokens,
            fixed_image_tokens=budget.fixed_image_tokens or 0,
        )

    def estimate(self, rendered: RenderedSample) -> int:
        chars = len(rendered.target)
        tokens = 0
        for message in rendered.context:
            content = message.content
            if isinstance(content, str):
                chars += len(content)
                continue
            for part in content or []:
                if isinstance(part, ContentPartTextParam):
                    chars += len(part.text)
                elif isinstance(part, ContentPartImageParam):
                    tokens += self._image_tokens(part.image_url.url)
        return tokens + math.ceil(chars / self.chars_per_token)

    def _image_tokens(self, url: str) -> int:
        if self.image_tokens == "fixed":
            return self.fixed_image_tokens
        _header, _sep, payload = url.partition(",")
        try:
            width, height = image_size(base64.b64decode(payload, validate=True))
        except binascii.Error:
            width, height = 0, 0
        return math.ceil(width / _QWEN_PATCH) * math.ceil(height / _QWEN_PATCH) + 2


def check_length(rendered: RenderedSample, estimator: LengthEstimator, max_tokens: int) -> None:
    """Raise :class:`SampleTooLongError` for a record over the ceiling — never truncate."""
    estimated = estimator.estimate(rendered)
    if estimated > max_tokens:
        raise SampleTooLongError(rendered.sample.sample_id, estimated, max_tokens)


__all__ = [
    "MIN_LEAK_CHARS",
    "ApproxTokenEstimator",
    "LengthEstimator",
    "ReferenceScanner",
    "check_coordinates",
    "check_credentials",
    "check_images",
    "check_length",
    "image_parts",
]
