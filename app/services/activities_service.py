from fastapi import HTTPException


NOT_RATEABLE = "Solo puedes calificar a deportistas con quienes hiciste match o compartiste una actividad ya realizada."


class ActivitiesService:
    def __init__(self, repository, users, matching=None):
        self.repository, self.users, self.matching = repository, users, matching

    def present(self, row, organizer):
        # Response schemas expose only the public card, never email, RUT or private location.
        spots = None if row['capacity'] is None else row['capacity'] - row['accepted_count']
        return {**row, "available_spots": spots, "organizer": organizer}

    def create(self, actor, payload):
        organizer = self.users.card(actor.id, actor.token)
        return self.present(self.repository.create(actor.id, payload), organizer)

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
        return self.repository.apply(activity_id, actor.id)

    def my_application(self, actor, activity_id):
        return self.repository.application(activity_id, actor.id)

    def withdraw(self, actor, activity_id):
        self.repository.withdraw(activity_id, actor.id)

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
