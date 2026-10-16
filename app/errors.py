class StorageError(RuntimeError):
    """Base class for storage operations that could not be completed."""


class StorageUnavailableError(StorageError):
    """Raised when the database remains unavailable after retries."""


class IdempotencyConflictError(StorageError):
    """Raised when an idempotency key is reused for a different request."""
