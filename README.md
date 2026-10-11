# CSE Permission & Leave Tracking System

A college portal for co-curricular and extracurricular **permission requests**
and for **faculty leave**. A student asks to be absent for an event; the
lecturer who owns that section decides. Everything else in this file exists to
make that decision correct, fast, and auditable.

Live at **<https://permission.vijayapardhu.tech>**

> **For whoever runs this:** see **[OPERATIONS.md](OPERATIONS.md)** — deployment,
> backups, restoring, data imports, troubleshooting. That is the document you
> want at 2am. This one explains what the thing is.

---

## What it does

**Permission requests.** A student asks to miss class on given dates, for a
stated reason, alone or as a group. It lands in the queue of somebody who can
judge it. They approve or refuse, the student is notified, and an approved
request produces a printable letter with a QR code that a college can verify.

**Faculty leave.** Teaching staff file leave; it climbs a fixed ladder
(lecturer → HOD → Dean). Period-level substitution is tracked against the
timetable, so an uncovered period is visible rather than discovered.

**Classwork and objections.** A lecturer schedules assessed work against a
section. If it clashes with a permission that was already granted, that
permission is suspended and sent back for review — the request had been
approved against a world in which there was no exam that day. Any lecturer can
raise an objection; the reviewer answers. This is the module that stops the
department discovering a clash after the fact.

---

## The five roles

| Role | Sees | Can do |
|---|---|---|
| **Student** | Their own requests, classes, proctor | File a request alone or as a group, upload proof, print a letter, withdraw |
| **Lecturer** | Their own classes, proctored students, their own decisions | Decide requests, schedule classwork, answer objections, file leave, upload a roster |
| **Deputy HOD** | Everything a lecturer sees, **plus** the department's whole queue | Grant or refuse **any** request directly, read who currently holds a permission |
| **HOD** | The whole department | Everything above, plus routing, the faculty register, events, the student register, and appointing deputies |
| **Dean** | Faculty leave only | Sign leave, above the HOD |

A **Deputy HOD** is a promotion, not a new person: the same account with more
authority, so their classes and proctor list do not move. They get exactly two
powers — deciding directly, and reading the permission register — and
deliberately **not** the administrative panels, and **not** authority over their
own leave.

Every request for a lecturer's queue is filtered to what they own or have
decided. A lecturer cannot see another lecturer's students.

---

## How a request is routed

1. **The proctor wins.** If the student is on a lecturer's proctor list, it goes
   to that lecturer.
2. **Then an event.** A request naming a college event goes to that event's
   coordinator.
3. **Then a review desk.** Desks are configured rows — a name, some words, a
   reviewer, an order — so a sports desk or a placements desk is a form, not a
   code change.
4. **Otherwise the least-loaded queue.**

Escalated requests wait for the HOD. The HOD, or a Deputy HOD, may also take any
request directly without waiting for its reviewer.

---

## Running it locally

```bash
git clone https://github.com/Vijayapardhu/PermissionSystem.git
cd PermissionSystem
python setup.py          # virtualenv, dependencies, .env, schema. Safe to re-run.
# fill in the real values in .env  (CLIENT_ID, TENANT_ID, CLIENT_SECRET, …)
python wsgi.py           # http://127.0.0.1:5000
```

`setup.py` is safe to re-run — it skips anything already in place.

To point it at the existing student data rather than starting empty, copy a
`local-dev.db` next to the code, or run the college import in `migrations/`.

### The data store

Firestore by default. Set `DATABASE_BACKEND=sql` to use SQL instead — either
SQLite (a local file, nothing to install) or Postgres via `DATABASE_URL`. Both
speak the same interface, so nothing in `app/` changes. The VM this project
runs on uses SQLite at `local-dev.db`.

**That file holds real student records. It is gitignored and must never be
committed.**

---

## Layout

```
app/
  auth/      sign-in, sessions, the account page
  models/    the data layer, one module per subject
  permissions/
    service.py              request rules, routing, who may decide what
    pages.py                the role/page table — every route must appear here
    classwork_service.py    scheduling, clash detection, objections
  faculty/ hod/ dean/ deputy/ student/
    routes.py               the views for each role
  utils/     email, navigation, QR letters, change previews
templates/   Jinja, one folder per role
static/      css, js, vendored Bootstrap and icons
migrations/  import scripts — run these, they are not part of the app
verify.py    the main test suite
```

