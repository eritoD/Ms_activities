"""HU-64 in the group chat of an activity: answer a specific message of the same chat. Real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from tests.test_activities import A, B, C, PREFIX, headers, outbox, remove, start, system  # noqa: F401
from tests.test_group_chat import chat, group, read

GONE = "El mensaje citado no está disponible."


def post(client, activity_id, actor=A, text="hola", key=None, **body):
    return client.post(chat(activity_id) + "/messages", headers=headers(actor),
                       json={"text": text, "client_message_id": str(key or uuid4()), **body})


def sent(client, activity_id, actor=A, text="hola", **body):
    response = post(client, activity_id, actor, text, **body)
    assert response.status_code == 201, response.text
    return response.json()


def run(repo, statement, *params):
    with repo.pool.connection() as conn, conn.cursor() as cur:
        repo.execute(cur, statement, params)
        return cur.fetchall() if cur.description else []


def test_a_message_without_reply_has_no_reply_fields(system):
    client, *_ = system
    activity = group(client, B)
    message = sent(client, activity["id"])
    assert message["reply_to_id"] is None and message["reply_to"] is None
    assert "author_photo" in message


def test_answering_a_message_of_another_member_and_your_own(system):
    client, *_ = system
    activity = group(client, B)
    theirs = sent(client, activity["id"], B, "¿Quién lleva balones?")
    reply = sent(client, activity["id"], A, "Yo llevo", reply_to=theirs["id"])
    assert reply["reply_to_id"] == theirs["id"]
    assert reply["reply_to"] == {"id": theirs["id"], "author_id": str(B), "author_name": "Deportista T.",
                                 "text": "¿Quién lleva balones?", "available": True}
    mine = sent(client, activity["id"], A, "Y agua", reply_to=reply["id"])
    assert mine["reply_to"]["author_id"] == str(A) and mine["reply_to"]["text"] == "Yo llevo"
    assert "reply_to" not in mine["reply_to"] and set(mine["reply_to"]) == {"id", "author_id", "author_name", "text", "available"}


def test_the_preview_is_cut_short(system):
    client, *_ = system
    activity = group(client, B)
    long = sent(client, activity["id"], B, "y" * 900)
    preview = sent(client, activity["id"], A, reply_to=long["id"])["reply_to"]
    assert preview["text"] == "y" * 100 + "…"


def test_quoting_another_chat_or_a_missing_message_is_refused_the_same_way_and_saves_nothing(system):
    client, repo, *_ = system
    activity, other = group(client, B), group(client, C)
    secret = sent(client, other["id"], A, "mensaje de otro chat")
    before = run(repo, "SELECT count(*) FROM activities_api.group_messages")
    foreign = post(client, activity["id"], A, reply_to=secret["id"])
    missing = post(client, activity["id"], A, reply_to=987654321)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": GONE} and "otro chat" not in foreign.text
    assert run(repo, "SELECT count(*) FROM activities_api.group_messages") == before


def test_the_database_itself_refuses_a_reply_across_chats(system):
    client, repo, *_ = system
    activity, other = group(client, B), group(client, C)
    secret = sent(client, other["id"], A, "otro")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(repo, """INSERT INTO activities_api.group_messages (activity_id, author_id, text, client_message_id, reply_to_id, is_reply)
            VALUES (%s, %s, 'x', %s, %s, TRUE)""", activity["id"], A, uuid4(), secret["id"])


@pytest.mark.parametrize("value", [0, -1, "abc", 1.5, 2 ** 63])
def test_a_malformed_reply_id_is_422(system, value):
    client, *_ = system
    activity = group(client, B)
    assert post(client, activity["id"], A, reply_to=value).status_code == 422


def test_only_members_with_a_session_can_answer_or_load_around(system):
    client, *_ = system
    activity = group(client, B)
    original = sent(client, activity["id"], B)
    assert post(client, activity["id"], C, reply_to=original["id"]).status_code == 403      # not a member
    assert client.post(chat(activity["id"]) + "/messages",
                       json={"text": "x", "client_message_id": str(uuid4()), "reply_to": original["id"]}).status_code == 401
    assert read(client, activity["id"], C, around_id=original["id"]).status_code == 403
    assert read(client, activity["id"], B, around_id=original["id"]).status_code == 200
    assert read(client, activity["id"], B, around_id=original["id"]).json()["mensajes"][0]["id"] == original["id"]


def test_the_chat_closes_for_answers_like_for_any_message(system):
    client, repo, *_ = system
    activity = group(client, B)
    original = sent(client, activity["id"], B)
    start(repo, activity["id"])                                          # it already happened: read only
    assert post(client, activity["id"], A, reply_to=original["id"]).status_code == 403
    assert read(client, activity["id"], A, around_id=original["id"]).status_code == 200
    cancelled = group(client, B)
    other = sent(client, cancelled["id"], B)
    assert remove(client, cancelled["id"]).status_code == 204
    assert post(client, cancelled["id"], A, reply_to=other["id"]).status_code == 403


def test_the_history_carries_every_preview_with_one_lookup_of_people_and_few_queries(system):
    client, repo, directory, _ = system
    activity = group(client, B, C)
    base = sent(client, activity["id"], B, "base")
    for number in range(30):
        sent(client, activity["id"], A if number % 2 else C, f"r{number}", reply_to=base["id"])
    statements, lookups = [], []
    original, cards = repo.execute, directory.cards
    repo.execute = lambda cursor, statement, params=None: (statements.append(statement), original(cursor, statement, params))[1]
    directory.cards = lambda ids, token: (lookups.append(set(ids)), cards(ids, token))[1]
    try:
        page = read(client, activity["id"], A, limit=50).json()["mensajes"]
    finally:
        repo.execute, directory.cards = original, cards
    assert len(page) == 31 and all(m["reply_to"]["text"] == "base" and m["reply_to"]["author_name"] for m in page[1:])
    assert len(lookups) == 1 and len(statements) <= 4, (lookups, statements)


def test_a_removed_original_leaves_the_reply_and_says_it_is_not_available(system):
    client, repo, *_ = system
    activity = group(client, B)
    original = sent(client, activity["id"], B, "texto privado del original")
    reply = sent(client, activity["id"], A, "respuesta", reply_to=original["id"])
    run(repo, "DELETE FROM activities_api.group_messages WHERE id = %s", original["id"])
    [saved] = read(client, activity["id"], A).json()["mensajes"]
    assert saved["id"] == reply["id"] and saved["text"] == "respuesta" and saved["reply_to_id"] is None
    assert saved["reply_to"] == {"id": None, "author_id": None, "author_name": None, "text": None, "available": False}
    assert "privado" not in str(saved)
    assert post(client, activity["id"], A, reply_to=original["id"]).status_code == 404


def test_loading_around_a_quoted_message(system):
    client, *_ = system
    activity = group(client, B)
    ids = [sent(client, activity["id"], A if n % 2 else B, f"m{n}")["id"] for n in range(120)]
    page = read(client, activity["id"], A, around_id=ids[20], limit=20).json()["mensajes"]
    got = [m["id"] for m in page]
    assert got == sorted(got) and len(got) == 20 and ids[20] in got and got[0] == ids[11] and got[-1] == ids[30]
    assert read(client, activity["id"], A, around_id=ids[-1], limit=20).json()["mensajes"][-1]["id"] == ids[-1]
    assert len(read(client, activity["id"], A, around_id=ids[20], limit=100).json()["mensajes"]) == 71


def test_around_needs_a_message_of_this_chat_and_a_single_cursor(system):
    client, *_ = system
    activity, other = group(client, B), group(client, C)
    mine, foreign = sent(client, activity["id"]), sent(client, other["id"])
    for value in (foreign["id"], 987654321):
        assert read(client, activity["id"], A, around_id=value).status_code == 404
    assert read(client, activity["id"], A, around_id=mine["id"], before_id=mine["id"]).status_code == 422
    assert read(client, activity["id"], A, around_id=mine["id"], after_id=1).status_code == 422
    for bad in (0, -1, "x"):
        assert read(client, activity["id"], A, around_id=bad).status_code == 422
    assert read(client, activity["id"], A, around_id=mine["id"], limit=101).status_code == 422


def test_a_retry_of_a_reply_is_the_same_message_and_a_different_quote_is_a_conflict(system):
    client, repo, *_ = system
    activity = group(client, B)
    one, two = sent(client, activity["id"], B, "uno"), sent(client, activity["id"], B, "dos")
    key = uuid4()
    first = post(client, activity["id"], A, "ok", key, reply_to=one["id"])
    again = post(client, activity["id"], A, "ok", key, reply_to=one["id"])
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    assert again.json()["reply_to"]["text"] == "uno"
    assert post(client, activity["id"], A, "ok", key, reply_to=two["id"]).status_code == 409
    assert post(client, activity["id"], A, "ok", key).status_code == 409                # dropping the quote also differs
    assert run(repo, "SELECT count(*) FROM activities_api.group_messages WHERE activity_id = %s", activity["id"])[0][0] == 3


def test_two_simultaneous_answers_to_the_same_message_are_both_saved(system):
    client, repo, *_ = system
    activity = group(client, B)
    original = sent(client, activity["id"], B, "base")

    def answer(who):
        return post(client, activity["id"], who, f"de {who}", reply_to=original["id"])

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(answer, (A, B))
    assert first.status_code == second.status_code == 201 and first.json()["id"] != second.json()["id"]
    assert run(repo, "SELECT count(*) FROM activities_api.group_messages WHERE reply_to_id = %s", original["id"])[0][0] == 2


def test_an_answer_notifies_like_any_message_respecting_muted_members_and_never_carries_the_text(system):
    client, repo, *_ = system
    activity = group(client, B, C)
    assert client.put(chat(activity["id"]) + "/mute", headers=headers(C), json={"muted": True}).status_code == 204
    original = sent(client, activity["id"], B, "secreto del original")
    before = len(outbox(repo))
    sent(client, activity["id"], A, "secreto de la respuesta", reply_to=original["id"])
    new = outbox(repo)[before:]
    assert [user for user, *_ in new] == [str(B)]                    # the organizer's reply: B yes, C (muted) no, A is the author
    assert all("secreto" not in str(row) for row in new)


def test_the_purge_of_a_chat_removes_replies_and_originals_together(system):
    client, repo, *_ = system
    activity = group(client, B)
    original = sent(client, activity["id"], B, "uno")
    sent(client, activity["id"], A, "dos", reply_to=original["id"])
    now = datetime.now(timezone.utc)
    run(repo, "UPDATE activities_api.activities SET starts_at=%s WHERE id=%s", now - timedelta(days=8), activity["id"])
    assert repo.purge_old_chats(now=now) == 1
    assert run(repo, "SELECT count(*) FROM activities_api.group_messages WHERE activity_id = %s", activity["id"])[0][0] == 0


def test_the_migration_can_run_again_and_keeps_old_messages(system):
    client, repo, *_ = system
    activity = group(client, B)
    sent(client, activity["id"], A, "antes")
    repo.migrate()
    repo.migrate()
    count = run(repo, "SELECT count(*) FROM pg_constraint WHERE conname = 'group_messages_reply_same_chat' "
                      "AND conrelid = to_regclass('activities_api.group_messages')")
    assert count[0][0] == 1
    assert [m["text"] for m in read(client, activity["id"], A).json()["mensajes"]] == ["antes"]
