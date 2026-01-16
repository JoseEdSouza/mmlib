class MatcherError(Exception):
    """Base exception for all Matcher related errors."""

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


class MatcherConfigurationError(MatcherError, ValueError):
    """Raised when the matcher configuration is invalid (e.g., negative batch size)."""

    pass


class MatcherInputError(MatcherError, ValueError):
    """Raised when the input to the matcher is invalid (e.g., empty points list)."""

    pass


class MatcherRuntimeError(MatcherError):
    """Raised when an unexpected error occurs during matching."""

    pass
