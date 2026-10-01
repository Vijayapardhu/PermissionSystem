# CSE Permission & Leave Tracking System

Flask + MySQL permission tracker for the Department of Computer Science & Engineering,
built to the SRS: Microsoft Entra ID sign-in, Leave and Classroom permission requests,
proof upload, lecturer verification, and an HOD analytics dashboard with printable reports.

## Layout

Staff pages use a two-column shell: a persistent sidebar and the content column.
Students keep the compact top navigation.

The sidebar is a **real flex sibling**, not a fixed column paired with a
`margin-left` on the content. That distinction matters:

- With `position: fixed` + `margin-left`, the two must be kept exactly the same
  width forever. Below the `lg` breakpoint the sidebar becomes an off-canvas
  drawer while the margin would still apply, leaving a permanent empty strip
  down the left of every mobile and tablet page.
- As a flex item (`flex: 0 0 var(--au-sidebar-w)` on the sidebar, `flex: 1 1 auto`
  with `min-width: 0` on the column) the two meet exactly at any width, with no
  offset to drift.

Bootstrap's `offcanvas-lg` forces `width: auto !important` at `lg` and uses
`--bs-offcanvas-width` below it, so both are overridden explicitly rather than
left to the cascade.

Wide tables scroll inside `.au-table-wrap` (`overflow-x: auto` as a **base**
rule, not inside a `max-width` query, or the document overflows on desktop).
`.au-content` and `.card-au` carry `min-width: 0` so a wide table cannot stretch
the flex column.

Verified in headless Chrome at 1600 / 1280 / 1000 / 992 / 900 px: zero gap
between sidebar and content, and zero horizontal overflow.

## Branding

The interface mirrors `digitalservices.adityauniversity.in`. Tokens were taken
from that site's stylesheet rather than eyeballed:

| Token | Value |
|-------|-------|
| Primary | `#004e92` (dark `#003666`) |
| Top bar gradient | `linear-gradient(140deg, #000428, #013c6e, #000428)` |
| Page background | `aliceblue` |
| Surface / panel | `#ffffff` / `#f8fafc` |
| Border | `#e2e8f0` |
| Text | `#1e293b`, muted `#475569` |
| Status | success `#10b981`, warning `#f59e0b`, danger `#ef4444` |
| Radius | 8 / 12 / 16 / 20 / pill |
| Max width | 1400px |
| Font | Google Sans |

All tokens live in `static/css/style.css` as CSS variables, and a dark theme is
included with a toggle in the top bar (choice persists in `localStorage`).

The Aditya University gold logo ships as two assets, because the source is a
wide wordmark that must never be cropped:

| Asset | Size | Use |
|---|---|---|
| `aditya-logo.png` | 3134×1168 (2.68:1) | Top bar, login card, printed letterhead — always `object-fit: contain` |
| `aditya-crest.png` | 512×512 | Favicon and square slots; the sun emblem cropped from the wordmark |

The wordmark sits on a white plate (`.au-logo-plate`) so the gold reads against
the dark blue gradient. Nothing in the CSS or templates applies `object-fit: cover`
to it, and `verify.py` asserts that.

## Formal permission letter

Every request has a printable letter at `/student/requests/<id>/letter`, reachable
from the student's detail page, the lecturer's review page, and the HOD daily
report. It is a formal document: letterhead, "To Whomsoever It May Concern",
reference number `REQ-0001`, a bordered status block, and Faculty / HOD signature
blocks.

The status block is colour-coded and states the outcome in words, including:

| Status | Wording |
|---|---|
| `APPROVED` | APPROVED — "has been **APPROVED**" |
| `REJECTED` | REJECTED — "has been **REJECTED**" |
| `PENDING` | AWAITING VERIFICATION — explicitly not a grant of permission |
| `CANCELLED` | CANCELLED BY STUDENT — "confers no permission whatsoever" |

## Request status display

`CANCELLED` appears everywhere a status can appear: the status badge, the coloured
banner on the request detail page (with the withdrawal timestamp), the formal
letter, and every table and report. `CANCELLED` and `EXPIRED` sit alongside
`PENDING`, `APPROVED` and `REJECTED` in the schema.

## Student identity: roll number

University mailboxes are provisioned as `<rollno>@adityauniversity.in` in lower
case (for example `26b21cs058@adityauniversity.in`) and the Outlook display name
is the same value, so **`roll_number` is the authoritative student identifier**.
There is no separate PIN column.

`derive_roll_number()` in `app/models/user.py` reads it from the mailbox and
falls back to the display name, so the value is populated automatically on first
sign-in even before the HOD edits the record. `roll_number` is nullable and
renders as `—` when the department has not recorded one.

## Stack

