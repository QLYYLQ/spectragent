"""Browser restoration evidence for tests: the measures a test names, the default limits.

``BrowserRestorationEvidence`` records the limits its checks apply (the
``BrowserCapability`` knobs ``screenshot_max_distance`` and
``media_time_tolerance_s``); a test that is about a measure gets the
capability's defaults for them unless it names its own.
"""

from typing import Any

from spectragent.core.capabilities.browser.restoration_evidence import (
    BrowserRestorationEvidence,
)

#: The ``BrowserCapability`` defaults for the limits its restoration checks apply.
DEFAULT_LIMITS: dict[str, Any] = {"screenshot_max_distance": 5, "media_time_tolerance_s": 1.0}


def browser_evidence(**fields: Any) -> BrowserRestorationEvidence:
    """The browser's evidence with *fields*, and the default limits unless given."""
    return BrowserRestorationEvidence(**{**DEFAULT_LIMITS, **fields})
