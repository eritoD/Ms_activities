from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ActivityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_activity_id: UUID
    title: str = Field(min_length=3, max_length=120)
    sport_code: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    description: str = Field(default="", max_length=2000)
    starts_at: AwareDatetime
    location: str = Field(min_length=3, max_length=200)
    capacity: int | None = Field(default=None, ge=1, le=100)


class ActivityUpdate(BaseModel):
    """Only the fields sent are changed. `capacity: null` removes the limit."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=3, max_length=120)
    sport_code: str | None = Field(default=None, min_length=1, max_length=50, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    description: str | None = Field(default=None, max_length=2000)
    starts_at: AwareDatetime | None = None
    location: str | None = Field(default=None, min_length=3, max_length=200)
    capacity: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def requires_changes(self):
        if not self.model_fields_set:
            raise ValueError("Indica al menos un dato para modificar.")
        for field in self.model_fields_set - {"capacity"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} no puede ser nulo.")
        return self


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
    updated_at: datetime | None = None
    cancelled_at: datetime | None = None
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

