from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ActivityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_activity_id: UUID
    title: str = Field(min_length=3, max_length=120)
    sport_code: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    description: str = Field(default="", max_length=2000)
    starts_at: AwareDatetime
    location: str = Field(min_length=3, max_length=200)
    capacity: int | None = Field(default=None, ge=1, le=100)


class PublicAthlete(BaseModel):
    user_id: UUID
    nombre: str
    apellido_inicial: str
    foto_perfil: str | None = None


class Organizer(PublicAthlete):
    pass


class Activity(BaseModel):
    id: UUID
    title: str
    sport_code: str
    description: str
    starts_at: datetime
    location: str
    created_at: datetime
    capacity: int | None
    available_spots: int | None
    organizer: Organizer


class ActivityPage(BaseModel):
    items: list[Activity]
    next_cursor: UUID | None


class ActivityApplication(BaseModel):
    id: UUID
    activity_id: UUID
    status: Literal["pending", "accepted", "rejected"]
    created_at: datetime
    decided_at: datetime | None = None


class ReceivedApplication(ActivityApplication):
    applicant: PublicAthlete

