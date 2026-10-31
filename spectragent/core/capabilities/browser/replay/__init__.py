"""Replay: the browser's three replay axes (§4.14).

- ``replayability`` (``ReplayabilityRegistrar``: ``none`` / ``pinned``) — how far a
  recording is pinned so it can be replayed;
- ``network_source`` (``NetworkSourceRegistrar``: ``live`` / ``archive``) — where
  the network comes from; ``archive`` replays a recorded run offline;
- ``miss_policy`` (``MissPolicyRegistrar``: ``strict``) — how an ``archive``
  source answers what the recording does not hold.

This file imports nothing: the browser's ``declarations.py`` imports the three
registrar modules (``replayability.py``, ``network_source.py``,
``miss_policy.py``) without loading the replay machinery. A strategy registers
when its module is imported, which discovery and ``capability.py`` do.

A replaying run's moving parts: ``archive_index.py`` loads the source
run, ``canonical.py`` / ``matcher.py`` / ``site_rules.py`` match, ``server.py``
answers paused requests (``responder.py`` shapes the answers), ``replay_log.py``
records every decision and reads them back, ``consistency.py`` checks the
source run against the configuration before anything launches.
"""
