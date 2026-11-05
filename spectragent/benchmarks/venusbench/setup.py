"""The ``venusbench`` setup component: AndroidWorld's service prepares and ends each VenusBench task.

``docs/architecture_design.md`` §4.10.1, §11.2 "VenusBench-Mobile 的接入方案".
Written in the VenusBench benchmark's ``setup`` block and hosted by the
Android capability. Everything it does is the part AndroidWorld's component
shares (``androidworld/service_setup.py``): the service, started from
VenusBench's installation by VenusBench's launcher, prepares each task with
its suite, initialize and reset, and ends it with its tear-down; the device
is reset between tasks, as VenusBench's own runner (AndroidWorld's
``suite_utils._run_task``) resets it. VenusBench has no MiniWoB++ and its
goals do not change at initialize (each checked on the pinned commit), so
the component makes no goal and ends no episode.

The tasks its first version does not run (``tasks.py``'s ``UNSUPPORTED``)
are refused in ``prepare()``, with the reason the loader gives for leaving
them out.
"""

from __future__ import annotations

from dataclasses import dataclass

from spectragent.benchmarks.androidworld.service_setup import AndroidWorldServiceSetup
from spectragent.benchmarks.venusbench.service import VenusBenchInstallation
from spectragent.benchmarks.venusbench.tasks import UNSUPPORTED


@dataclass
class VenusBenchSetup(AndroidWorldServiceSetup):
    """VenusBench's tasks, prepared and ended on the device by AndroidWorld's service on its checkout."""

    __registry_key__ = "venusbench"
    __installation__ = VenusBenchInstallation
    __left_out__ = UNSUPPORTED


__all__ = ["VenusBenchSetup"]
