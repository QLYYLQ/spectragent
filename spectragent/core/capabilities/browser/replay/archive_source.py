"""``archive`` — replay the network from a recorded run's archive (§4.14 "回放").

Every request Chrome makes is paused at the Fetch Request stage on the
recorder connection and answered from the recorded run's ``browser.network``
archive — or, when the archive has nothing for it, by the ``miss_policy``
receptor (fail, 204 or 404) — and never continued to the network.

Knobs (all reachable from YAML; the generated config nests ``matcher`` and the
``miss_policy`` union inside this source's block):

- ``matcher`` — :class:`~.matcher.MatcherConfig`: the levels to try, the fuzzy
  threshold, the site-rules directory;
- ``isolation`` — ``required`` (the default): the browser is launched cut off
  from the network, ``localhost`` included (``--host-resolver-rules=MAP *
  ~NOTFOUND`` and ``--no-proxy-server``), by a provider declaring
  ``NETWORK_ISOLATION``; ``none_acknowledged`` says, in writing, to rely on
  Fetch interception alone (which leaks connections Fetch never sees);
- ``shift_dates`` — ``none`` (the default; pairs with ``base_time:
  from_archive``) or ``all`` (``Date`` / ``Expires`` / ``Last-Modified`` /
  cookie expiry shifted together by now − record time; pairs with
  ``base_time: run_start``) — never a subset;
- ``max_fulfill_body_bytes`` — the largest body fulfilled in one CDP message
  (64 MiB; at most 72 MiB, since one message is capped at 100 MiB and base64
  inflates by a third); a larger one is a ``too_large`` miss;
- ``fulfill_timeout_s`` — the replay server's budget per paused request;
- ``log_repeat_limit`` — identical log entries per collection before they fold
  into one ``summary`` (a polling page can miss a thousand times a minute).

**Not a dataclass**, and ``__init__`` is hand-written: the source carries the
``miss_policy`` receptor, and conscribe reads ``inspect.signature(__init__)``
at class creation, before ``@dataclass`` would synthesise one (af-add-a-receptor
§6). ``strict`` is imported above the class so its key is registered when
``__wiring__`` is read.

.. note::
   This module — unlike the other strategy modules of the package — uses
   ``from __future__ import annotations``, for the reason ``capability.py``
   does: conscribe writes a ``.pyi`` stub next to every class with an
   injected receptor (``miss_policy``) and renders its ``__init__`` signature
   as text. Evaluated ``Annotated[..., Field(...)]`` annotations come out as
   ``FieldInfo(...)`` reprs (an invalid stub, measured: 25 pyright errors);
   PEP 563 strings come out verbatim. The trap the package rule guards
   against — a subclass resolving an inherited ``__init__``'s strings in its
   own globals and dropping the field — cannot happen here: the class defines
   its own ``__init__`` and nothing subclasses it, and
   ``tests/test_generated/test_no_dropped_annotated_fields.py`` would catch a
   dropped knob.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field as PydanticField
from pydantic import TypeAdapter

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.replay.archive_index import (
    RecordedArchive,
    load_archive,
)
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from spectragent.core.capabilities.browser.replay.miss_policy import (
    MissPolicyProtocol,
    default_miss_policy,
)
from spectragent.core.capabilities.browser.replay.network_source import (
    Isolation,
    NetworkSourceBase,
)
from spectragent.core.capabilities.browser.replay.server import ReplayContext, ReplayServer
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from spectragent.core.capabilities.browser.replay.strict import (  # noqa: F401 — registration
    StrictMissPolicy,
)

MIB = 1024 * 1024
#: The default fulfil cap.
DEFAULT_FULFILL_BODY_BYTES = 64 * MIB
#: The largest fulfil cap: base64 (4/3) of 72 MiB plus the JSON envelope stays
#: under the 100 MiB a single CDP message may carry (S2a).
MAX_FULFILL_BODY_BYTES = 72 * MIB

_MATCHER: TypeAdapter[MatcherConfig] = TypeAdapter(MatcherConfig)
#: The ``matcher`` default (frozen, so one shared instance is safe).
DEFAULT_MATCHER = MatcherConfig()


class ArchiveNetworkSource(NetworkSourceBase):
    """Serve the network from a recorded archive; never go to the network."""

    __registry_key__ = "archive"
    __wiring__: ClassVar[dict[str, Any]] = {"miss_policy": ("miss_policy", ["strict"])}

    # Annotation only: conscribe installs a ``WiredField`` and the Composer
    # injects the configured policy; ``__init__`` shadows it with the default.
    miss_policy: MissPolicyProtocol  # type: ignore[misc]

    # ── the knobs, as attributes (assigned in __init__) ──
    # Declared so pyright and conscribe's stub writer both see them.
    matcher: MatcherConfig
    shift_dates: Literal["none", "all"]
    max_fulfill_body_bytes: int
    fulfill_timeout_s: float
    log_repeat_limit: int
    _isolation: Isolation

    def __init__(
        self,
        *,
        matcher: Annotated[
            MatcherConfig,
            PydanticField(
                description=(
                    "How requests are matched to the recording: levels (exact, "
                    "normalized, fuzzy), fuzzy_threshold, site_rules directory."
                ),
            ),
        ] = DEFAULT_MATCHER,
        isolation: Annotated[
            Isolation,
            PydanticField(
                description=(
                    "'required': launch the browser cut off from the network "
                    "(needs a provider declaring NETWORK_ISOLATION, e.g. "
                    "local_chrome). 'none_acknowledged': rely on Fetch "
                    "interception alone, which Fetch-invisible traffic "
                    "(preconnects, WebSockets) escapes."
                ),
            ),
        ] = "required",
        shift_dates: Annotated[
            Literal["none", "all"],
            PydanticField(
                description=(
                    "'none': serve recorded Date/Expires/Last-Modified/cookie "
                    "dates as recorded (pairs with base_time: from_archive). "
                    "'all': shift all four by now minus the record time (pairs "
                    "with base_time: run_start). Never a subset."
                ),
            ),
        ] = "none",
        max_fulfill_body_bytes: Annotated[
            int,
            PydanticField(
                ge=1,
                # MAX_FULFILL_BODY_BYTES, spelled out: conscribe copies this
                # annotation into a stub that cannot see module constants.
                le=75_497_472,
                description=(
                    "Largest body fulfilled in one CDP message; a larger one is "
                    "a too_large miss. At most 72 MiB: one CDP message carries "
                    "at most 100 MiB, and base64 inflates by a third."
                ),
            ),
        ] = DEFAULT_FULFILL_BODY_BYTES,
        fulfill_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0,
                description=(
                    "Seconds the replay server has to answer one paused "
                    "request; past it the request fails (never continues)."
                ),
            ),
        ] = 10.0,
        log_repeat_limit: Annotated[
            int,
            PydanticField(
                ge=1,
                description=(
                    "Identical replay-log entries kept per collection before "
                    "the rest fold into one summary entry with a count."
                ),
            ),
        ] = 3,
    ) -> None:
        # The receptor's in-Python default; the Composer injects the configured one.
        self.miss_policy = default_miss_policy()
        # The Composer hands a nested knob over as a dict; Python callers pass
        # the config itself.
        self.matcher = (
            matcher if isinstance(matcher, MatcherConfig) else _MATCHER.validate_python(matcher)
        )
        if isolation not in ("required", "none_acknowledged"):
            raise ValueError(
                f"archive: isolation is 'required' or 'none_acknowledged', got {isolation!r}"
            )
        if shift_dates not in ("none", "all"):
            raise ValueError(f"archive: shift_dates is 'none' or 'all', got {shift_dates!r}")
        if not 1 <= max_fulfill_body_bytes <= MAX_FULFILL_BODY_BYTES:
            raise ValueError(
                f"archive: max_fulfill_body_bytes must be 1..{MAX_FULFILL_BODY_BYTES} "
                f"(one CDP message carries at most 100 MiB), got {max_fulfill_body_bytes}"
            )
        if fulfill_timeout_s <= 0:
            raise ValueError(f"archive: fulfill_timeout_s must be > 0, got {fulfill_timeout_s}")
        if log_repeat_limit < 1:
            raise ValueError(f"archive: log_repeat_limit must be >= 1, got {log_repeat_limit}")
        self._isolation = isolation
        self.shift_dates = shift_dates
        self.max_fulfill_body_bytes = max_fulfill_body_bytes
        self.fulfill_timeout_s = float(fulfill_timeout_s)
        self.log_repeat_limit = log_repeat_limit

    # ── NetworkSourceProtocol ─────────────────────────────────────────

    @property
    def intercepts(self) -> bool:
        return True

    @property
    def isolation(self) -> Isolation:
        """The knob itself: what this source needs of the provider."""
        return self._isolation

    def open_replay(self, reader: ArchiveReader) -> RecordedArchive:
        """Load the recorded run, keyed with this source's site rules (before the browser starts)."""
        return load_archive(reader, SiteRules.load(self.matcher.site_rules))

    def serve(self, recorded: RecordedArchive, ctx: ReplayContext) -> ReplayServer:
        """This run's server: this source's knobs, the configured miss policy, the run's state."""
        return ReplayServer(
            recorded,
            ctx=ctx,
            matcher=self.matcher,
            policy=self.miss_policy,
            shift_dates=self.shift_dates,
            max_fulfill_body_bytes=self.max_fulfill_body_bytes,
            fulfill_timeout_s=self.fulfill_timeout_s,
            log_repeat_limit=self.log_repeat_limit,
            source_fingerprint=self.fingerprint(),
        )

    def fingerprint(self) -> dict[str, Any]:
        """The knobs a replay fingerprint records (the server adds the run's facts)."""
        return {
            "source_id": self.source_id,
            "matcher": self.matcher.fingerprint(),
            "miss_policy_id": getattr(
                type(self.miss_policy), "__registry_key__", type(self.miss_policy).__name__
            ),
            "shift_dates": self.shift_dates,
            "isolation": self.isolation,
        }
