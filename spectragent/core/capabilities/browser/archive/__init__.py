"""Browser archivers — the browser's half of the archive axis (§4.14).

Importing this package registers the four archivers under
``ArchiverRegistrar`` — ``browser.network``, ``browser.storage``,
``browser.dom_raw`` and ``browser.replay_log`` — which
``BrowserCapability.__wiring__["archiver"]`` declares as provided (conscribe
drops a wiring entry whose keys do not resolve, so the classes are imported
before the capability class exists).

See :mod:`.base` for the configured-instance / run-time-session split,
:mod:`.run` for one recording's archive side, :mod:`.redaction` for the
browser's privacy rules.
"""

from spectragent.core.capabilities.browser.archive.base import (
    SELF_CAPTURE,
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    ParkedRaw,
    RawCapture,
    RawCaptureStore,
    RecorderHandle,
    ReplayLogFeed,
    archive_needs,
)
from spectragent.core.capabilities.browser.archive.dom_raw import DomRawArchiver
from spectragent.core.capabilities.browser.archive.network import NetworkArchiver
from spectragent.core.capabilities.browser.archive.redaction import (
    BROWSER_REDACTION_POLICY,
    redact_post_body,
)
from spectragent.core.capabilities.browser.archive.replay_log import ReplayLogArchiver
from spectragent.core.capabilities.browser.archive.run import BrowserArchiveRun
from spectragent.core.capabilities.browser.archive.storage import StorageArchiver

__all__ = [
    "BROWSER_REDACTION_POLICY",
    "SELF_CAPTURE",
    "ParkedRaw",
    "ArchiveNeed",
    "BrowserArchiveContext",
    "BrowserArchiveRun",
    "BrowserArchiveSession",
    "BrowserArchiverBase",
    "DomRawArchiver",
    "NetworkArchiver",
    "RawCapture",
    "RawCaptureStore",
    "RecorderHandle",
    "ReplayLogArchiver",
    "ReplayLogFeed",
    "StorageArchiver",
    "archive_needs",
    "redact_post_body",
]
