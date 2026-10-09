from fastapi import HTTPException


class ActivitiesService:
    def __init__(self, repository, users):
        self.repository, self.users = repository, users

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
