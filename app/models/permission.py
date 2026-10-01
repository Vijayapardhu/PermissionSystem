from app.models import PermissionRequest, PermissionType, RequestStatus, ProofDocument, ApprovalHistory, ApprovalAction
from app.models.database import db
from datetime import date, datetime, time, timedelta
from typing import Optional, List, Tuple


def _to_time(value):
    """Normalise a TIME column to datetime.time.

    psycopg already hands back a datetime.time, so the first branch is the one
    that runs in production. The timedelta and string branches are kept so the
    shape stays enforced for any other driver or for a raw CSV import.
    """
    if value is None:
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return time(hours % 24, minutes % 60, seconds)
    if isinstance(value, str):
        for pattern in ('%H:%M:%S.%f', '%H:%M:%S', '%H:%M'):
            try:
                return datetime.strptime(value, pattern).time()
            except ValueError:
                continue
        return None
    return None


class PermissionModel:
    @staticmethod
    def create(student_id: int, permission_type: PermissionType, reason: str,
               start_date: date, end_date: date, start_time: time = None, 
               end_time: time = None, assigned_faculty_id: int = None) -> PermissionRequest:
        with db.get_cursor() as cursor:
            cursor.execute(
                """INSERT INTO permission_requests
                   (student_id, permission_type, reason, start_date, end_date, start_time, end_time,
                     assigned_faculty_id, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (student_id, permission_type.value, reason, start_date, end_date,
                 start_time, end_time, assigned_faculty_id, RequestStatus.PENDING.value,
                 datetime.now(), datetime.now())
            )
            # Map the RETURNING row rather than re-reading with find_by_id(). A
            # second read would run in a different transaction and could not see
            # this INSERT, which is only committed when get_cursor() exits.
            return PermissionModel._row_to_request(cursor.fetchone())

    @staticmethod
    def find_by_id(request_id: int) -> Optional[PermissionRequest]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM permission_requests WHERE id = %s",
                (request_id,)
            )
            row = cursor.fetchone()
            return PermissionModel._row_to_request(row) if row else None

    @staticmethod
    def find_by_student(student_id: int, status: RequestStatus = None, 
                        limit: int = 50, offset: int = 0) -> List[PermissionRequest]:
        with db.get_cursor() as cursor:
            query = "SELECT * FROM permission_requests WHERE student_id = %s"
            params = [student_id]
            if status:
                query += " AND status = %s"
                params.append(status.value)
            query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [PermissionModel._row_to_request(row) for row in rows]

    @staticmethod
    def find_pending_for_faculty(faculty_id: int) -> List[PermissionRequest]:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT * FROM permission_requests 
                   WHERE assigned_faculty_id = %s AND status = 'PENDING'
                   ORDER BY created_at ASC""",
                (faculty_id,)
            )
            rows = cursor.fetchall()
            return [PermissionModel._row_to_request(row) for row in rows]

    @staticmethod
    def find_all_for_hod(status: RequestStatus = None, permission_type: PermissionType = None,
                         start_date: date = None, end_date: date = None,
                         limit: int = 100, offset: int = 0) -> List[PermissionRequest]:
        with db.get_cursor() as cursor:
            query = """SELECT pr.*, u.name AS student_name, u.roll_number, u.phone
                       FROM permission_requests pr
                       JOIN users u ON pr.student_id = u.id
                       WHERE 1=1"""
            params = []
            if status:
                query += " AND pr.status = %s"
                params.append(status.value)
            if permission_type:
                query += " AND pr.permission_type = %s"
                params.append(permission_type.value)
            if start_date:
                query += " AND pr.start_date >= %s"
                params.append(start_date)
            if end_date:
                query += " AND pr.end_date <= %s"
                params.append(end_date)
            query += " ORDER BY pr.created_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [PermissionModel._row_to_request_with_student(row) for row in rows]

    @staticmethod
    def get_today_approved() -> List[PermissionRequest]:
        with db.get_cursor() as cursor:
            today = date.today()
            cursor.execute(
                """SELECT pr.*, u.name AS student_name, u.roll_number, u.phone
                   FROM permission_requests pr
                   JOIN users u ON pr.student_id = u.id
                   WHERE pr.status = 'APPROVED' 
                   AND pr.start_date <= %s 
                   AND pr.end_date >= %s
                   ORDER BY pr.permission_type, u.roll_number""",
                (today, today)
            )
            rows = cursor.fetchall()
            return [PermissionModel._row_to_request_with_student(row) for row in rows]

    @staticmethod
    def update_status(request_id: int, status: RequestStatus, faculty_id: int = None) -> bool:
        with db.get_cursor() as cursor:
            query = "UPDATE permission_requests SET status = %s, updated_at = %s"
            params = [status.value, datetime.now()]
            if faculty_id:
                query += ", assigned_faculty_id = %s"
                params.append(faculty_id)
            query += " WHERE id = %s"
            params.append(request_id)
            cursor.execute(query, params)
            return cursor.rowcount > 0

    @staticmethod
    def get_stats_for_hod() -> dict:
        with db.get_cursor() as cursor:
            stats = {}
            
            cursor.execute("SELECT COUNT(*) as total FROM permission_requests")
            stats['total'] = cursor.fetchone()['total']
            
            cursor.execute("SELECT COUNT(*) as approved FROM permission_requests WHERE status = 'APPROVED'")
            stats['approved'] = cursor.fetchone()['approved']
            
            cursor.execute("SELECT COUNT(*) as rejected FROM permission_requests WHERE status = 'REJECTED'")
            stats['rejected'] = cursor.fetchone()['rejected']
            
            cursor.execute("SELECT COUNT(*) as pending FROM permission_requests WHERE status = 'PENDING'")
            stats['pending'] = cursor.fetchone()['pending']
            
            cursor.execute("SELECT COUNT(*) as leave_count FROM permission_requests WHERE permission_type = 'LEAVE'")
            stats['leave_count'] = cursor.fetchone()['leave_count']
            
            cursor.execute("SELECT COUNT(*) as classroom_count FROM permission_requests WHERE permission_type = 'CLASSROOM'")
            stats['classroom_count'] = cursor.fetchone()['classroom_count']
            
            # ILIKE, not LIKE. The MySQL build ran under a case-insensitive
            # collation, so "Medical appointment" matched '%medical%'. Postgres
            # LIKE is case-sensitive and would silently empty this chart.
            cursor.execute(
                """SELECT
                    COUNT(*) FILTER (WHERE reason ILIKE ANY (ARRAY['%medical%', '%health%', '%doctor%'])) AS medical,
                    COUNT(*) FILTER (WHERE reason ILIKE ANY (ARRAY['%personal%', '%family%'])) AS personal,
                    COUNT(*) FILTER (WHERE reason ILIKE ANY (ARRAY['%event%', '%workshop%', '%seminar%', '%conference%'])) AS event,
                    COUNT(*) FILTER (WHERE reason NOT ILIKE ANY (ARRAY['%medical%', '%health%', '%doctor%',
                                                                   '%personal%', '%family%',
                                                                   '%event%', '%workshop%', '%seminar%', '%conference%'])) AS other
                   FROM permission_requests"""
            )
            reasons = cursor.fetchone()
            stats['reasons'] = reasons
            
            return stats

    @staticmethod
    def _row_to_request(row) -> PermissionRequest:
        return PermissionRequest(
            id=row['id'],
            student_id=row['student_id'],
            permission_type=PermissionType(row['permission_type']),
            reason=row['reason'],
            start_date=row['start_date'],
            end_date=row['end_date'],
            start_time=_to_time(row['start_time']),
            end_time=_to_time(row['end_time']),
            status=RequestStatus(row['status']),
            assigned_faculty_id=row['assigned_faculty_id'],
            created_at=row['created_at'],
            updated_at=row['updated_at']
        )

    @staticmethod
    def _row_to_request_with_student(row) -> PermissionRequest:
        req = PermissionModel._row_to_request(row)
        req.student_name = row.get('student_name')
        req.student_identifier = row.get('roll_number') or row.get('student_name')
        req.student_roll_number = row.get('roll_number')
        req.student_phone = row.get('phone')
        return req


