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
