"""HU-50 group chat: access rules without a database, then the API on isolated PostgreSQL schemas."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.activities_service import ActivitiesService, author_name, chat_access
from tests.test_activities import (
    A, B, C, PREFIX, apply, decide, headers, outbox, publish, remove, start, system,  # noqa: F401
)


# ---------- rules (no database) ----------
@pytest.mark.parametrize('member, cancelled, started, expected', [
    (True, False, False, 'escribir'), (True, False, True, 'leer'),
    (True, True, False, 'ninguno'), (True, True, True, 'ninguno'),
    (False, False, False, 'ninguno'), (False, False, True, 'ninguno'),
])
def test_chat_access_rules(member, cancelled, started, expected):
    assert chat_access(member, cancelled, started) == expected


def test_expired_chat_has_no_access():
    assert chat_access(True, False, True, closed=True) == 'ninguno'


def test_author_name_uses_first_name_and_initial():
    assert author_name({'nombre': 'Ana', 'apellido_inicial': 'P.'}) == 'Ana P.'
    assert author_name({'nombre': 'Ana', 'apellido_inicial': ''}) == 'Ana'
    assert author_name(None) == 'Deportista no disponible'


class FakeRepository:
    def __init__(self, **activity):
        self.activity = {'member': True, 'cancelled': False, 'started': False, 'participants': 2, **activity}
        self.rows = [{'id': i, 'author_id': A if i % 2 else B, 'text': 'hola', 'created_at': datetime.now(timezone.utc)}
                     for i in range(1, 6)]

    def chat_activity(self, activity_id, user_id):
        return self.activity

    def chat_page(self, activity_id, user_id, after_id, before_id, limit, check):
        check(self.activity)
        return self.activity, False, self.rows


class EmptyRepository(FakeRepository):
    def chat_page(self, activity_id, user_id, after_id, before_id, limit, check):
        check(self.activity)
        return self.activity, False, []


class CountingUsers:
    def __init__(self):
        self.calls = []

    def cards(self, user_ids, token):
        self.calls.append(set(user_ids))
        return {uid: {'nombre': 'Ana', 'apellido_inicial': 'P.'} for uid in user_ids}


class Actor:
    id, token = A, 'token'


def test_author_cards_are_fetched_once_per_request():
    users = CountingUsers()
    page = ActivitiesService(FakeRepository(), users).chat_messages(Actor, uuid4(), None, None, 50)
    assert users.calls == [{A, B}]
    assert [m['own'] for m in page['mensajes']] == [True, False, True, False, True]
    assert page['acceso'] == 'escribir' and page['mensajes'][0]['author_name'] == 'Ana P.'


def test_a_poll_without_new_messages_does_not_call_ms_users():
    # The open chat polls every few seconds; an empty poll must stay cheap.
    users = CountingUsers()
    page = ActivitiesService(EmptyRepository(), users).chat_messages(Actor, uuid4(), 5, None, 50)
    assert users.calls == [] and page['mensajes'] == []


@pytest.mark.parametrize('activity', [{'member': False}, {'cancelled': True}])
def test_without_access_the_service_answers_403(activity):
    users = CountingUsers()
    with pytest.raises(HTTPException) as error:
        ActivitiesService(FakeRepository(**activity), users).chat_messages(Actor, uuid4(), None, None, 50)
    assert error.value.status_code == 403 and users.calls == []


# ---------- API with PostgreSQL ----------
def chat(activity_id):
    return f"{PREFIX}/{activity_id}/chat"


def send(client, activity_id, actor=A, text='Hola a todos', client_message_id=None):
    return client.post(chat(activity_id) + '/messages', headers=headers(actor),
                       json={'text': text, 'client_message_id': str(client_message_id or uuid4())})


def read(client, activity_id, actor=A, **params):
    return client.get(chat(activity_id) + '/messages', headers=headers(actor), params=params)


def group(client, *accepted, capacity=5):
    """An upcoming activity organized by A with the given athletes accepted."""
    activity = publish(client, capacity=capacity)
    for athlete in accepted:
        application = apply(client, activity['id'], actor=athlete).json()
        assert decide(client, activity['id'], application['id'], 'accept').status_code == 200
    return activity


def test_organizer_and_accepted_participant_write_and_read(system):
    client, _, _, _ = system
    activity = group(client, B)
    first = send(client, activity['id'], text='  Nos vemos a las 7  ')
    assert first.status_code == 201, first.text
    message = first.json()
    assert set(message) == {'id', 'author_id', 'author_name', 'author_photo', 'text', 'created_at', 'own',
                           'reply_to_id', 'reply_to'}  # HU-64 added the photo and the reply fields
    assert message['text'] == 'Nos vemos a las 7' and message['own'] is True
    assert message['author_id'] == str(A) and message['author_name'] == 'Deportista T.'
    assert send(client, activity['id'], actor=B, text='Listo').status_code == 201
    page = read(client, activity['id'], actor=B).json()
    assert page['acceso'] == 'escribir' and page['participantes'] == 2 and page['silenciado'] is False
    assert [(m['text'], m['own']) for m in page['mensajes']] == [('Nos vemos a las 7', False), ('Listo', True)]


def test_who_has_no_access_gets_403(system):
    client, _, directory, _ = system
    outsider = uuid4()
    directory.active.add(outsider)
    activity = group(client, B, capacity=5)
    apply(client, activity['id'], actor=C)                              # Pending.
    for actor in (C, outsider):
        assert send(client, activity['id'], actor=actor).status_code == 403
        assert read(client, activity['id'], actor=actor).status_code == 403
        assert client.post(chat(activity['id']) + '/read', headers=headers(actor),
                           json={'last_read_id': 1}).status_code == 403
        assert client.put(chat(activity['id']) + '/mute', headers=headers(actor),
                          json={'muted': True}).status_code == 403
    rejected = publish(client)
    application = apply(client, rejected['id']).json()
    decide(client, rejected['id'], application['id'], 'reject')
    assert send(client, rejected['id'], actor=B).status_code == 403
    assert read(client, uuid4()).status_code == 404
    assert client.get(chat(activity['id']) + '/messages').status_code == 401


def test_cancelled_activity_closes_the_chat(system):
    client, _, _, _ = system
    activity = group(client, B)
    send(client, activity['id'])
    assert remove(client, activity['id']).status_code == 204
    for actor in (A, B):
        assert read(client, activity['id'], actor=actor).status_code == 403
        assert send(client, activity['id'], actor=actor).status_code == 403
    assert client.get(PREFIX + '/chats', headers=headers(B)).json() == []


def test_past_activity_is_read_only(system):
    client, repo, _, _ = system
    activity = group(client, B)
    retry = uuid4()
    send(client, activity['id'], client_message_id=retry, text='Antes de empezar')
    start(repo, activity['id'])
    page = read(client, activity['id'], actor=B).json()
    assert page['acceso'] == 'leer' and [m['text'] for m in page['mensajes']] == ['Antes de empezar']
    assert send(client, activity['id'], actor=B).status_code == 403
    # A late retry of a message that was already saved still answers with it.
    late = send(client, activity['id'], client_message_id=retry, text='Antes de empezar')
    assert late.status_code == 201 and late.json()['id'] == page['mensajes'][0]['id']


def test_chat_closes_and_disappears_seven_days_after_activity(system):
    client, repo, _, _ = system
    activity = group(client, B)
    send(client, activity['id'])
    with repo.pool.connection() as conn:
        repo.execute(conn, "UPDATE activities_api.activities SET starts_at=CURRENT_TIMESTAMP-INTERVAL '8 days' WHERE id=%s",
                     (activity['id'],))
    response = read(client, activity['id'])
    assert response.status_code == 403
    assert response.json()['detail'] == 'El chat de esta actividad ya se cerró.'
    listed = client.get(PREFIX + '/chats', headers=headers(A)).json()
    assert activity['id'] not in {row['activity_id'] for row in listed}


def test_purge_old_chats_accepts_an_injected_clock(system):
    client, repo, _, _ = system
    activity = group(client, B)
    send(client, activity['id'])
    now = datetime.now(timezone.utc)
    with repo.pool.connection() as conn:
        repo.execute(conn, "UPDATE activities_api.activities SET starts_at=%s WHERE id=%s",
                     (now - timedelta(days=8), activity['id']))
    assert repo.purge_old_chats(now=now) == 1
    with repo.pool.connection() as conn, conn.cursor() as cur:
        repo.execute(cur, "SELECT count(*) FROM activities_api.group_messages WHERE activity_id=%s", (activity['id'],))
        assert cur.fetchone()[0] == 0
        repo.execute(cur, "SELECT count(*) FROM activities_api.group_chat_state WHERE activity_id=%s", (activity['id'],))
        assert cur.fetchone()[0] == 0
    assert repo.purge_old_chats(now=now) == 0


def test_retries_are_idempotent(system):
    client, repo, _, _ = system
    activity = group(client, B)
    key = uuid4()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: send(client, activity['id'], client_message_id=key), range(4)))
    assert {r.status_code for r in results} == {201} and len({r.json()['id'] for r in results}) == 1
    assert len(read(client, activity['id']).json()['mensajes']) == 1
    assert send(client, activity['id'], client_message_id=key, text='Otro texto').status_code == 409
    # The key belongs to its author: another member may use the same one.
    assert send(client, activity['id'], actor=B, client_message_id=key).status_code == 201
    assert len([e for e in outbox(repo) if e[1].startswith('Nuevo mensaje')]) == 2


@pytest.mark.parametrize('body', [
    {'text': ''}, {'text': '   '}, {'text': 'x' * 1001}, {'text': 'Hola', 'client_message_id': 'nope'},
    {'text': 'Hola', 'author_id': str(B)}, {'client_message_id': str(uuid4())},
])
def test_invalid_messages_rejected(system, body):
    client, _, _, _ = system
    activity = group(client, B)
    response = client.post(chat(activity['id']) + '/messages', headers=headers(A),
                           json={'client_message_id': str(uuid4()), **body})
    assert response.status_code == 422
    assert read(client, activity['id']).json()['mensajes'] == []


def test_long_message_up_to_the_limit_is_accepted(system):
    client, _, _, _ = system
    activity = group(client)
    assert send(client, activity['id'], text='x' * 1000).status_code == 201


def test_pagination_is_ascending_with_cursors(system):
    client, _, _, _ = system
    activity = group(client, B)
    ids = [send(client, activity['id'], text=f'm{i}').json()['id'] for i in range(5)]
    assert [m['id'] for m in read(client, activity['id'], limit=2).json()['mensajes']] == ids[3:]
    assert [m['id'] for m in read(client, activity['id'], before_id=ids[3], limit=2).json()['mensajes']] == ids[1:3]
    assert [m['id'] for m in read(client, activity['id'], after_id=ids[1], limit=2).json()['mensajes']] == ids[2:4]
    assert read(client, activity['id'], after_id=ids[-1]).json()['mensajes'] == []
    for params in [{'limit': 0}, {'limit': 101}, {'after_id': -1}, {'before_id': 'x'}, {'after_id': 2 ** 63}]:
        assert read(client, activity['id'], **params).status_code == 422


def test_unread_and_mark_read(system):
    client, _, _, _ = system
    activity = group(client, B)
    send(client, activity['id'], text='uno')
    second = send(client, activity['id'], text='dos').json()
    send(client, activity['id'], actor=B, text='mío')
    listed = client.get(PREFIX + '/chats', headers=headers(B)).json()
    assert listed[0]['unread'] == 2 and listed[0]['ultimo']['text'] == 'mío' and listed[0]['ultimo']['own'] is True
    read_url = chat(activity['id']) + '/read'
    assert client.post(read_url, headers=headers(B), json={'last_read_id': second['id'] - 1}).status_code == 204
    assert client.get(PREFIX + '/chats', headers=headers(B)).json()[0]['unread'] == 1
    # Going back never un-reads; an id past the end only reaches the last message.
    assert client.post(read_url, headers=headers(B), json={'last_read_id': 0}).status_code == 204
    assert client.get(PREFIX + '/chats', headers=headers(B)).json()[0]['unread'] == 1
    assert client.post(read_url, headers=headers(B), json={'last_read_id': 10 ** 12}).status_code == 204
    assert client.get(PREFIX + '/chats', headers=headers(B)).json()[0]['unread'] == 0
    third = send(client, activity['id'], text='tres')
    assert third.status_code == 201
    assert client.get(PREFIX + '/chats', headers=headers(B)).json()[0]['unread'] == 1
    for body in [{}, {'last_read_id': -1}, {'last_read_id': 'x'}, {'last_read_id': 2 ** 63}]:
        assert client.post(read_url, headers=headers(B), json=body).status_code == 422


def test_alerts_skip_author_and_muted_members_and_never_carry_the_text(system):
    client, repo, _, _ = system
    activity = group(client, B, C, capacity=5)
    before = len(outbox(repo))
    send(client, activity['id'], text='Secreto: llevo pelota')
    alerts = outbox(repo)[before:]
    assert sorted(user for user, _, _ in alerts) == sorted([str(B), str(C)])
    assert all('Secreto' not in title + body for _, title, body in alerts)
    mute_url = chat(activity['id']) + '/mute'
    assert client.put(mute_url, headers=headers(C), json={'muted': True}).status_code == 204
    assert read(client, activity['id'], actor=C).json()['silenciado'] is True
    before = len(outbox(repo))
    send(client, activity['id'], actor=B)
    assert [user for user, _, _ in outbox(repo)[before:]] == [str(A)]
    assert client.put(mute_url, headers=headers(C), json={'muted': False}).status_code == 204
    before = len(outbox(repo))
    send(client, activity['id'], actor=B)
    assert sorted(user for user, _, _ in outbox(repo)[before:]) == sorted([str(A), str(C)])
    with repo.pool.connection() as conn, conn.cursor() as cur:
        repo.execute(cur, "SELECT DISTINCT kind, data FROM activities_api.notification_outbox WHERE title LIKE 'Nuevo mensaje%%'")
        assert cur.fetchall() == [('mensaje', {'screen': 'activity_chat', 'activityId': activity['id']})]
    assert client.put(mute_url, headers=headers(C), json={}).status_code == 422


def test_chats_list_only_member_activities_by_last_message(system):
    client, repo, directory, _ = system
    quiet = group(client, B)
    busy = group(client, B)
    held = group(client, B)
    send(client, held['id'], text='antes')
    send(client, busy['id'], text='último')
    start(repo, held['id'])
    pending = publish(client)
    apply(client, pending['id'], actor=C)
    listed = client.get(PREFIX + '/chats', headers=headers(B)).json()
    assert [c['activity_id'] for c in listed] == [busy['id'], held['id'], quiet['id']]
    assert set(listed[0]) == {'activity_id', 'title', 'sport_code', 'starts_at', 'acceso', 'participantes',
                              'ultimo', 'unread', 'silenciado'}
    assert [c['acceso'] for c in listed] == ['escribir', 'leer', 'escribir']
    assert listed[2]['ultimo'] is None and listed[2]['unread'] == 0 and listed[0]['participantes'] == 2
    assert len(client.get(PREFIX + '/chats', headers=headers(A)).json()) == 4  # The organizer sees all four.
    assert client.get(PREFIX + '/chats', headers=headers(C)).json() == []       # Pending is not a member.
    # A participant who withdraws leaves the chat; their messages stay for the others.
    send(client, quiet['id'], actor=B, text='me voy')
    assert client.delete(f"{PREFIX}/{quiet['id']}/applications/me", headers=headers(B)).status_code == 204
    assert read(client, quiet['id'], actor=B).status_code == 403
    assert read(client, quiet['id']).json()['mensajes'][0]['text'] == 'me voy'
    # A disabled author's messages remain, without their card.
    directory.active.remove(B)
    assert read(client, quiet['id']).json()['mensajes'][0]['author_name'] == 'Deportista no disponible'
    assert client.get(PREFIX + '/chats').status_code == 401
