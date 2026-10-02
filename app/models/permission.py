from datetime import date, datetime, time, timedelta
from typing import Optional, List

from app.models import (
    ApprovalAction,
    ApprovalHistory,
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
REASON_KEYWORDS = {
    'medical': ('medical', 'health', 'doctor'),
    'personal': ('personal', 'family'),
    'event': ('event', 'workshop', 'seminar', 'conference'),
}


def _matches(reason: str, keyword: str) -> bool:
    return keyword in (reason or '').lower()


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


class PermissionModel:
    @staticmethod
    def create(student_id: int, permission_type: PermissionType, reason: str,
               start_date: date, end_date: date, start_time: time = None,
               end_time: time = None,
               assigned_faculty_id: int = None) -> PermissionRequest:
        row = store.insert(REQUESTS, {
            'student_id': student_id,
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
        rows = store.documents(REQUESTS, student_id=student_id)
        requests = [PermissionModel._to_request(row) for row in rows]
        if status:
            requests = [r for r in requests if r.status == status]
        requests.sort(key=_newest_first)
        return requests[offset:offset + limit]

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
            'leave_count': 0,
            'classroom_count': 0,
        }
        reasons = {'medical': 0, 'personal': 0, 'event': 0, 'other': 0}

        for row in rows:
            status = row.get('status')
            if status == RequestStatus.APPROVED.value:
                stats['approved'] += 1
            elif status == RequestStatus.REJECTED.value:
                stats['rejected'] += 1
            elif status == RequestStatus.PENDING.value:
                stats['pending'] += 1

            kind = row.get('permission_type')
            if kind == PermissionType.LEAVE.value:
                stats['leave_count'] += 1
            elif kind == PermissionType.CLASSROOM.value:
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
        )


def attach_students(requests: List[PermissionRequest]) -> None:
    """Fill in the student fields the old JOIN used to select.

    One lookup of the students involved, keyed by id, rather than a read per
    request. The fields are attached to the request objects the caller already
    holds, so nothing above this line knows the difference.
    """
    student_ids = {r.student_id for r in requests if r.student_id}
    if not student_ids:
        return

    students = {}
    for student_id in student_ids:
        row = store.get('users', student_id)
        if row:
            students[student_id] = row

    for request in requests:
        student = students.get(request.student_id)
        if not student:
            continue
        request.student_name = student.get('name')
        request.student_roll_number = student.get('roll_number')
        request.student_identifier = (
            student.get('roll_number') or student.get('name'))
        request.student_phone = student.get('phone')


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