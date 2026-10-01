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
    _item('faculty.search', 'bi-search', 'Find Student', 'Review'),

    _item('faculty.classes', 'bi-people', 'My Classes', 'Teaching'),
    _item('faculty.attendance_overview', 'bi-check2-square', 'Attendance', 'Teaching'),

    _item('faculty.reports', 'bi-graph-up', 'My Reports', 'Insights'),
]

HOD_NAV = [
    _item('hod.dashboard', 'bi-speedometer2', 'Overview', 'Overview'),
    _item('hod.requests', 'bi-list-check', 'All Requests', 'Overview'),
    _item('hod.students', 'bi-mortarboard', 'Students', 'Overview'),
    _item('hod.faculty_workload', 'bi-person-workspace', 'Faculty Workload', 'Overview'),

    _item('hod.classes', 'bi-people', 'Classes', 'Academic'),
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

ROLE_NAV = {
    'LECTURER': LECTURER_NAV,
    'HOD': HOD_NAV,
    'STUDENT': STUDENT_NAV,
}

# Sidebar is used by staff; students keep the compact top navigation.
SIDEBAR_ROLES = ('LECTURER', 'HOD')


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