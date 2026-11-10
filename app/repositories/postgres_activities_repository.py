from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from psycopg import sql
from psycopg.rows import dict_row


# Columns an organizer may change; anything else never reaches the UPDATE.
EDITABLE_COLUMNS = ("title", "sport_code", "description", "starts_at", "location", "capacity")
CANCELLED = "La actividad fue cancelada."


# A message with the author and text of the one it quotes, in the same query (no query per message).
_WITH_REPLY = """SELECT g.*, o.author_id AS reply_author_id, o.text AS reply_text
    FROM activities_api.group_messages g
    LEFT JOIN activities_api.group_messages o ON o.activity_id = g.activity_id AND o.id = g.reply_to_id"""


class PostgresActivitiesRepository:
    def __init__(self, pool, schema="activities_api"):
        self.pool, self.schema = pool, schema

    def execute(self, cursor, statement, params=None):
        query = sql.SQL(statement.replace("activities_api", "{schema}")).format(schema=sql.Identifier(self.schema))
        return cursor.execute(query, params)

    def migrate(self):
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('activities-api-schema', 0))")
            for migration in sorted((Path(__file__).parents[2] / "db/migrations").glob("*.sql")):
                self.execute(conn, migration.read_text(encoding='utf-8'))

    def ready(self):
        with self.pool.connection() as conn:
            self.execute(conn, "SELECT id FROM activities_api.activities LIMIT 1")

    def create(self, actor_id, payload):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Serialize retries before checking the date, even if the event has since started.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"activity:{actor_id}:{payload.client_activity_id}",))
            self.execute(cur, "SELECT * FROM activities_api.activities WHERE organizer_id=%s AND client_activity_id=%s",
                         (actor_id, payload.client_activity_id))
            existing = cur.fetchone()
            if existing:
                if any(existing[key] != value for key, value in payload.model_dump().items()):
                    raise HTTPException(409, "Este intento ya publicó otra actividad. Actualiza la pantalla.")
                return existing
            if payload.starts_at <= datetime.now(timezone.utc):
                raise HTTPException(422, "La fecha y hora deben ser futuras.")
            self.execute(cur, """INSERT INTO activities_api.activities
                (id, organizer_id, client_activity_id, title, sport_code, description, starts_at, location, capacity)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (uuid4(), actor_id, payload.client_activity_id, payload.title, payload.sport_code,
                 payload.description, payload.starts_at, payload.location, payload.capacity))
            return cur.fetchone()

    def get(self, activity_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, "SELECT * FROM activities_api.activities WHERE id=%s", (activity_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Actividad no disponible.")
            return row

    def _own_activity_for_update(self, cur, activity_id, organizer_id):
        # Lock the row so edits, cancellation and acceptances never interleave.
        self.execute(cur, "SELECT * FROM activities_api.activities WHERE id=%s FOR UPDATE", (activity_id,))
        activity = cur.fetchone()
        if not activity or activity['organizer_id'] != organizer_id:
            raise HTTPException(404, "Actividad no disponible.")
        return activity

    def update(self, activity_id, organizer_id, changes):
        """Changes only the given fields of an organizer's upcoming, active activity."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            activity = self._own_activity_for_update(cur, activity_id, organizer_id)
            now = datetime.now(timezone.utc)
            if activity['cancelled_at']:
                raise HTTPException(409, CANCELLED)
            if activity['starts_at'] <= now:
                raise HTTPException(422, "La actividad ya comenzó; no se puede modificar.")
            if 'starts_at' in changes and changes['starts_at'] <= now:
                raise HTTPException(422, "La fecha y hora deben ser futuras.")
            capacity = changes.get('capacity', activity['capacity'])
            if capacity is not None and capacity < activity['accepted_count']:
                raise HTTPException(409, f"Ya hay {activity['accepted_count']} postulantes aceptados; "
                                         "los cupos no pueden ser menos.")
            columns = [column for column in EDITABLE_COLUMNS if column in changes]
            assignments = ", ".join(f"{column}=%s" for column in columns)
            self.execute(cur, f"""UPDATE activities_api.activities SET {assignments}, updated_at=CURRENT_TIMESTAMP
                WHERE id=%s RETURNING *""", (*(changes[column] for column in columns), activity_id))
            return cur.fetchone()

    def cancel(self, activity_id, organizer_id):
        """Cancels instead of deleting, so applicants can still see what happened. Repeating is harmless."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            activity = self._own_activity_for_update(cur, activity_id, organizer_id)
            if activity['cancelled_at']:
                return activity
            if activity['starts_at'] <= datetime.now(timezone.utc):
                raise HTTPException(422, "La actividad ya comenzó; no se puede eliminar.")
            self.execute(cur, """UPDATE activities_api.activities SET cancelled_at=CURRENT_TIMESTAMP
                WHERE id=%s RETURNING *""", (activity_id,))
            return cur.fetchone()

    def apply(self, activity_id, applicant_id):
        """Returns (application, created). A repeated request returns the existing one."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, """SELECT starts_at, capacity, accepted_count, cancelled_at
                FROM activities_api.activities WHERE id=%s""", (activity_id,))
            activity = cur.fetchone()
            if not activity:
                raise HTTPException(404, "Actividad no disponible.")
            existing = self._application(cur, activity_id, applicant_id)
            if existing:
                if existing['status'] == 'removed':
                    raise HTTPException(409, "El organizador te retiró de esta actividad; no puedes volver a postular.")
                return existing, False
            if activity['cancelled_at']:
                raise HTTPException(409, CANCELLED)
            if activity['starts_at'] <= datetime.now(timezone.utc):
                raise HTTPException(422, "La actividad ya comenzó; no admite postulaciones.")
            if self._full(activity):
                raise HTTPException(409, "La actividad no tiene cupos disponibles.")
            self.execute(cur, """INSERT INTO activities_api.activity_applications (id, activity_id, applicant_id)
                VALUES (%s,%s,%s) ON CONFLICT (activity_id, applicant_id) DO NOTHING RETURNING *""",
                (uuid4(), activity_id, applicant_id))
            created = cur.fetchone()
            # A concurrent retry may have inserted first; both callers get the same application.
            return (created, True) if created else (self._application(cur, activity_id, applicant_id), False)

    @staticmethod
    def _full(activity):
        return activity['capacity'] is not None and activity['accepted_count'] >= activity['capacity']

    def decide(self, activity_id, organizer_id, application_id, status):
        """Accepts or rejects a pending application; accepting takes one spot atomically."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # The lock also keeps concurrent acceptances from exceeding its capacity.
            activity = self._own_activity_for_update(cur, activity_id, organizer_id)
            self.execute(cur, """SELECT * FROM activities_api.activity_applications
                WHERE id=%s AND activity_id=%s FOR UPDATE""", (application_id, activity_id))
            application = cur.fetchone()
            if not application:
                raise HTTPException(404, "Postulación no disponible.")
            if application['status'] == status:
                return application
            if application['status'] != 'pending':
                raise HTTPException(409, "Esta postulación ya fue respondida.")
            if activity['cancelled_at']:
                raise HTTPException(409, CANCELLED)
            if activity['starts_at'] <= datetime.now(timezone.utc):
                raise HTTPException(422, "La actividad ya comenzó; no se pueden responder postulaciones.")
            if status == 'accepted':
                if self._full(activity):
                    raise HTTPException(409, "La actividad no tiene cupos disponibles.")
                self.execute(cur, "UPDATE activities_api.activities SET accepted_count=accepted_count+1 WHERE id=%s",
                             (activity_id,))
            self.execute(cur, """UPDATE activities_api.activity_applications
                SET status=%s, decided_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *""", (status, application_id))
            return cur.fetchone()

    def withdraw(self, activity_id, applicant_id):
        """The applicant cancels a pending or accepted application; an accepted one frees its spot."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Same lock as accept/reject, so a spot is never freed and taken at the same time.
            self.execute(cur, "SELECT * FROM activities_api.activities WHERE id=%s FOR UPDATE", (activity_id,))
            activity = cur.fetchone()
            if not activity:
                raise HTTPException(404, "Actividad no disponible.")
            self.execute(cur, """SELECT * FROM activities_api.activity_applications
                WHERE activity_id=%s AND applicant_id=%s FOR UPDATE""", (activity_id, applicant_id))
            application = cur.fetchone()
            if not application:
                raise HTTPException(404, "No has postulado a esta actividad.")
            if application['status'] == 'removed':
                raise HTTPException(409, "El organizador te retiró de esta actividad; no puedes cancelar la postulación.")
            if application['status'] == 'rejected':
                raise HTTPException(409, "Tu postulación fue rechazada; no se puede cancelar.")
            if activity['cancelled_at']:
                raise HTTPException(409, CANCELLED)
            if activity['starts_at'] <= datetime.now(timezone.utc):
                raise HTTPException(422, "La actividad ya comenzó; no puedes cancelar tu postulación.")
            if application['status'] == 'accepted':
                self.execute(cur, """UPDATE activities_api.activities SET accepted_count=accepted_count-1
                    WHERE id=%s""", (activity_id,))
            self.execute(cur, "DELETE FROM activities_api.activity_applications WHERE id=%s", (application['id'],))

    def remove_participant(self, activity_id, organizer_id, applicant_id):
        """Removes an accepted participant and releases their spot atomically."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            activity = self._own_activity_for_update(cur, activity_id, organizer_id)
            if activity['cancelled_at']:
                raise HTTPException(409, CANCELLED)
            if activity['starts_at'] <= datetime.now(timezone.utc):
                raise HTTPException(422, "La actividad ya comenzó; no puedes sacar participantes.")
            self.execute(cur, """SELECT * FROM activities_api.activity_applications
                WHERE activity_id=%s AND applicant_id=%s FOR UPDATE""", (activity_id, applicant_id))
            application = cur.fetchone()
            if not application or application['status'] != 'accepted':
                raise HTTPException(404, "Participante no disponible.")
            self.execute(cur, """UPDATE activities_api.activity_applications
                SET status='removed', decided_at=CURRENT_TIMESTAMP WHERE id=%s""", (application['id'],))
            self.execute(cur, """UPDATE activities_api.activities SET accepted_count=accepted_count-1
                WHERE id=%s""", (activity_id,))

    def application_by_id(self, activity_id, application_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, "SELECT * FROM activities_api.activity_applications WHERE id=%s AND activity_id=%s",
                         (application_id, activity_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Postulación no disponible.")
            return row

    def _application(self, cur, activity_id, applicant_id):
        self.execute(cur, "SELECT * FROM activities_api.activity_applications WHERE activity_id=%s AND applicant_id=%s",
                     (activity_id, applicant_id))
        return cur.fetchone()

    def application(self, activity_id, applicant_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = self._application(cur, activity_id, applicant_id)
            if not row:
                raise HTTPException(404, "No has postulado a esta actividad.")
            return row

    def applications(self, activity_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, """SELECT * FROM activities_api.activity_applications WHERE activity_id=%s
                ORDER BY created_at, id""", (activity_id,))
            return cur.fetchall()

    def upcoming(self, limit, cursor):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            boundary = None
            if cursor:
                self.execute(cur, "SELECT starts_at, id FROM activities_api.activities WHERE id=%s", (cursor,))
                boundary = cur.fetchone()
                if not boundary:
                    raise HTTPException(422, "Página inválida. Actualiza las actividades.")
            self.execute(cur, """SELECT * FROM activities_api.activities WHERE starts_at > CURRENT_TIMESTAMP
                AND cancelled_at IS NULL AND (%s::timestamptz IS NULL OR (starts_at,id) > (%s,%s))
                ORDER BY starts_at,id LIMIT %s""",
                (boundary['starts_at'] if boundary else None, boundary['starts_at'] if boundary else None,
                 boundary['id'] if boundary else None, limit + 1))
            rows = cur.fetchall()
            return rows[:limit], rows[limit - 1]['id'] if len(rows) > limit else None

    # ---------- history and ratings ----------
    # Who took part in an activity that already happened: the organizer and every accepted applicant.
    # Cancelled activities never count.
    _MEMBERS = """SELECT a.id AS activity_id, a.organizer_id AS user_id, 'organizer' AS role
            FROM activities_api.activities a
            WHERE a.cancelled_at IS NULL AND a.starts_at <= CURRENT_TIMESTAMP
        UNION ALL
        SELECT a.id, p.applicant_id, 'participant'
            FROM activities_api.activities a
            JOIN activities_api.activity_applications p ON p.activity_id = a.id AND p.status = 'accepted'
            WHERE a.cancelled_at IS NULL AND a.starts_at <= CURRENT_TIMESTAMP"""

    def history(self, user_id, limit):
        """Activities already held where the athlete organized or was accepted, newest first."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, f"""WITH members AS ({self._MEMBERS})
                SELECT a.*, m.role, count(*) OVER () AS total
                FROM members m JOIN activities_api.activities a ON a.id = m.activity_id
                WHERE m.user_id = %s ORDER BY a.starts_at DESC, a.id LIMIT %s""", (user_id, limit))
            rows = cur.fetchall()
            if rows:
                return rows, rows[0]['total']
            return [], 0

    def participants(self, activity_id):
        """Organizer and accepted applicants of an activity, whether or not it already happened."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, """SELECT organizer_id AS user_id, 'organizer' AS role FROM activities_api.activities
                WHERE id=%s
                UNION ALL
                SELECT applicant_id, 'participant' FROM activities_api.activity_applications
                WHERE activity_id=%s AND status='accepted'""", (activity_id, activity_id))
            return cur.fetchall()

    def shared_past_activity(self, user_id, other_id):
        with self.pool.connection() as conn, conn.cursor() as cur:
            self.execute(cur, f"""WITH members AS ({self._MEMBERS})
                SELECT EXISTS (SELECT 1 FROM members x JOIN members y ON x.activity_id = y.activity_id
                               WHERE x.user_id = %s AND y.user_id = %s)""", (user_id, other_id))
            return cur.fetchone()[0]

    def rate(self, rater_id, rated_id, score, comment):
        """One rating per pair of athletes; rating again replaces the previous score and comment."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, """INSERT INTO activities_api.ratings (id, rater_id, rated_id, score, comment)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (rater_id, rated_id) DO UPDATE
                SET score=EXCLUDED.score, comment=EXCLUDED.comment, updated_at=CURRENT_TIMESTAMP
                RETURNING *""", (uuid4(), rater_id, rated_id, score, comment))
            return cur.fetchone()

    def received_ratings(self, user_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, """SELECT * FROM activities_api.ratings WHERE rated_id=%s
                ORDER BY updated_at DESC, id""", (user_id,))
            return cur.fetchall()

    def given_ratings(self, user_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, """SELECT * FROM activities_api.ratings WHERE rater_id=%s
                ORDER BY updated_at DESC, id""", (user_id,))
            return cur.fetchall()

    # ---------- group chat ----------
    # Members are the organizer and the accepted applicants; "started" uses the same clock as the rest.
    _CHAT_ACTIVITY = """SELECT a.id, a.title, a.sport_code, a.starts_at,
            a.cancelled_at IS NOT NULL AS cancelled, a.starts_at <= CURRENT_TIMESTAMP AS started,
            COALESCE(a.cancelled_at, a.starts_at) + INTERVAL '7 days' <= CURRENT_TIMESTAMP AS chat_closed,
            1 + a.accepted_count AS participants,
            (a.organizer_id = %s OR EXISTS (SELECT 1 FROM activities_api.activity_applications p
                WHERE p.activity_id = a.id AND p.applicant_id = %s AND p.status = 'accepted')) AS member
        FROM activities_api.activities a"""

    def _chat_activity(self, cur, activity_id, user_id, lock=False):
        # FOR SHARE waits for a cancellation, acceptance or withdrawal in progress (they lock FOR UPDATE).
        self.execute(cur, self._CHAT_ACTIVITY + " WHERE a.id=%s" + (" FOR SHARE OF a" if lock else ""),
                     (user_id, user_id, activity_id))
        activity = cur.fetchone()
        if not activity:
            raise HTTPException(404, "Actividad no disponible.")
        return activity

    def chat_activity(self, activity_id, user_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return self._chat_activity(cur, activity_id, user_id)

    def chat_has_message(self, activity_id, message_id):
        with self.pool.connection() as conn, conn.cursor() as cur:
            self.execute(cur, "SELECT 1 FROM activities_api.group_messages WHERE activity_id=%s AND id=%s",
                         (activity_id, message_id))
            return cur.fetchone() is not None

    def chat_page(self, activity_id, user_id, after_id, before_id, limit, check, around_id=None):
        """(activity, muted, messages) with one connection: the chat is polled every few seconds.

        `check(activity)` raises before any message is read when the user has no access.
        Messages come in ascending order: the oldest after `after_id`, otherwise the newest before `before_id`.
        """
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            activity = self._chat_activity(cur, activity_id, user_id)
            check(activity)
            self.execute(cur, "SELECT muted FROM activities_api.group_chat_state WHERE activity_id=%s AND user_id=%s",
                         (activity_id, user_id))
            state = cur.fetchone()
            if around_id is not None:
                # HU-64: the page that holds a quoted message, centred on it. 404 if it is not one of this chat's.
                self.execute(cur, "SELECT 1 FROM activities_api.group_messages WHERE activity_id=%s AND id=%s",
                             (activity_id, around_id))
                if cur.fetchone() is None:
                    raise HTTPException(404, "El mensaje no está disponible.")
                self.execute(cur, f"""SELECT * FROM ({_WITH_REPLY} WHERE g.activity_id=%s AND g.id<=%s
                        ORDER BY g.id DESC LIMIT %s) older
                    UNION ALL
                    SELECT * FROM ({_WITH_REPLY} WHERE g.activity_id=%s AND g.id>%s ORDER BY g.id LIMIT %s) newer
                    ORDER BY id""", (activity_id, around_id, (limit + 1) // 2, activity_id, around_id, limit // 2))
                return activity, bool(state and state['muted']), cur.fetchall()
            newest_first = after_id is None
            self.execute(cur, f"""{_WITH_REPLY} WHERE g.activity_id=%s
                AND (%s::bigint IS NULL OR g.id > %s) AND (%s::bigint IS NULL OR g.id < %s)
                ORDER BY g.id {'DESC' if newest_first else 'ASC'} LIMIT %s""",
                (activity_id, after_id, after_id, before_id, before_id, limit))
            rows = cur.fetchall()
            return activity, bool(state and state['muted']), rows[::-1] if newest_first else rows

    def _saved_message(self, cur, message_id):
        self.execute(cur, f"{_WITH_REPLY} WHERE g.id=%s", (message_id,))
        return cur.fetchone()

    def post_message(self, activity_id, author_id, text, client_message_id, check, reply_to=None):
        """Returns (message, created). `check(activity, existing)` raises when the author may not write."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            activity = self._chat_activity(cur, activity_id, author_id, lock=True)
            self.execute(cur, """SELECT * FROM activities_api.group_messages
                WHERE activity_id=%s AND author_id=%s AND client_message_id=%s""",
                (activity_id, author_id, client_message_id))
            existing = cur.fetchone()
            check(activity, existing)
            if existing:
                if existing['text'] != text or existing['reply_to_id'] != reply_to:
                    raise HTTPException(409, "Este intento ya envió otro mensaje. Actualiza el chat.")
                return self._saved_message(cur, existing['id']), False
            if reply_to is not None:
                # HU-64: only a message of THIS chat can be answered. One answer for "does not exist" and "belongs
                # to another chat", so the id of somebody else's message tells nothing.
                self.execute(cur, "SELECT 1 FROM activities_api.group_messages WHERE activity_id=%s AND id=%s",
                             (activity_id, reply_to))
                if cur.fetchone() is None:
                    raise HTTPException(404, "El mensaje citado no está disponible.")
            self.execute(cur, """INSERT INTO activities_api.group_messages
                    (activity_id, author_id, text, client_message_id, reply_to_id, is_reply)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (activity_id, author_id, client_message_id) DO NOTHING
                RETURNING *""", (activity_id, author_id, text, client_message_id, reply_to, reply_to is not None))
            created = cur.fetchone()
            if created:
                return self._saved_message(cur, created['id']), True
            # A concurrent retry inserted first; both callers get the same message.
            self.execute(cur, """SELECT * FROM activities_api.group_messages
                WHERE activity_id=%s AND author_id=%s AND client_message_id=%s""",
                (activity_id, author_id, client_message_id))
            existing = cur.fetchone()
            if existing['text'] != text or existing['reply_to_id'] != reply_to:
                raise HTTPException(409, "Este intento ya envió otro mensaje. Actualiza el chat.")
            return self._saved_message(cur, existing['id']), False

    def mark_read(self, activity_id, user_id, last_read_id):
        """Only moves forward, and never past the chat's last message."""
        with self.pool.connection() as conn:
            self.execute(conn, """INSERT INTO activities_api.group_chat_state (activity_id, user_id, last_read_id)
                SELECT %s, %s, LEAST(%s, COALESCE(max(id), 0)) FROM activities_api.group_messages WHERE activity_id=%s
                ON CONFLICT (activity_id, user_id) DO UPDATE
                SET last_read_id=GREATEST(group_chat_state.last_read_id, EXCLUDED.last_read_id)""",
                (activity_id, user_id, last_read_id, activity_id))

    def set_muted(self, activity_id, user_id, muted):
        with self.pool.connection() as conn:
            self.execute(conn, """INSERT INTO activities_api.group_chat_state (activity_id, user_id, muted)
                VALUES (%s,%s,%s) ON CONFLICT (activity_id, user_id) DO UPDATE SET muted=EXCLUDED.muted""",
                (activity_id, user_id, muted))

    def chats(self, user_id):
        """Non-cancelled activities where the athlete is a member, with their last message and unread count."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, f"""WITH mine AS ({self._CHAT_ACTIVITY} WHERE a.cancelled_at IS NULL AND
                    a.starts_at > CURRENT_TIMESTAMP - INTERVAL '7 days' AND
                    (a.organizer_id = %s OR a.id IN (SELECT p.activity_id FROM activities_api.activity_applications p
                        WHERE p.applicant_id = %s AND p.status = 'accepted')))
                SELECT m.*, COALESCE(s.muted, false) AS muted,
                    (SELECT count(*) FROM activities_api.group_messages g WHERE g.activity_id = m.id
                        AND g.author_id <> %s AND g.id > COALESCE(s.last_read_id, 0)) AS unread,
                    last.id AS last_id, last.author_id AS last_author_id, last.text AS last_text,
                    last.created_at AS last_created_at
                FROM mine m
                LEFT JOIN activities_api.group_chat_state s ON s.activity_id = m.id AND s.user_id = %s
                LEFT JOIN LATERAL (SELECT * FROM activities_api.group_messages g WHERE g.activity_id = m.id
                    ORDER BY g.id DESC LIMIT 1) last ON true
                ORDER BY last.id DESC NULLS LAST, m.starts_at DESC, m.id""",
                (user_id, user_id, user_id, user_id, user_id, user_id))
            return cur.fetchall()

    def purge_old_chats(self, now=None):
        """Purges chat rows for up to 500 expired activities under a cross-replica lock."""
        now = now or datetime.now(timezone.utc)
        with self.pool.connection() as conn:
            lock_key = "activities-api-chat-purge"
            locked = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (lock_key,)).fetchone()[0]
            if not locked:
                return 0
            try:
                with conn.cursor(row_factory=dict_row) as cur:
                    self.execute(cur, """SELECT a.id FROM activities_api.activities a
                        WHERE COALESCE(a.cancelled_at, a.starts_at) <= %s - INTERVAL '7 days'
                            AND (EXISTS (SELECT 1 FROM activities_api.group_messages m WHERE m.activity_id=a.id)
                                 OR EXISTS (SELECT 1 FROM activities_api.group_chat_state s WHERE s.activity_id=a.id))
                        ORDER BY COALESCE(a.cancelled_at, a.starts_at), a.id
                        LIMIT 500 FOR UPDATE OF a SKIP LOCKED""", (now,))
                    activity_ids = [row['id'] for row in cur.fetchall()]
                    if activity_ids:
                        self.execute(cur, "DELETE FROM activities_api.group_messages WHERE activity_id = ANY(%s)",
                                     (activity_ids,))
                        self.execute(cur, "DELETE FROM activities_api.group_chat_state WHERE activity_id = ANY(%s)",
                                     (activity_ids,))
                conn.commit()
                return len(activity_ids)
            finally:
                conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,))
                conn.commit()
