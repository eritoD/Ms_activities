from fastapi import HTTPException

from app.services.entitlements_client import enforce, uso


NOT_RATEABLE = "Solo puedes calificar a deportistas con quienes hiciste match o compartiste una actividad ya realizada."
NO_CHAT = "No tienes acceso al chat de esta actividad."
CHAT_CLOSED = "El chat de esta actividad ya se cerró."
UNKNOWN_AUTHOR = "Deportista no disponible"
REPLY_PREVIEW_CHARACTERS = 100  # a quoted message is shown cut short, never whole (HU-64)


def chat_access(member, cancelled, started, closed=False):
    """HU-50: members write until the activity starts, then only read. Cancelled chats close for everyone."""
    if not member or cancelled or closed:
        return "ninguno"
    return "leer" if started else "escribir"


def author_name(card):
    """First name and last-name initial, as on the public card."""
    if card is None:
        return UNKNOWN_AUTHOR
    return f"{card['nombre']} {card['apellido_inicial']}".strip()


class ActivitiesService:
    def __init__(self, repository, users, matching=None, entitlements=None):
        self.repository, self.users, self.matching = repository, users, matching
        # HU-54: plan limits read from ms-pago; None = no limits (fail-open).
        self.entitlements = entitlements

    def present(self, row, organizer):
        # Response schemas expose only the public card, never email, RUT or private location.
        spots = None if row['capacity'] is None else row['capacity'] - row['accepted_count']
        return {**row, "available_spots": spots, "organizer": organizer}

    def create(self, actor, payload):
        organizer = self.users.card(actor.id, actor.token)
        if self.entitlements is not None and not self.repository.activity_exists(actor.id, payload.client_activity_id):
            enforce(self.entitlements, "usuario", actor.id, "actividades_organizadas",
                    lambda since: self.repository.activities_created_since(actor.id, since))
        return self.present(self.repository.create(actor.id, payload), organizer)

    def plan_usage(self, actor):
        """HU-54: uso de las acciones limitadas de este servicio (mismas consultas que el 402)."""
        return uso(self.entitlements, "usuario", actor.id, {
            "postulaciones_actividades": lambda since: self.repository.applications_since(actor.id, since),
            "actividades_organizadas": lambda since: self.repository.activities_created_since(actor.id, since),
        })

    def get(self, actor, activity_id):
        row = self.repository.get(activity_id)
        organizer = self.users.card(row['organizer_id'], actor.token)
        return self.present(row, organizer)

    def update(self, actor, activity_id, payload):
        row = self.repository.update(activity_id, actor.id, payload.model_dump(exclude_unset=True))
        return self.present(row, self.users.card(actor.id, actor.token))

    def cancel(self, actor, activity_id):
        self.repository.cancel(activity_id, actor.id)

    def apply(self, actor, activity_id):
        # get() hides activities whose organizer is no longer available.
        activity = self.get(actor, activity_id)
        if activity['organizer_id'] == actor.id:
            raise HTTPException(409, "No puedes postular a tu propia actividad.")
        if existing is None and self.entitlements is not None:  # repeating an application creates nothing
            enforce(self.entitlements, "usuario", actor.id, "postulaciones_actividades",
                    lambda since: self.repository.applications_since(actor.id, since))
        return self.repository.apply(activity_id, actor.id)

    def my_application(self, actor, activity_id):
        return self.repository.application(activity_id, actor.id)

    def withdraw(self, actor, activity_id):
        self.repository.withdraw(activity_id, actor.id)

    def remove_participant(self, actor, activity_id, user_id):
        self.repository.remove_participant(activity_id, actor.id, user_id)

    def received_applications(self, actor, activity_id):
        # Only the organizer reviews requests; others get 404 so the list's existence is not revealed.
        if self.repository.get(activity_id)['organizer_id'] != actor.id:
            raise HTTPException(404, "Actividad no disponible.")
        rows = self.repository.applications(activity_id)
        applicants = self.users.cards(list({row['applicant_id'] for row in rows}), actor.token)
        return [{**row, "applicant": applicants[row['applicant_id']]} for row in rows
                if row['applicant_id'] in applicants]

    def decide(self, actor, activity_id, application_id, status):
        if status == 'accepted':
            # Only an athlete who is still active can take a spot.
            applicant_id = self.repository.application_by_id(activity_id, application_id)['applicant_id']
            self.users.card(applicant_id, actor.token)
        return self.repository.decide(activity_id, actor.id, application_id, status)

    def upcoming(self, actor, limit, cursor):
        rows, next_cursor = self.repository.upcoming(limit, cursor)
        organizers = self.users.cards(list({row['organizer_id'] for row in rows}), actor.token)
        return {"items": [self.present(row, organizers[row['organizer_id']]) for row in rows
                          if row['organizer_id'] in organizers], "next_cursor": next_cursor}

    # ---------- history ----------
    def history(self, actor, limit):
        rows, total = self.repository.history(actor.id, limit)
        organizers = self.users.cards(list({row['organizer_id'] for row in rows}), actor.token)
        return {"count": total, "items": [{**self.present(row, organizers[row['organizer_id']]), "role": row['role']}
                                          for row in rows if row['organizer_id'] in organizers]}

    def participants(self, actor, activity_id):
        """Who took part, for the organizer and accepted participants only (others get 404)."""
        members = self.repository.participants(activity_id)
        if not members:
            raise HTTPException(404, "Actividad no disponible.")
        if actor.id not in {member['user_id'] for member in members}:
            raise HTTPException(404, "Actividad no disponible.")
        others = [member for member in members if member['user_id'] != actor.id]
        cards = self.users.cards([member['user_id'] for member in others], actor.token)
        return [{**cards[member['user_id']], "role": member['role']} for member in others
                if member['user_id'] in cards]

    # ---------- ratings ----------
    def rate(self, actor, rated_id, payload):
        if rated_id == actor.id:
            raise HTTPException(422, "No puedes calificarte a ti mismo.")
        # Only active athletes can be rated; an unavailable one answers 404.
        self.users.card(rated_id, actor.token)
        # The local check is cheap, so Matching is only asked when there is no shared activity.
        shared = self.repository.shared_past_activity(actor.id, rated_id)
        if not shared and not (self.matching and self.matching.matched_with(rated_id, actor.token)):
            raise HTTPException(403, NOT_RATEABLE)
        row = self.repository.rate(actor.id, rated_id, payload.score, payload.comment or "")
        return {**row, "rated_user_id": row['rated_id']}

    def given(self, actor):
        return [{**row, "rated_user_id": row['rated_id']} for row in self.repository.given_ratings(actor.id)]

    def reputation(self, actor):
        rows = self.repository.received_ratings(actor.id)
        raters = self.users.cards(list({row['rater_id'] for row in rows}), actor.token)
        average = round(sum(row['score'] for row in rows) / len(rows), 2) if rows else None
        # The score of a rater who is no longer active still counts; their card is just not shown.
        return {"average": average, "count": len(rows),
                "items": [{**row, "rater": raters.get(row['rater_id'])} for row in rows]}

    # ---------- group chat (HU-50) ----------
    def _chat_access(self, activity):
        access = chat_access(activity['member'], activity['cancelled'], activity['started'],
                             activity.get('chat_closed', False))
        if access == "ninguno":
            message = CHAT_CLOSED if activity.get('chat_closed') else NO_CHAT
            raise HTTPException(403, message)
        return access

    @staticmethod
    def _reply_preview(row, cards):
        """What a reply shows of the message it quotes (HU-64): author and the first characters; or only
        "not available" if it was removed. Nothing for a message that is not a reply."""
        if row.get('reply_to_id') is not None:
            text = (row.get('reply_text') or "").strip()
            if len(text) > REPLY_PREVIEW_CHARACTERS:
                text = text[:REPLY_PREVIEW_CHARACTERS].rstrip() + "…"
            return {"id": row['reply_to_id'], "author_id": row.get('reply_author_id'), "text": text, "available": True,
                    "author_name": author_name(cards.get(row.get('reply_author_id')))}
        if row.get('is_reply'):  # a reply whose quoted message no longer exists
            return {"id": None, "author_id": None, "author_name": None, "text": None, "available": False}
        return None

    def _present_messages(self, actor, rows):
        # One card lookup per request, not per message (the quoted authors travel in the same lookup).
        # Authors no longer active keep their messages.
        wanted = {row['author_id'] for row in rows} | {row['reply_author_id'] for row in rows if row.get('reply_author_id')}
        cards = self.users.cards(list(wanted), actor.token) if rows else {}
        return [{**row, "author_name": author_name(cards.get(row['author_id'])),
                 "author_photo": (cards.get(row['author_id']) or {}).get('foto_perfil'),
                 "own": row['author_id'] == actor.id, "reply_to": self._reply_preview(row, cards)} for row in rows]

    def chat_messages(self, actor, activity_id, after_id, before_id, limit, around_id=None):
        if sum(cursor is not None for cursor in (after_id, before_id, around_id)) > 1:
            raise HTTPException(422, "Usa un solo cursor de mensajes.")
        extra = {} if around_id is None else {"around_id": around_id}
        activity, muted, rows = self.repository.chat_page(activity_id, actor.id, after_id, before_id, limit,
                                                          self._chat_access, **extra)
        return {"acceso": self._chat_access(activity), "participantes": activity['participants'],
                "silenciado": muted, "mensajes": self._present_messages(actor, rows)}

    def post_message(self, actor, activity_id, payload):
        def check(activity, existing):
            access = self._chat_access(activity)
            # A retry of a message already saved answers with it, even if the chat closed meanwhile.
            if access != "escribir" and not existing:
                raise HTTPException(403, "La actividad ya ocurrió; su chat es solo de lectura.")
        extra = {} if payload.reply_to is None else {"reply_to": payload.reply_to}
        row, _ = self.repository.post_message(activity_id, actor.id, payload.text, payload.client_message_id, check,
                                              **extra)
        return self._present_messages(actor, [row])[0]

    def mark_chat_read(self, actor, activity_id, last_read_id):
        self._chat_access(self.repository.chat_activity(activity_id, actor.id))
        self.repository.mark_read(activity_id, actor.id, last_read_id)

    def mute_chat(self, actor, activity_id, muted):
        self._chat_access(self.repository.chat_activity(activity_id, actor.id))
        self.repository.set_muted(activity_id, actor.id, muted)

    def chats(self, actor):
        rows = self.repository.chats(actor.id)
        last = [{"id": row['last_id'], "author_id": row['last_author_id'], "text": row['last_text'],
                 "created_at": row['last_created_at']} for row in rows if row['last_id'] is not None]
        presented = {message['id']: message for message in self._present_messages(actor, last)}
        return [{"activity_id": row['id'], "title": row['title'], "sport_code": row['sport_code'],
                 "starts_at": row['starts_at'], "acceso": chat_access(True, False, row['started']),
                 "participantes": row['participants'], "ultimo": presented.get(row['last_id']),
                 "unread": row['unread'], "silenciado": row['muted']} for row in rows]
