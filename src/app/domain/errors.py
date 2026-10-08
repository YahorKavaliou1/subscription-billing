"""Domain errors. The API layer maps them to HTTP statuses."""


class DomainError(Exception):
    """Base class for all business rule violations."""


class InvalidValueError(DomainError):
    """Input breaks a domain invariant (maps to 422)."""


class InvalidStateTransitionError(DomainError):
    """Operation is not allowed in the entity's current state (maps to 409)."""


class NotFoundError(DomainError):
    """Requested entity does not exist (maps to 404)."""


class SubscriptionNotFoundError(NotFoundError):
    def __init__(self, subscription_id: str) -> None:
        super().__init__(f"Subscription {subscription_id!r} not found")
        self.subscription_id = subscription_id


class PaymentNotFoundError(NotFoundError):
    def __init__(self, payment_ref: str) -> None:
        super().__init__(f"Payment {payment_ref!r} not found")
        self.payment_ref = payment_ref


class ConflictError(DomainError):
    """Request conflicts with already stored data (maps to 409)."""


class SubscriptionOwnershipConflictError(ConflictError):
    def __init__(self, subscription_id: str) -> None:
        super().__init__(f"Subscription {subscription_id!r} belongs to another user")
        self.subscription_id = subscription_id


class PaymentIdempotencyConflictError(ConflictError):
    def __init__(self, provider_payment: str) -> None:
        super().__init__(f"Payment {provider_payment!r} was already registered with different data")
        self.provider_payment = provider_payment
