"""The browser's restoration evidence: what it measures between two pages, and its checks.

``BrowserCapability`` names :class:`BrowserRestorationEvidence` as its
``RestorationEvidence`` (``docs/architecture_design.md`` §4.9, §4.14
"恢复：重定位与校验"): ``verify_restoration`` returns one per restored
observation (``restoration.py::measure``), and the trace reader reads a
recorded one back as this type by its ``capability_id``, as it reads an
action's ``Effect``. The measures are the URL, the interactive-element
signatures, the screenshot distance, the replay's main-frame document misses
and the media playback state; the checks are :data:`BrowserRestorationReason`,
judged by :meth:`BrowserRestorationEvidence.failed_checks` against the limits
the capability's own knobs set (``screenshot_max_distance``,
``media_time_tolerance_s``), which the evidence records with what it measured.

Pydantic models only: readers of recorded evidence (the flywheel metrics, the
lookahead tools) import this module without the browser implementation.
"""

from typing import ClassVar, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.restoration import RestorationEvidence

#: ``BrowserCapability``'s registry key: the ``capability_id`` every browser evidence carries.
BROWSER: Final = "browser"

#: The browser's checks, in the order they are reported: a page the replay could not
#: serve explains every mismatch after it; a page at another URL explains a different
#: element set, media state and picture.
BrowserRestorationReason = Literal[
    "document_miss",
    "url_mismatch",
    "signature_mismatch",
    "media_mismatch",
    "screenshot_distance",
]

#: How a media element of one observation corresponds to the other's
#: (``docs/architecture_design.md`` §4.14 "恢复：重定位与校验"). ``matched``:
#: exactly one counterpart, in the same place and order; ``missing``: a
#: recorded element with no counterpart; ``extra``: a restored element no
#: recorded one corresponds to; ``ambiguous``: candidates the position and
#: order cannot single out.
MediaCorrespondence = Literal["matched", "missing", "extra", "ambiguous"]

#: Which of the two readings around the screenshots a comparison used.
MediaReadingPoint = Literal["before", "after"]

#: What aligning one media element came to. ``aligned``: moved to the recorded
#: position and play state, the seek (and the play) completed in time;
#: ``already_aligned``: it was already there, nothing was done; ``failed``: the
#: seek or the play did not complete in time, and it was put back where it
#: was (``rolled_back``); ``not_restorable``: nothing can be aligned (a live
#: stream, an ambiguous or missing counterpart); ``unknown``: the recorded
#: position or play state is not known, so there was no target.
MediaAlignmentOutcome = Literal["aligned", "already_aligned", "failed", "not_restorable", "unknown"]

#: What alignment did to one element: paused and seeked; seeked and played; seeked a
#: short lead before the end and played through to the natural end (a recorded
#: element that had ended); or nothing.
MediaAlignmentOperation = Literal["pause_and_seek", "seek_and_play", "seek_and_play_to_end", "none"]


class MediaComparison(BaseModel):
    """One media element compared between a recorded and a restored observation.

    ``element`` is the capability's name for it (the browser's
    ``<frame>#<order> <tag>``). ``live``: either side is a live stream, which
    has no position to compare. The times and play states are the reading
    both sides took at the same point (``reading``: before the screenshot
    where both have it, else after); ``None`` where it was not measured,
    which never decides. Measures only: the media check applies the tolerance
    (:meth:`BrowserRestorationEvidence.failed_checks`).
    """

    model_config = ConfigDict(frozen=True)

    element: str
    correspondence: MediaCorrespondence
    live: bool = False
    reading: MediaReadingPoint | None = None
    recorded_time: float | None = None
    restored_time: float | None = None
    recorded_paused: bool | None = None
    restored_paused: bool | None = None
    detail: str = ""


class MediaAlignment(BaseModel):
    """What aligning one media element to the recorded state did, before a restored capture.

    ``target_*``: the recorded state aimed at; ``from_*``: the element before
    alignment; ``reached_*``, ``ready_state`` and ``seeking``: where it stood
    when alignment finished. ``seek_ms``: how long the seek took to complete
    (``None``: it did not, or none was made). ``rolled_back`` is set only for a
    ``failed`` alignment: whether putting the element back where it was
    completed. ``detail`` says why, in words.
    """

    model_config = ConfigDict(frozen=True)

    element: str
    outcome: MediaAlignmentOutcome
    operation: MediaAlignmentOperation = "none"
    target_time: float | None = None
    target_paused: bool | None = None
    from_time: float | None = None
    from_paused: bool | None = None
    reached_time: float | None = None
    reached_paused: bool | None = None
    ready_state: int | None = None
    seeking: bool | None = None
    seek_ms: float | None = Field(default=None, ge=0)
    rolled_back: bool | None = None
    detail: str = ""


