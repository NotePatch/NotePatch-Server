class RetryableTaskError(RuntimeError):
    """A transient infrastructure or model error that may succeed on retry."""


class PermanentTaskError(RuntimeError):
    """An invalid input or output that must fail without queue retry."""


class TaskCancelledError(RuntimeError):
    """Raised at worker checkpoints after a cooperative cancellation request."""
