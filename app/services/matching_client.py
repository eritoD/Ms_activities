from uuid import UUID

import httpx
from fastapi import HTTPException


class MatchingClient:
    """Asks Ms_Matching, with the athlete's own token, whom they have an accepted match with."""
    def __init__(self, client: httpx.Client):
        self.client = client

    def matched_with(self, user_id: UUID, token: str) -> bool:
        try:
            response = self.client.get("/api/v1/matching/state", headers={"Authorization": f"Bearer {token}"})
        except httpx.RequestError as error:
            raise HTTPException(503, "No se pudo revisar tus matches. Inténtalo de nuevo.") from error
        if response.status_code in (401, 403, 404):
            raise HTTPException(response.status_code, "Deportista no disponible o sesión sin autorización.")
        if response.status_code != 200:
            raise HTTPException(503, "El servicio de matching no está disponible.")
        return any(str(user_id) in (match["sender_id"], match["recipient_id"])
                   for match in response.json()["matches"])
