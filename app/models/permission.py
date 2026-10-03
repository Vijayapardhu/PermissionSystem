from datetime import date, datetime, time, timedelta
from typing import Optional, List
from app.models import (
    ApprovalAction,
    ApprovalHistory,
    DEAD_STATUSES,
    PermissionRequest,
    PermissionType,
    ProofDocument,
    RequestStatus,
)
from app.models.firestore import store

REQUESTS = 'permission_requests'
PROOFS = 'proof_documents'
HISTORY = 'approval_history'

# Keyword buckets behind the HOD's reason chart. The SQL version counted these
# with ILIKE, which is a case-insensitive substring match; the same rule is
# applied here in Python so the chart classifies exactly what it used to.
#
# The medical and personal buckets went with leave management. What is left
# describes an activity request, and `other` absorbs the historical rows that
# were filed against the retired type rather than forcing them into a bucket
# they do not belong to.
REASON_KEYWORDS = {
    'event': ('event', 'workshop', 'seminar', 'conference', 'fest', 'club'),
    'sports': ('sports', 'match', 'tournament', 'practice'),
    'interview': ('interview', 'placement', 'internship'),
}


def _matches(reason: str, keyword: str) -> bool:
    return keyword in (reason or '').lower()


def normalise_members(requester_id: int, member_ids=None) -> List[int]:
    """The membership of a request: requester first, no duplicates, capped.

    The requester is always member one and cannot be removed by submitting an
    empty list, because a request with nobody on it has no meaning. The cap is
    enforced here rather than only in the form so that a hand-rolled POST cannot
    put twenty students on one letter.
    """
    ordered, seen = [], set()
    for candidate in [requester_id] + list(member_ids or []):
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered[:MAX_GROUP_MEMBERS]


def categorise_reason(reason: str) -> str:
    """The bucket a reason falls into: medical, personal, event or other."""
    text = (reason or '').lower()
    for bucket, keywords in REASON_KEYWORDS.items():
        if any(_matches(reason, keyword) for keyword in keywords):
            return bucket
    return 'other'


def _to_time(value):
    """Normalise a stored start/end time to datetime.time.

    Times are written as ISO strings and read straight back, so the branches that
    matter are the identity and the ISO parse. The timedelta branch is kept for a
    raw CSV import, which is the only other thing that has ever produced one.
    """
    if value is None or isinstance(value, time):
        return value
    if isinstance(value, str):
        for pattern in ('%H:%M:%S.%f', '%H:%M:%S', '%H:%M'):
            try:
                return datetime.strptime(value, pattern).time()
            except ValueError:
                continue
        return None
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return time(hours % 24, minutes % 60, seconds)
    return None


MAX_GROUP_MEMBERS = 4


