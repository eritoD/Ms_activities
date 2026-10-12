from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.dependencies import get_actor, get_service
from app.schemas.activities import (
    Activity, ActivityApplication, ActivityCreate, ActivityHistory, ActivityPage, ActivityUpdate, GivenRating,
    Participant, RatingInput, ReceivedApplication, Reputation,
)

router = APIRouter(prefix="/api/v1/activities", tags=["Activities"])


@router.get("/health/ready")
def ready(service=Depends(get_service)):
    try:
        service.repository.ready()
    except Exception as error:
        raise HTTPException(503, "Actividades no está disponible.") from error
    return {"status": "ready", "service": "ms_activities"}


@router.get("", response_model=ActivityPage)
def upcoming(limit: int = Query(20, ge=1, le=100), cursor: UUID | None = None,
             actor=Depends(get_actor), service=Depends(get_service)):
    return service.upcoming(actor, limit, cursor)


@router.post("", response_model=Activity, status_code=201)
def create(payload: ActivityCreate, actor=Depends(get_actor), service=Depends(get_service)):
    return service.create(actor, payload)


# Declared before "/{activity_id}" so "history" is not read as an activity ID.
@router.get("/history", response_model=ActivityHistory)
def history(limit: int = Query(50, ge=1, le=100), actor=Depends(get_actor), service=Depends(get_service)):
    return service.history(actor, limit)


@router.get("/{activity_id}/participants", response_model=list[Participant])
def participants(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.participants(actor, activity_id)


@router.get("/{activity_id}", response_model=Activity)
def detail(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.get(actor, activity_id)


@router.patch("/{activity_id}", response_model=Activity)
def update(activity_id: UUID, payload: ActivityUpdate, actor=Depends(get_actor), service=Depends(get_service)):
    return service.update(actor, activity_id, payload)


@router.delete("/{activity_id}", status_code=204)
def cancel(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    service.cancel(actor, activity_id)
    return Response(status_code=204)


@router.post("/{activity_id}/applications", response_model=ActivityApplication, status_code=201)
def apply(activity_id: UUID, response: Response, actor=Depends(get_actor), service=Depends(get_service)):
    application, created = service.apply(actor, activity_id)
    if not created:
        response.status_code = 200
    return application


@router.get("/{activity_id}/applications/me", response_model=ActivityApplication)
def my_application(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.my_application(actor, activity_id)


@router.delete("/{activity_id}/applications/me", status_code=204)
def withdraw(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    service.withdraw(actor, activity_id)
    return Response(status_code=204)


@router.get("/{activity_id}/applications", response_model=list[ReceivedApplication])
def received_applications(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.received_applications(actor, activity_id)


@router.post("/{activity_id}/applications/{application_id}/accept", response_model=ActivityApplication)
def accept(activity_id: UUID, application_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.decide(actor, activity_id, application_id, "accepted")


@router.post("/{activity_id}/applications/{application_id}/reject", response_model=ActivityApplication)
def reject(activity_id: UUID, application_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.decide(actor, activity_id, application_id, "rejected")


ratings_router = APIRouter(prefix="/api/v1/ratings", tags=["Ratings"])


@ratings_router.get("/me", response_model=Reputation)
def my_reputation(actor=Depends(get_actor), service=Depends(get_service)):
    return service.reputation(actor)


@ratings_router.get("/given", response_model=list[GivenRating])
def given_ratings(actor=Depends(get_actor), service=Depends(get_service)):
    return service.given(actor)


@ratings_router.put("/{user_id}", response_model=GivenRating)
def rate(user_id: UUID, payload: RatingInput, actor=Depends(get_actor), service=Depends(get_service)):
    return service.rate(actor, user_id, payload)
