"""What stands in an observation when a fragment could not be captured.

A capability can fail mid-run for reasons that are nobody's bug — a flaky
ADB call, a CDP target swapping documents under the scrape, a container
that just died. When the fragment it owed was **optional**, killing the
whole observation throws away every other capability's already-captured
work; but filling the slot with an empty ``DOMObs`` would be worse, and is
the failure this module exists to refuse.

So the slot carries a :class:`FragmentFailure`: not a fragment at all, and
not mistakable for one. Same discipline as the LLM layer's
``_failed_call`` (``core/hooks/trace_writer.py``), which records a failed
completion as ``{error, detail}`` rather than as a completion-shaped object
with an error inside it — whoever reads the trace has to be able to tell
data the world produced from data the framework invented.

Two consequences worth stating, because both are deliberate:

- ``obs.dom`` may not be a ``DOMObs``. Code that reads a fragment it
  declared **optional** has to cope with that — which is what declaring it
  optional meant. A *required* fragment never carries one: the environment
  raises instead, so a required-fragment failure stays loud.
- It is a plain ``BaseModel``, not a ``FragmentBase`` subclass. It has no
  registry key, no ``__source__``, and never negotiates. ``isinstance(x,
  FragmentBase)`` is therefore a reliable "this is real data" test, and the
  trace writer's binary-artifact path skips it without a special case.

**How to read an optional fragment.** The trap is worth spelling out
because the obvious guard does not work::

    dom = getattr(obs, "dom", None)
    if dom is not None:
        dom.element_tree          # ← AttributeError on the failure path

An optional slot holds ``Fragment | FragmentFailure``; it is never
``None``, and a ``FragmentFailure`` is truthy. The ``is not None`` check
passes and the *next* line is where it breaks — on exactly the path the
widening exists to survive. Test the type instead::

    dom = getattr(obs, "dom", None)
    if isinstance(dom, DOMObs):       # or FragmentBase, for "any real data"
        dom.element_tree

``FragmentFailure`` raises an ``AttributeError`` that says this, naming
the fragment and the original error, rather than the bare
``'FragmentFailure' object has no attribute 'element_tree'`` pydantic
would otherwise produce. It is still an ``AttributeError``, so
``getattr(failure, "url", "")`` keeps returning the default — code that
was already defaulting per field degrades rather than crashing, but do
not copy that as the pattern: a defaulted ``url=""`` is the framework
inventing data, which is the thing this module exists to refuse.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class FragmentFailure(BaseModel):
    """A fragment slot the run could not fill, and why.

    Attributes:
        fragment_id: The fully-qualified key that failed
            (``browser.dom``), so the trace file this lands in is named
            after the fragment that is missing rather than after the
            capability that is present.
        capability: ``capability_id`` of the capability that owed it.
        error: Exception class name — ``RuntimeError``, ``TimeoutError``.
        detail: ``str(exc)``.
        captured_at: When the failure was recorded. Deliberately the same
            field name a real fragment carries, so a reader plotting a
            step's capture times does not have to special-case the gaps.
    """

    fragment_id: str
    capability: str = ""
    error: str
    detail: str = ""
    captured_at: float = Field(default_factory=time.time)

    @classmethod
    def from_exception(
        cls, fragment_key: str, capability: str, exc: BaseException
    ) -> FragmentFailure:
        """Record *exc* as the reason *fragment_key* is absent."""
        return cls(
            fragment_id=fragment_key,
            capability=capability,
            error=type(exc).__name__,
            detail=str(exc),
        )

    def __getattr__(self, item: str) -> Any:
        """Explain the mistake instead of reporting a missing attribute.

        Reading a fragment field off a failure means the caller's guard
        was ``is not None`` (or nothing at all) on a slot that is never
        ``None``. Pydantic's own message — *'FragmentFailure' object has
        no attribute 'element_tree'* — names the symptom and not the
        cause, and it surfaces on the rare path, in a run that has
        already gone wrong once.

        Deliberately still an ``AttributeError``: ``hasattr`` and
        ``getattr(x, name, default)`` must keep working, or this "helpful"
        message would itself break the defaulting readers it is warning
        about. Dunder and private names fall through untouched, so
        ``copy.deepcopy``'s ``__deepcopy__`` probe and pydantic's own
        private-attribute machinery behave normally.
        """
        try:
            return super().__getattr__(item)  # type: ignore[misc]
        except AttributeError:
            pass
        if item.startswith("_"):
            raise AttributeError(item)
        raise AttributeError(
            f"{item!r} is not readable: the {self.fragment_id!r} slot holds a "
            f"FragmentFailure, not a fragment — capability "
            f"{self.capability!r} could not capture it "
            f"({self.error}: {self.detail}). This slot is optional, so it "
            f"carries `Fragment | FragmentFailure` and is never None; guard "
            f"it with `isinstance(value, FragmentBase)` rather than "
            f"`value is not None`."
        )

    def summary(self) -> str:
        """One-line digest for ``StepRecord.observation_summary``.

        Mirrors :meth:`FragmentBase.summary` by duck type, so a failed
        fragment shows up in the step summary line instead of vanishing
        from it — a step whose screenshot failed should read as a step
        whose screenshot failed.
        """
        return f"FAILED({self.error})"