### Two rules the code enforces on itself

**Every route must be granted to a role.** `app/permissions/pages.py` raises at
start-up if an endpoint is not in its table. A new route cannot be added without
deciding who may reach it — it is not open to any signed-in account by default.

**Edits preview before they write.** Renaming a class, changing a request,
moving classwork: the form shows what each field holds now, and saving renders
the differences first. This is uniform across the app, because a preview is the
last place to notice that moving a date suspended three permissions.

---

## Deploying elsewhere

The live server runs this under gunicorn behind nginx. `render.yaml` is also
checked in for Render, and `Procfile` carries the same command.

```
gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 wsgi:app
```

**The Start Command must be `wsgi:app`, never `app:app`.** The `app/` package
directory wins over any same-named module in Python's import system, so
`gunicorn app:app` resolves to the *package* and fails with:

```
Failed to find attribute 'app' in 'app'
AppImportError: module 'app' has no attribute 'app'
```

That is why the entry point is `wsgi.py` and not `app.py` — see its docstring.

### Health check

`GET /healthz` returns `{"database":"ok","status":"ok"}` and non-200 if the
database is unreachable. Point Render's and any uptime monitor's health check at
it; it is the one endpoint that proves the whole stack works.

### Signing in without Microsoft

`DEV_MODE=true` enables a local sign-in that bypasses Entra ID, for
demonstrations. **It must be off in production.**

If sign-in fails with `AADSTS50011`, the reply URL in the Azure app
registration does not exactly match `REDIRECT_URI` in `.env` — including scheme,
trailing slash and port. Copy the URL from the error and match it character for
character.

### Seeding an empty database

```
migrations/seed_firestore.py     create collections and indexes
migrations/import_college_data.py  the faculty register
migrations/import_cse_students.py  the student roster
```

Full runbook for the live server: **[OPERATIONS.md](OPERATIONS.md)**.

---

## Tests

```bash
python verify.py              # 900+ checks, no database needed
python workflow_test.py       # end-to-end over HTTP, against a running server
python class_features_test.py # classes and rosters
python sidebar_test.py        # every page, for every role
```

`verify.py` is the one to run. The other three need the server up with
credentials set, because they assert against real state.

Three checks in `verify.py` are known to fail and have done for some time: two
around conflicting-submission previews and one about an unknown event
coordinator. They are stale harness assertions, not defects — see the comment
at the top of the file.

---

## Security

- Sign-in is Microsoft Entra ID. `DEV_MODE` enables a local password-less
  sign-in and **must be off anywhere real** — check it first if the site is
  misbehaving.
- Content-Security-Policy is set in nginx, not the app. Bootstrap and its icons
  are served from `/static/vendor`, so the policy needs no third-party host.
- Uploads are validated by content, not by extension, and proof files are
  served from a key-checked route rather than a public directory.
- Every write is POST with a CSRF token. Sign-out is POST for the same reason.
- The supervisor's SSH, fail2ban, unattended upgrades and sysctl hardening are
  described in [OPERATIONS.md](OPERATIONS.md).

---

## Known gaps

- **Sign-in is effectively closed to students.** Of 1,138 student accounts, the
  great majority have no email address, and that number rises as students sign
  in for the first time. The accounts, names and phone numbers are all correct;
  the college mailboxes are not allocated. This is an IT task, not a code
  change. To see the current figure:

  ```bash
  cd /home/ubuntu/app && SQLITE_PATH=local-dev.db DATABASE_BACKEND=sql .venv/bin/python -c "
  from app import create_app; from app.models import UserRole
  from app.models.user import UserModel
  a = create_app()
  with a.app_context():
      s = UserModel._by_role(UserRole.STUDENT.value, 'name')
      print('students:', len(s), 'with an email:', sum(1 for u in s if u.email))"
  ```
- **No automated backup of the live database.** See
  [OPERATIONS.md](OPERATIONS.md#backups) — this is the most serious gap here.
- `MAIL_USERNAME` is empty on the live server, so **no notification email is
  actually sent**. The code path runs and logs a suppression notice. Students
  must open the site to see decisions until SMTP is configured.
- Faculty leave substitutions are computed from the timetable, which is
  maintained by hand.