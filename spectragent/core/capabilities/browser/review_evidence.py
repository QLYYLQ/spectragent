"""Offline browser evidence extraction through the archive boundary and configured DOM strategies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urldefrag, urljoin

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.archive.dom_raw import (
    capture_from_document,
    read_dom_document,
)
from spectragent.core.capabilities.browser.dom.budget import DOMBudget, DOMBudgetPass
from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerProtocol
from spectragent.core.capabilities.browser.dom.models import DOMElementNode
from spectragent.core.capabilities.browser.dom.serializers import DOMSerializerProtocol
from spectragent.core.capabilities.browser.replay.replay_log import iter_decisions
from spectragent.core.capabilities.browser.replay.rule_snapshot import recorded_site_rules


@dataclass(frozen=True)
class CleanDOMEvidence:
    url: str
    text: str
    links: list[dict[str, Any]]
    geometry: list[dict[str, Any]]
    viewport: list[int]
    captured_at: float
    cycle: int | None
    path: str
    sha256: str
    scope: dict[str, Any]


class BrowserEvidenceUnavailable(ValueError):
    """Committed captures do not identify a unique requested observation."""


class BrowserEvidenceUnsupported(ValueError):
    """The configured cleaner cannot consume the archived capture dialect."""


def recorded_replay_facts(reader: ArchiveReader, step: int) -> dict[str, Any]:
    """Rule identity and serving evidence retain their own collection ownership."""
    fingerprint = reader.archive_run_file("browser")
    rules = (
        {
            "recorded": True,
            "snapshot": recorded_site_rules(reader).snapshot().model_dump(mode="json"),
            "archive_file": fingerprint.path,
            "archive_sha256": fingerprint.sha256,
        }
        if fingerprint is not None
        else {"recorded": False}
    )
    counts: dict[str, int] = {}
    for at, decision, count in iter_decisions(reader):
        if at == step:
            counts[decision.kind] = counts.get(decision.kind, 0) + count
    return {
        "rules": rules,
        "serving_counts": counts,
        "collection_step": step,
        "action_causation_inferred": False,
        "log_files": {
            entry.path: entry.sha256 for entry in reader.archive_files(step, "browser.replay_log")
        },
    }


def _walk(node: DOMElementNode | None):
    if node is not None:
        yield node
        for child in node.composed_children():
            yield from _walk(child)


async def clean_recorded_dom(
    reader: ArchiveReader,
    step: int,
    *,
    url: str,
    cleaner: DOMCleanerProtocol,
    cleaner_key: str,
    serializer: DOMSerializerProtocol,
    budget: DOMBudget,
    max_text_chars: int,
    max_links: int,
    loader_id: str | None = None,
) -> CleanDOMEvidence:
    """Resolve exactly one current document, retaining prior selector-map context.

    Ambiguous same-URL captures are rejected, rather than silently assigning one
    tab's pixels to another tab's text. All reads follow the committed archive.
    """
    previous: dict[str, Any] = {}
    selected = []
    for at in range(step + 1):
        for entry in reader.archive_files(at, "browser.dom_raw"):
            document = read_dom_document(reader.read_extra(entry.path))
            capture_key = document.get("cleaner")
            if capture_key != cleaner_key and not (
                capture_key == "self_capture" and cleaner_key == "browser_use"
            ):
                raise BrowserEvidenceUnsupported(
                    f"Recorded capture {capture_key!r} cannot be cleaned with configured {cleaner_key!r}"
                )
            tab = document["tab"]
            viewport = document["viewport"]
            state = await cleaner.clean(
                capture_from_document(document["capture"]),
                (viewport[0], viewport[1]),
                previous_selector_map=previous.get(tab),
                device_pixel_ratio=document["device_pixel_ratio"],
            )
            previous[tab] = state.selector_map
            report = DOMBudgetPass(budget).apply(state, serializer.serialize)
            if at != step or urldefrag(document["url"])[0] != urldefrag(url)[0]:
                continue
            if loader_id is not None and document.get("loader_id") != loader_id:
                continue
            links = []
            for node in _walk(state.dom_tree):
                href = node.attributes.get("href")
                if not href or not node.is_visible or not node.should_display:
                    continue
                label = " ".join(
                    n.node_value or n.text_content for n in _walk(node) if n.is_visible
                ).strip()
                links.append({"text": label[:512], "url": urljoin(document["url"], href)[:2048]})
            geometry = []
            for index, node in state.selector_map.items():
                geometry.append(
                    {
                        "index": index,
                        "tag": node.tag_name,
                        "name": node.ax_name,
                        "bounds_document_css": asdict(node.bounds) if node.bounds else None,
                        "visible": node.is_visible,
                        "hit_test": asdict(node.hit_test) if node.hit_test else None,
                        "focus": next(
                            (p.value for p in node.ax_properties or [] if p.name == "focused"), None
                        ),
                    }
                )
            selected.append(
                CleanDOMEvidence(
                    url=document["url"],
                    text=state.serialized_dom[:max_text_chars],
                    links=links[:max_links],
                    geometry=geometry[:max_links],
                    viewport=viewport,
                    captured_at=entry.captured_at,
                    cycle=entry.cycle,
                    path=entry.path,
                    sha256=entry.sha256,
                    scope={
                        "source": "recleaned_raw_dom",
                        "cleaner": cleaner_key,
                        "text_chars_total": len(state.serialized_dom),
                        "text_chars_kept": min(len(state.serialized_dom), max_text_chars),
                        "links_total": len(links),
                        "links_kept": min(len(links), max_links),
                        "link_label_char_limit": 512,
                        "link_url_char_limit": 2048,
                        "geometry_total": len(geometry),
                        "geometry_kept": min(len(geometry), max_links),
                        "budget": report.model_dump(mode="json") if report else None,
                        "scroll": asdict(state.scroll_info),
                        "device_pixel_ratio": document["device_pixel_ratio"],
                        "loader_id": document.get("loader_id"),
                        "serializer_text_may_be_truncated": True,
                    },
                )
            )
    if len(selected) != 1:
        raise BrowserEvidenceUnavailable(
            f"Expected one committed DOM capture for this document at step {step}; found {len(selected)}"
        )
    return selected[0]