class MediaEvidence(BaseModel):
    """Media playback state compared between two observations, and what was aligned first.

    ``comparisons`` is ``None`` when no comparison could be made — a side
    carries no media state, or both of its readings are unknown — and
    ``detail`` says why; an empty list means neither side has a media
    element. ``recorded_unreachable_frames`` / ``restored_unreachable_frames``
    name the embedded frames whose own media read did not answer on each
    side: what media they hold is unknown, so the step cannot be restored.
    ``alignment`` is ``None`` when no alignment was requested for the
    restored observation (the capability's ``align_media: false``); otherwise
    one entry per recorded element it considered, and ``alignment_detail``
    says why it is empty when it is.
    """

    model_config = ConfigDict(frozen=True)

    comparisons: list[MediaComparison] | None = None
    detail: str = ""
    recorded_unreachable_frames: list[str] = Field(default_factory=list)
    restored_unreachable_frames: list[str] = Field(default_factory=list)
    alignment: list[MediaAlignment] | None = None
    alignment_detail: str = ""


#: What a masked box covers: a media element (``<video>`` / ``<audio>``), or an
#: embedded frame whose own media read did not answer on that side, so what it
#: plays is unknown (on the declared video sites it is the player's frame).
MaskedAreaKind = Literal["media_element", "unreachable_frame"]


class MaskedArea(BaseModel):
    """One box masked out of a screenshot comparison.

    ``side``: whose media facts measured it (the mask is the union of both
    sides' boxes and is applied to both images); ``element``: the media
    element's name (the browser's ``<frame>#<order> <tag>``) or the frame's
    path. The box is in CSS pixels of the top viewport, clipped to it; a box
    that falls outside the viewport masks nothing and is not listed.
    """

    model_config = ConfigDict(frozen=True)

    side: Literal["recorded", "restored"]
    kind: MaskedAreaKind
    element: str
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class MediaAreaEvidence(BaseModel):
    """What a page's media-area rules set aside in one restoration measurement.

    Present only on a page a media-area rule declares (the browser's replay
    rule set, ``docs/architecture_design.md`` §4.14): ``rule_ids`` are the
    rules whose scope matched both page URLs. The media playback state is
    then not judged (the media check reads this), and ``areas`` are masked out
    of both screenshots before their distance. ``masked_share`` is the share
    of the image's pixels the mask covers (the larger of the two images;
    ``None`` without both images). ``unmasked_distance`` is the distance of
    the whole images, ``masked_distance`` the distance after masking.
    ``mask_applied`` says which one ``BrowserRestorationEvidence.screenshot_distance``
    carries: the masked one, unless the mask covers so much of the image
    that the rest says too little about the page — then the whole images'
    one, as on an undeclared page, never a pass by a nearly blank
    comparison. ``detail`` says why the mask was not applied, why a side's
    boxes are unknown, or that there was no pair of images.
    """

    model_config = ConfigDict(frozen=True)

    rule_ids: list[str] = Field(min_length=1)
    areas: list[MaskedArea] = Field(default_factory=list)
    masked_share: float | None = Field(default=None, ge=0, le=1)
    unmasked_distance: int | None = Field(default=None, ge=0)
    masked_distance: int | None = Field(default=None, ge=0)
    mask_applied: bool = False
    detail: str = ""


def media_failed(media: MediaEvidence, *, time_tolerance_s: float) -> bool:
    """Whether media evidence fails (``docs/architecture_design.md`` §4.14).

    Fails on an element present on one side only, an ambiguous
    correspondence, a live stream, a matched element whose two known
    positions are more than *time_tolerance_s* apart or whose two known
    paused states differ, an embedded frame whose own media read did not
    answer on either side (what it holds is unknown, so the step cannot be
    restored), and on an alignment that failed or could not be done.
    Unknown values — a comparison that could not be made, a position or
    state not measured, an alignment with no recorded target — never decide;
    without a comparison the unreachable frames do not decide either.
    """
    if media.comparisons is not None and (
        media.recorded_unreachable_frames or media.restored_unreachable_frames
    ):
        return True
    for item in media.comparisons or []:
        if item.correspondence != "matched" or item.live:
            return True
        if (
            item.recorded_time is not None
            and item.restored_time is not None
            and abs(item.recorded_time - item.restored_time) > time_tolerance_s
        ):
            return True
        if (
            item.recorded_paused is not None
            and item.restored_paused is not None
            and item.recorded_paused != item.restored_paused
        ):
            return True
    return any(record.outcome in ("failed", "not_restorable") for record in media.alignment or [])