| Layer     | Technology                                    |
|-----------|-----------------------------------------------|
| Frontend  | HTML5, Bootstrap 5.3, Bootstrap Icons, Chart.js 4 |
| Backend   | Python 3, Flask 3, Jinja2                     |
| Auth      | Microsoft Entra ID (MSAL, authorization code) |
| Database  | MySQL 8 (`mysql-connector-python`, pooled)    |
| Storage   | Local filesystem, year/month buckets          |
| Email     | Outlook SMTP via Flask-Mail                   |

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env
```

Import the schema, then seed accounts:

```bash
mysql -u root -p < migrations/schema.sql
```

Run:

```bash
python app.py
```

Open http://localhost:5000

## Authentication

Real sign-in uses Microsoft Entra ID. Request these values from university IT:

| Setting      | Where it comes from                          |
|--------------|----------------------------------------------|
| `CLIENT_ID`  | App registration &gt; Overview               |
| `TENANT_ID`  | App registration &gt; Overview               |
| `CLIENT_SECRET` | Certificates &amp; secrets                |
| `REDIRECT_URI` | Matches `REDIRECT_URI` in `.env`          |

Register the redirect URI under **Authentication &gt; Add a platform &gt; Web**.

Until IT provides these, `DEV_MODE=true` shows a local sign-in picker on the login
page. It resolves roles through the same `users` table as the Entra path, so the
workflow can be exercised end to end. Set `DEV_MODE=false` once SSO is live — the
`/auth/dev-login` route then returns 404.

## Roles

| Role       | Access                                                     |
|------------|------------------------------------------------------------|
| `STUDENT`  | Submit requests, upload proof, view own history, withdraw pending |
| `LECTURER` | Review assigned requests, approve or reject with remarks   |
| `HOD`      | Analytics dashboard, all-requests filters, printable report |

Roles come solely from the `users.email` column after a verified identity — never from
client input.

## Workflow

```
Student (Entra sign-in)
  -> choose LEAVE or CLASSROOM
  -> fill dates, reason, upload proof
  -> lecturer is auto-assigned (fewest open requests)
  -> reviewer notified by Outlook email
Lecturer
  -> opens request, previews proof inline
  -> APPROVE or REJECT (remark required to reject)
Student + HOD notified of the decision
HOD
  -> live charts, filterable approved list, print report
```

## Layout

```
config.py              Settings loaded from .env
app.py                 Entry point
app/__init__.py        Application factory, blueprints, error handlers
app/auth/              MSAL client and sign-in routes
app/models/            Dataclasses, MySQL pool, queries
app/permissions/       Validation and orchestration (service layer)
app/student/           Student portal routes
app/faculty/           Lecturer portal and proof download
app/hod/               Analytics dashboard and printable report
app/utils/             Security helpers, proof storage, email
templates/             Jinja2 templates
static/                CSS and Chart.js dashboard
storage/proofs/        Uploaded proofs (year/month buckets)
migrations/schema.sql  Database schema and seed accounts
verify.py              Test harness (158 checks)
```

## Security notes

- No local passwords; identities come from Microsoft.
- Proof uploads are validated by extension **and** magic bytes, so a renamed
  `.exe` or script is rejected.
- Stored filenames are random UUIDs; original names are kept only in MySQL.
- Path resolution refuses any path escaping the storage root (traversal defence).
- Proof downloads are authorised: a student sees only their own, a lecturer only
  their assigned requests.
- `next` redirects are restricted to same-site relative paths.
- Files live under `storage/proofs/<year>/<month>/`, never in the database.

## Security

| Control | Implementation |
|---|---|
| Identity | Microsoft Entra ID only; roles come from the `users` table, never from input |
| CSRF | Every `POST`/`PUT`/`PATCH`/`DELETE` validated against a per-session token in `app/utils/security.py` |
| Sign-out | `POST`-only, so it cannot be triggered by a link or prefetch |
| Session fixation | Session cleared and CSRF token rotated on every sign-in |
| Idle timeout | Session discarded after `IDLE_TIMEOUT_SECONDS` (default 3600) of inactivity |
| Cookies | `HttpOnly`, `SameSite=Lax`, custom name; add `SESSION_COOKIE_SECURE=true` under HTTPS |
| Headers | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `COOP`, `Permissions-Policy` |
| Caching | `Cache-Control: no-store` on `/student`, `/faculty`, `/hod` pages |
| Uploads | Extension **and** magic-byte validation, 5 MB cap, random filenames, path-traversal refusal |
| Authorisation | Role guards per route; students see only their own requests, lecturers only assigned ones |
| Redirects | `next` restricted to same-site relative paths |

Set `SESSION_COOKIE_SECURE=true` and serve over HTTPS before going live.

### Signing out

- **Sign out** — ends the portal session only.
- **Sign out of all Microsoft sessions** — additionally ends the university SSO
  session, so other university services and other devices are signed out too.

The sign-out button is a `POST` form, so a link, an image tag or a browser
prefetch can never end someone's session. Navigating straight to
`/auth/logout` is still supported: `GET` shows a confirmation page with the
CSRF-protected `POST` form rather than a bare 405. Any other method mismatch
renders a branded 405 page rather than Werkzeug's default.

## Navigation

Staff get a persistent sidebar; students keep the compact top navigation.
Definition lives in `app/utils/nav.py`, rendered by `templates/_sidebar.html`,
and injected as `nav_items` / `nav_sections` by a context processor.

One markup tree serves both presentations: a fixed column at `lg` and above,
and a Bootstrap off-canvas drawer below it, opened from the header button.

Design rules:

- Deep navy surface (`#0a1f38`) rather than the busy header gradient, so page
  content carries the colour.
