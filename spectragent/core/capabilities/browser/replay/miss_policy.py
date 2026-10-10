"""The ``miss_policy`` axis: how a replay answers a request it cannot serve (§4.14).

A miss policy is a receptor of the archive network source
(``ArchiveNetworkSource.__wiring__["miss_policy"]``), so it is configured
nested inside the ``network_source`` block and arrives constructed, knobs and
all. v1 has one policy, ``strict`` (``strict.py``); the axis exists because the
user asked for room to add others (PRD R3). It decides three things about
one miss and nothing else: the request's class (``document`` / ``benign`` /
``subresource``), how to answer it (fulfil with a status, or fail), and
whether the miss means the replay diverged from the recording.

What a miss *is* — the facts the replay server knows when it gives up — is
:class:`ReplayMiss`; the answer is :class:`MissDecision`. Neither knows CDP:
the server turns a decision into ``Fetch.fulfillRequest`` /
``Fetch.failRequest`` and writes the replay log.

No ``from __future__ import annotations`` here or in the policy modules:
conscribe reads the knobs' ``Annotated`` metadata at class creation.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from conscribe import create_registrar

#: Why a request was not served from the archive. ``no_match``: nothing
#: recorded matches it; ``too_large``: the match's body is over the fulfil cap
#: (or was never stored); ``body_unavailable``: the match was recorded
#: without a body (incomplete, still pending at the run's end, a streamed body
#: that never arrived); ``range_uncovered``: a ``Range`` the recorded bytes do
#: not cover; ``websocket``: a WebSocket handshake (never served in v1);
#: ``unservable``: a request Fetch never paused (a nested worker's script).
MissReason = Literal[
    "no_match", "too_large", "body_unavailable", "range_uncovered", "websocket", "unservable"
]

#: A request's class, as the policy sorts it.
RequestClass = Literal["document", "benign", "subresource"]

#: The header on every response a replay made up instead of serving a
#: recorded one (a fulfilled miss), so page code and a reader of the page's
#: own network log can tell them apart.
REPLAY_MISS_HEADER = ("x-replay-miss", "1")
#: How a document miss fails: ``Failed`` keeps Chrome's http fallback alive
#: (a 404 or ``BlockedByClient`` cuts it off — S2a).
DOCUMENT_ERROR_REASON = "Failed"


@dataclass(frozen=True)
class ReplayMiss:
    """One request the archive did not serve, as the replay server saw it.

    Attributes:
        method: The request method.
        url: The request URL (no fragment).
        resource_type: CDP's ``resourceType`` (``Document``, ``XHR``, ``Ping``, …).
        is_document: The request loads a document (``resourceType: Document``).
        frame: For a document: ``main`` (the page target's own frame) or
            ``sub``; ``None`` for anything else.
        content_type: The request's ``Content-Type`` (a CSP report's, say).
        reason: Why it was not served.
        site_benign: The request's site rule synthesises a 204 for it
            (``synthesize_204``).
    """

    method: str
    url: str
    resource_type: str | None
    is_document: bool
    frame: Literal["main", "sub"] | None
    content_type: str | None
    reason: MissReason
    site_benign: bool = False


@dataclass(frozen=True)
class MissDecision:
    """How to answer one miss.

    Attributes:
        request_class: What the policy decided the request is.
        answer: ``fulfill`` with :attr:`status` and :attr:`headers`, or
            ``fail`` with :attr:`error_reason` (a ``Network.ErrorReason``).
            Never "continue": a replay sends nothing to the network.
        status: The fulfilled status.
        headers: The fulfilled response headers, in order.
        error_reason: The failure reason (``Failed`` for a document: a 404
            or ``BlockedByClient`` would cut off Chrome's http fallback).
        diverged: The miss means the replay left the recording (a
            main-frame document miss).
    """

    request_class: RequestClass
    answer: Literal["fulfill", "fail"]
    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    error_reason: str | None = None
    diverged: bool = False


@runtime_checkable
class MissPolicyProtocol(Protocol):
    """A miss policy (``strict``)."""

    def decide(self, miss: ReplayMiss) -> MissDecision:
        """How to answer *miss*. Pure: the same miss always gets the same decision."""
        ...


MissPolicyRegistrar = create_registrar(
    "miss_policy",
    MissPolicyProtocol,
    discriminator_field="miss_policy_id",
)


class MissPolicyBase(metaclass=MissPolicyRegistrar.Meta):
    """Base for miss policies — subclass to auto-register.

    Each policy sets ``__registry_key__`` explicitly (the YAML-facing
    ``miss_policy_id``) and takes its knobs as ``__init__`` keyword
    parameters. There is no safe default answer to a miss, so
    :meth:`decide` must be written by every policy.
    """

    __abstract__ = True
    __registry_key__: str

    @property
    def miss_policy_id(self) -> str:
        return self.__registry_key__

    def decide(self, miss: ReplayMiss) -> MissDecision:
        raise NotImplementedError(
            f"miss policy {self.miss_policy_id!r} must decide how to answer a miss"
        )


def default_miss_policy() -> MissPolicyProtocol:
    """The policy used when no configuration named one: ``strict``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for a hand-built archive source.
    """
    from spectragent.core.capabilities.browser.replay.strict import StrictMissPolicy

    return StrictMissPolicy()
