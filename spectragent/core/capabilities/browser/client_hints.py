"""Chrome's own user-agent client hints, and the identifiers they can stand behind (§4.14).

A page reads the browser's identity twice: the user agent string
(``navigator.userAgent``, the ``User-Agent`` header) and the user-agent client
hints (``navigator.userAgentData``: brands, platform, mobile, and on request
the full versions, architecture and the rest; the ``Sec-CH-UA*`` headers).
``replayability: pinned`` sets the first with ``Emulation.setUserAgentOverride``;
an override without ``userAgentMetadata`` empties the second — no brands, no
``Sec-CH-UA`` — which no real Chrome does, so a site can see the identifier is
not what it claims (measured, Chrome 155).

So the pinned override carries Chrome's own client hints, read from the
browser right after launch (:func:`measure_client_hints`), whenever the
identifier is Chrome's own form (:func:`chrome_identifier`). Then a page sees
exactly what this Chrome shows by itself under the same launch: the low-entropy
hints are its own (brands of its version, its platform, not mobile), and the
high-entropy ones are what Chrome itself reports when launched with
``--user-agent`` — empty (measured, Chrome 155: architecture, bitness, the full
version list, platform version and form factors, in ``getHighEntropyValues``
and in the headers alike). A Chrome identifier that names another version, OS
or form than the browser's own hints cannot be stood behind; the capability
refuses it (:func:`identifier_problems`).

``navigator.userAgentData`` exists only in a secure context, and the first
tab's ``about:blank`` is not one (measured): the read is made in a temporary
``chrome://version`` tab, a secure page Chrome serves itself (no network),
opened in the background, read in an isolated world and closed again before
any tab is adopted.
"""

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Where the read is made: a secure context Chrome serves itself.
CLIENT_HINTS_URL = "chrome://version/"
#: The isolated world the read runs in (``Page.createIsolatedWorld``).
CLIENT_HINTS_WORLD = "af-client-hints"
#: Every hint ``getHighEntropyValues`` reports; brands, mobile and platform come with any.
HIGH_ENTROPY_HINTS = (
    "architecture",
    "bitness",
    "formFactors",
    "fullVersionList",
    "model",
    "platformVersion",
    "uaFullVersion",
    "wow64",
)
#: What the read evaluates: ``null`` in a frame that is not (yet) a secure context.
CLIENT_HINTS_PROBE = (
    "(async () => { const d = navigator.userAgentData;"
    " if (!d) return JSON.stringify(null);"
    f" return JSON.stringify(await d.getHighEntropyValues({json.dumps(list(HIGH_ENTROPY_HINTS))}));"
    " })()"
)

