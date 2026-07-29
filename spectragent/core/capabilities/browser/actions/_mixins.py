"""Shared mixins for browser action classes.

Currently houses :class:`SettleableAction` — a per-action "wait extra
milliseconds before returning" knob for sites where DOM mutates a few
seconds after the primary lifecycle event fires (login modals, A/B
overlays, infinite-scroll feed loaders, etc.).

Mixin must come **before** ``ActionBase`` in the subclass MRO so the
dataclass decorator merges fields with mixin fields appearing first;
since ``post_delay_ms`` has a default, this also keeps any required
fields the action itself declares from being shadowed.

.. note::
   This module deliberately does **not** use ``from __future__ import
   annotations``. ``post_delay_ms`` must stay a real ``Annotated[...]``
   object: the subclass's dataclass-generated ``__init__`` carries the
   *subclass module's* ``__globals__``, and most action modules do not
   import ``PydanticField``. Under PEP 563 the annotation survives as the
   string ``"Annotated[int, PydanticField(...)]"``, type-hint resolution
   fails there, and ``__config_annotated_only__`` silently drops the knob
   from the generated config — which is exactly how six actions lost
   ``post_delay_ms`` from YAML while ``navigate`` (whose own module
   happens to import ``PydanticField``) kept it.
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field as PydanticField


@dataclass
class SettleableAction:
    """Mixin: capability-side per-action post-action settle delay.

    After the action's primary side-effect resolves (i.e. after
    ``await event``), the action awaits ``post_delay_ms`` extra
    milliseconds before returning success. Configured per action in the
    capability's ``actions:`` list::

        capabilities:
          - capability_id: browser
            actions:
              - action_id: browser.navigate
                post_delay_ms: 6000

    Use cases:
      - Sites that fire login modals via ``setTimeout`` after ``load``.
      - Lazy-rendered content that appears 1–2 seconds post-click.
      - Infinite-scroll feeds that load the next page on scroll-stop.

    The mixin is orthogonal to ``NavigateToUrlEvent.wait_until`` —
    ``wait_until`` waits for a CDP lifecycle phase (load /
    domcontentloaded / networkidle), which is not enough on sites
    where the relevant DOM appears via a JS timer post-load.
    """

    post_delay_ms: Annotated[
        int,
        PydanticField(
            description=(
                "Extra milliseconds to wait after the primary action "
                "completes, before returning success. Used to absorb "
                "delayed DOM mutations (login modals, lazy overlays). "
                "Default 0 means no delay."
            ),
            ge=0,
        ),
    ] = 0

    async def settle(self) -> None:
        """Sleep for ``post_delay_ms`` milliseconds (no-op if 0)."""
        if self.post_delay_ms > 0:
            await asyncio.sleep(self.post_delay_ms / 1000)
