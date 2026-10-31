"""Browser capability package.

Self-contained CDP-based browser automation capability. Owns its entire
mid-layer protocol stack (CDPClient, EventScope/Watchdog system, DOM
pipeline, profiles); ``capability.py`` holds the ``BrowserCapability`` class.

This file imports nothing. Every subpackage of ``spectragent/core/capabilities``
is a capability, and the framework reads only its ``declarations.py``; importing
that module runs this file first, so an import here would load the whole browser
(agent-cdp, websockets) for a program that only reads declarations. Import from
the defining module instead.

See docs/architecture_design.md §4.2.1 and §4.9.
"""
