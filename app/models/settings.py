"""Department settings: who reviews which kind of request.

A single document (`settings/routing`) holds the three dedicated reviewers the
HOD names:

- `event_faculty_id` -- hackathons, contests and other events go to one person.
- `curricular_faculty_id` -- curricular requests go to one person.
- `general_faculty_id` -- the general proctor, the fallback when a student is
  on nobody's proctor list.

Any of them may be unset, in which case routing falls through to the next rule
(see `resolve_faculty` in the service layer). A missing document reads as "all
unset", so a department that never configured routing behaves exactly as
before: every request goes to the student's proctor, then to the
least-loaded lecturer.
"""

from typing import Optional

from app.models.firestore import store

COLLECTION = 'settings'
ROUTING_DOC = 'routing'


def _as_id(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class SettingsModel:
    @staticmethod
    def get_routing() -> dict:
        """The three configured reviewer ids, None where the HOD set nobody."""
        row = store.get(COLLECTION, ROUTING_DOC) or {}
        return {
            'event_faculty_id': _as_id(row.get('event_faculty_id')),
            'curricular_faculty_id': _as_id(row.get('curricular_faculty_id')),
            'general_faculty_id': _as_id(row.get('general_faculty_id')),
        }

    @staticmethod
    def set_routing(event_faculty_id=None, curricular_faculty_id=None,
                    general_faculty_id=None) -> dict:
        """Replace the routing document. Each id is an int or None to clear."""
        values = {
            'event_faculty_id': _as_id(event_faculty_id),
            'curricular_faculty_id': _as_id(curricular_faculty_id),
            'general_faculty_id': _as_id(general_faculty_id),
        }
        if store.get(COLLECTION, ROUTING_DOC) is None:
            store.insert(COLLECTION, values, doc_id=ROUTING_DOC,
                         timestamps=('created_at',))
        else:
            store.update(COLLECTION, ROUTING_DOC, values)
        return SettingsModel.get_routing()