class PermissionModel:
    @staticmethod
    def create(student_id: int, permission_type: PermissionType, reason: str,
               start_date: date, end_date: date, start_time: time = None,
               end_time: time = None,
               assigned_faculty_id: int = None,
               member_ids: List[int] = None) -> PermissionRequest:
        """Create one request covering one to four students.

        `student_id` stays the requester and the only field the older code reads.
        The full membership goes in `member_ids`, so a record written before
        group permissions existed still reads correctly through `members`.
        """
        row = store.insert(REQUESTS, {
            'student_id': student_id,
            'member_ids': normalise_members(student_id, member_ids),
            'permission_type': permission_type.value,
            'reason': reason,
            'start_date': start_date,
            'end_date': end_date,
            'start_time': start_time,
            'end_time': end_time,
            'status': RequestStatus.PENDING.value,
            'assigned_faculty_id': assigned_faculty_id,
        })
        return PermissionModel._to_request(row)

    @staticmethod
    def find_by_id(request_id: int) -> Optional[PermissionRequest]:
        row = store.get(REQUESTS, request_id)
        return PermissionModel._to_request(row) if row else None

    @staticmethod
    def find_by_student(student_id: int, status: RequestStatus = None,
                        limit: int = 50, offset: int = 0) -> List[PermissionRequest]:
        """Every request a student is on, whether they filed it or were added.

        Two queries, because membership lives in two shapes: `student_id` for the
        requester, and inside the `member_ids` array for everyone else. Asking
        only one of them is what made a co-member unable to see the permission
        they were covered by. Results are merged and de-duplicated by id, so a
        requester matched by both queries appears once.
        """
        rows = list(store.documents(REQUESTS, student_id=student_id))
        seen = {row.get('id') for row in rows}
        for row in store.documents(REQUESTS,
                                   array_contains={'member_ids': student_id}):
            if row.get('id') not in seen:
                seen.add(row.get('id'))
                rows.append(row)

        requests = [PermissionModel._to_request(row) for row in rows]
        if status:
            requests = [r for r in requests if r.status == status]
        requests.sort(key=_newest_first)
        return requests[offset:offset + limit]

    @staticmethod
    def find_overlapping_for_students(student_ids, start_date: date,
                                      end_date: date,
                                      exclude_id: int = None) -> List[dict]:
        """Live requests overlapping the window, for any of these students.

        Returns one entry per clash as `{'student_id', 'student', 'request'}` so
        the form can say *whose* permission is in the way. A duplicate on a
        co-member is as much a duplicate as one on the requester, and naming the
        student is the only way the message is actionable.

        Read through each student's own rows rather than filtering a range:
        Firestore would want a composite index for a status filter plus an
        ordering, and that fails at request time rather than merely running slowly.
        """
        if not start_date or not end_date:
            return []

        clashes, seen = [], set()
        for student_id in dict.fromkeys(student_ids or []):
            for row in store.documents(REQUESTS, student_id=student_id):
                existing = PermissionModel._to_request(row)
                if exclude_id and existing.id == exclude_id:
                    continue
                if existing.status in DEAD_STATUSES:
                    continue
                if not existing.start_date or not existing.end_date:
                    continue
                if existing.start_date > end_date or existing.end_date < start_date:
                    continue
                if existing.id in seen:
                    continue
                seen.add(existing.id)
                clashes.append({'student_id': student_id,
                                'student': None,
                                'request': existing})

        clashes.sort(key=lambda c: _newest_first(c['request']))
        return clashes

    @staticmethod
    def find_pending_for_faculty(faculty_id: int) -> List[PermissionRequest]:
        """The reviewer's queue, oldest first: the request waiting longest leads.
        """
        rows = store.documents(REQUESTS, assigned_faculty_id=faculty_id,
                               status=RequestStatus.PENDING.value)
        requests = [PermissionModel._to_request(row) for row in rows]
        requests.sort(key=_oldest_first)
        return requests

    @staticmethod
    def find_all_for_hod(status: RequestStatus = None,
                         permission_type: PermissionType = None,
                         start_date: date = None, end_date: date = None,
                         limit: int = 100, offset: int = 0) -> List[PermissionRequest]:
        requests = [PermissionModel._to_request(row)
                    for row in store.documents(REQUESTS)]
        if status:
            requests = [r for r in requests if r.status == status]
        if permission_type:
            requests = [r for r in requests if r.permission_type == permission_type]
        if start_date:
            requests = [r for r in requests if r.start_date >= start_date]
        if end_date:
            requests = [r for r in requests if r.end_date <= end_date]
        requests.sort(key=_newest_first)

        page = requests[offset:offset + limit]
        attach_students(page)
        return page

    @staticmethod
    def get_today_approved(today: date = None) -> List[PermissionRequest]:
        """Approved permissions covering a given day, classroom requests first."""
        today = today or date.today()
        rows = store.documents(REQUESTS, status=RequestStatus.APPROVED.value)
        covering = []
        for row in rows:
            start, end = row.get('start_date'), row.get('end_date')
            if start and end and start <= today <= end:
                covering.append(PermissionModel._to_request(row))
        attach_students(covering)
        covering.sort(key=lambda r: (r.permission_type.value,
                                     r.student_roll_number or ''))
        return covering

    @staticmethod
    def find_awaiting_hod(limit: int = 100) -> List[PermissionRequest]:
        """The HOD's approval queue, oldest first.

        One filtered read rather than a scan of the collection: unlike the
        reason chart and the totals, this list is the HOD's actual to-do and it
        is the one query worth keeping cheap.
        """
        rows = store.documents(REQUESTS, status=RequestStatus.AWAITING_HOD.value)
        requests = [PermissionModel._to_request(row) for row in rows]
        requests.sort(key=_oldest_first)
        attach_students(requests[:limit])
        return requests[:limit]

    @staticmethod
    def set_approved_members(request_id: int, member_ids: List[int]) -> bool:
        """Record which members the HOD's approval actually covers.

        Written as an explicit list rather than left implicit, because "the HOD
        struck two students off" is a fact the letter and the register have to be
        able to show months later.
        """
        return store.update(REQUESTS, request_id,
                            {'approved_member_ids': list(member_ids)})

    @staticmethod
    def update_status(request_id: int, status: RequestStatus,
                      faculty_id: int = None) -> bool:
        values = {'status': status.value}
        if faculty_id:
            values['assigned_faculty_id'] = faculty_id
        return store.update(REQUESTS, request_id, values)

    @staticmethod
    def get_stats_for_hod() -> dict:
        """Department totals and the reason mix.

        One read of the collection and every count in Python. This used to be
        seven queries with a FILTER clause each, which Firestore has no
        equivalent of; counting in the application is the whole reason there is
        only one round trip.
        """
        rows = store.documents(REQUESTS)

        stats = {
            'total': len(rows),
            'approved': 0,
            'rejected': 0,
            'pending': 0,
            'awaiting_hod': 0,
            'classroom_count': 0,
        }
        reasons = {'event': 0, 'sports': 0, 'interview': 0, 'other': 0}

        for row in rows:
            status = row.get('status')
            if status == RequestStatus.APPROVED.value:
                stats['approved'] += 1
            elif status == RequestStatus.REJECTED.value:
                stats['rejected'] += 1
            elif status == RequestStatus.PENDING.value:
                stats['pending'] += 1
            elif status == RequestStatus.AWAITING_HOD.value:
                # Counted separately from pending: this is the HOD's own queue,
                # not the department's backlog, and merging the two would hide
                # exactly the work the HOD is accountable for.
                stats['awaiting_hod'] += 1

            # Retired types are still counted in the totals but get no activity
            # bucket, so a historical leave row cannot inflate the activity mix.
            if row.get('permission_type') == PermissionType.CLASSROOM.value:
                stats['classroom_count'] += 1

            reasons[categorise_reason(row.get('reason'))] += 1

        stats['reasons'] = reasons
        return stats

    @staticmethod
    def _to_request(row) -> PermissionRequest:
        return PermissionRequest(
            id=int(row['id']),
            student_id=row.get('student_id'),
            permission_type=PermissionType(
                row.get('permission_type') or PermissionType.LEAVE.value),
            reason=row.get('reason') or '',
            start_date=row.get('start_date'),
            end_date=row.get('end_date'),
            start_time=_to_time(row.get('start_time')),
            end_time=_to_time(row.get('end_time')),
            status=RequestStatus(
                row.get('status') or RequestStatus.PENDING.value),
            assigned_faculty_id=row.get('assigned_faculty_id'),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at') or row.get('created_at'),
            member_ids=[int(m) for m in (row.get('member_ids') or [])
                        if isinstance(m, (int, float)) or str(m).isdigit()],
            approved_member_ids=(
                [int(m) for m in row['approved_member_ids']
                 if isinstance(m, (int, float)) or str(m).isdigit()]
                if isinstance(row.get('approved_member_ids'), list) else None),
        )


