"""Browser-specific, explicitly selected runtime hooks."""

from spectragent.core.capabilities.browser.hooks.ui_navigation import UiNavigationGuard

__all__ = ["UiNavigationGuard"]
