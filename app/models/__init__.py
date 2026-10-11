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
    #: A lecturer the HOD has given delegated authority to.
    #:
    #: Promotion, not a second account: the person keeps the classes they teach
    #: and their own proctor list, and gains the ability to decide a request that
    #: has not reached them. Which is the point -- the HOD is one person, and a
    #: queue that only they can clear stops moving whenever they are in a
    #: meeting, on leave, or off campus.
    #:
    #: Kept separate from HOD rather than folded into it. The HOD configures
    #: routing, owns the event desk and holds the administrative panels; a deputy
    #: answers requests and reads the permission register. Sharing the role would
    #: hand out all three at once, and the first thing anyone would do with a
    #: deputy is give them exactly the two things they were asked for.
    DEPUTY_HOD = 'DEPUTY_HOD'
    #: The final approver for faculty leave, above the HOD.
    #:
    #: Deliberately not part of `is_faculty`. A Dean in this department decides;
    #: they do not hold a class group, teach, or file leave here. The
    #: last of those is the reason: an approver who can also apply needs somebody
    #: above them, and there is nobody above a Dean in this portal -- a Dean's own
    #: application would land in the one queue they are meant to empty. HODs are
    #: teaching staff and so are in both panels; a Dean is not.
    DEAN = 'DEAN'


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


class LeaveType(Enum):
    """Faculty leave categories, as the college's own forms name them."""

    CASUAL = 'CASUAL'
    MEDICAL = 'MEDICAL'
    EARNED = 'EARNED'
    ON_DUTY = 'ON_DUTY'
    MATERNITY = 'MATERNITY'
    EMERGENCY = 'EMERGENCY'
    OTHER = 'OTHER'


#: Shown on the apply form, in the order a reader would expect to pick one.
LEAVE_TYPE_CHOICES = (
    (LeaveType.CASUAL, 'Casual Leave'),
    (LeaveType.MEDICAL, 'Medical Leave'),
    (LeaveType.EARNED, 'Earned Leave'),
    (LeaveType.ON_DUTY, 'On Duty'),
    (LeaveType.MATERNITY, 'Maternity Leave'),
    (LeaveType.EMERGENCY, 'Emergency Leave'),
    (LeaveType.OTHER, 'Other'),
)


class LeaveStatus(Enum):
    """Where a faculty leave application has reached.

    The order is the workflow. `ADJUSTMENT_PENDING` and `ADJUSTMENT_COMPLETE`
    sit before any approval because a leave that leaves a class untaught is not
    something an approver should have to wave through, and moving the check
    earlier is cheaper than unwinding an approved leave later.
    """

    #: Created, affected periods not yet confirmed by the applicant.
    DRAFT = 'DRAFT'
    #: The applicant has confirmed the periods they are missing; substitutes
    #: are being arranged.
    ADJUSTMENT_PENDING = 'ADJUSTMENT_PENDING'
    #: Every affected period has an accepted substitute.
    ADJUSTMENT_COMPLETE = 'ADJUSTMENT_COMPLETE'
    #: With the HOD for a decision.
    HOD_PENDING = 'HOD_PENDING'
    #: HOD has approved; the Dean's stage comes next.
    DEAN_PENDING = 'DEAN_PENDING'
    #: Every approver has signed. Leave balance moves from pending to used.
    FINAL_APPROVED = 'FINAL_APPROVED'
    REJECTED = 'REJECTED'
    CANCELLED = 'CANCELLED'


#: Statuses that no longer hold leave balance and stop constraining the faculty.
#: A rejected or withdrawn application must not keep reserving days, and must
#: not make the applicant look busy to the availability search.
LEAVE_DEAD_STATUSES = frozenset({
    LeaveStatus.REJECTED,
    LeaveStatus.CANCELLED,
})


#: Statuses where the applicant is away, so nobody may be offered their periods.
LEAVE_UNAVAILABLE_STATUSES = frozenset({
    LeaveStatus.HOD_PENDING,
    LeaveStatus.DEAN_PENDING,
    LeaveStatus.FINAL_APPROVED,
})


class AdjustmentStatus(Enum):
    """One affected period's substitution."""

    #: Offered to a substitute, awaiting their answer.
    PENDING = 'PENDING'
    ACCEPTED = 'ACCEPTED'
    REJECTED = 'REJECTED'
    #: The applicant withdrew the offer.
    CANCELLED = 'CANCELLED'
    #: No substitute could be found. Recorded rather than hidden, so the HOD
    #: sees it and can decide whether to approve anyway -- which the plan allows.
    UNCOVERED = 'UNCOVERED'


