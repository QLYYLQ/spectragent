"""DOM observation budgets — what a ``browser.dom`` fragment is allowed to cost.

The screenshot fragment already had a cost surface (``screenshot_format`` /
``screenshot_quality`` on :class:`BrowserCapability`); the DOM fragment had
none, even though it is the expensive half of a step: a content-heavy page
serializes to tens of thousands of characters and hundreds of indexed
elements, every one of them re-sent on every step.

Four knobs, all scalars on the capability rather than strategy keys —
there is nothing to swap here, the same way ``screenshot_quality`` is a
number and not a registrar. A resolver applies them with one call,
:meth:`DOMBudgetPass.apply`, which owns the order: element cuts, then the
serializer, then the length cut.

**A budget that cuts must say so.** Every cut is reported on
``DOMObs.budget`` (:class:`DOMBudgetReport`), because a trace where a
truncated page and a sparse page look identical is a trace that lies: an
evaluator reading ``42 interactive elements`` has to be able to tell "the
page had 42" from "the page had 900 and the run paid for 42". ``None``
on that field means nothing was cut — what the fragment carries is what
the page had.

**What a cut element loses is its index.** The prune works on the node
graph rather than on the serialized text, so the selector map and every
registered serializer see the same reduced truth: the entry leaves
``state.selector_map``, ``element_index`` becomes ``None``, and
``build_guarded_act`` — which routes on that map — can no longer reach
it. It does *not* follow that the element disappears from the prompt.
``TextSerializer`` renders a node whose tag is structural regardless of
its index (``should_render = is_interactive or is_scrollable or
is_iframe``, ``text_serializer.py:147``), so a de-indexed scroll
container still prints as ``|scroll element|<div />`` and a de-indexed
iframe as ``|IFRAME|``; ``HTMLSerializer`` likewise keeps a de-indexed
element that carries text, just without ``data-idx``. The invariant to
rely on is **unindexed means unreachable**, not "gone". Only
``interactive_only`` removes nodes from the tree, and it removes exactly
those that are neither indexed nor an ancestor of an indexed node.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMState,
)

#: Appended to a length-trimmed serialization so the *model* reading the
#: prompt knows it is looking at a cut page, not a short one. It is
#: metadata about the cut and is deliberately **not** counted against
#: ``max_serialized_chars`` — that budget governs page content, and
#: ``DOMBudgetReport.serialized_chars_after`` reports the kept content.
#: So the final string runs ~45 characters over the configured limit;
#: the limit is a budget for page content, not a hard cap on the field.
_TRUNCATION_NOTE = "\n... [dom budget: {kept} of {total} characters shown]"


@dataclass(frozen=True, slots=True)
class DOMBudget:
    """The four knobs, as the capability resolved them.

    ``None`` / ``False`` everywhere is the unbudgeted default and is
    byte-identical to the behaviour before budgets existed: :attr:`is_open`
    short-circuits the whole pass.
    """

    #: Keep at most this many indexed elements, in document order.
    #: ``None`` = unlimited.
    max_elements: int | None = None
    #: Keep at most this many characters of serialized DOM. ``None`` =
    #: unlimited.
    max_serialized_chars: int | None = None
    #: Drop indexed entries that are not themselves interactive — the
    #: scroll containers ``_assign_interactive_indices`` indexes so an
    #: agent can scroll them — and drop from the tree every node that is
    #: neither indexed nor an ancestor of an indexed node. That second
    #: half is where the tokens are: it removes the page's prose.
    interactive_only: bool = False
    #: Drop indexed elements whose page-coordinate ``bounds`` do not
    #: intersect the current viewport. An element with no measured bounds
    #: is **kept** — this budget never cuts what it cannot measure.
    viewport_only: bool = False

    @property
    def is_open(self) -> bool:
        """``True`` when no knob is set, i.e. nothing to do."""
        return (
            self.max_elements is None
            and self.max_serialized_chars is None
            and not self.interactive_only
            and not self.viewport_only
        )


class DOMBudgetReport(BaseModel):
    """What a budget actually removed from one ``browser.dom`` fragment.

    Present on the fragment only when something *was* removed, so its mere
    presence is the signal. Counts are of indexed elements (selector-map
    entries) and of serialized characters.
    """

    #: Knob names that removed something: any of ``viewport_only``,
    #: ``interactive_only``, ``max_elements``, ``max_serialized_chars``.
    #: In the order the *steps* that removed something ran, which is not
    #: the order the knobs are declared in: ``interactive_only`` runs
    #: twice (selector-map drop, then tree prune) and is recorded the
    #: first time either half cuts, so with every knob on the list reads
    #: ``['viewport_only', 'max_elements', 'interactive_only']`` when the
    #: selector-map half found nothing to drop. Treat it as a set of
    #: names plus a rough sequence, not as a declaration order.
    applied: list[str] = Field(default_factory=list)
    #: Indexed elements the cleaner produced, before any budget.
    elements_before: int = 0
    #: Indexed elements the fragment carries.
    elements_after: int = 0
    #: Non-indexed tree nodes ``interactive_only`` removed — text nodes
    #: and structural tags. ``0`` when that knob is off.
    nodes_dropped: int = 0
    #: Length of the serialization before ``max_serialized_chars``. It
    #: already reflects the element budgets above.
    serialized_chars_before: int = 0
    #: Length of the serialized content the fragment carries, excluding the
    #: appended truncation note.
    serialized_chars_after: int = 0

    def summary(self) -> str:
        return (
            f"budget {'+'.join(self.applied)}: "
            f"{self.elements_after}/{self.elements_before} elements, "
            f"{self.serialized_chars_after}/{self.serialized_chars_before} chars"
        )


class DOMBudgetPass:
    """One budget application, spanning the serialization step.

    Usage inside a resolver — one call, because the order is the contract::

        report = DOMBudgetPass(budget).apply(state, serializer.serialize)
        # report is None when nothing was cut

    The pass *brackets* serialization: the element budgets have to land
    before the serializer runs (otherwise the string is paid for and
    thrown away) and the length budget can only land after. That used to
    be three public methods and a docstring asking the caller to call
    them in the right order, which is the same silent-failure shape this
    module exists to prevent — skip :meth:`_trim` and
    ``max_serialized_chars`` is quietly never applied *and* never
    reported. :meth:`apply` takes the serializer instead, so the order
    is not the caller's to get wrong.
    """

    def __init__(self, budget: DOMBudget) -> None:
        self._budget = budget
        self._applied: list[str] = []
        self._elements_before = 0
        self._elements_after = 0
        self._nodes_dropped = 0
        self._chars_before = 0
        self._chars_after = 0
        self._spent = False

    # -- the one entry point ------------------------------------------

    def apply(
        self, state: DOMState, serialize: Callable[[DOMState], str]
    ) -> DOMBudgetReport | None:
        """Budget ``state``, serialize it, budget the string. Returns the cut.

        ``serialize`` is the resolver's configured serializer's
        ``serialize`` — taken as an argument rather than looked up, for
        the same reason the serializer itself is a receptor. It is called
        exactly once, and only when there is a tree: a stateless empty
        string would otherwise overwrite whatever ``state.serialized_dom``
        already carried.

        Returns ``None`` when nothing was cut, which is the encoding
        ``DOMObs.budget`` relies on.
        """
        if self._spent:  # pragma: no cover — defensive
            raise RuntimeError(
                "DOMBudgetPass is one-shot: its counters describe a single "
                "capture. Build a new pass per observation."
            )
        self._spent = True
        self._prune(state)
        if state.dom_tree is not None:
            state.serialized_dom = serialize(state)
        self._trim(state)
        return self._report()

    # -- before serialization ----------------------------------------

    def _prune(self, state: DOMState) -> None:
        """Apply the element budgets to ``state``, in place."""
        self._elements_before = len(state.selector_map)
        self._elements_after = self._elements_before
        if self._budget.is_open:
            return

        if self._budget.viewport_only:
            self._drop(state, self._outside_viewport(state), "viewport_only")
        if self._budget.interactive_only:
            self._drop(
                state,
                [handle for handle, node in state.selector_map.items() if not node.is_interactive],
                "interactive_only",
            )
        if self._budget.max_elements is not None:
            over = list(state.selector_map)[self._budget.max_elements :]
            self._drop(state, over, "max_elements")

        if self._budget.interactive_only and state.dom_tree is not None:
            dropped = _prune_to_indexed(state.dom_tree)
            self._nodes_dropped = dropped
            if dropped and "interactive_only" not in self._applied:
                self._applied.append("interactive_only")

        self._elements_after = len(state.selector_map)

    # -- after serialization -----------------------------------------

    def _trim(self, state: DOMState) -> None:
        """Apply ``max_serialized_chars`` to ``state.serialized_dom``.

        Cuts at the last line boundary inside the limit, so the model
        never reads half an element line. A limit shorter than the first
        line has no boundary to cut at and falls back to a hard slice —
        that is a limit too small to be useful, not a case worth
        preserving lines for.
        """
        text = state.serialized_dom
        self._chars_before = len(text)
        self._chars_after = self._chars_before

        limit = self._budget.max_serialized_chars
        if limit is None or len(text) <= limit:
            return

        cut = text.rfind("\n", 0, limit)
        kept = text[:cut] if cut > 0 else text[:limit]
        self._chars_after = len(kept)
        self._applied.append("max_serialized_chars")
        state.serialized_dom = kept + _TRUNCATION_NOTE.format(
            kept=len(kept), total=self._chars_before
        )

    # -- the answer ---------------------------------------------------

    def _report(self) -> DOMBudgetReport | None:
        """The cut, or ``None`` when the fragment is the whole truth."""
        if not self._applied:
            return None
        return DOMBudgetReport(
            applied=list(self._applied),
            elements_before=self._elements_before,
            elements_after=self._elements_after,
            nodes_dropped=self._nodes_dropped,
            serialized_chars_before=self._chars_before,
            serialized_chars_after=self._chars_after,
        )

    # -- internals ----------------------------------------------------

    def _drop(self, state: DOMState, handles: list[int], knob: str) -> None:
        if not handles:
            return
        for handle in handles:
            node = state.selector_map.pop(handle, None)
            if node is not None:
                node.element_index = None
        self._applied.append(knob)

    @staticmethod
    def _outside_viewport(state: DOMState) -> list[int]:
        """Indexed handles whose bounds miss the scrolled viewport.

        An empty answer when the viewport size is unknown: a zero-sized
        viewport would otherwise "cut" the entire page, which is the exact
        class of silent lie this module exists to prevent.
        """
        info = state.scroll_info
        if info.viewport_width <= 0 or info.viewport_height <= 0:
            return []
        left, top = info.scroll_x, info.scroll_y
        right = left + info.viewport_width
        bottom = top + info.viewport_height
        outside: list[int] = []
        for handle, node in state.selector_map.items():
            box = node.bounds
            if box is None:
                continue
            if (
                box.x + box.width <= left
                or box.x >= right
                or box.y + box.height <= top
                or box.y >= bottom
            ):
                outside.append(handle)
        return outside


def _prune_to_indexed(root: DOMElementNode) -> int:
    """Keep only nodes that are indexed or lead to one. Returns the count dropped.

    Walks the *composed* tree — light children, shadow roots, frame
    documents — so an indexed element inside a shadow root or an iframe
    keeps its whole chain of ancestors and is still printed and still
    clickable. Everything else, text nodes included, goes.
    """
    dropped = 0

    def keep(node: DOMElementNode) -> bool:
        nonlocal dropped
        kept_children = [child for child in node.children if keep(child)]
        dropped += len(node.children) - len(kept_children)
        node.children = kept_children

        if node.shadow_roots:
            kept_roots = [sr for sr in node.shadow_roots if keep(sr)]
            dropped += len(node.shadow_roots) - len(kept_roots)
            node.shadow_roots = kept_roots or None

        if node.content_document is not None and not keep(node.content_document):
            dropped += 1
            node.content_document = None

        if node.element_index is not None:
            return True
        return bool(kept_children or node.shadow_roots or node.content_document)

    keep(root)
    return dropped


__all__ = ["DOMBudget", "DOMBudgetPass", "DOMBudgetReport"]
