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
from spectragent.core.capabilities.browser.replay.replay_log import iter_decisions, replay_log
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


#: What the viewport image rule cannot see; every measured result names it.
VIEWPORT_IMAGE_RULE_NOT_COVERED = (
    "CSS background images from stylesheets",
    "references inside subframe documents",
)
#: Served answers: the archive record itself, its last record repeated, or an
#: available record with the identical body answering for one without a body.
_SERVED_KINDS = frozenset({"hit", "exhausted", "substituted"})


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
            if at != step or not _same_document(document, url, loader_id):
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


def _same_document(document: dict[str, Any], url: str, loader_id: str | None) -> bool:
    return urldefrag(document["url"])[0] == urldefrag(url)[0] and (
        loader_id is None or document.get("loader_id") == loader_id
    )


def _in_viewport(bounds: list[float], scroll: tuple[float, float], viewport: list[int]) -> bool:
    x, y, w, h = bounds[:4]
    return (
        w > 1
        and h > 1
        and x < scroll[0] + viewport[0]
        and x + w > scroll[0]
        and y < scroll[1] + viewport[1]
        and y + h > scroll[1]
    )


def _references(snapshot_document: dict[str, Any], strings: list[str], urls: set[str]):
    """Node index -> missed URLs it references (attribute value contains it, or current source)."""
    nodes = snapshot_document.get("nodes") or {}
    found: dict[int, set[str]] = {}
    for index, flat in enumerate(nodes.get("attributes") or []):
        for value_index in flat[1::2]:
            value = strings[value_index] if 0 <= value_index < len(strings) else ""
            for url in urls:
                if url in value:
                    found.setdefault(index, set()).add(url)
    current = nodes.get("currentSourceURL") or {}
    for index, value_index in zip(
        current.get("index") or [], current.get("value") or [], strict=False
    ):
        value = strings[value_index] if 0 <= value_index < len(strings) else ""
        if value in urls:
            found.setdefault(index, set()).add(value)
    return found


def replay_images_not_served_in_viewport(
    reader: ArchiveReader,
    step: int,
    *,
    replay_log_recorded: bool,
    url: str,
    loader_id: str | None,
    observed_at: float | None,
    max_listed: int = 20,
) -> dict[str, Any]:
    """Image requests the replay did not serve that an element in the viewport references.

    Only the reviewed step's own collection counts, keeping decisions that
    happened no later than ``observed_at`` (a folded summary counts from its
    first event). Earlier collections are left out on purpose: a single-page
    application keeps one document across many steps, and an image missed
    there may since have been served or left the page. An image URL counts
    when the replay answered it as a miss (strict: a 404 or a failure),
    whether or not it also served it. Its reference is an attribute value of
    a main-document node that contains the URL, or the node's
    ``currentSourceURL``; it is in the viewport when that node's layout box
    meets the viewport. :data:`VIEWPORT_IMAGE_RULE_NOT_COVERED` lists what
    this cannot see. ``measurable`` is false, with a reason, when the replay
    log or the DOM snapshot layout is missing: unknown is never zero.
    """
    if not replay_log_recorded:
        return {"measurable": False, "reason": "the replay log was not recorded"}
    selected = [
        document
        for entry in reader.archive_files(step, "browser.dom_raw")
        if _same_document(
            document := read_dom_document(reader.read_extra(entry.path)), url, loader_id
        )
    ]
    if len(selected) != 1:
        raise BrowserEvidenceUnavailable(
            f"Expected one committed DOM capture for this document at step {step}; found {len(selected)}"
        )
    document = selected[0]
    decisions: dict[str, dict[str, Any]] = {}
    for entry in replay_log(reader, step):
        if entry.resource_type != "Image":
            continue
        kind = entry.folded_kind if entry.kind == "summary" else entry.kind
        weight = entry.count if entry.kind == "summary" and entry.count else 1
        occurred = entry.first_captured_at if entry.kind == "summary" else entry.captured_at
        if observed_at is not None and occurred is not None and occurred > observed_at:
            continue
        record = decisions.setdefault(
            entry.url, {"misses": 0, "served": 0, "statuses": set(), "reasons": set()}
        )
        if kind == "miss":
            record["misses"] += weight
            if entry.status is not None:
                record["statuses"].add(entry.status)
            if entry.reason is not None:
                record["reasons"].add(entry.reason)
        elif kind in _SERVED_KINDS:
            record["served"] += weight
    missed = {image for image, record in decisions.items() if record["misses"]}
    measured: dict[str, Any] = {
        "measurable": True,
        "collection_step": step,
        "observed_at": observed_at,
        "images_not_served": len(missed),
        "referenced_in_main_document": 0,
        "in_viewport": 0,
        "referenced_only_in_subframes": 0,
        "listed": [],
        "listed_total": 0,
        "not_covered": list(VIEWPORT_IMAGE_RULE_NOT_COVERED),
    }
    if not missed:
        return measured
    snapshot = (document.get("capture") or {}).get("snapshot") or {}
    documents = snapshot.get("documents") or []
    if not documents:
        return {
            "measurable": False,
            "reason": "the raw DOM capture has no DOM snapshot layout",
            "collection_step": step,
            "images_not_served": len(missed),
        }
    strings = snapshot.get("strings") or []
    dpr = document.get("device_pixel_ratio") or 1.0
    main = documents[0]
    layout = main.get("layout") or {}
    bounds_of = {
        node: [value / dpr for value in bounds]
        for node, bounds in zip(
            layout.get("nodeIndex") or [], layout.get("bounds") or [], strict=False
        )
        if len(bounds) >= 4
    }
    scroll = (main.get("scrollOffsetX", 0.0) / dpr, main.get("scrollOffsetY", 0.0) / dpr)
    referenced: set[str] = set()
    visible: set[str] = set()
    for node, urls in _references(main, strings, missed).items():
        referenced |= urls
        bounds = bounds_of.get(node)
        if bounds is not None and _in_viewport(bounds, scroll, document["viewport"]):
            visible |= urls
    in_subframes: set[str] = set()
    for sub in documents[1:]:
        for urls in _references(sub, strings, missed - referenced).values():
            in_subframes |= urls
    listed = [
        {
            "url": image[:2048],
            "in_viewport": image in visible,
            "referenced_in_main_document": image in referenced,
            "misses": decisions[image]["misses"],
            "served": decisions[image]["served"],
            "statuses": sorted(decisions[image]["statuses"]),
            "reasons": sorted(decisions[image]["reasons"]),
        }
        for image in sorted(missed, key=lambda u: (u not in visible, u not in referenced, u))
    ]
    return {
        **measured,
        "referenced_in_main_document": len(referenced),
        "in_viewport": len(visible),
        "referenced_only_in_subframes": len(in_subframes),
        "listed": listed[:max_listed],
        "listed_total": len(listed),
    }
