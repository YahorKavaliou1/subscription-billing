from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from app.api.deps import ContainerDep
from app.api.schemas.common import Problem
from app.api.schemas.subscriptions import SubscriptionResponse, UpsertSubscriptionRequest
from app.application.dto import UpsertSubscriptionCommand

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

SubscriptionId = Annotated[str, Path(min_length=1, max_length=64, examples=["sub-1"])]


@router.put(
    "/{subscription_id}",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_200_OK,
    summary="Create or update a subscription and schedule its notifications",
    responses={
        201: {"model": SubscriptionResponse, "description": "Subscription created"},
        409: {"model": Problem, "description": "Subscription belongs to another user"},
        422: {"model": Problem},
    },
)
async def upsert_subscription(
    subscription_id: SubscriptionId,
    body: UpsertSubscriptionRequest,
    container: ContainerDep,
    response: Response,
) -> SubscriptionResponse:
    result = await container.upsert_subscription.execute(
        UpsertSubscriptionCommand(
            subscription_id=subscription_id,
            user_id=body.user_id,
            day_count=body.day_count,
            expected_expires_on=body.expected_expires_on,
        )
    )
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    return SubscriptionResponse.model_validate(result.subscription)


@router.get(
    "/{subscription_id}",
    response_model=SubscriptionResponse,
    summary="Subscription state and history of its notifications",
    responses={404: {"model": Problem}},
)
async def get_subscription(
    subscription_id: SubscriptionId, container: ContainerDep
) -> SubscriptionResponse:
    view = await container.get_subscription.execute(subscription_id)
    return SubscriptionResponse.model_validate(view)