class BrowserRestorationEvidence(RestorationEvidence):
    """What the browser measured between a recorded and a restored page, and the limits it judges by.

    Every measure is ``None`` when the browser does not measure it or could
    not this time (no screenshot fragment in this run, say); a ``None`` never
    takes part in a check. ``signature_missing`` / ``signature_extra`` detail
    a signature comparison: elements of the recorded page absent from the
    restored one, and the reverse (both 0 when no comparison was made).
    ``media`` compares media playback state and records the media alignment
    done before the restored capture (:class:`MediaEvidence`).

    ``recorded_url`` / ``restored_url`` are the two page URLs as observed;
    ``url_match`` compares them after leaving out the query parameters a
    site's page-URL rules declare (the replay rule set, §4.14).
    ``url_ignored_query`` names the parameters left out of either URL,
    ``url_rule_ids`` the rules that left them out; both are empty when the
    URLs were compared exactly.

    ``media_areas`` is set only on a page a site's media-area rules declare
    (:class:`MediaAreaEvidence`): ``media`` is still measured and kept, but
    not judged, and ``screenshot_distance`` is the distance with the media
    areas masked out of both images (the whole images' distance when the
    mask would cover too much of them).

    ``screenshot_max_distance`` and ``media_time_tolerance_s`` are the limits
    the checks apply: the ``BrowserCapability`` knobs of the same names when
    it measured. ``capability_id`` is always ``browser``.
    """

    REASONS: ClassVar[tuple[str, ...]] = get_args(BrowserRestorationReason)

    #: Pinned to ``BrowserCapability``'s registry key: a browser evidence can
    #: name no other capability, and the trace reader finds this type by it.
    capability_id: Literal["browser"] = BROWSER
    url_match: bool | None = None
    recorded_url: str | None = None
    restored_url: str | None = None
    url_ignored_query: list[str] = Field(default_factory=list)
    url_rule_ids: list[str] = Field(default_factory=list)
    signature_match: bool | None = None
    signature_missing: int = Field(default=0, ge=0)
    signature_extra: int = Field(default=0, ge=0)
    screenshot_distance: int | None = Field(default=None, ge=0)
    screenshot_max_distance: int = Field(ge=0, le=64)
    document_misses: int | None = Field(default=None, ge=0)
    media: MediaEvidence | None = None
    media_time_tolerance_s: float = Field(ge=0, le=60)
    media_areas: MediaAreaEvidence | None = None

    def failed_checks(self) -> list[str]:
        """The browser's checks this measurement does not pass, in :attr:`REASONS` order.

        - ``document_miss`` — the replay could not serve main-frame documents;
        - ``url_mismatch`` — the restored URL is not the recorded one;
        - ``signature_mismatch`` — the interactive elements differ;
        - ``media_mismatch`` — the media evidence fails (:func:`media_failed`),
          except on a page a media-area rule declares (``media_areas``);
        - ``screenshot_distance`` — the screenshots lie further apart than
          ``screenshot_max_distance``.
        """
        failed: list[str] = []
        if self.document_misses is not None and self.document_misses > 0:
            failed.append("document_miss")
        if self.url_match is False:
            failed.append("url_mismatch")
        if self.signature_match is False:
            failed.append("signature_mismatch")
        if (
            self.media is not None
            and self.media_areas is None
            and media_failed(self.media, time_tolerance_s=self.media_time_tolerance_s)
        ):
            failed.append("media_mismatch")
        if (
            self.screenshot_distance is not None
            and self.screenshot_distance > self.screenshot_max_distance
        ):
            failed.append("screenshot_distance")
        return failed


__all__ = [
    "BROWSER",
    "BrowserRestorationEvidence",
    "BrowserRestorationReason",
    "MaskedArea",
    "MaskedAreaKind",
    "MediaAlignment",
    "MediaAlignmentOperation",
    "MediaAlignmentOutcome",
    "MediaAreaEvidence",
    "MediaComparison",
    "MediaCorrespondence",
    "MediaEvidence",
    "MediaReadingPoint",
    "media_failed",
]
