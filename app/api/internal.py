"""Endpoint interno de Ms_Activities (HU-47): quién publicó una actividad, para que Ms_Users pueda sancionar a la
persona autora de una actividad reportada.

Nunca se declara en el gateway ni se publica fuera de Docker: otros servicios lo llaman por la red interna con el
secreto compartido INTERNAL_SERVICE_TOKEN en el encabezado X-Internal-Service-Token. Un token de usuario no sirve."""
from __future__ import annotations

import hmac
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

MIN_TOKEN_BYTES = 32

router = APIRouter(prefix="/internal/v1/activities", tags=["internal"], include_in_schema=False)


def _expected_token() -> str | None:
    token = os.environ.get("INTERNAL_SERVICE_TOKEN")
    return token if token and len(token.encode("utf-8")) >= MIN_TOKEN_BYTES else None


def require_internal_service(token: str | None) -> None:
    expected = _expected_token()
    if expected is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "internal authentication is not configured")
    if token is None or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "service authentication required")


@router.get("/{activity_id}/author")
def activity_author(
    activity_id: UUID,
    request: Request,
    x_internal_service_token: str | None = Header(default=None, max_length=256),
) -> dict[str, str]:
    """Only the account that published it: nothing else about the activity or the person."""
    require_internal_service(x_internal_service_token)
    row = request.app.state.service.repository.get(activity_id)      # 404 if the activity does not exist
    return {"user_id": str(row["organizer_id"])}
