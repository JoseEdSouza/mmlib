class MatcherError(Exception):
    """Base exception for Matcher related errors."""

    pass


class MatcherConnectionError(MatcherError):
    """Raised when connection to the matcher service fails."""

    pass


class MatcherTimeoutError(MatcherError):
    """Raised when an operation times out."""

    pass


class MatcherProtocolError(MatcherError):
    """Raised when the matcher service returns an invalid response."""

    pass