- **Gold (`#ffd166`) appears only on the current page** — as the active row
  tint, the active icon tile fill, and the left marker. Nowhere else.
- Icons sit in 30px rounded tiles so the list scans vertically.
- Footer holds the signed-in user block and a separate sign-out row.
- `aria-current="page"` on the active link; visible focus rings throughout.

| Role | Sections and pages |
|---|---|
| Lecturer | Review: Overview, All Requests, Find Student &middot; Teaching: My Classes, Attendance &middot; Insights: My Reports |
| HOD | Overview: Overview, All Requests, Students, Faculty Workload &middot; Academic: Classes, Daily Report &middot; Insights: Analytics |
| Student | Top nav: Dashboard, New Request, My Requests, My Classes, Account |

## Classes, rosters and attendance

A lecturer creates a class, then bulk-loads the roster from a spreadsheet.

```
POST /faculty/classes/new          name, section, academic year
POST /faculty/classes/<id>/roster   .xlsx | .xlsm | .csv | .txt
```

The parser tolerates a header row, numbers instead of text, and extra columns
(`26B21CS058 - Ravi Kumar` still yields the roll number). Roll numbers with no
student account are **stored anyway** and linked automatically the first time
that student signs in, so a roster can be loaded before the cohort has ever used
the portal.

| Feature | Route |
|---|---|
| Class list and creation | `/faculty/classes` |
| Roster + permissions for a chosen date | `/faculty/classes/<id>?date=YYYY-MM-DD` |
| Attendance picker | `/faculty/attendance` |
| Mark attendance | `/faculty/classes/<id>/attendance` |

Attendance defaults to `PRESENT`, auto-switches to `ON_PERMISSION` for any
student with an approved permission covering that date, refuses future dates, and
upserts so re-marking updates rather than duplicating. `ON_PERMISSION` is only
ever applied when an **approved** request actually spans that date.

## Tests

```bash
python verify.py              # 321 checks, no database required
python workflow_test.py       #  59 checks, end-to-end over HTTP
python class_features_test.py #  44 checks, classes / roster / attendance
python sidebar_test.py        #  68 checks, every page for every role
```

All four pass. The last three need the server and MySQL running.

`verify.py` covers app construction, role enforcement, sign-in failure modes,
upload security, request validation, MySQL `TIME` normalisation, CSRF and
logout redirects, branding assets, responsive CSS rules, template compilation
(including the `with context` requirement for macros and the ban on
`request.path`, which child templates shadow), and full page rendering.

`workflow_test.py` drives the running server over HTTP and then asserts database
state: real sign-in, submission with a genuine PDF upload, rejection of a `.php`
upload and of a `.pdf` containing script, lecturer approve/reject rules, blocked
double-actioning, cross-role isolation, and HOD analytics and reporting.

All four suites pass: **321/321**, **59/59**, **44/44** and **68/68**.

## Notes

- Outlook SMTP often blocks the default port. If delivery fails, the app logs it and
  continues rather than rejecting the student's request.
- `MAX_LEAVE_DAYS` in `app/permissions/service.py` caps a single request at 30 days.
- Reason categorisation for the HOD charts is keyword-based
  (`categorize_reason`) and can be swapped for a lookup table.
- Reviewer load is balanced automatically: a new request goes to the lecturer with
  the fewest pending items.

## Local setup notes

MySQL 8.4 does not register a Windows service from the standalone MSI. If the
service is missing, either run it directly:

```bash
& "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqld.exe" ^
   --datadir="C:\ProgramData\MySQL\MySQL Server 8.4\Data" --port=3300 ^
   --bind-address=127.0.0.1 --console
```

or register it from an elevated prompt:

```bash
& "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqld.exe" --install MySQL84 ^
   --datadir="C:\ProgramData\MySQL\MySQL Server 8.4\Data"
```

Importing the schema with `mysql < file.sql` does not work in PowerShell (no `<`
redirect). Pipe it instead:

```bash
Get-Content migrations/schema.sql -Raw | mysql -u root -p
```

The schema is not incremental; re-importing after an edit drops existing rows.