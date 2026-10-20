"""Align committed observations and reconstruct only the evidence the reviewer sees."""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlsplit

import yaml
from PIL import Image

from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.capabilities.browser.review_evidence import (
    CAPTURE_STATE_FRAGMENT,
    BrowserEvidenceUnavailable,
    BrowserEvidenceUnsupported,
    capture_state_failed_images,
    clean_recorded_dom,
    recorded_replay_facts,
    replay_images_not_served_in_viewport,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.decision import DecisionImage, ImageProvenance
from spectragent.core.registries import collect_registrars
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.replay_review.base import BaseReplayReviewer
from spectragent.replay_review.models import (
    EvidenceRef,
    MachineFacts,
    ReviewCase,
    ReviewEvidenceError,
    ReviewObservation,
    ReviewPair,
    TaskIdentity,
    ViewportImageReadings,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _browser_config(value: dict[str, Any]) -> dict[str, Any]:
    candidates = []

    def walk(item):
        if isinstance(item, dict):
            if item.get("capability_id") == "browser":
                candidates.append(item)
            else:
                for child in item.values():
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    if len(candidates) != 1:
        raise ReviewEvidenceError(
            "unsupported_input", "Review requires exactly one configured browser capability"
        )
    return candidates[0]


def _ref(trace: RunTrace, step: int) -> EvidenceRef:
    return EvidenceRef(
        run_id=trace.run_id,
        step=step,
        trace=str(trace.trace_dir),
        manifest_sha256=_digest(trace.manifest.model_dump_json().encode()),
        config_hash=trace.manifest.config_hash,
    )


def _capture_state_images(trace: RunTrace, step: int) -> dict[str, Any]:
    """The step's capture_state reading of failed visible images, or why there is none."""
    negotiation = trace.config.negotiation
    return capture_state_failed_images(
        trace.observation_fragment(step, CAPTURE_STATE_FRAGMENT),
        negotiated=negotiation is not None
        and CAPTURE_STATE_FRAGMENT in negotiation.active_fragments,
    )


def _image(data: bytes) -> DecisionImage:
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        fmt = image.format
    if fmt not in {"PNG", "JPEG"}:
        raise ReviewEvidenceError("unsupported_input", "Recorded screenshot must be PNG or JPEG")
    return DecisionImage(
        format="png" if fmt == "PNG" else "jpg",
        data_base64=base64.b64encode(data).decode(),
        provenance=ImageProvenance(
            source_sha256=_digest(data), source_width=width, source_height=height
        ),
    )


async def _observation(
    trace: RunTrace, step: int, capability: Any, reviewer: BaseReplayReviewer
) -> tuple[ReviewObservation, EvidenceRef]:
    if not trace.config.recording.observations:
        raise ReviewEvidenceError(
            "scope_unrecorded", "Source observations were not selected for recording"
        )
    if (
        not trace.config.negotiation
        or "browser.dom_raw" not in trace.config.negotiation.active_archivers
    ):
        raise ReviewEvidenceError("scope_unrecorded", "Raw DOM was not selected for recording")
    meta = trace.observation_fragment(step, "browser.browser_meta")
    screen = trace.observation_fragment(step, "browser.screenshot")
    url = (
        meta.url
        if isinstance(meta, BrowserMetaObs)
        else screen.source_url
        if isinstance(screen, ScreenshotObs)
        else None
    )
    if not url:
        raise ReviewEvidenceError(
            "missing_evidence", "No recorded document URL for observation alignment"
        )
    # Navigation history includes anchors; the capture stamp and DOM document
    # may keep the base URL. The loader must still identify the same document.
    if (
        isinstance(screen, ScreenshotObs)
        and screen.source_url
        and screen.source_url != url
        and (
            urldefrag(screen.source_url)[0] != urldefrag(url)[0]
            or not isinstance(meta, BrowserMetaObs)
            or not meta.loader_id
        )
    ):
        raise ReviewEvidenceError("alignment_error", "Screenshot and DOM document URLs disagree")
    try:
        dom = await clean_recorded_dom(
            trace,
            step,
            url=url,
            cleaner=capability.dom_cleaner,
            cleaner_key=capability.dom_cleaner.cleaner_type,
            serializer=capability.dom_serializer,
            budget=capability.dom_budget,
            max_text_chars=reviewer.evidence.max_text_chars,
            max_links=reviewer.evidence.max_links,
            loader_id=meta.loader_id if isinstance(meta, BrowserMetaObs) else None,
        )
    except BrowserEvidenceUnavailable as exc:
        raise ReviewEvidenceError("missing_evidence", str(exc)) from exc
    except BrowserEvidenceUnsupported as exc:
        raise ReviewEvidenceError("unsupported_input", str(exc)) from exc
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ReviewEvidenceError(
            "corrupt_evidence", f"Cannot reconstruct committed DOM: {exc}"
        ) from exc
    ref = _ref(trace, step)
    ref.files[dom.path] = dom.sha256
    pixels = trace.screenshot(step) if reviewer.evidence.include_screenshots else None
    image = _image(pixels) if pixels is not None else None
    if pixels is not None:
        ref.files["browser.screenshot"] = _digest(pixels)
    return ReviewObservation(
        url=url,
        text=dom.text,
        links=dom.links,
        geometry=dom.geometry,
        viewport=dom.viewport,
        captured_at=dom.captured_at,
        cycle=dom.cycle,
        scope={
            **dom.scope,
            "screenshot_view": "raw_browser",
            "dom_capture_url": dom.url,
            "screenshot_source_url": screen.source_url
            if isinstance(screen, ScreenshotObs)
            else None,
            "alignment": "document_url_and_loader; fragment retained in observation URL",
            "screenshot_captured_at": screen.captured_at
            if isinstance(screen, ScreenshotObs)
            else None,
        },
        image=image,
    ), ref


async def prepare_case(pair: ReviewPair, reviewer: BaseReplayReviewer) -> ReviewCase:
    """No browser, current website, model invocation, or write to either trace."""
    if any(not (Path(path) / "manifest.json").is_file() for path in (pair.source, pair.replay)):
        raise ReviewEvidenceError("missing_source", "A source or replay trace directory is missing")
    try:
        source, replay = RunTrace(Path(pair.source)), RunTrace(Path(pair.replay))
        if not source.config.recording.trajectory:
            raise ReviewEvidenceError(
                "scope_unrecorded", "Source actions were not selected for recording"
            )
        if any(step <= pair.source_step for step in source.manifest.missing_steps) or any(
            step <= pair.replay_step for step in replay.manifest.missing_steps
        ):
            raise ReviewEvidenceError(
                "missing_evidence", "A gap prevents complete DOM history reconstruction"
            )
        original, current = source.step(pair.source_step), replay.step(pair.replay_step)
        if original is None or current is None:
            raise ReviewEvidenceError("missing_evidence", "A requested step is not committed")
        branch = replay.config.branch
        if (
            branch is None
            or branch.source_run_id != source.run_id
            or current.restoration is None
            or current.restoration.step != pair.source_step
            or pair.replay_step != pair.source_step
        ):
            raise ReviewEvidenceError(
                "alignment_error", "Replay step has no matching source restoration evidence"
            )
        config_path = Path(pair.source_config)
        if not config_path.is_file():
            raise ReviewEvidenceError("missing_source", "Source run configuration is missing")
        composer = ConfigComposer(registrars=collect_registrars())
        source_config = composer.top_level_type.model_validate(
            yaml.safe_load(config_path.read_text())
        )
        configured = _browser_config(source_config.model_dump(mode="json"))
        # Instantiate the recorded browser recipe once, without constructing the
        # source Agent's model, hooks or loaders. Offline evidence needs none of
        # their credentials or runtime resources.
        cap = composer.compose({"capability": [configured]}).single("capability")
        recorded = _browser_config(source.config.resolved)
        recipe_keys = (
            "dom_cleaner",
            "dom_serializer",
            "dom_max_elements",
            "dom_max_serialized_chars",
            "dom_interactive_only",
            "dom_viewport_only",
        )
        for key in recipe_keys:
            if configured.get(key) != recorded.get(key):
                raise ReviewEvidenceError(
                    "alignment_error",
                    f"Source configuration differs from recorded DOM strategy field {key}",
                )
        reference, source_ref = await _observation(source, pair.source_step, cap, reviewer)
        restored, replay_ref = await _observation(replay, pair.replay_step, cap, reviewer)
        observed_at = restored.scope.get("screenshot_captured_at") or restored.captured_at
        try:
            replay_facts = recorded_replay_facts(replay, pair.replay_step)
            images = replay_images_not_served_in_viewport(
                replay,
                pair.replay_step,
                replay_log_recorded=replay.config.negotiation is not None
                and "browser.replay_log" in replay.config.negotiation.active_archivers,
                url=restored.url,
                loader_id=restored.scope.get("loader_id"),
                observed_at=observed_at,
            )
            captured = _capture_state_images(replay, pair.replay_step)
            source_captured = _capture_state_images(source, pair.source_step)
        except BrowserEvidenceUnavailable as exc:
            raise ReviewEvidenceError("missing_evidence", str(exc)) from exc
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ReviewEvidenceError(
                "corrupt_evidence", f"Cannot read recorded replay facts: {exc}"
            ) from exc
        readings = ViewportImageReadings(
            replay_log=images["in_viewport"] > 0 if images["measurable"] else None,
            capture_state=captured["failed"] if captured["measurable"] else None,
        )
        config = source.config
        facts = MachineFacts(
            site_host=urlsplit(reference.url).hostname,
            task=TaskIdentity(
                benchmark=config.benchmark, subset=config.subset, task_id=config.task_id
            )
            if config.task_id
            else None,
            restoration_warning=current.restoration.verdict != "ok",
            viewport_image_not_served=readings.combined(),
            viewport_image_sources=readings,
        )
        return ReviewCase(
            task=source.config.task,
            next_actions=original.actions,
            reference=reference,
            replay=restored,
            source_ref=source_ref,
            replay_ref=replay_ref,
            facts=facts,
            visual_required=pair.visual_required,
            machine_evidence={
                "replay": replay_facts,
                "viewport_images": {
                    "replay_log": {
                        **images,
                        "observed_at_source": "replay_screenshot"
                        if restored.scope.get("screenshot_captured_at") is not None
                        else "replay_dom_capture",
                    },
                    "capture_state": captured,
                    # For comparison only: an image already broken in the
                    # source fails in the replay's capture too.
                    "source_capture_state": source_captured,
                },
                "restoration": current.restoration.model_dump(mode="json"),
                "source_dom_recipe": {key: configured.get(key) for key in recipe_keys},
                "source_origin": original.origin,
                "replay_origin": current.origin,
                "replay_config_hash": replay.manifest.config_hash,
            },
        )
    except TraceSchemaError as exc:
        raise ReviewEvidenceError("corrupt_evidence", str(exc)) from exc
