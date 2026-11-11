"""Shared errors for sources."""


class SourceError(RuntimeError):
    """A source could not produce download jobs."""


class StealthError(RuntimeError):
    """The anti-bot transport could not fetch a page or file."""


class ChallengeError(StealthError):
    """Cloudflare showed a solvable challenge page."""


class BlockedError(StealthError):
    """The egress IP is hard-blocked; only a clean proxy/session can help."""


class BrowserUnavailable(StealthError):
    """Playwright or a real Chrome installation is missing."""
