"""The Dean's panel -- the last signature on a faculty leave.

This is the stage that closes the workflow. Without it an approved application
sat at ``DEAN_PENDING`` forever: the HOD's `hod_act_on_leave` moves a leave there,
and nothing anywhere moved it again. A leave that can never finish is worse than
one that is refused, because the applicant has already been told it was approved.

The panel is deliberately narrower than the HOD's. A Dean decides leave; they do
not administer a department here. There is no routing, no faculty import, no
event coordination and no class register, because each of those belongs to the
HOD of the department concerned and giving a Dean a copy of them would make two
people responsible for one register.

It is also narrower than a lecturer's own panel: a Dean does not hold classes or
teach in this portal, and `is_faculty` excludes the role so they cannot
file a leave that nobody above them could decide.
"""

from datetime import date

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)

from app.models import LeaveStatus
from app.models.leave import (
    AdjustmentModel, LeaveModel, LeaveSummary, TimetableModel,
)
from app.models.user import UserModel
from app.permissions.leave_service import (
    LeaveError, dean_act_on_leave, letter_context,
)
from app.permissions.pages import permission_required
from app.utils.security import current_user

dean_bp = Blueprint('dean', __name__, url_prefix='/dean')


@dean_bp.route('/dashboard')
@permission_required('dean.dashboard')
def dashboard():
    """What is waiting, and what has come through.

    Three numbers and a short list, because the Dean's involvement is one decision
    per application and a dashboard longer than that would be reporting on work
    this role does not do.
    """
    user = current_user()

    pending = LeaveModel.find_by_status(LeaveStatus.DEAN_PENDING)
    for record in pending:
        LeaveSummary.attach(record, AdjustmentModel.find_by_leave(record.id))
    _attach_names(pending)

    decided = _recently_decided(user)

    # Both lists. `_recently_decided` attaches the coverage counts but not the
    # names -- it does not know the caller wants them -- so the dashboard has to
    # ask, or the "recently signed" table prints an em dash in its Faculty column.
    _attach_names(pending + decided)

    counts = {
        'awaiting': len(pending),
        'uncovered': sum(1 for l in pending if l.uncovered_count),
        'approved': sum(1 for l in decided
                        if l.status == LeaveStatus.FINAL_APPROVED),
        'rejected': sum(1 for l in decided
                        if l.status == LeaveStatus.REJECTED),
    }

    return render_template(
        'dean/dashboard.html',
        user=user,
        pending=pending[:8],
        decided=decided[:8],
        counts=counts,
    )


@dean_bp.route('/leave')
@permission_required('dean.leave_queue')
def leave_queue():
    """Faculty leave awaiting the Dean's decision, oldest first.

    The same shape as the HOD's queue: undecided above, decided below. An
    approval list that mixes the two makes the actual to-do harder to find, which
    is the only reason this page exists.
    """
    pending = LeaveModel.find_by_status(LeaveStatus.DEAN_PENDING)
    decided = _recently_decided(current_user())

    for record in pending:
        LeaveSummary.attach(record, AdjustmentModel.find_by_leave(record.id))
    _attach_names(pending + decided)

    pending.sort(key=lambda l: (l.submitted_at or l.created_at
                                or date.min, l.id))
    counts = {
        'awaiting': len(pending),
        'uncovered': sum(1 for l in pending if l.uncovered_count),
        'approved': sum(1 for l in decided
                        if l.status == LeaveStatus.FINAL_APPROVED),
        'rejected': sum(1 for l in decided
                        if l.status == LeaveStatus.REJECTED),
    }

    return render_template(
        'dean/leave.html',
        user=current_user(),
        pending=pending,
        decided=decided,
        counts=counts,
    )


@dean_bp.route('/leave/<int:leave_id>')
@permission_required('dean.leave_detail')
def leave_detail(leave_id: int):
    """One application, with the arrangement behind it and the HOD's signature.

    The Dean's decision is about the application as a whole rather than the
    teaching arrangements, but the arrangements are what the HOD's approval
    actually asserted, so they are shown rather than summarised away.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        abort(404)

    adjustments = AdjustmentModel.find_by_leave(leave_id)
    LeaveSummary.attach(leave, adjustments)
    _attach_names([leave])
    _attach_substitutes(adjustments)

    return render_template(
        'dean/leave_detail.html',
        user=current_user(),
        leave=leave,
        applicant=UserModel.find_by_id(leave.faculty_id),
        hod=UserModel.find_by_id(leave.hod_by) if leave.hod_by else None,
        adjustments=adjustments,
        coverage=TimetableModel.allocation_coverage(),
    )


@dean_bp.route('/leave/<int:leave_id>/action', methods=['POST'])
@permission_required('dean.leave_action')
def leave_action(leave_id: int):
    """Approve or refuse, which is the point the whole workflow exists to reach.

    A remark is required either way. The service enforces it as well as the form,
    because a POST written by hand skips the form.
    """
    user = current_user()
    approve = (request.form.get('decision') or '').lower() == 'approve'
    remarks = (request.form.get('remarks') or '').strip()

    try:
        leave = dean_act_on_leave(leave_id=leave_id, dean=user, approve=approve,
                                  remarks=remarks)
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('dean.leave_detail', leave_id=leave_id))

    if approve:
        flash(
            f'Leave application #{leave_id} approved. The approval letter is '
            f'now available to print.', 'success')
    else:
        flash(f'Leave application #{leave_id} rejected.', 'success')
    return redirect(url_for('dean.leave_queue'))


# ---------------------------------------------------------------- helpers

@dean_bp.route('/leave/<int:leave_id>/letter')
@permission_required('dean.leave_letter')
def leave_letter(leave_id: int):
    """The approval letter, as the Dean prints it.

    Same document the applicant receives -- built by `letter_context`, so there
    is one letter rather than three that might drift apart.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        abort(404)

    try:
        context = letter_context(leave)
    except LeaveError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('dean.leave_detail', leave_id=leave_id))

    return render_template('faculty/leave_letter.html', user=current_user(),
                           **context)


def _recently_decided(user, limit: int = 40):
    """Leaves this Dean has signed off, most recent first.

    Read as "signed by me" rather than "in either terminal state", so a Dean sees
    the decisions they are answerable for and not the department's whole history.
    """
    signed = LeaveModel.find_by_status(LeaveStatus.FINAL_APPROVED, limit=200) \
        + LeaveModel.find_by_status(LeaveStatus.REJECTED, limit=200)
    mine = [l for l in signed if l.dean_by == user.id]

    for record in mine:
        LeaveSummary.attach(record, AdjustmentModel.find_by_leave(record.id))
    mine.sort(key=lambda l: (l.dean_at or date.min, l.id), reverse=True)
    return mine[:limit]


def _attach_names(leaves) -> None:
    """Name the applicant on each leave, with one read per person for the list."""
    ids = {l.faculty_id for l in leaves if l.faculty_id}
    names = {}
    for person_id in ids:
        person = UserModel.find_by_id(person_id)
        if person:
            names[person_id] = person.name
    for leave in leaves:
        leave.faculty_name = names.get(leave.faculty_id)


def _attach_substitutes(adjustments) -> None:
    """Name the colleague covering each period."""
    ids = {a.assigned_faculty_id for a in adjustments
           if a.assigned_faculty_id}
    names = {}
    for person_id in ids:
        person = UserModel.find_by_id(person_id)
        if person:
            names[person_id] = person.name
    for adjustment in adjustments:
        adjustment.assigned_name = names.get(adjustment.assigned_faculty_id)