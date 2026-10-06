from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_actor, get_service
from app.schemas.activities import Activity, ActivityCreate, ActivityPage

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


@router.get("/{activity_id}", response_model=Activity)
def detail(activity_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.get(actor, activity_id)
