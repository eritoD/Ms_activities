"""Real PostgreSQL tests in a disposable, isolated schema (never the app tables)."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import sql

from app.core.config import Settings
from app.database.session import create_pool
from app.main import create_app
from app.repositories.postgres_activities_repository import PostgresActivitiesRepository
from app.services.activities_service import ActivitiesService

SECRET = "activities-test-secret-longer-than-thirty-two-bytes"
A, B, C = uuid4(), uuid4(), uuid4()
PREFIX = "/api/v1/activities"


def token(user_id, **extra):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=10),
        "iss": "sportmatch-auth", "aud": "sportmatch-mobile", "token_type": "access", **extra}, SECRET, algorithm="HS256")


def headers(user_id):
    return {"Authorization": f"Bearer {token(user_id)}"}


class Directory:
    def __init__(self):
        self.active = {A, B, C}

    def card(self, user_id, _token):
        if user_id not in self.active:
            raise HTTPException(404, "Deportista no disponible")
        return {"user_id": str(user_id), "nombre": "Deportista", "apellido_inicial": "T.",
                "edad": None, "foto_perfil": None, "biografia": None, "deportes": [], "compatibilidad": 0}

    def cards(self, user_ids, token):
        return {uid: self.card(uid, token) for uid in user_ids if uid in self.active}


@pytest.fixture
def system():
    url = os.environ.get("ACTIVITIES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set ACTIVITIES_TEST_DATABASE_URL to run isolated PostgreSQL integration tests")
    pool = create_pool(url)
    schema = "activities_test_" + uuid4().hex
    repository = PostgresActivitiesRepository(pool, schema=schema)
    repository.migrate()
    directory = Directory()
    service = ActivitiesService(repository, directory)
    settings = Settings(url, SECRET, "http://users.test", "sportmatch-auth", "sportmatch-mobile")
    try:
        with TestClient(create_app(service, settings)) as client:
            yield client, repository, directory, settings
    finally:
        with pool.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        pool.close()



def payload(**changes):
    return {"client_activity_id": str(uuid4()), "title": "Running en el parque", "sport_code": "running",
            "description": "Trote suave", "starts_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "location": "Parque, entrada principal", **changes}


def publish(client, actor=A, **changes):
    response = client.post(PREFIX, headers=headers(actor), json=payload(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def test_create_view_by_another_athlete_and_privacy(system):
    client, repo, _, _ = system
    created = publish(client)
    assert created['organizer']['user_id'] == str(A)
    assert set(created['organizer']) == {'user_id', 'nombre', 'apellido_inicial', 'foto_perfil'}
    assert 'client_activity_id' not in created and 'organizer_id' not in created
    detail = client.get(PREFIX + '/' + created['id'], headers=headers(B))
    assert detail.json() == created
    assert client.get(PREFIX, headers=headers(B)).json()['items'] == [created]
    assert str(repo.get(UUID(created['id']))['organizer_id']) == str(A)
    repo.migrate()  # Startup migrations are repeatable and preserve published activities.
    assert client.get(PREFIX + '/' + created['id'], headers=headers(C)).status_code == 200


def test_authentication_and_revoked_athletes(system):
    client, _, directory, _ = system
    assert client.get(PREFIX).status_code == 401
    assert client.post(PREFIX, json=payload()).status_code == 401
    assert client.get(PREFIX, headers={'Authorization': 'Bearer invalid'}).status_code == 401
    directory.active.remove(A)
    assert client.post(PREFIX, headers=headers(A), json=payload()).status_code == 404
    assert client.get(PREFIX, headers=headers(A)).status_code == 404


@pytest.mark.parametrize('changes', [
    {'title': '  '}, {'location': ' '}, {'sport_code': 'Running'}, {'sport_code': 'x; DROP TABLE'},
    {'description': 'x' * 2001}, {'title': 'x' * 121}, {'location': 'x' * 201},
    {'organizer_id': str(B)}, {'starts_at': '2030-01-01T10:00:00'},
    {'starts_at': 'yesterday'}, {'starts_at': '2020-01-01T00:00:00Z'},
])
def test_invalid_activity_rejected(system, changes):
    client, _, _, _ = system
    assert client.post(PREFIX, headers=headers(A), json=payload(**changes)).status_code == 422
    assert client.get(PREFIX, headers=headers(B)).json()['items'] == []


def test_concurrent_retries_are_idempotent_and_conflicting_payload_is_rejected(system):
    client, _, _, _ = system
    body = payload()
    def submit(_):
        return client.post(PREFIX, headers=headers(A), json=body)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(submit, range(4)))
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()['id'] for r in results}) == 1
    assert len(client.get(PREFIX, headers=headers(B)).json()['items']) == 1
    conflict = client.post(PREFIX, headers=headers(A), json={**body, 'title': 'Otro título'})
    assert conflict.status_code == 409
    assert client.post(PREFIX, headers=headers(B), json=body).status_code == 201


def test_pagination_same_date_no_duplicates_and_past_activity_not_listed(system):
    client, repo, _, _ = system
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    ids = {publish(client, starts_at=start)['id'] for _ in range(5)}
    past = publish(client)['id']
    with repo.pool.connection() as conn:
        repo.execute(conn, "UPDATE activities_api.activities SET starts_at=CURRENT_TIMESTAMP-INTERVAL '1 day' WHERE id=%s", (past,))
    seen = []
    cursor = None
    for _ in range(3):
        page = client.get(PREFIX, params={'limit': 2, **({'cursor': cursor} if cursor else {})}, headers=headers(B)).json()
        seen.extend(a['id'] for a in page['items'])
        cursor = page['next_cursor']
    assert cursor is None and set(seen) == ids and len(seen) == 5
    assert client.get(PREFIX + '/' + past, headers=headers(B)).status_code == 200
    for query in ['?limit=0', '?limit=101', '?cursor=wrong', '?cursor=' + str(uuid4())]:
        assert client.get(PREFIX + query, headers=headers(B)).status_code == 422
    assert client.get(PREFIX + '/' + str(uuid4()), headers=headers(B)).status_code == 404


def test_inactive_organizer_not_exposed(system):
    client, _, directory, _ = system
    created = publish(client)
    directory.active.remove(A)
    assert client.get(PREFIX, headers=headers(B)).json()['items'] == []
    assert client.get(PREFIX + '/' + created['id'], headers=headers(B)).status_code == 404
