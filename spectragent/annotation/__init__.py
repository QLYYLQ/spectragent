"""Human labels for review queues and run steps, kept outside every trace.

Tools write review queues into an annotation directory; people label them
in the viewer, which appends label lines there and nowhere else
(``docs/architecture_design.md`` §4.17).
"""

from spectragent.annotation.labels import LabelError, LabelLog, compute_verdict
from spectragent.annotation.models import (
    ADHOC_IMAGE,
    ADHOC_QUEUE_ID,
    LABEL_FORMAT,
    QUEUE_FORMAT,
    ArchiveImage,
    ImageSource,
    ItemField,
    ItemLink,
    LabelChoice,
    LabelDraft,
    LabelLine,
    LabelSpec,
    ObservationImage,
    QueueImage,
    QueueItem,
    Region,
    RegionKind,
    RegionSpec,
    ReviewQueue,
    Suggestion,
)
from spectragent.annotation.store import (
    ADHOC_SPEC,
    AnnotationError,
    AnnotationStore,
    LoadedQueue,
    QueueListing,
    write_queue,
)

__all__ = [
    "ADHOC_IMAGE",
    "ADHOC_QUEUE_ID",
    "ADHOC_SPEC",
    "LABEL_FORMAT",
    "QUEUE_FORMAT",
    "AnnotationError",
    "AnnotationStore",
    "ArchiveImage",
    "ImageSource",
    "ItemField",
    "ItemLink",
    "LabelChoice",
    "LabelDraft",
    "LabelError",
    "LabelLine",
    "LabelLog",
    "LabelSpec",
    "LoadedQueue",
    "ObservationImage",
    "QueueImage",
    "QueueItem",
    "QueueListing",
    "Region",
    "RegionKind",
    "RegionSpec",
    "ReviewQueue",
    "Suggestion",
    "compute_verdict",
    "write_queue",
]
