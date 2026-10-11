"""Credential shapes: what must never appear in a trace or an exported dataset (arch §4.15, §5.6).

Shapes, not names. A blocklist of key *names* lags the next backend; these
catch a credential that reaches disk through a field nobody thought to mark,
whatever it is called. One list, three readers: the secret guard test scans
every byte a redacted run left on disk with it, the training export's
credential validator scans every text byte it is about to write
(``spectragent/export/validate.py``), failing closed on a hit, and the
flywheel metrics' privacy measure counts the hits under a chain's traces and
datasets (``spectragent/flywheel/metrics.py``).

They recognise credentials, not personal data in general: a typed password
that does not look like a key is invisible to them (arch §4.15 "隐私").

And one way of reading what they are matched against: :func:`readable_text`
(a file's bytes as text, gzip peeled first — the network archive's WARC is
gzip members, raw DOM captures are gzip JSON) and :func:`iter_readable`
(every file under a directory, by walking it). The secret guard test, the
browser archive's privacy test and the flywheel metrics' privacy measure
(arch §4.16) read a tree the same way.

Imports only the standard library: the trace layer's leaf, safe for any
reader to import.
"""

import gzip
import re
import zlib
from collections.abc import Iterator
from pathlib import Path

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


#: The two bytes every gzip member starts with.
GZIP_MAGIC = b"\x1f\x8b"
#: Layers of gzip peeled off one file at most — a ``.gz`` inside a ``.gz``.
_MAX_GZIP_LAYERS = 4


def readable_text(path: Path) -> str:
    """A file's bytes as text — gunzipped first when it is gzip.

    Gzip by its magic bytes (``1f 8b``) or by a ``.gz`` name, peeled
    repeatedly for a gzip inside a gzip; a multi-member gzip (a WARC) is read
    whole. A file that only looks like gzip and does not decompress is read
    as it is: a scan never skips bytes because they would not decode.
    Undecodable bytes become U+FFFD, so ASCII-shaped credentials still match.
    """
    data = path.read_bytes()
    named_gzip = path.name.endswith(".gz")
    for _ in range(_MAX_GZIP_LAYERS):
        if not (named_gzip or data[:2] == GZIP_MAGIC):
            break
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error):  # not gzip after all
            break
        named_gzip = False
    return data.decode("utf-8", errors="replace")


def iter_readable(root: Path) -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every file under *root*, in path order (:func:`readable_text`).

    Walks the directory rather than any index: a leak in a file nothing
    indexes is still a leak, and an index (a trace's manifest) is exactly
    what a careless writer would forget to list it in. A *root* that does
    not exist yields nothing.
    """
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, readable_text(path)


__all__ = ["GZIP_MAGIC", "SECRET_PATTERNS", "find_secrets", "iter_readable", "readable_text"]