def attach_members(requests: List[PermissionRequest]) -> None:
    """Fill in name and roll number for every student on every request.

    One lookup of the students involved, keyed by id, rather than a read per
    member per request: a register page of a hundred group permissions would
    otherwise be four hundred round trips. The details are attached to the
    request objects the caller already holds, so nothing above this line knows
    the difference.
    """
    member_ids = set()
    for request in requests:
        member_ids.update(request.members)
    if not member_ids:
        return

    directory = {}
    for member_id in member_ids:
        row = store.get('users', member_id)
        if row:
            directory[member_id] = {
                'id': member_id,
                'name': row.get('name'),
                'roll_number': row.get('roll_number'),
                'department': row.get('department'),
            }

    for request in requests:
        details = [directory[m] for m in request.members if m in directory]
        request.member_details = details
        # The requester stays the single-student view every existing template
        # already reads, so a group is an addition rather than a rewrite.
        owner = directory.get(request.student_id)
        if owner:
            request.student_name = owner['name']
            request.student_roll_number = owner['roll_number']
            request.student_identifier = (
                owner['roll_number'] or owner['name'])
            request.student_phone = None
        else:
            request.member_details = details
        approved = set(request.approved_members)
        request.approved_member_details = [d for d in details if d['id'] in approved]
        request.dropped_member_details = [d for d in details if d['id'] not in approved]


