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
    status: Literal["pending", "accepted", "rejected", "removed"]
    created_at: datetime
    decided_at: datetime | None = None


class ReceivedApplication(ActivityApplication):
    applicant: PublicAthlete



class HistoryActivity(Activity):
    role: Literal["organizer", "participant"]


class ActivityHistory(BaseModel):
    count: int
    items: list[HistoryActivity]


class Participant(PublicAthlete):
    role: Literal["organizer", "participant"]


class RatingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=300)


class GivenRating(BaseModel):
    rated_user_id: UUID
    score: int
    comment: str
    created_at: datetime
    updated_at: datetime


class ReceivedRating(BaseModel):
    id: UUID
    score: int
    comment: str
    created_at: datetime
    updated_at: datetime
    rater: PublicAthlete | None


class Reputation(BaseModel):
    average: float | None
    count: int
    items: list[ReceivedRating]


# ---------- group chat (HU-50) ----------
MAX_ID = 2 ** 63 - 1  # bigint; larger values would fail in PostgreSQL instead of answering 422.


class GroupMessageInput(BaseModel):
    """The author always comes from the JWT; any other field is rejected."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=1000)
    client_message_id: UUID
    # HU-64: the message of this same chat this one answers.
    reply_to: int | None = Field(default=None, ge=1, le=MAX_ID)


class GroupReplyPreview(BaseModel):
    """What a reply shows of the message it quotes: author, a short text. If the quoted message is gone, only
    `available` false (nothing of what it said)."""

    id: int | None
    author_id: UUID | None
    author_name: str | None
    text: str | None
    available: bool


class GroupMessage(BaseModel):
    id: int
    author_id: UUID
    author_name: str
    # Photo of the author (HU-61 path on the gateway) so tapping the name or the photo can open the profile.
    author_photo: str | None = None
    text: str
    created_at: datetime
    own: bool
    reply_to_id: int | None = None
    reply_to: GroupReplyPreview | None = None


class GroupChatMessages(BaseModel):
    acceso: Literal["escribir", "leer"]
    participantes: int
    silenciado: bool
    mensajes: list[GroupMessage]


class GroupChat(BaseModel):
    activity_id: UUID
    title: str
    sport_code: str
    starts_at: datetime
    acceso: Literal["escribir", "leer"]
    participantes: int
    ultimo: GroupMessage | None
    unread: int
    silenciado: bool


class ChatReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_read_id: int = Field(ge=0, le=MAX_ID)


class ChatMuteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    muted: bool
