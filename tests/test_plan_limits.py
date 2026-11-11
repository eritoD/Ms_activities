"""HU-54: límites del plan gratuito en actividades, sin PostgreSQL.
Postular: 5 cada 24 h. Organizar: 3 cada 30 días (ventana móvil)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.activities import ActivityCreate
from app.services.activities_service import ActivitiesService
from app.services.entitlements_client import EntitlementsClient, EntitlementsSettings
from tests.test_activities import SECRET, Directory, headers, payload

PREFIX = "/api/v1/activities"
TOKEN = "internal-token-for-tests-at-least-32-bytes-long"
GRATIS = {"plan": "GRATIS", "beneficios": [], "limites": {
    "postulaciones_actividades": {"maximo": 5, "ventana_horas": 24},
    "actividades_organizadas": {"maximo": 3, "ventana_horas": 720}}}
PRO = {"plan": "PRO", "beneficios": ["destacado"], "limites": {
    "postulaciones_actividades": {"maximo": None, "ventana_horas": 24},
    "actividades_organizadas": {"maximo": None, "ventana_horas": 720}}}


class FakeRepository:
    def __init__(self):
        self.activities, self.applications = {}, []

    def create(self, actor_id, data):
        for row in self.activities.values():
            if row["organizer_id"] == actor_id and row["client_activity_id"] == data.client_activity_id:
                return row
        row = {**data.model_dump(), "id": uuid4(), "organizer_id": actor_id, "created_at": datetime.now(timezone.utc),
               "accepted_count": 0, "cancelled_at": None}
        self.activities[row["id"]] = row
        return row

    def get(self, activity_id):
        if activity_id not in self.activities:
            raise HTTPException(404, "Actividad no disponible.")
        return self.activities[activity_id]

    def activity_exists(self, organizer_id, client_activity_id):
        return any(r["organizer_id"] == organizer_id and r["client_activity_id"] == client_activity_id
                   for r in self.activities.values())

    def activities_created_since(self, organizer_id, since):
        return [r["created_at"] for r in self.activities.values()
                if r["organizer_id"] == organizer_id and r["created_at"] > since]

    def application(self, activity_id, applicant_id):
        for row in self.applications:
            if row["activity_id"] == activity_id and row["applicant_id"] == applicant_id:
                return row
        raise HTTPException(404, "No has postulado a esta actividad.")

    def apply(self, activity_id, applicant_id):
        try:
            return self.application(activity_id, applicant_id), False
        except HTTPException:
            pass
        row = {"id": uuid4(), "activity_id": activity_id, "applicant_id": applicant_id, "status": "pending",
               "created_at": datetime.now(timezone.utc), "decided_at": None}
        self.applications.append(row)
        return row, True

    def applications_since(self, applicant_id, since):
        return [r["created_at"] for r in self.applications if r["applicant_id"] == applicant_id and r["created_at"] > since]


def payments_client(body=GRATIS, status=200):
    def handler(request):
        if request.headers.get("X-Internal-Service-Token") != TOKEN:
            return httpx.Response(401)
        if status == "down":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(status, json=body)
    return EntitlementsClient(EntitlementsSettings("http://pago.test", TOKEN, 1, 60),
                              httpx.Client(base_url="http://pago.test", transport=httpx.MockTransport(handler)))


def make(entitlements):
    directory = Directory()
    users = [uuid4() for _ in range(3)]
    directory.active |= set(users)
    repository = FakeRepository()
    service = ActivitiesService(repository, directory, directory, entitlements=entitlements)
    settings = Settings("postgresql://unused", SECRET, "http://users.test", "sportmatch-auth", "sportmatch-mobile")
    return TestClient(create_app(service, settings)), repository, users


def publish(client, user, **changes):
    return client.post(PREFIX, headers=headers(user), json=payload(**changes))


def test_free_athlete_organizes_three_activities_then_402():
    client, repository, (me, *_) = make(payments_client())
    first = payload()
    assert client.post(PREFIX, headers=headers(me), json=first).status_code == 201
    for _ in range(2):
        assert publish(client, me).status_code == 201
    # A retry of an activity already published is not a new one.
    assert client.post(PREFIX, headers=headers(me), json=first).status_code == 201
    response = publish(client, me)
    assert response.status_code == 402
    body = response.json()
    assert (body["codigo"], body["limite"], body["usado"]) == ("LIMITE_PLAN", 3, 3)
    oldest = min(r["created_at"] for r in repository.activities.values())
    assert datetime.fromisoformat(body["se_libera_en"].replace("Z", "+00:00")) == oldest + timedelta(days=30)
    assert len(repository.activities) == 3


def test_free_athlete_applies_five_times_in_24_hours_then_402():
    client, repository, (organizer, me, _) = make(payments_client())
    # The organizer has the free plan too: create the activities directly in the fake.
    ids = [repository.create(organizer, ActivityCreate(**payload()))["id"] for _ in range(7)]
    for activity_id in ids[:5]:
        assert client.post(f"{PREFIX}/{activity_id}/applications", headers=headers(me)).status_code == 201
    # Applying again to the same activity returns the existing application.
    assert client.post(f"{PREFIX}/{ids[0]}/applications", headers=headers(me)).status_code == 200
    response = client.post(f"{PREFIX}/{ids[5]}/applications", headers=headers(me))
    assert response.status_code == 402
    assert response.json()["usado"] == 5 and response.json()["limite"] == 5
    for row in repository.applications:
        row["created_at"] -= timedelta(hours=25)
    assert client.post(f"{PREFIX}/{ids[5]}/applications", headers=headers(me)).status_code == 201


def test_pro_athlete_has_no_limits():
    client, _, (me, *_) = make(payments_client(PRO))
    for _ in range(6):
        assert publish(client, me).status_code == 201


@pytest.mark.parametrize("status", ["down", 503])
def test_fail_open_when_ms_pago_is_down(status):
    client, _, (me, *_) = make(payments_client(status=status))
    for _ in range(5):
        assert publish(client, me).status_code == 201


def test_without_entitlements_client_nothing_is_limited():
    client, _, (me, *_) = make(None)
    for _ in range(5):
        assert publish(client, me).status_code == 201


def test_plan_usage_reports_both_resources():
    client, repository, (organizer, me, _) = make(payments_client())
    activity_id = repository.create(organizer, ActivityCreate(**payload()))["id"]
    assert client.post(f"{PREFIX}/{activity_id}/applications", headers=headers(me)).status_code == 201
    assert publish(client, me).status_code == 201
    body = client.get(f"{PREFIX}/plan-usage", headers=headers(me))
    assert body.status_code == 200, body.text
    items = {item["recurso"]: item for item in body.json()["items"]}
    assert body.json()["plan"] == "GRATIS"
    assert (items["postulaciones_actividades"]["limite"], items["postulaciones_actividades"]["usado"]) == (5, 1)
    assert (items["actividades_organizadas"]["limite"], items["actividades_organizadas"]["usado"]) == (3, 1)
    assert items["actividades_organizadas"]["ventana_horas"] == 720


def test_plan_usage_without_ms_pago_is_unknown():
    client, _, (me, *_) = make(None)
    body = client.get(f"{PREFIX}/plan-usage", headers=headers(me)).json()
    assert body["plan"] is None and all(item["limite"] is None for item in body["items"])
