"""Configurable routing rules, replacing three hard-coded reviewer slots.

The HOD used to name exactly three people: an event desk, a curricular desk and
a general fallback. Three is not enough for a real department -- sports,
interviews, medical, placements, cultural, sports-day each want somebody who
knows that part -- and the old scheme had no way to add a fourth without a code
change and a redeploy.

So a route is now a row: a name, the words that identify it, and the lecturer
who reviews it. The HOD manages the list; routing reads it.

Matching stays deliberately coarse. Words are substrings and a rule matches when
any of them appears in the student's reason, which is enough to steer a request
towards the right desk and is honest about being a guess. Nothing here is a
judgement of how important the request is -- that is the reviewer's decision,
and a system that guessed it would be the same mistake `classify_route` already
made.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from app.models.firestore import store

ROUTES = 'routing_rules'


@dataclass
class RouteRule:
    """One named desk, and the words that send a request to it."""

    id: int
    name: str
    #: The lecturer who reviews requests on this route. Null means the rule is
    #: inert: it still matches, but routing falls through as if it were absent.
    faculty_id: Optional[int] = None
    keywords: List[str] = field(default_factory=list)
    #: Lower runs first. Ties and overlaps are common -- "sports day" and
    #: "annual sports meet" both match a sports rule -- so the order is the HOD's
    #: to set rather than depending on dict order.
    position: int = 0
    description: str = ''
    created_at: Optional[datetime] = None

    # Filled in by the query helpers.
    faculty_name: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return bool(self.faculty_id) and bool(self.keywords)

    @property
    def keyword_list(self) -> str:
        return ', '.join(self.keywords)


def _normalise_keywords(values) -> List[str]:
    """Lower-cased, de-duplicated, blanks dropped, order preserved.

    Keywords are matched as substrings, so they are stored as the HOD typed them
    apart from case and surrounding space -- trimming here rather than at match
    time means the list on the settings page reads the way it was entered.
    """
    seen, out = set(), []
    for value in values or []:
        word = str(value).strip().lower()
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def _split_keywords(stored) -> List[str]:
    """Read keywords back, accepting a list or a comma-separated string.

    A row written before this existed stores them as a string, and returning
    `[stored]` for that would make the whole sentence one keyword that matches
    nothing.
    """
    if stored is None:
        return []
    if isinstance(stored, list):
        return [str(k).strip().lower() for k in stored if str(k).strip()]
    return _normalise_keywords(str(stored).split(','))


class RouteModel:
    @staticmethod
    def create(name: str, keywords, faculty_id=None, position: int = 0,
               description: str = '') -> RouteRule:
        row = store.insert(ROUTES, {
            'name': (name or '').strip(),
            'keywords': _normalise_keywords(keywords),
            'faculty_id': RouteModel._as_id(faculty_id),
            'position': int(position or 0),
            'description': (description or '').strip(),
        })
        return RouteModel._to_rule(row)

    @staticmethod
    def update(route_id: int, name: str = None, keywords=None,
               faculty_id=None, position: int = None,
               description: str = None) -> bool:
        values = {}
        if name is not None:
            values['name'] = name.strip()
        if keywords is not None:
            values['keywords'] = _normalise_keywords(keywords)
        if faculty_id is not None:
            values['faculty_id'] = RouteModel._as_id(faculty_id)
        if position is not None:
            values['position'] = int(position)
        if description is not None:
            values['description'] = description.strip()
        if not values:
            return False
        return store.update(ROUTES, route_id, values)

    @staticmethod
    def delete(route_id: int) -> bool:
        return store.delete(ROUTES, route_id)

    @staticmethod
    def find_all() -> List[RouteRule]:
        """Every rule, in the order the HOD set."""
        rows = [RouteModel._to_rule(row) for row in store.documents(ROUTES)]
        rows.sort(key=lambda r: (r.position, r.name or ''))
        RouteModel._attach_names(rows)
        return rows

    @staticmethod
    def find_active() -> List[RouteRule]:
        return [r for r in RouteModel.find_all() if r.is_active]

    @staticmethod
    def find_by_id(route_id: int) -> Optional[RouteRule]:
        row = store.get(ROUTES, route_id)
        return RouteModel._to_rule(row) if row else None

    @staticmethod
    def match(reason: str) -> Optional[RouteRule]:
        """The first active rule whose keywords appear in this reason.

        Returns None rather than raising on a bad row: routing runs inside a
        student's submission, and one malformed rule must not cost them their
        request.
        """
        text = (reason or '').lower()
        if not text:
            return None
        for rule in RouteModel.find_active():
            if any(word in text for word in rule.keywords):
                return rule
        return None

    @staticmethod
    def _as_id(value) -> Optional[int]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _attach_names(rules: List[RouteRule]) -> None:
        from app.models.user import UserModel

        ids = {r.faculty_id for r in rules if r.faculty_id}
        for faculty_id in ids:
            user = UserModel.find_by_id(faculty_id)
            if user:
                for rule in rules:
                    if rule.faculty_id == faculty_id:
                        rule.faculty_name = user.name

    @staticmethod
    def _to_rule(row) -> RouteRule:
        try:
            position = int(row.get('position') or 0)
        except (TypeError, ValueError):
            position = 0
        return RouteRule(
            id=int(row['id']),
            name=row.get('name') or '',
            faculty_id=RouteModel._as_id(row.get('faculty_id')),
            keywords=_split_keywords(row.get('keywords')),
            position=position,
            description=row.get('description') or '',
            created_at=row.get('created_at'),
        )