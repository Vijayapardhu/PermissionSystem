import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import List, Optional
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
    # Leave management has been retired: a student cannot request one, and
    # nothing in the interface offers, filters, charts or reports it.
    #
    # The member stays because Firestore holds rows written while it existed, and
    # `PermissionType(row['permission_type'])` raises on anything it does not
    # know -- deleting this would turn every historical leave row into a 500 on
    # the dashboard, the register and the letter. It is read-only: nothing may
    # create one, which is enforced by OFFERABLE_PERMISSION_TYPES below rather
    # than by the enum, so a legacy row still renders as "Leave (retired)".
    LEAVE = 'LEAVE'
    # Co-curricular and extracurricular activity permission: the only type the
    # system issues now.
    CLASSROOM = 'CLASSROOM'


# What may actually be requested. Anything not listed here cannot be created,
# whatever the enum contains -- the form, the service and the analytics all read
# this one list, so a retired type cannot reappear in one place and not another.
OFFERABLE_PERMISSION_TYPES = (PermissionType.CLASSROOM,)


class RequestStatus(Enum):
    PENDING = 'PENDING'
    # The lecturer has recommended approval but the HOD has not yet decided.
    # A request in this state is not a grant of permission: the letter says so,
    # and nothing counts it as approved.
    AWAITING_HOD = 'AWAITING_HOD'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    CANCELLED = 'CANCELLED'
    EXPIRED = 'EXPIRED'


# Statuses that no longer constrain the student: they grant nothing and are not
# worth being checked against when looking for an overlapping request.
DEAD_STATUSES = frozenset({
    RequestStatus.REJECTED,
    RequestStatus.CANCELLED,
    RequestStatus.EXPIRED,
})


class ApprovalAction(Enum):
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    # A lecturer who checked the request but will not grant it themselves: they
    # verify it and hand it to the HOD for the final decision. It is its own
    # action rather than a re-labelled APPROVED because the student and the HOD
    # have to be able to tell "a lecturer granted this" from "a lecturer passed
    # it up" months later -- those are two different facts about one request.
    FORWARDED = 'FORWARDED'


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
    # Everyone the permission covers, requester first. Empty on records written
    # before group permissions existed, which is why `members` below falls back
    # to the single student rather than treating those rows as having nobody.
    member_ids: List[int] = field(default_factory=list)
    # The subset the HOD actually approved. None means "all of them", which is
    # both the single-student case and the group nobody edited before approving.
    approved_member_ids: Optional[List[int]] = None
    # How the request was routed to its reviewer: EVENT, CURRICULAR or GENERAL.
    # Decided once at submission from the reason text and stored so the register
    # can show it even if the routing keywords change later. Rows written before
    # routing existed carry None and are classified on the fly when displayed.
    route_category: Optional[str] = None

    @property
    def members(self) -> List[int]:
        """Everyone covered, requester first. Never empty."""
        if self.member_ids:
            seen, ordered = set(), []
            for mid in self.member_ids:
                if mid and mid not in seen:
                    seen.add(mid)
                    ordered.append(mid)
            if ordered:
                return ordered
        return [self.student_id] if self.student_id else []

    @property
    def approved_members(self) -> List[int]:
        """Who the approval actually covers.

        An approval with an empty subset would mean the HOD struck everyone off,
        which the route refuses to submit. Falling back to the full membership
        keeps an older record, or one nobody edited, meaning what it says.
        """
        if self.approved_member_ids:
            allowed = set(self.approved_member_ids)
            kept = [m for m in self.members if m in allowed]
            if kept:
                return kept
        return self.members

    @property
    def group_size(self) -> int:
        return len(self.members)

    @property
    def is_group(self) -> bool:
        return self.group_size > 1

    @property
    def dropped_members(self) -> List[int]:
        """Members the HOD struck off before approving."""
        if not self.approved_member_ids:
            return []
        kept = set(self.approved_members)
        return [m for m in self.members if m not in kept]

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
    # The role of the person who decided, filled in by find_by_request. The
    # letter needs it to say who actually granted a permission: a lecturer's
    # final approval and the HOD's are different signatures on the document.
    faculty_role: Optional[str] = None

    @property
    def action_label(self) -> str:
        """The action in words a student can read.

        Kept in one place so the student page, the faculty page, the HOD page
        and the letter cannot disagree about what FORWARDED is called.
        """
        if self.action == ApprovalAction.FORWARDED:
            return 'Verified & forwarded'
        return self.action.value.title()