"""Sidebar navigation definition.

Kept in one place so the sidebar, the mobile drawer and any future breadcrumbs
stay in sync. Each item names the endpoint, an icon, a label, and how it decides
whether it is the current page.
"""


def _item(endpoint, icon, label, section=None):
    return {'endpoint': endpoint, 'icon': icon, 'label': label, 'section': section}


LECTURER_NAV = [
    _item('faculty.dashboard', 'bi-speedometer2', 'Overview', 'Review'),
    _item('faculty.requests_browser', 'bi-inboxes', 'All Requests', 'Review'),
    # The disputes queue. Always listed rather than hidden when empty, because a
    # lecturer who has just scheduled a clash needs to be able to check whether
    # anyone has answered it without first going to look for the link.
    _item('faculty.objections', 'bi-exclamation-diamond', 'Objections', 'Review'),
    _item('faculty.search', 'bi-search', 'Find Student', 'Review'),

    _item('faculty.classes', 'bi-people', 'My Classes', 'Teaching'),
    _item('faculty.classwork', 'bi-journal-check', 'Classwork', 'Teaching'),

    _item('faculty.proctor_students', 'bi-person-check', 'Proctor Students', 'Proctoring'),

    # Leave and the substitutions it creates. "My Leave" is where an applicant
    # works, and the inbox is where they answer, so both sit under one heading --
    # they are two ends of the same request.
    _item('faculty.my_leave', 'bi-calendar2-check', 'My Leave', 'Leave'),
    _item('faculty.my_adjustments', 'bi-arrow-left-right', 'Adjustments', 'Leave'),

    _item('faculty.reports', 'bi-graph-up', 'My Reports', 'Insights'),
]

HOD_NAV = [
    _item('hod.dashboard', 'bi-speedometer2', 'Overview', 'Overview'),
    _item('hod.requests', 'bi-list-check', 'All Requests', 'Overview'),
    _item('hod.students', 'bi-mortarboard', 'Students', 'Overview'),
    _item('hod.search', 'bi-search', 'Search', 'Overview'),

    _item('hod.faculty_workload', 'bi-person-workspace', 'Workload', 'Faculty'),
    _item('hod.import_faculty', 'bi-file-earmark-arrow-up', 'Add Faculty', 'Faculty'),
    _item('hod.routing', 'bi-signpost-2', 'Routing', 'Faculty'),

    # Its own section, placed between two others rather than inside one. The
    # section list is derived by grouping consecutive items that share a label, so
    # slotting a new section in the middle of an existing one splits it in two and
    # the sidebar shows "Academic" twice.
    _item('hod.leave_queue', 'bi-calendar2-check', 'Leave Approvals', 'Faculty Leave'),

    _item('hod.classes', 'bi-people', 'Classes', 'Academic'),
    _item('hod.events', 'bi-trophy', 'Events', 'Academic'),
    _item('hod.print_report', 'bi-printer', 'Daily Report', 'Academic'),

    _item('hod.reports', 'bi-bar-chart-line', 'Analytics', 'Insights'),
]

STUDENT_NAV = [
    _item('student.dashboard', 'bi-speedometer2', 'Dashboard'),
    _item('student.new_request', 'bi-plus-circle', 'New Request'),
    _item('student.requests', 'bi-journal-text', 'My Requests'),
    _item('student.classes_for_student', 'bi-people', 'My Classes'),
    _item('auth.profile', 'bi-person-circle', 'Account'),
]

# A Dean signs faculty leave and does little else here, so the list is short
# because the job is -- not the routing, the events, the class register and the
# analytics, which belong to the HOD of the department concerned. Giving a Dean a
# second copy of each would make two people responsible for one register.
DEAN_NAV = [
    _item('dean.dashboard', 'bi-speedometer2', 'Overview', 'Overview'),
    _item('dean.leave_queue', 'bi-calendar2-check', 'Leave Approvals',
          'Faculty Leave'),
]

# An HOD teaches here as well as administering, so their sidebar carries both
# panels: the administrative one first, because that is the job they were sent
# to do, and the teaching one after it under "My ..." headings.
#
# The prefix is not decoration. The HOD list already ends in a section called
# "Insights" and the teaching list has one too, and sections are derived by
# grouping consecutive items that share a label -- so without the rename the
# sidebar would print "Insights" twice, once for the department's analytics and
# once for the HOD's own classes, with nothing to tell them apart.
HOD_TEACHING_NAV = [
    _item(entry['endpoint'], entry['icon'], entry['label'],
          f'My {entry["section"]}' if entry['section'] else 'My Teaching')
    for entry in LECTURER_NAV
]

# A Deputy HOD keeps the whole teaching sidebar -- they still teach and still
# proctor -- and gets one extra section on top for the two powers the HOD gave
# them. Built by addition rather than a separate list so a change to the teaching
# navigation reaches a deputy without anyone remembering to copy it.
DEPUTY_NAV = HOD_TEACHING_NAV + [
    _item('deputy.dashboard', 'bi-shield-check', 'Deputy Panel', 'Deputy'),
    _item('deputy.permitted', 'bi-person-check', 'Permitted Today', 'Deputy'),
    _item('deputy.leave_queue', 'bi-calendar2-check', 'Leave Approvals',
          'Deputy'),
]

ROLE_NAV = {
    'LECTURER': LECTURER_NAV,
    'HOD': HOD_NAV + HOD_TEACHING_NAV,
    'DEPUTY_HOD': DEPUTY_NAV,
    'DEAN': DEAN_NAV,
    'STUDENT': STUDENT_NAV,
}

# Every role that has a nav also gets the sidebar, so the shell is identical for
# all of them: a permanent column from lg up, an off-canvas drawer below it.
# Derived from ROLE_NAV rather than listed by hand -- a hand-written tuple drifts
# the moment a role is added, and that role silently loses its navigation.
SIDEBAR_ROLES = tuple(ROLE_NAV)


def nav_for(role_value):
    return ROLE_NAV.get(role_value, [])


def group_sections(items):
    """Split a flat item list into (section_label, [items]) pairs.

    Items may carry no section at all, so the first bucket is created up front
    rather than lazily on the first section change.
    """
    if not items:
        return []

    sections = [(items[0]['section'], [])]
    for entry in items:
        if entry['section'] != sections[-1][0]:
            sections.append((entry['section'], []))
        sections[-1][1].append(entry)

    return [(label, group) for label, group in sections if group]