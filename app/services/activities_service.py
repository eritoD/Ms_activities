class ActivitiesService:
    def __init__(self, repository, users):
        self.repository, self.users = repository, users

    def present(self, row, organizer):
        # Response schemas expose only the public card, never email, RUT or private location.
        return {**row, "organizer": organizer}

    def create(self, actor, payload):
        organizer = self.users.card(actor.id, actor.token)
        return self.present(self.repository.create(actor.id, payload), organizer)

    def get(self, actor, activity_id):
        row = self.repository.get(activity_id)
        organizer = self.users.card(row['organizer_id'], actor.token)
        return self.present(row, organizer)

    def upcoming(self, actor, limit, cursor):
        rows, next_cursor = self.repository.upcoming(limit, cursor)
        organizers = self.users.cards(list({row['organizer_id'] for row in rows}), actor.token)
        return {"items": [self.present(row, organizers[row['organizer_id']]) for row in rows
                          if row['organizer_id'] in organizers], "next_cursor": next_cursor}
