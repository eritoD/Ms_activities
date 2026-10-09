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
                self.execute(conn, migration.read_text())

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
