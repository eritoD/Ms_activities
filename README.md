# Ms_Activities — actividades deportivas

Microservicio independiente para que deportistas activos y con correo verificado publiquen actividades y consulten las publicadas por otros. Usa FastAPI y PostgreSQL, siguiendo la estructura de Ms_Matching.

## Estructura

- `app/api/v1/activities.py`: endpoints y dependencias de autenticación.
- `app/services/activities_service.py`: creación, consulta y datos públicos del organizador.
- `app/services/users_client.py`: validación del deportista mediante HTTP a Ms_Users.
- `app/repositories/postgres_activities_repository.py`: persistencia, transacciones y paginación.
- `app/schemas/activities.py`: contratos y validaciones.
- `db/migrations/001_activities.sql`: esquema propio `activities_api`, sin modificar tablas de ejemplo anteriores.
- `tests/test_activities.py`: pruebas con PostgreSQL en esquemas aislados.

## API

El gateway publica estas rutas; todas requieren un token de acceso de Ms_Users.

| Método | Ruta | Resultado |
|---|---|---|
| POST | `/api/v1/activities` | Publica una actividad (201). |
| GET | `/api/v1/activities?limit=20&cursor=UUID` | Próximas actividades por fecha, con `items` y `next_cursor`. |
| GET | `/api/v1/activities/{id}` | Detalle, incluido organizador público. |
| PATCH | `/api/v1/activities/{id}` | Solo el organizador: modifica los campos enviados (200). |
| DELETE | `/api/v1/activities/{id}` | Solo el organizador: elimina (cancela) la actividad (204). |
| POST | `/api/v1/activities/{id}/applications` | Postula a la actividad; la solicitud queda `pending` (201, o 200 si ya existía). |
| GET | `/api/v1/activities/{id}/applications/me` | Estado de la postulación propia (404 si no ha postulado). |
| GET | `/api/v1/activities/{id}/applications` | Solo el organizador: solicitudes recibidas (todas, con su estado) y la tarjeta pública de cada postulante. |
| POST | `/api/v1/activities/{id}/applications/{application_id}/accept` | Solo el organizador: acepta y descuenta un cupo (200). |
| POST | `/api/v1/activities/{id}/applications/{application_id}/reject` | Solo el organizador: rechaza (200). |

Ejemplo del cuerpo para publicar (usar una fecha futura y un UUID nuevo por publicación):

```json
{
  "client_activity_id": "20000000-0000-4000-8000-000000000001",
  "title": "Running en el parque",
  "sport_code": "running",
  "starts_at": "2027-01-10T19:00:00-03:00",
  "location": "Parque Bicentenario, entrada principal, Vitacura",
  "description": "Trote recreativo de 5 km. Llevar agua.",
  "capacity": 10
}
```

El organizador se obtiene del JWT; no se acepta un `organizer_id` enviado por el cliente. Users comprueba el estado actual del deportista en cada solicitud. La respuesta del organizador solo incluye ID, nombre, inicial del apellido y foto pública. Una cuenta deshabilitada deja de aparecer como organizadora.

Fecha y hora deben incluir zona horaria y ser futuras. La app interpreta la fecha ingresada en la zona del dispositivo y envía UTC. La lista omite actividades iniciadas; su detalle sigue disponible por ID. Las actividades se ordenan por fecha e ID, con páginas de 1 a 100 registros. El cursor avanza sobre registros almacenados; una página puede tener menos resultados si algún organizador dejó de estar disponible.

`client_activity_id` permite reintentar una publicación sin duplicarla. La clave es única por organizador; reutilizarla con otro contenido devuelve 409. La base serializa reintentos concurrentes.

## Postulaciones

El botón "Postular" de la app llama a `POST /api/v1/activities/{id}/applications` sin cuerpo; el postulante se obtiene del JWT. La solicitud queda en estado `pending` hasta que el organizador la revise. Reglas:

- Una postulación por deportista y actividad (`UNIQUE` en base). Repetir la solicitud devuelve la misma postulación con 200, también ante reintentos concurrentes.
- El organizador no puede postular a su propia actividad (409).
- No se admiten postulaciones a actividades ya iniciadas (422) ni a actividades inexistentes o de organizadores no disponibles (404).
- Solo el organizador puede ver las solicitudes recibidas; para otros deportistas responde 404. Los postulantes deshabilitados no aparecen en la lista.

`db/migrations/002_activity_applications.sql` crea la tabla `activity_applications`.

## Cupos y gestión de postulaciones

`capacity` (1 a 100) es opcional al publicar; si se omite, la actividad no tiene límite de cupos. Actividad y detalle devuelven `capacity` y `available_spots` (`null` si no hay límite). Las actividades creadas antes de esta versión quedan sin límite.

