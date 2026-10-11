"""Working out what an edit would actually change, before it changes it.

Every editable record in this app follows the same three steps: a form shows the
stored values, a save is previewed as a list of field changes, and only then is
it applied. The comparison itself is here rather than in each route so that
"says nothing changed" and "says exactly what changed" cannot drift apart
between records.

Values are compared and displayed as strings. The caller formats dates, enums
and names into what a person would recognise, which keeps this module free of
any knowledge of what a leave or a class actually is.
"""

from typing import Iterable, List, Mapping


class FieldChange:
    """One field whose stored value differs from the proposed one."""

    def __init__(self, key: str, label: str, before: str, after: str):
        #: The document field. Kept alongside the label because callers need to
        #: react to *which* field moved -- "the dates changed" means dropping the
        #: periods that fell outside them -- and matching on a display label to
        #: do that would break the moment somebody renames one.
        self.key = key
        self.label = label
        self.before = before
        self.after = after

    @property
    def is_clearing(self) -> bool:
        """The field is being emptied rather than given a new value."""
        return not self.after and bool(self.before)

    def as_dict(self) -> dict:
        return {'label': self.label, 'before': self.before,
                'after': self.after}

    def __repr__(self) -> str:
        return (f'FieldChange({self.key!r}: '
                f'{self.before!r} -> {self.after!r})')


def _text(value) -> str:
    """One representation of "nothing", so clearing a field that was already
    empty is not reported as a change."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value).strip()


def diff_fields(current: Mapping[str, object],
                proposed: Mapping[str, object],
                labels: Mapping[str, str] = None) -> List[FieldChange]:
    """The fields in `proposed` whose value differs from `current`.

    Only fields present in `proposed` are considered, so a form that submits only
    the fields it owns cannot report the other 40 columns of a document as
    changed. `labels` maps a key to the name to show; unknown keys fall back to
    the key turned into words.
    """
    labels = labels or {}
    changes = []
    for key, raw in proposed.items():
        before = _text(current.get(key))
        after = _text(raw)
        if before == after:
            continue
        label = labels.get(key, key.replace('_', ' ').capitalize())
        changes.append(FieldChange(key, label, before, after))
    return changes


def summarise(changes: Iterable[FieldChange]) -> str:
    """One line naming how many fields move, for a flash or a page heading."""
    changes = list(changes)
    if not changes:
        return 'No changes to save.'
    count = len(changes)
    noun = 'field' if count == 1 else 'fields'
    return f'{count} {noun} will change.'


def record_changes(history, record_id: int, actor_id: int,
                   changes: Iterable[FieldChange], now) -> None:
    """Append an amendment to a record's history.

    Kept separate from the diff because recording an amendment is a separate
    decision from showing one: the preview shows what *would* change, this
    records what *did*.
    """
    changes = [c for c in changes]
    if not changes:
        return
    history.append({
        'record_id': record_id,
        'actor_id': actor_id,
        'at': now,
        'changes': [c.as_dict() for c in changes],
    })