#: Adjustment states that count as "this class is covered".
RESOLVED_ADJUSTMENTS = frozenset({AdjustmentStatus.ACCEPTED})


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
    # Not a decision: the permission was granted, and then a lecturer scheduled
    # classwork that clashed with it, so the grant was suspended and sent back
    # to the reviewer. It needs its own action because "approved" and "approved,
    # then disputed" are different facts, and the history is what the reviewer
    # reads to decide whether to restore it.
    SUSPENDED = 'SUSPENDED'


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
    # Staff identity from the college register. Nullable because students have
    # neither: a student is identified by roll number, which is also their
    # mailbox. Faculty carry the employee number the timetable and the published
    # rosters are both keyed on, so this is what joins a person to the sections
    # they teach.
    employee_code: Optional[str] = None
    designation: Optional[str] = None

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
    def is_dean(self) -> bool:
        return self.role == UserRole.DEAN

    @property
    def is_deputy_hod(self) -> bool:
        """A lecturer the HOD has given delegated authority to."""
        return self.role == UserRole.DEPUTY_HOD

    @property
    def can_decide_requests(self) -> bool:
        """Somebody who may grant a permission without waiting for its reviewer.

        Wider than `is_approver` on purpose. `is_approver` answers "whose
        decision is this" for the leave workflow, where the ladder is fixed and
        self-approval is the hazard. This answers "may this person grant an
        activity permission right now", which is what delegated authority is
        for: the HOD can no longer be the only person who can clear a queue.

        Deliberately excludes the Dean. A Dean decides faculty leave and does
        not administer the department's request queues at all, so granting them
        this would be a privilege nobody asked for.
        """
        return self.role in (UserRole.HOD, UserRole.DEPUTY_HOD)

    @property
    def is_approver(self) -> bool:
        """Somebody who signs other people's leave, rather than filing their own.

        Both the HOD and the Dean, and used where the question is "whose decision
        is this" rather than "can this person teach" -- routing an HOD's own
        application past the HOD stage, and refusing self-approval at either
        level, both ask this.

        A Deputy HOD is not in it. They decide activity permissions; faculty
        leave still goes up the fixed ladder, and a deputy who could sign their
        own leave past the HOD would be a hole in a workflow that exists to stop
        exactly that.
        """
        return self.role in (UserRole.HOD, UserRole.DEAN)

    @property
    def is_faculty(self) -> bool:
        """Teaching staff: anyone who can hold a class, teach, or apply
        for leave.

        An HOD in this department teaches a subject as well as running it, so the
        two are not exclusive roles and anything that asks "does this person
        teach?" has to accept both. The 5 HODs among the 219 staff hold 4 class
        groups between them, and they are on the same timetable as everyone else.

        `is_lecturer` is deliberately left as the narrower question. It is what
        separates a reviewer's own approve/reject buttons from the HOD's, which
        are separate endpoints on purpose -- an HOD approving their own queue is
        exactly the case that is meant to be visible.
        """
        return self.role in (UserRole.LECTURER, UserRole.HOD,
                             UserRole.DEPUTY_HOD)

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

    @property
    def staff_code_display(self) -> str:
        """The college's own number for a member of staff, or a dash.

        Every member of teaching staff has an employee code and none has a roll
        number, so anything printing a roll for everybody shows a lecturer a
        dash under a heading that says "Roll number" -- which reads as missing
        data rather than as the wrong identifier.
        """
        return self.employee_code or '—'

    @property
    def id_label(self) -> str:
        """What to call this person's identifier on screen: roll or employee code.

        The two populations are identified by different numbers and the
        difference is not cosmetic -- a roll number is the college's own for a
        student, an employee code is the payroll number for staff. Printing one
        under the other's heading is how a lecturer ends up looking like an
        account with nothing filled in.
        """
        return 'Roll number' if self.is_student else 'Employee code'

    @property
    def id_value(self) -> str:
        """The identifier itself, whichever kind this person has."""
        return self.roll_number if self.is_student else self.employee_code


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
    # The HOD-managed event this request names, if any (e.g. a hackathon the
    # department organises). Decided once at submission; the coordinator at
    # the time becomes the reviewer, and this id is what the register shows.
    event_id: Optional[int] = None
    # The session the student selected when the request is for a single day and
    # no explicit times were given. One of: full_day, morning, afternoon.
    # Stored so the letter and the register can show it even if the slot
    # definitions change later. Rows written before periods existed carry None
    # and are shown as plain times when displayed.
    period: Optional[str] = None

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
        if self.action == ApprovalAction.SUSPENDED:
            return 'Suspended — classwork scheduled'
        return self.action.value.title()


