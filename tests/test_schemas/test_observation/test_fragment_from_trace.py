"""Every fragment reads back from a trace as the type it was written as.

``FragmentBase.from_trace(payload, artifact)`` is the documented inverse of
``trace_binary()`` / ``trace_json_exclude()`` (``docs/architecture_design.md``
§5.5). These tests hold that contract for every registered fragment class
at once: a fragment that writes bytes it cannot read back, or a JSON shape
its class cannot decode, fails here — not in a trace reader months later,
on a trace that cannot be re-recorded.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.schemas.observation._registrar import FragmentBase, ObservationRegistrar
from spectragent.schemas.observation.fragments import TerminalObs
from tests._helpers.fragment_samples import JPEG_BYTES, PNG_BYTES, SAMPLES, shipped_fragment_keys


def _trace_form(fragment: FragmentBase) -> tuple[dict[str, Any], bytes | None]:
    """What a trace holds for *fragment*: the sidecar as parsed JSON, and the artifact.

    The two writer hooks applied exactly as the trace writer applies them,
    through a real JSON round trip — so tuples, int keys and bytes arrive
    the way a reader will see them.
    """
    binary = fragment.trace_binary()
    payload = json.loads(fragment.model_dump_json(exclude=fragment.trace_json_exclude()))
    return payload, (binary[1] if binary is not None else None)


CASES = [(key, variant) for key, variants in SAMPLES.items() for variant in variants]


@pytest.mark.parametrize(("key", "variant"), CASES, ids=[f"{k}:{v}" for k, v in CASES])
def test_decoding_what_was_encoded_changes_nothing(key: str, variant: str) -> None:
    fragment = SAMPLES[key][variant]()
    cls = type(fragment)
    assert fragment.fragment_id == key
    assert ObservationRegistrar.get(key) is cls

    payload, artifact = _trace_form(fragment)
    decoded = cls.from_trace(payload, artifact)

    assert type(decoded) is cls
    assert _trace_form(decoded) == (payload, artifact)


def test_every_shipped_fragment_class_has_a_sample() -> None:
    """The guard that makes the round trip cover the *next* fragment too."""
    missing = sorted(shipped_fragment_keys() - set(SAMPLES))
    assert not missing, (
        f"Observation fragments with no round-trip sample: {missing}. Add one to SAMPLES "
        f"in tests/_helpers/fragment_samples.py — a fragment a trace cannot read back as "
        f"its own type is a fragment no evaluator can use."
    )


@pytest.mark.parametrize(
    ("data", "suffix"), [(PNG_BYTES, ".png"), (JPEG_BYTES, ".jpg")], ids=["png", "jpeg"]
)
def test_a_screenshot_gets_its_image_back(data: bytes, suffix: str) -> None:
    shot = ScreenshotObs(image_bytes=data, viewport_width=1280, viewport_height=720)
    binary = shot.trace_binary()
    assert binary == (suffix, data)
    payload, artifact = _trace_form(shot)
    assert "image_bytes" not in payload  # the sidecar never holds the image

    decoded = ScreenshotObs.from_trace(payload, artifact)

    assert decoded.image_bytes == data
    assert decoded.viewport_width == 1280


def test_a_screenshot_that_wrote_no_bytes_reads_back_without_any() -> None:
    shot = ScreenshotObs(image_path="/tmp/shot.png")
    payload, artifact = _trace_form(shot)
    assert artifact is None

    decoded = ScreenshotObs.from_trace(payload, None)

    assert decoded.image_bytes is None
    assert decoded.image_path == "/tmp/shot.png"


def test_a_fragment_without_a_binary_refuses_an_artifact() -> None:
    """Dropping the bytes would hand back a fragment with its payload missing."""
    payload, _ = _trace_form(TerminalObs(stdout="ok"))
    with pytest.raises(ValueError, match=r"TerminalObs.*from_trace\(\)"):
        TerminalObs.from_trace(payload, b"\x89PNG")


def test_a_sidecar_of_the_wrong_shape_is_a_value_error() -> None:
    """A reader maps ``ValueError`` from ``from_trace`` to ``TraceSchemaError``."""
    with pytest.raises(ValueError):
        TerminalObs.from_trace({"fragment_id": "docker.terminal", "exit_code": "boom"}, None)


def test_provenance_survives_the_round_trip() -> None:
    """``fragment_id`` and ``captured_at`` come back as recorded, not re-stamped."""
    fragment = TerminalObs(stdout="ok", captured_at=1760000000.25)
    decoded = TerminalObs.from_trace(*_trace_form(fragment))
    assert decoded.fragment_id == "docker.terminal"
    assert decoded.captured_at == 1760000000.25
