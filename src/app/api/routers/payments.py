import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.deps import ContainerDep
from app.api.schemas.common import Problem
from app.api.schemas.payments import (
    PaymentStatusResponse,
    RegisterPaymentRequest,
    RegisterPaymentResponse,
)
from app.application.dto import RegisterPaymentCommand

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post(
    "",
    response_model=RegisterPaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment and enqueue an event about its result",
    description=(
        "The payment and its event are committed in one transaction. "
        "Idempotent by `provider_payment`: a retry with the same data returns 200 "
        "with the original payment, a retry with different data returns 409."
    ),
    responses={
        200: {"model": RegisterPaymentResponse, "description": "Already registered (replay)"},
        404: {"model": Problem, "description": "Subscription not found"},
        409: {"model": Problem, "description": "provider_payment reused with different data"},
        422: {"model": Problem},
    },
)
async def register_payment(
    body: RegisterPaymentRequest,
    container: ContainerDep,
    request: Request,
    response: Response,
) -> RegisterPaymentResponse:
    result = await container.register_payment.execute(
        RegisterPaymentCommand(
            subscription_id=body.subscription_id,
            provider_payment=body.provider_payment,
            amount=body.amount,
            currency=body.currency,
            status=body.status,
        )
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = str(
        request.url_for("get_payment", payment_id=str(result.payment.id)).path
    )
    return RegisterPaymentResponse.model_validate(result)


@router.get(
    "/{payment_id}",
    response_model=PaymentStatusResponse,
    name="get_payment",
    summary="Payment status and whether its event and notification were delivered",
    responses={404: {"model": Problem}},
)
async def get_payment(payment_id: uuid.UUID, container: ContainerDep) -> PaymentStatusResponse:
    view = await container.get_payment_status.by_id(payment_id)
    return PaymentStatusResponse.model_validate(view)


@router.get(
    "",
    response_model=PaymentStatusResponse,
    summary="Find a payment by the provider's payment id",
    responses={404: {"model": Problem}},
)
async def find_payment(
    provider_payment: Annotated[str, Query(min_length=1, max_length=128)],
    container: ContainerDep,
) -> PaymentStatusResponse:
    view = await container.get_payment_status.by_provider_payment(provider_payment)
    return PaymentStatusResponse.model_validate(view)
