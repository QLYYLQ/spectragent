"""Credential shapes: what must never appear in a trace or an exported dataset (arch §4.15, §5.6).

Shapes, not names. A blocklist of key *names* lags the next backend; these
catch a credential that reaches disk through a field nobody thought to mark,
whatever it is called. One list, two readers: the secret guard test scans
every byte a redacted run left on disk with it, and the training export's
credential validator scans every text byte it is about to write
(``spectragent/export/validate.py``), failing closed on a hit.

They recognise credentials, not personal data in general: a typed password
that does not look like a key is invisible to them (arch §4.15 "隐私").

Imports only ``re``: the trace layer's leaf, safe for any reader to import.
"""

import re

#: ``(label, pattern)`` per credential shape, in a fixed order.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style api key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("anthropic-style api key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}")),
    ("url with credentials", re.compile(r"\b[a-z]+://[^\s/:@\"]+:[^\s/@\"]+@")),
    ("url carrying a token", re.compile(r"[?&](access_token|token|key)=[A-Za-z0-9._-]{8,}")),
    ("websocket devtools token", re.compile(r"\bwss?://[^\s\"]*devtools/browser/[A-Za-z0-9-]{8,}")),
)


def find_secrets(text: str) -> list[tuple[str, re.Match[str]]]:
    """Every credential-shaped span of *text*: ``(label, match)``, in pattern order.

    Within one pattern the matches come in the order they appear. Callers
    that report a hit should name the label and where it was found, not
    echo the match: the point is that the value goes no further.
    """
    return [
        (label, match) for label, pattern in SECRET_PATTERNS for match in pattern.finditer(text)
    ]


__all__ = ["SECRET_PATTERNS", "find_secrets"]