@dataclass
class FacultyLeave:
    """One faculty leave application.

    `status` and `adjustment_status` are kept apart on purpose. The workflow is
    leave -> find affected classes -> arrange substitutes -> approve, and the two
    move independently for most of it: the applicant is still chasing substitutes
    while the approval state is untouched. Folding them into one field would mean
    either losing "HOD has not seen this yet" or losing "two classes are still
    uncovered".
    """

    id: int
    faculty_id: int
    leave_type: LeaveType
    from_date: date
    to_date: date
    days: int
    reason: str
    status: LeaveStatus
    # Summary of the substitution state, kept in step with the adjustment rows so
    # a list view does not have to read every row of every leave.
    adjustment_status: str
    submitted_at: Optional[datetime] = None
    hod_by: Optional[int] = None
    hod_at: Optional[datetime] = None
    hod_remarks: Optional[str] = None
    # The Dean's decision, kept in the same shape as the HOD's above rather than
    # folded into one "approved_by". Two signatures on one leave are two
    # separable facts: the HOD's says the teaching was arranged, the Dean's says
    # the application is granted. Collapsing them would make a letter unable to
    # say which of the two it is relying on.
    dean_by: Optional[int] = None
    dean_at: Optional[datetime] = None
    dean_remarks: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Filled in by the query helpers, not stored.
    faculty_name: Optional[str] = None
    employee_code: Optional[str] = None
    #: Counts derived from the adjustment rows, attached by
    #: ``LeaveAdjustmentModel.summarise``.
    affected_count: int = 0
    accepted_count: int = 0
    uncovered_count: int = 0
    pending_count: int = 0

    @property
    def is_away(self) -> bool:
        """True once this leave actually takes the faculty out of teaching."""
        return self.status in LEAVE_UNAVAILABLE_STATUSES

    @property
    def balance_is_reserved(self) -> int:
        """Days this leave holds against the balance, or 0.

        Reserved from submission rather than from approval, because a leave that
        is waiting on a substitute is still days the faculty cannot spend. What
        it must not do is move them to *used* -- an approval can still be
        refused, and a balance that quietly spent days on a rejected leave is
        worse than one that reserves them.
        """
        if self.status in LEAVE_DEAD_STATUSES:
            return 0
        return self.days or 0

    @property
    def adjustments_resolved(self) -> bool:
        """Every affected period has an accepted substitute."""
        return self.affected_count > 0 and self.accepted_count >= self.affected_count

    @property
    def is_final(self) -> bool:
        """Signed off by everybody. The only state a letter may be printed for."""
        return self.status == LeaveStatus.FINAL_APPROVED

    @property
    def awaiting_dean(self) -> bool:
        return self.status == LeaveStatus.DEAN_PENDING

    @property
    def can_go_to_hod(self) -> bool:
        """Whether the application may be put in front of the HOD.

        A leave with no affected classes needs no approval to be actionable, so
        it qualifies on its own. One with affected classes qualifies only once
        they are all covered, or once the applicant has explicitly marked the
        rest UNCOVERED for the HOD to decide -- which is the override the plan
        allows, and is why UNCOVERED does not block here.
        """
        if self.status != LeaveStatus.ADJUSTMENT_COMPLETE:
            return False
        return self.affected_count == 0 or (
            self.accepted_count + self.uncovered_count) >= self.affected_count


@dataclass
class ClassAdjustment:
    """One teaching block of one section that needs somebody to teach it.

    A block, not a single period. A subject that runs for four periods on a
    Saturday is four slots on the timetable and *one* thing to arrange: the
    substitute is asked to take the lot or none of it, and one person covering
    one block is one promise. Stored per period, the same colleague was sent four
    requests for one class, each answerable on its own, which left open the state
    where three periods are covered and the fourth is not -- something nobody can
    act on and nobody should have to.

    `period` remains the first period of the block and is what the list is
    ordered by; `periods` is the whole block.
    """

    id: str
    leave_id: int
    date: date
    day: str
    period: int
    section: str
    class_id: Optional[int]
    subject_code: Optional[str]
    course_code: Optional[str]
    original_faculty_id: int
    assigned_faculty_id: Optional[int] = None
    status: AdjustmentStatus = AdjustmentStatus.PENDING
    requested_at: Optional[datetime] = None
    responded_at: Optional[datetime] = None
    remarks: Optional[str] = None

    # Every period in the block, ascending. Empty on a row written before blocks
    # existed, which is why `period_list` falls back to the single `period`.
    periods: List[int] = field(default_factory=list)

    # Filled in by the query helpers.
    slot_label: str = ''
    assigned_name: Optional[str] = None
    eligible_faculty_ids: List[int] = field(default_factory=list)

    @property
    def period_list(self) -> List[int]:
        """The block's periods, with the single-period case filled in.

        A row written before this change has no `periods` list. A caller that
        took that empty list literally would treat the block as covering nothing,
        which reads as "no cover needed" and under-counts without failing.
        """
        return sorted(set(self.periods)) if self.periods else [self.period]

    @property
    def span_label(self) -> str:
        """'4-7', or '4' for a single period. What the interfaces print."""
        periods = self.period_list
        if len(periods) == 1:
            return str(periods[0])
        return f'{periods[0]}-{periods[-1]}'

    @property
    def period_count(self) -> int:
        return len(self.period_list)

    @property
    def is_resolved(self) -> bool:
        return self.status in RESOLVED_ADJUSTMENTS

    @property
    def needs_action(self) -> bool:
        """True while this period is still uncovered by an accepted substitute."""
        return self.status in (AdjustmentStatus.PENDING,
                               AdjustmentStatus.UNCOVERED)