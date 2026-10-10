"""Browser provider capability constants.

Each provider declares a ``__capabilities__: ClassVar[set[str]]`` built from
these constants.  Downstream consumers (BrowserEnvironment, AgentRunner) inspect
capabilities to decide feature availability — e.g. whether to enable
anti-detect trajectory simulation or managed proxy routing.
"""

# Core — every browser provider exposes at least CDP.
CDP = "cdp"

# Networking
PROXY = "proxy"  # Managed geo-targeted proxy
CUSTOM_PROXY = "custom_proxy"  # User-supplied proxy server

# Anti-detection
FINGERPRINT = "fingerprint"  # Browser fingerprint configuration
STEALTH = "stealth"  # Anti-detect / stealth browsing mode
ANTI_DETECT = "anti_detect"  # Provider-level anti-detection

# Session features
RECORDING = "recording"  # Session video recording
CAPTCHA_SOLVE = "captcha_solve"  # Automatic CAPTCHA solving
ADBLOCK = "adblock"  # Ad / tracker blocking
EXTENSIONS = "extensions"  # Chrome extension injection

# Persistence
PROFILE = "profile"  # Browser profile (cookies, storage)
CONTEXT_PERSISTENCE = "context_persistence"  # Full context persistence across sessions

# Display
SCREEN_SIZE = "screen_size"  # Custom screen / viewport size

# Ownership and launch control (docs/architecture_design.md §4.9.x, §4.14).
# v1: only ``local_chrome`` declares these two.
#
# The browser process is launched by the capability through this provider and
# killed at teardown, and its CDP endpoint accepts a second browser-level client
# (``waitForDebuggerOnStart`` auto-attach, Network, Fetch); the provider
# implements ``NetworkCaptureProvider`` (``persistent_profile``: whether that
# browser starts from a fresh profile — §5.6's ``logged_in``). Required by every
# configuration that opens the recorder connection: a bound browser archiver,
# or ``replayability`` other than ``off``.
NETWORK_CAPTURE = "network_capture"
# ``open(*, launch_args=...)`` accepts extra Chrome switches for this session,
# and the provider implements ``LaunchArgsProvider`` (``launch_arg_conflicts``).
# Required by ``replayability: pinned``, whose switches can enter the browser
# no other way.
LAUNCH_ARGS = "launch_args"
