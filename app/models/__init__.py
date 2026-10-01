import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional
from enum import Enum

# Student roll identifiers at Aditya University are PIN-shaped, e.g. 26B21CS058.
# The value doubles as the university mailbox prefix and the Outlook display
# name, so roll_number is the authoritative student identifier in this system.
ROLL_ID_RE = re.compile(r'^[0-9]{2}[A-Z0-9]{6,12}$')


def is_roll_id(value: str) -> bool:
    """True when the value looks like a university roll identifier."""
    if not value:
        return False
    candidate = value.strip().upper()
    return bool(
        ROLL_ID_RE.match(candidate) and any(ch.isalpha() for ch in candidate)
    )


class UserRole(Enum):
    STUDENT = 'STUDENT'
    LECTURER = 'LECTURER'
    HOD = 'HOD'


class PermissionType(Enum):
    LEAVE = 'LEAVE'
    CLASSROOM = 'CLASSROOM'


class RequestStatus(Enum):
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    CANCELLED = 'CANCELLED'
    EXPIRED = 'EXPIRED'


class ApprovalAction(Enum):
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'


@dataclass
class User:
    id: int
    microsoft_id: Optional[str]
    email: str
    name: str
    roll_number: Optional[str]
    phone: Optional[str]
    role: UserRole
    department: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @property
    def is_student(self) -> bool:
        return self.role == UserRole.STUDENT

    @property
    def is_lecturer(self) -> bool:
        return self.role == UserRole.LECTURER

    @property
    def is_hod(self) -> bool:
        return self.role == UserRole.HOD

    @property
    def display_name(self) -> str:
        """Students are identified by roll number; staff keep their real name."""
        if self.is_student and self.roll_number:
            return self.roll_number
        if self.roll_number and is_roll_id(self.name):
            return self.roll_number
        return self.name

    @property
    def identifier(self) -> str:
        """Best available identifier for reports and notifications."""
        return self.roll_number or self.name

    @property
    def roll_number_display(self) -> str:
        return self.roll_number or '—'


@dataclass
class PermissionRequest:
    id: int
    student_id: int
    permission_type: PermissionType
    reason: str
    start_date: date
    end_date: date
    start_time: Optional[time]
    end_time: Optional[time]
    status: RequestStatus
    assigned_faculty_id: Optional[int]
    created_at: datetime
    updated_at: datetime

    @property
    def is_pending(self) -> bool:
        return self.status == RequestStatus.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == RequestStatus.APPROVED

    @property
    def is_rejected(self) -> bool:
        return self.status == RequestStatus.REJECTED


@dataclass
class ProofDocument:
    id: int
    request_id: int
    original_filename: str
    stored_filename: str
    file_path: str
    file_type: str
    file_size: int
    uploaded_at: datetime


@dataclass
class ApprovalHistory:
    id: int
    request_id: int
    faculty_id: int
    action: ApprovalAction
    remarks: Optional[str]
    actioned_at: datetime