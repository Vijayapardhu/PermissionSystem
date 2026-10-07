"""Department events with coordinators: main events and their sub-events.

The HOD creates a main event (say "Hackathon"), hangs sub-events under it
("Smart India Hackathon", "Internal Qualifier"), and names a coordinator
-- a lecturer -- for each. When a student files a request whose reason names
a sub-event, the request routes to that sub-event's coordinator rather than
to the general event desk.

Only two levels exist: a main event and its sub-events. A sub-event cannot
have children of its own, because a third level has never been needed and an
unbounded tree would make "who reviews this" unexplainable.

A sub-event without a coordinator inherits its parent's: the coordinator
lookup walks exactly one level up. An event nobody coordinates matches
nothing and routing falls through to the defaults.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from app.models.firestore import store

EVENTS = 'events'


@dataclass
class Event:
    id: int
    name: str
    parent_id: Optional[int]
    coordinator_id: Optional[int]
    created_at: datetime
    coordinator_name: Optional[str] = None
    sub_count: int = 0

    @property
    def is_main(self) -> bool:
        """True when this event has no parent: it is a main event."""
        return not self.parent_id


class EventModel:
    @staticmethod
    def create(name: str, coordinator_id: int = None,
               parent_id: int = None) -> Event:
        row = store.insert(EVENTS, {
            'name': (name or '').strip(),
            'parent_id': parent_id,
            'coordinator_id': coordinator_id,
        })
        return _to_event(row)

    @staticmethod
    def find_by_id(event_id: int) -> Optional[Event]:
        row = store.get(EVENTS, event_id)
        return _to_event(row) if row else None

    @staticmethod
    def list_all() -> List[Event]:
        """Every event, mains first then subs, each alphabetical by name."""
        rows = store.documents(EVENTS)
        events = [_to_event(row) for row in rows]
        events.sort(key=lambda e: ((e.parent_id is not None), e.name or ''))
        _attach_counts(events)
        return events

    @staticmethod
    def list_tree() -> list:
        """Mains each carrying their subs as `subs`, for the management page."""
        events = EventModel.list_all()
        by_id = {e.id: e for e in events}
        mains = []
        for event in events:
            if event.is_main:
                event.subs = []
                mains.append(event)
        for event in events:
            if not event.is_main and event.parent_id in by_id:
                by_id[event.parent_id].subs.append(event)
        _attach_names(mains)
        return mains

    @staticmethod
    def update(event_id: int, name: str,
               coordinator_id: int = None) -> bool:
        """Rename and/or reassign the coordinator. Returns False if missing."""
        return store.update(EVENTS, event_id, {
            'name': (name or '').strip(),
            'coordinator_id': coordinator_id,
        })

    @staticmethod
    def delete(event_id: int) -> int:
        """Delete an event and its sub-events. Returns how many went."""
        removed = 0
        for row in store.documents(EVENTS, parent_id=event_id):
            if store.delete(EVENTS, row['id']):
                removed += 1
        if store.delete(EVENTS, event_id):
            removed += 1
        return removed

    @staticmethod
    def match_reason(text: str, events: List[Event]) -> Optional[Event]:
        """The most specific coordinated event named anywhere in the text.

        Pure function of the text and the event list, so submission and tests
        always agree. The longest name wins: "Smart India Hackathon" beats
        "Hackathon" when the reason names both. An event whose coordinator
        chain is empty matches nothing, because a match with nobody to send
        it to is worse than no match at all.
        """
        lowered = (text or '').lower()
        if not lowered:
            return None
        by_id = {e.id: e for e in events}
        candidates = [
            e for e in events
            if (e.name or '').strip().lower() in lowered
        ]
        candidates.sort(key=lambda e: len(e.name or ''), reverse=True)
        for event in candidates:
            if EventModel.effective_coordinator(event, by_id):
                return event
        return None

    @staticmethod
    def find_match(reason: str) -> Optional[Event]:
        """The coordinated event a reason names, if any."""
        return EventModel.match_reason(reason, EventModel.list_all())

    @staticmethod
    def effective_coordinator(event: Event, by_id: dict = None) -> Optional[int]:
        """The coordinator id in force: the event's own, else its parent's."""
        if event.coordinator_id:
            return event.coordinator_id
        if event.parent_id:
            parent = (by_id or {}).get(event.parent_id)
            if parent is None:
                row = store.get(EVENTS, event.parent_id)
                parent = _to_event(row) if row else None
            if parent is not None and parent.coordinator_id:
                return parent.coordinator_id
        return None


def _attach_counts(events: List[Event]) -> None:
    subs = {}
    for event in events:
        if event.parent_id:
            subs[event.parent_id] = subs.get(event.parent_id, 0) + 1
    for event in events:
        event.sub_count = subs.get(event.id, 0)


def _attach_names(mains: list) -> None:
    """Fill in coordinator names for mains and their subs in one pass."""
    wanted = set()
    for main in mains:
        if main.coordinator_id:
            wanted.add(main.coordinator_id)
        for sub in getattr(main, 'subs', []):
            if sub.coordinator_id:
                wanted.add(sub.coordinator_id)
    names = {}
    for faculty_id in wanted:
        row = store.get('users', faculty_id)
        if row:
            names[faculty_id] = row.get('name')
    for main in mains:
        main.coordinator_name = names.get(main.coordinator_id)
        for sub in getattr(main, 'subs', []):
            sub.coordinator_name = names.get(sub.coordinator_id)


def _to_event(row) -> Event:
    return Event(
        id=int(row['id']),
        name=row.get('name') or '',
        parent_id=row.get('parent_id'),
        coordinator_id=row.get('coordinator_id'),
        created_at=row.get('created_at'),
    )