#: Chrome's own user agent string, in the form Chrome writes it: the platform,
#: the engine tokens, ``Chrome/<version>`` and ``Safari/537.36`` (``Mobile
#: Safari`` on a phone), nothing after. Edge (``Edg/``), Opera (``OPR/``) and
#: headless Chrome (``HeadlessChrome/``) do not match.
_CHROME_IDENTIFIER = re.compile(
    r"^Mozilla/5\.0 \((?P<platform>[^()]+)\) AppleWebKit/537\.36 \(KHTML, like Gecko\) "
    r"Chrome/(?P<major>\d+)(?:\.\d+){0,3} (?P<mobile>Mobile )?Safari/537\.36$"
)
#: The identifier's platform token → the client hint ``platform`` Chrome reports there.
_PLATFORMS = (
    ("Android", "Android"),
    ("CrOS", "Chrome OS"),
    ("Macintosh", "macOS"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
)


@dataclass(frozen=True)
class ChromeIdentifier:
    """What a user agent string in Chrome's own form claims."""

    #: The major version after ``Chrome/``.
    major: str
    #: The client hint ``platform`` Chrome reports on that platform; ``None`` when unknown.
    platform: str | None
    mobile: bool


def chrome_identifier(user_agent: str | None) -> ChromeIdentifier | None:
    """*user_agent* parsed when it is Chrome's own form; ``None`` for any other string."""
    if not user_agent:
        return None
    match = _CHROME_IDENTIFIER.match(user_agent)
    if match is None:
        return None
    token = match.group("platform")
    platform = next((name for key, name in _PLATFORMS if key in token), None)
    return ChromeIdentifier(
        major=match.group("major"), platform=platform, mobile=match.group("mobile") is not None
    )


def _brand_list(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return ()
    return tuple(
        (str(item.get("brand", "")), str(item.get("version", "")))
        for item in raw
        if isinstance(item, Mapping)
    )


@dataclass(frozen=True)
class ClientHints:
    """The user-agent client hints Chrome reports by itself (CDP's ``UserAgentMetadata``)."""

    #: ``(brand, major version)``, in Chrome's order (its GREASE brand included).
    brands: tuple[tuple[str, str], ...]
    full_version_list: tuple[tuple[str, str], ...] = ()
    full_version: str = ""
    platform: str = ""
    platform_version: str = ""
    architecture: str = ""
    model: str = ""
    mobile: bool = False
    bitness: str = ""
    wow64: bool = False
    form_factors: tuple[str, ...] = ()

    @classmethod
    def from_page(cls, values: Mapping[str, Any]) -> "ClientHints":
        """From what ``navigator.userAgentData.getHighEntropyValues`` resolved to."""
        return cls(
            brands=_brand_list(values.get("brands")),
            full_version_list=_brand_list(values.get("fullVersionList")),
            full_version=str(values.get("uaFullVersion") or ""),
            platform=str(values.get("platform") or ""),
            platform_version=str(values.get("platformVersion") or ""),
            architecture=str(values.get("architecture") or ""),
            model=str(values.get("model") or ""),
            mobile=bool(values.get("mobile")),
            bitness=str(values.get("bitness") or ""),
            wow64=bool(values.get("wow64")),
            form_factors=tuple(str(item) for item in values.get("formFactors") or ()),
        )

    @classmethod
    def from_record(cls, record: Any) -> "ClientHints | None":
        """The hints a fingerprint recorded (:meth:`as_metadata`'s shape); ``None`` for none."""
        if not isinstance(record, Mapping):
            return None
        return cls(
            brands=_brand_list(record.get("brands")),
            full_version_list=_brand_list(record.get("fullVersionList")),
            full_version=str(record.get("fullVersion") or ""),
            platform=str(record.get("platform") or ""),
            platform_version=str(record.get("platformVersion") or ""),
            architecture=str(record.get("architecture") or ""),
            model=str(record.get("model") or ""),
            mobile=bool(record.get("mobile")),
            bitness=str(record.get("bitness") or ""),
            wow64=bool(record.get("wow64")),
            form_factors=tuple(str(item) for item in record.get("formFactors") or ()),
        )

    def as_metadata(self) -> dict[str, Any]:
        """CDP's ``Emulation.UserAgentMetadata`` — also what the fingerprint records."""
        return {
            "brands": [{"brand": brand, "version": version} for brand, version in self.brands],
            "fullVersionList": [
                {"brand": brand, "version": version} for brand, version in self.full_version_list
            ],
            "fullVersion": self.full_version,
            "platform": self.platform,
            "platformVersion": self.platform_version,
            "architecture": self.architecture,
            "model": self.model,
            "mobile": self.mobile,
            "bitness": self.bitness,
            "wow64": self.wow64,
            "formFactors": list(self.form_factors),
        }

    def low_entropy(self) -> dict[str, Any]:
        """What every page reads without asking: ``navigator.userAgentData.toJSON()``."""
        return {
            "brands": [{"brand": brand, "version": version} for brand, version in self.brands],
            "mobile": self.mobile,
            "platform": self.platform,
        }

    @property
    def major(self) -> str | None:
        """The browser's major version: the ``Chromium`` brand's."""
        return next((version for brand, version in self.brands if brand == "Chromium"), None)

    def header(self) -> str:
        """``Sec-CH-UA`` as Chrome writes it from these brands."""
        return ", ".join(f'"{brand}";v="{version}"' for brand, version in self.brands)


def identifier_problems(user_agent: str, hints: ClientHints) -> list[str]:
    """How a Chrome identifier and this browser's own client hints disagree, one line each.

    Empty for an identifier not in Chrome's form: it is sent without client hints.
    """
    claimed = chrome_identifier(user_agent)
    if claimed is None:
        return []
    where = (
        f"replayability user_agent {user_agent!r} ↔ this browser's own client hints "
        f"({hints.header() or 'no brands'}; platform {hints.platform!r}, "
        f"mobile {str(hints.mobile).lower()})"
    )
    problems: list[str] = []
    if claimed.major != hints.major:
        browser = f"Chrome {hints.major}" if hints.major else "a browser with no Chromium brand"
        problems.append(
            f"{where}: the identifier says Chrome {claimed.major}, the browser is {browser}; "
            f"its brands would contradict the identifier — write the browser's version in "
            f"user_agent"
        )
    if claimed.platform is not None and claimed.platform != hints.platform:
        problems.append(
            f"{where}: the identifier's platform is {claimed.platform!r}, the browser reports "
            f"{hints.platform!r}"
        )
    if claimed.mobile != hints.mobile:
        problems.append(
            f"{where}: the identifier is {'a mobile' if claimed.mobile else 'a desktop'} Chrome, "
            f"the browser reports mobile={str(hints.mobile).lower()}"
        )
    return problems


class HintsClient(Protocol):
    """The slice of a CDP client the read uses."""

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...


async def measure_client_hints(client: HintsClient, *, timeout: float) -> ClientHints:
    """Chrome's own client hints, read right after launch in a page no override reaches.

    A temporary ``chrome://version`` tab (module docstring), opened in the
    background; the read is retried until its document is a secure context,
    within *timeout*. The tab is closed again, and gone from ``Target.getTargets``,
    before this returns.

    Raises:
        TimeoutError: The page did not report within *timeout*.
        RuntimeError: A command failed.
    """
    loop = asyncio.get_running_loop()
    created = await client.send(
        "Target.createTarget",
        {"url": CLIENT_HINTS_URL, "background": True},
        None,
        timeout=timeout,
    )
    target_id = str((created or {}).get("targetId") or "")
    if not target_id:
        raise RuntimeError("Target.createTarget returned no targetId")
    try:
        attached = await client.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, None, timeout=timeout
        )
        session_id = str((attached or {}).get("sessionId") or "")
        if not session_id:
            raise RuntimeError("Target.attachToTarget returned no sessionId")
        deadline = loop.time() + timeout
        last: Exception | None = None
        while True:
            try:
                values = await _read(client, session_id, timeout)
                if values is not None:
                    return ClientHints.from_page(values)
            except Exception as exc:  # the document is still being replaced
                last = exc
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{CLIENT_HINTS_URL} reported no client hints within {timeout:g}s"
                    + (f" (last error: {last!r})" if last else "")
                )
            await asyncio.sleep(0.05)
    finally:
        await _close(client, target_id, timeout)


