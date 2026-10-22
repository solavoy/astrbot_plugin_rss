class RSSPluginError(Exception):
    """Base error for expected plugin failures."""


class ConfigurationError(RSSPluginError, ValueError):
    pass


class RenderingError(RSSPluginError):
    pass


class PlatformUnsupportedError(RSSPluginError):
    pass


class PermanentSendError(RSSPluginError):
    pass


class RetryableSendError(RSSPluginError):
    pass


class UnknownSendResultError(RSSPluginError):
    """The platform may have accepted the send; automatic retry is unsafe."""


class FeedFetchError(RSSPluginError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
