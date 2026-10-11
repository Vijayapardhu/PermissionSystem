"""The Deputy HOD's panel: decide requests directly, and read the register.

A small panel on purpose. A deputy is still a lecturer -- they keep their classes,
their proctor list and their own leave -- and what the HOD gave them is two
specific powers: the ability to grant a permission without waiting for the
reviewer it is sitting with, and the ability to see who currently holds one.

Everything else the HOD does -- routing, events, faculty records -- is
deliberately not reachable from here. Delegated authority that quietly grows
into a second HOD is how a department ends up with two people who think they
own the process and neither of whom is accountable for it.
"""

from datetime import date

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request,
    url_for,
)

from app.models import ApprovalAction, RequestStatus
from app.models.permission import (
    ApprovalModel, PermissionModel, attach_members,
)
from app.models.user import UserModel
from app.permissions.pages import permission_required
from app.permissions.service import ValidationError, hod_act_directly
from app.utils.security import current_user

deputy_bp = Blueprint('deputy', __name__, url_prefix='/deputy')


def _app_root() -> str:
    """The public origin, for the links in notification emails."""
    return url_for('deputy.dashboard', _external=True).rsplit('/', 1)[0]


@deputy_bp.route('/dashboard')
@permission_required('deputy.dashboard')
def dashboard():
    """Where the deputy starts: what is waiting, and who is away today."""
    from app.models.classes import ClassModel, KIND_CLASS

    user = current_user()

    # The whole department, because a deputy's job is to clear a queue that is
    # not theirs. `find_pending_for_faculty` would only ever return what routing
    # happened to send them, which is the opposite of delegated authority.
    pending = [r for r in PermissionModel.find_all_for_hod(
        status=RequestStatus.PENDING, limit=200)]
    attach_members(pending)

    pending.sort(key=lambda r: (r.created_at is None, r.created_at))

    today = date.today()
    approved_today = PermissionModel.get_today_approved(today)

    groups = ClassModel.find_for_faculty(user.id, kind=KIND_CLASS)

    stats = {
        'pending': len(pending),
        'approved_today': len(approved_today),
        'own_classes': len(groups),
    }

    return render_template(
        'deputy/dashboard.html',
        user=user,
        pending=pending[:25],
        pending_total=len(pending),
        approved_today=approved_today,
        groups=groups,
        stats=stats,
        today=today,
    )


@deputy_bp.route('/permitted')
@permission_required('deputy.permitted')
def permitted():
    """Every student holding a permission on a given day.

    The question a deputy is asked most often is "who do I actually have today",
    and the answer needs to be one page rather than a filter applied to a
    request list. Sorted by section so a deputy covering a lab session can see
    the whole room in one pass.
    """
    user = current_user()

    view_date = request.args.get('date')
    if view_date:
        try:
            from datetime import datetime
            view_date = datetime.strptime(view_date, '%Y-%m-%d').date()
        except ValueError:
            view_date = date.today()
    else:
        view_date = date.today()

    status_filter = (request.args.get('status') or '').strip().upper()
    try:
        wanted_status = RequestStatus(status_filter) if status_filter else None
    except ValueError:
        wanted_status = None

    records = PermissionModel.get_today_approved(view_date)
    if wanted_status:
        records = [r for r in records if r.status == wanted_status]
    attach_members(records)

    records.sort(key=lambda r: (r.student_roll_number or '',
                                r.start_time or view_date))

    # Rolled up by section, because "the third year CSE-A" is how a deputy
    # thinks about it and "eleven separate rows" is not.
    by_section = {}
    for record in records:
        roll = (record.student_roll_number or '')
        by_section.setdefault(roll[:2] if len(roll) >= 2 else '—', []).append(record)

    return render_template(
        'deputy/permitted.html',
        user=user,
        records=records,
        by_section=by_section,
        view_date=view_date,
        status_filter=wanted_status,
    )


@deputy_bp.route('/requests/<int:request_id>')
@permission_required('deputy.request_detail')
def request_detail(request_id: int):
    """One request, with everything needed to decide it."""
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    attach_members([record])
    student = UserModel.find_by_id(record.student_id)
    history = ApprovalModel.find_by_request(request_id)

    # The classwork panel the lecturer's own review page has. Without it a
    # deputy deciding a request cannot see what the section has scheduled, which
    # is the whole reason the panel exists.
    from app.permissions.classwork_service import classwork_for_request
    try:
        scheduled = classwork_for_request(record)
    except Exception:
        scheduled = []

    return render_template(
        'deputy/request_detail.html',
        user=user,
        request=record,
        student=student,
        history=history,
        classwork=scheduled,
    )


@deputy_bp.route('/requests/<int:request_id>/decide', methods=['POST'])
@permission_required('deputy.decide')
def decide(request_id: int):
    """Grant or refuse directly, without waiting for the assigned reviewer.

    Shares the service with the HOD's own route rather than reimplementing it,
    so the two cannot drift: the same group rules apply, the same self-approval
    refusal applies, and the same history entry is written either way.
    """
    user = current_user()
    action_name = (request.form.get('action') or '').strip().lower()
    if action_name not in ('approve', 'reject'):
        flash('Choose approve or reject.', 'danger')
        return redirect(url_for('deputy.request_detail', request_id=request_id))

    action = (ApprovalAction.APPROVED if action_name == 'approve'
              else ApprovalAction.REJECTED)
    remarks = (request.form.get('remarks') or '').strip()

    if action_name == 'reject' and len(remarks) < 5:
        flash('Add a remark so the student understands the refusal.', 'danger')
        return redirect(url_for('deputy.request_detail', request_id=request_id))

    try:
        hod_act_directly(
            request_id=request_id,
            hod=user,
            action=action,
            remarks=remarks,
            base_url=_app_root(),
        )
    except ValidationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('deputy.request_detail', request_id=request_id))

    verb = 'approved' if action == ApprovalAction.APPROVED else 'refused'
    flash(f'Request #{request_id} {verb} directly.', 'success')
    return redirect(url_for('deputy.permitted'))