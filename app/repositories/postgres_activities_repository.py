from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from psycopg import sql
from psycopg.rows import dict_row


# Columns an organizer may change; anything else never reaches the UPDATE.
EDITABLE_COLUMNS = ("title", "sport_code", "description", "starts_at", "location", "capacity")
CANCELLED = "La actividad fue cancelada."


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