def attach_students(requests: List[PermissionRequest]) -> None:
    """Fill in the student fields the old JOIN used to select.

    Kept as a thin alias of `attach_members`: a group request still has a single
    requester, so every caller that wanted "who filed this" still gets it.
    """
    attach_members(requests)


def _newest_first(request) -> tuple:
    created = request.created_at
    return (created is None, _sort_key(created), -int(request.id or 0))


def _oldest_first(request) -> tuple:
    created = request.created_at
    return (created is None, _sort_key(created))


def _sort_key(value) -> str:
    """Datetimes and strings sort alike when they are both rendered to text."""
    return value.isoformat() if hasattr(value, 'isoformat') else str(value or '')


class ProofModel:
    @staticmethod
    def create(request_id: int, original_filename: str, stored_filename: str,
               file_path: str, file_type: str, file_size: int) -> ProofDocument:
        row = store.insert(PROOFS, {
            'request_id': request_id,
            'original_filename': original_filename,
            'stored_filename': stored_filename,
            'file_path': file_path,
            'file_type': file_type,
            'file_size': file_size,
        }, timestamps=('uploaded_at',))
        return ProofModel._to_proof(row)

    @staticmethod
    def find_by_id(proof_id: int) -> Optional[ProofDocument]:
        row = store.get(PROOFS, proof_id)
        return ProofModel._to_proof(row) if row else None

    @staticmethod
    def find_by_request(request_id: int) -> List[ProofDocument]:
        rows = store.documents(PROOFS, request_id=request_id)
        proofs = [ProofModel._to_proof(row) for row in rows]
        proofs.sort(key=lambda p: _sort_key(p.uploaded_at))
        return proofs

    @staticmethod
    def _to_proof(row) -> ProofDocument:
        return ProofDocument(
            id=int(row['id']),
            request_id=row.get('request_id'),
            original_filename=row.get('original_filename') or '',
            stored_filename=row.get('stored_filename') or '',
            file_path=row.get('file_path') or '',
            file_type=row.get('file_type') or '',
            file_size=row.get('file_size') or 0,
            uploaded_at=row.get('uploaded_at'),
        )


class ApprovalModel:
    @staticmethod
    def create(request_id: int, faculty_id: int, action: ApprovalAction,
               remarks: str = None) -> ApprovalHistory:
        row = store.insert(HISTORY, {
            'request_id': request_id,
            'faculty_id': faculty_id,
            'action': action.value,
            'remarks': remarks,
        }, timestamps=('actioned_at',))
        return ApprovalModel._to_history(row)

    @staticmethod
    def find_by_id(history_id: int) -> Optional[ApprovalHistory]:
        row = store.get(HISTORY, history_id)
        return ApprovalModel._to_history(row) if row else None

    @staticmethod
    def find_by_request(request_id: int) -> List[ApprovalHistory]:
        rows = store.documents(HISTORY, request_id=request_id)
        history = [ApprovalModel._to_history(row) for row in rows]
        history.sort(key=lambda h: _sort_key(h.actioned_at))

        faculty_ids = {h.faculty_id for h in history if h.faculty_id}
        names = {}
        for faculty_id in faculty_ids:
            row = store.get('users', faculty_id)
            if row:
                names[faculty_id] = row.get('name')
        for entry in history:
            entry.faculty_name = names.get(entry.faculty_id)
        return history

    @staticmethod
    def _to_history(row) -> ApprovalHistory:
        return ApprovalHistory(
            id=int(row['id']),
            request_id=row.get('request_id'),
            faculty_id=row.get('faculty_id'),
            action=ApprovalAction(row.get('action') or ApprovalAction.APPROVED.value),
            remarks=row.get('remarks'),
            actioned_at=row.get('actioned_at'),
        )