- El organizador acepta o rechaza cada postulación pendiente con `.../accept` o `.../reject`, sin cuerpo. Para cualquier otro deportista responde 404.
- Aceptar descuenta un cupo. La fila de la actividad se bloquea durante la transacción, por lo que aceptaciones simultáneas nunca superan `capacity`. Sin cupos, aceptar responde 409.
- Con la actividad llena, nuevas postulaciones responden 409; las pendientes se pueden seguir rechazando.
- La decisión es definitiva: repetir la misma acción devuelve la postulación sin cambios (no descuenta otro cupo) y la acción contraria responde 409.
- No se puede responder una postulación de una actividad ya iniciada (422), ni aceptar a un deportista que dejó de estar disponible (404).
- `decided_at` registra cuándo se respondió. El deportista ve el resultado en `.../applications/me`.

`db/migrations/003_activity_capacity.sql` agrega `capacity`, `accepted_count` y `decided_at` sin modificar los datos existentes.

## Modificar y eliminar

El organizador modifica con `PATCH /api/v1/activities/{id}` enviando solo lo que cambia: `title`, `sport_code`, `description`, `starts_at`, `location` y `capacity`. Cualquier otro campo, un cuerpo vacío o `null` en un campo obligatorio responden 422. `capacity: null` quita el límite de cupos.

```json
{ "title": "Running largo en el parque", "starts_at": "2027-01-10T20:00:00-03:00", "capacity": 12 }
```

`DELETE /api/v1/activities/{id}` no borra la fila: marca `cancelled_at`, para que quienes postularon sigan viendo la actividad y su estado. Reglas:

- Solo el organizador; para cualquier otro deportista responde 404, igual que las postulaciones.
- No se modifica ni elimina una actividad ya iniciada (422). La nueva fecha también debe ser futura (422).
- Los cupos no pueden quedar por debajo de los postulantes ya aceptados (409).
- Una actividad cancelada sale del listado, no admite modificaciones, postulaciones nuevas ni respuestas a postulaciones (409). El detalle y `.../applications/me` siguen disponibles, con `cancelled_at`.
- Eliminar dos veces responde 204 sin cambios. La fila de la actividad se bloquea durante la operación, así que no se mezcla con aceptaciones simultáneas.
- Detalle y listado incluyen `updated_at` (última modificación) y `cancelled_at`.

`db/migrations/004_activity_edit_cancel.sql` agrega `updated_at` y `cancelled_at` sin modificar los datos existentes.

## Ejecutar

Con Docker, configurar `ACTIVITIES_DATABASE_URL` y el JWT compartido en `../Ms_gateway/.env`, y ejecutar desde esa carpeta:

```sh
docker compose up --build -d
```

El servicio escucha internamente en el puerto 8003. El gateway se conecta por `http://ms_activities:8003`. La base debe existir y el usuario debe poder crear su propio esquema. Las migraciones idempotentes se ejecutan al iniciar, protegidas por un bloqueo transaccional.

Para desarrollo sin Docker, crear un entorno virtual, instalar `requirements.txt` y exportar las variables descritas en `.env.example` antes de ejecutar:

```sh
uvicorn app.main:app --host 127.0.0.1 --port 8003
```

`USERS_SERVICE_URL` debe apuntar a Ms_Users. Los secretos de JWT, issuer y audience deben coincidir con Users y gateway. No se carga `.env` automáticamente.

Comprobación interna: `GET /api/v1/activities/health/ready`. El gateway incluye este servicio en su readiness y no publica su ruta interna de health.

## Pruebas

```sh
pip install -r requirements-dev.txt
# Configurar ACTIVITIES_TEST_DATABASE_URL con la conexión PostgreSQL de pruebas.
python -m pytest tests -q
```

Cada prueba crea y elimina exclusivamente un esquema `activities_test_<uuid>`. Sin esa variable, las pruebas de PostgreSQL se omiten.

## Alcance de esta entrega

Publicación, listado, paginación y detalle. La pantalla principal y Actividades usan datos reales y conservan el estilo de las tarjetas existentes. El lugar se ingresa como texto; no se calculan distancias para actividades. La edición y la cancelación se agregaron después (ver «Modificar y eliminar»); el chat grupal sigue fuera de alcance.

## Validación local — 6 de octubre de 2026

- 16 pruebas de integración PostgreSQL y 9 de autenticación/comunicación con Users correctas.
- 58 pruebas del gateway y 33 del frontend correctas.
- TypeScript, ESLint de los archivos modificados y exportación web completados.
- Flujo real por gateway Docker: cuenta A publica; cuenta B lista y abre el detalle; persistencia verificada con otra conexión PostgreSQL. Los reintentos mantienen el ID y una identidad enviada en el cuerpo se rechaza. Las dos cuentas y la actividad de QA se eliminaron al terminar.
- Para ejecutar localmente se reutilizaron imágenes Docker con las mismas dependencias debido a la espera del registro externo; los Dockerfiles versionables conservan la instalación reproducible desde requirements.
