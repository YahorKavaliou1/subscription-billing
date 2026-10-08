"""Errors raised by port implementations and understood by use cases."""


class DuplicateKeyError(Exception):
    """A unique constraint was violated, typically by a concurrent request."""

    def __init__(self, constraint: str | None = None) -> None:
        super().__init__(f"Unique constraint violated: {constraint or 'unknown'}")
        self.constraint = constraint


class ConcurrentUpdateError(Exception):
    """The entity was changed by another transaction (optimistic lock failed)."""