async def _read(client: HintsClient, session_id: str, timeout: float) -> dict[str, Any] | None:
    tree = await client.send("Page.getFrameTree", {}, session_id, timeout=timeout)
    frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
    world = await client.send(
        "Page.createIsolatedWorld",
        {"frameId": frame["id"], "worldName": CLIENT_HINTS_WORLD},
        session_id,
        timeout=timeout,
    )
    reply = await client.send(
        "Runtime.evaluate",
        {
            "expression": CLIENT_HINTS_PROBE,
            "returnByValue": True,
            "awaitPromise": True,
            "contextId": (world or {})["executionContextId"],
        },
        session_id,
        timeout=timeout,
    )
    value = ((reply or {}).get("result") or {}).get("value")
    values = json.loads(value) if isinstance(value, str) else None
    return values if isinstance(values, dict) else None


async def _close(client: HintsClient, target_id: str, timeout: float) -> None:
    """Close the temporary tab and wait until Chrome no longer lists it (bounded)."""
    loop = asyncio.get_running_loop()
    try:
        await client.send("Target.closeTarget", {"targetId": target_id}, None, timeout=timeout)
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            reply = await client.send("Target.getTargets", {}, None, timeout=timeout)
            if all(
                info.get("targetId") != target_id for info in (reply or {}).get("targetInfos", [])
            ):
                return
            await asyncio.sleep(0.02)
        logger.warning(
            "client hints: the %s tab was still listed after %gs", CLIENT_HINTS_URL, timeout
        )
    except Exception as exc:
        logger.warning("client hints: closing the %s tab failed: %r", CLIENT_HINTS_URL, exc)
