"""Environment and ActionHandler Protocol stubs.

Agents program against these Protocols -- they never import concrete
environment implementations.  The real Environment layer will be built
separately and injected at runtime.
"""

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


@runtime_checkable
class Environment(Protocol):
    """Abstract environment that an agent interacts with.

    ``observe()`` and ``act()`` accept an optional ``session_id`` to target
    a specific tab.  When *None*, the environment uses its default tab.
    This enables both single-agent and multi-agent parallel usage.
    """

    async def observe(self, session_id: str | None = None) -> Any:
        """Return the current observation of the environment."""
        ...

    async def act(self, action: Action, session_id: str | None = None) -> ActionResult:
        """Execute *action* and return its result."""
        ...

    def get_action_space(self) -> list[ActionDescriptor]:
        """Declare the actions this environment supports."""
        ...

    async def setup(self, **kwargs: Any) -> None:
        """Initialize the environment (e.g. launch browser, navigate)."""
        ...

    async def teardown(self) -> None:
        """Tear down the environment (e.g. close browser)."""
        ...

    @property
    def is_alive(self) -> bool:
        """Whether the environment is still operational."""
        ...


@runtime_checkable
class ActionHandler(Protocol):
    """Handles a subset of actions within an Environment."""

    async def execute(self, action: Action) -> ActionResult:
        """Execute *action* and return its result."""
        ...

    def supported_actions(self) -> list[ActionDescriptor]:
        """Declare the actions this handler can execute."""
        ...