class ProofModel:
    @staticmethod
    def create(request_id: int, original_filename: str, stored_filename: str,
               file_path: str, file_type: str, file_size: int) -> ProofDocument:
        with db.get_cursor() as cursor:
            cursor.execute(
                """INSERT INTO proof_documents 
                   (request_id, original_filename, stored_filename, file_path, file_type, file_size, uploaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (request_id, original_filename, stored_filename, file_path, file_type, file_size, datetime.now())
            )
            return ProofModel._row_to_proof(cursor.fetchone())

    @staticmethod
    def find_by_id(proof_id: int) -> Optional[ProofDocument]:
        with db.get_cursor() as cursor:
            cursor.execute("SELECT * FROM proof_documents WHERE id = %s", (proof_id,))
            row = cursor.fetchone()
            return ProofModel._row_to_proof(row) if row else None

    @staticmethod
    def find_by_request(request_id: int) -> List[ProofDocument]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM proof_documents WHERE request_id = %s ORDER BY uploaded_at",
                (request_id,)
            )
            rows = cursor.fetchall()
            return [ProofModel._row_to_proof(row) for row in rows]

    @staticmethod
    def _row_to_proof(row) -> ProofDocument:
        return ProofDocument(
            id=row['id'],
            request_id=row['request_id'],
            original_filename=row['original_filename'],
            stored_filename=row['stored_filename'],
            file_path=row['file_path'],
            file_type=row['file_type'],
            file_size=row['file_size'],
            uploaded_at=row['uploaded_at']
        )


class ApprovalModel:
    @staticmethod
    def create(request_id: int, faculty_id: int, action: ApprovalAction, remarks: str = None) -> ApprovalHistory:
        with db.get_cursor() as cursor:
            cursor.execute(
                """INSERT INTO approval_history (request_id, faculty_id, action, remarks, actioned_at)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING *""",
                (request_id, faculty_id, action.value, remarks, datetime.now())
            )
            return ApprovalModel._row_to_history(cursor.fetchone())

    @staticmethod
    def find_by_id(history_id: int) -> Optional[ApprovalHistory]:
        with db.get_cursor() as cursor:
            cursor.execute("SELECT * FROM approval_history WHERE id = %s", (history_id,))
            row = cursor.fetchone()
            return ApprovalModel._row_to_history(row) if row else None

    @staticmethod
    def find_by_request(request_id: int) -> List[ApprovalHistory]:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT ah.*, u.name as faculty_name 
                   FROM approval_history ah
                   JOIN users u ON ah.faculty_id = u.id
                   WHERE ah.request_id = %s ORDER BY ah.actioned_at""",
                (request_id,)
            )
            rows = cursor.fetchall()
            return [ApprovalModel._row_to_history_with_faculty(row) for row in rows]

    @staticmethod
    def _row_to_history(row) -> ApprovalHistory:
        return ApprovalHistory(
            id=row['id'],
            request_id=row['request_id'],
            faculty_id=row['faculty_id'],
            action=ApprovalAction(row['action']),
            remarks=row['remarks'],
            actioned_at=row['actioned_at']
        )

    @staticmethod
    def _row_to_history_with_faculty(row) -> ApprovalHistory:
        hist = ApprovalModel._row_to_history(row)
        hist.faculty_name = row.get('faculty_name')
        return hist