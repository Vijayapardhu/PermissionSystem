# CSE Permission & Leave Tracking System

Flask + Postgres permission tracker for the Department of Computer Science & Engineering,
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
| Database  | Postgres (Supabase, `psycopg` 3, pooled)     |
| Storage   | Firebase Storage (`firebase-admin`), private bucket, year/month keys |
| Email     | Outlook SMTP via Flask-Mail                   |
| Hosting   | Render (web service, gunicorn)                |

## Deploy to Render

The app runs on Render, keeps its database in Supabase and its proofs in Firebase Storage.

### 1. Supabase: database

Create a project, then open **SQL Editor** and paste `migrations/schema.sql`.
Run it once. It drops and recreates every table, so never re-run it against a
database that holds real data.

Then copy **Project Settings → Database → Connection string → URI (Direct)**:

```
postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres
```

Use that URI exactly as written. The app dials it verbatim: there is no pooler
host to derive, no startup probe and no fallback, and the target printed in the
startup log is the URI configured here.

Two things about this URI:

- Use the **Direct** connection, not the Supavisor pooler. Supavisor reaps and
  reschedules session connections underneath the client, which is where
  `consuming input failed: SSL SYSCALL error: EOF detected` comes from, and it
  shares one slot limit between everything pointed at the project. This app
  pools for itself, so it has nothing for Supavisor to pool.
- A password containing `@ / : #` must be percent-encoded, or the DSN silently
  parses up to the wrong separator.

`db.<ref>.supabase.co` is published as an **IPv6-only AAAA record**, so the host
running the app needs IPv6 egress to resolve it at all. The pool opens in the
background rather than failing the boot, so an unresolvable host still starts and
recovers on its own once egress exists.

### 2. Firebase: proof bucket

**Storage → Get started**, create a bucket, and leave **public access off**.

Proofs are medical and identity documents. A public bucket would hand every
object to anyone with the URL; the app instead fetches each object through
`/faculty/proofs/<id>/download`, which authorises the request first.

Then **Project settings → Service accounts → Generate new private key**, save the
JSON, and record:

| Value | Used for |
|---|---|
| `FIREBASE_PROJECT_ID` | Project settings → General |
| `FIREBASE_STORAGE_BUCKET` | Storage, the bucket you created |
| `FIREBASE_CREDENTIALS_PATH` | Server-side only. Path to that JSON. |

`firebase-admin` bypasses Storage rules with that key, so it is a server-side
secret: never put it in a template, a JavaScript file, or any client-side code,
and rotate it if it is ever committed or pasted somewhere it should not be.
Uploads fail with a plain error on the form if it is missing, not at startup.

Render has no key file on disk, so paste the JSON itself into
`FIREBASE_CREDENTIALS_JSON` there and leave the path unset. The two are mutually
exclusive, and the JSON wins when both are set.

### 3. Supabase: keys

Proofs live in Firebase, so this app needs no Supabase service-role key. Two
values from **Project Settings → API Keys** are worth knowing anyway:

| Value | Used for |
|---|---|
| `SUPABASE_PUBLISHABLE_KEY` | Not used by this app. |

If an older deployment still carries a service-role key, it is now unused:
never put it in a template, a JavaScript file, or any client-side code, and
rotate it if it is ever committed or pasted somewhere it should not be.

### 4. Entra ID

Under **App registrations → your app → Authentication → Web**, add the exact
redirect URI. Entra matches character for character, so paste it rather than
typing it:

```
https://permissionsystem.onrender.com/auth/callback
```

Set the same string in `REDIRECT_URI`. A mismatch gives `AADSTS50011`.

### 5. Render

The repository carries a `render.yaml` blueprint:

```bash
render blueprint launch
```

It creates the web service and prompts for each `sync: false` value. Or set it
up by hand with **New → Web Service**:

| Setting | Value |
|---|---|
| Runtime | Python |
| Build command | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start command | `gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 --access-logfile - wsgi:app` |
| Health check path | `/healthz` |

`render.yaml` and `Procfile` hold the start command, so leave Render's field on
its default and let it read the blueprint.

Environment variables to set on the service:

| Variable | Source |
|---|---|
| `DATABASE_URL` | Supabase, Direct URI (`db.<ref>.supabase.co`, not the pooler) |
| `SECRET_KEY` | Render's **Generate** button |
| `CLIENT_ID`, `TENANT_ID`, `CLIENT_SECRET` | Entra ID |
| `REDIRECT_URI` | `https://permissionsystem.onrender.com/auth/callback` |
| `FIREBASE_STORAGE_BUCKET` | Firebase → Storage |
| `FIREBASE_CREDENTIALS_JSON` | The service account key, pasted in (Render has no key file) |
| `SESSION_COOKIE_SECURE` | `true` (Render terminates TLS) |
| `DEV_MODE` | `false` |
| `FLASK_DEBUG` | `false` |

### Two Render constraints worth knowing

**Free plans block outbound SMTP.** Ports 25, 465 and 587 are blocked, and
`smtp.office365.com` needs 587. On the free plan the app still works, but every
notification is silently suppressed — `app/utils/email.py` logs the failure and
continues so a student's request is never lost. `render.yaml` therefore targets
the `starter` plan. To stay on free, leave `MAIL_USERNAME` empty and accept that
no email is sent.

**Sessions are signed cookies, not files.** Render's filesystem is ephemeral and
is wiped on every deploy and every 15-minute idle spin-down, so a filesystem
session store would sign everyone out on a schedule. The cookie store needs no
extra service and survives both, provided `SECRET_KEY` is set — if it is not, the
app falls back to a hardcoded development value and every deploy invalidates all
sessions.

### Deploy-time settings that matter

- `--workers 2` rather than 4: `DB_POOL_MAX` is per process, and the connection
  limit is what runs out first on the free Supabase plan.
- `--timeout 120`: uploads run to 5 MB and the permission letter renders inline,
  which is tight against gunicorn's 30-second default.
- `/healthz` stays 200 even when Postgres is unreachable. Render restarts the
  service on a non-2xx health check, and restarting cannot fix a database the
  container cannot route to — the replacement process has the same network path.
  The probe reports `{"status":"ok","database":"unreachable"}` instead, and caches
  that verdict for `HEALTH_DB_CACHE_SECONDS` so polling does not itself take a
  pooled connection.

## Install on a new computer

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.9 or newer | <https://www.python.org/downloads/> &mdash; tick **"Add Python to PATH"** on Windows |
| A Git client | any | Or download this repository as a ZIP |
| psql | 14 or newer | Optional. Only needed to load the schema from the command line |
| A Supabase project | any | The database |
| A Firebase project | any | The proof bucket and its service account key |

You also need the department's Entra ID values, read from
**Entra ID &rarr; App registrations &rarr; Aditya University Permission Portal**:

| Value | Where it lives |
|---|---|
| `CLIENT_ID` | Overview |
| `TENANT_ID` | Overview |
| `CLIENT_SECRET` | Certificates &amp; secrets &rarr; New client secret (copy the **value**) |

### Steps

```bash
# 1. Get the code
git clone https://github.com/Vijayapardhu/PermissionSystem.git
cd PermissionSystem

# 2. Automated setup: virtual environment, dependencies, .env, schema
python setup.py

# 3. Enter the real values in .env
#    CLIENT_ID, TENANT_ID, CLIENT_SECRET, DATABASE_URL,
#    FIREBASE_PROJECT_ID, FIREBASE_STORAGE_BUCKET, FIREBASE_CREDENTIALS_PATH

# 4. Start the app
python wsgi.py
```

Open **<http://127.0.0.1:5000>**. `setup.py` is safe to re-run: it skips
anything already in place.

### Manual setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

Then fill in `.env` and create the storage bucket and database (below).

### Create the database

The app creates no tables itself. Load the schema once, either by pasting
`migrations/schema.sql` into the Supabase **SQL Editor**, or:

```bash
# macOS / Linux
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/schema.sql

# Windows PowerShell has no `<` redirect, so pipe instead:
Get-Content migrations\schema.sql -Raw | psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1
```

`ON_ERROR_STOP=1` matters: without it psql reports success even when an earlier
statement failed, and you end up with half a schema.

The script is **not incremental**. It drops and recreates the tables, which
deletes every user, request and attendance mark. Load it once against an empty
project and never again.

Then set the connection string in `.env`:

```env
DATABASE_URL=postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres
```

### Create the proof bucket

In the Firebase console: **Storage → Get started**, create a bucket with public
access off, then **Project settings → Service accounts → Generate new private
key** and point `FIREBASE_CREDENTIALS_PATH` at the JSON. Nothing is written to the
local filesystem any more, so there is no `storage/proofs/` directory to create.

### Signing in

While `DEV_MODE=true` the login page shows a **local account picker** using the
same code path as real sign-in, so you can test everything before IT responds:

| Role | Email |
|---|---|
| HOD | `hod.cse@adityauniversity.in` |
| Lecturer | `lecturer1.cse@adityauniversity.in`, `lecturer2.cse@adityauniversity.in` |
| Student | `26b21cs058@adityauniversity.in`, `26b21cs059@`, `25b21cs012@`, `24b21cs145@` |

Once Outlook sign-in works, set `DEV_MODE=false`; `/auth/dev-login` then returns 404
and cannot be used to bypass Microsoft.

### Serving on a shared machine or server

`python wsgi.py` runs the Flask development server, which is for testing only.

```bash
pip install waitress            # Windows
waitress-serve --port=8080 wsgi:app

pip install gunicorn            # Linux / Render
gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 4 --timeout 120 wsgi:app
```

The target is `wsgi:app`, not `app:app`. `wsgi.py` cannot be called `app.py`
because the `app/` package would shadow it: Python resolves `import app` to the
package directory, so gunicorn would load `app/__init__.py`, find no `app`
attribute in it, and exit with
`AppImportError: Failed to find attribute 'app' in 'app'`.

Then in `.env`:

```env
HOST=0.0.0.0
PORT=8080
FLASK_DEBUG=false
SESSION_COOKIE_SECURE=true      # only once HTTPS is in front of the app
```

**Redirect URI.** Entra ID matches redirect URIs exactly. Serving from anywhere
other than `localhost:5000` means adding that URL under
**App registrations &rarr; your app &rarr; Authentication &rarr; Web &rarr; Redirect URIs**
and setting the same string in `REDIRECT_URI`. A mismatch gives `AADSTS50011`.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python: command not found` | Python missing or not on PATH; reinstall and tick "Add Python to PATH" |
| `ModuleNotFoundError: flask` | Virtual environment not active, or dependencies not installed |
| `DATABASE_URL is not set` | `.env` missing the connection string, or the service has no env vars |
| `could not translate host name` / `Connection refused` | Wrong host or port in `DATABASE_URL`. It must be the Direct host `db.<ref>.supabase.co` on 5432 — a pooler URI is no longer rewritten |
| `password authentication failed` | Wrong password, or an unencoded `@` / `:` / `#` in the URI |
| `too many connections` | `DB_POOL_MAX` x gunicorn `--workers` exceeds the plan limit. Free Supabase allows 15 |
| "The user directory is temporarily unavailable" on sign-in | The pool could not get a connection. Search the log for `Postgres checkout failed` — it names the host, the last driver error and the pool stats, which distinguishes "the database is unreachable" from "the pool was full" |
| The log says `couldn't get a connection after 5.00 sec` repeatedly | Every request is queueing. `DB_POOL_MAX` must be at least the `--threads` one worker serves, or the worker deadlocks against itself |
| The log says `connection timeout expired` on connect | The host cannot reach `db.<ref>.supabase.co`. It is an IPv6-only AAAA record, so the host needs IPv6 egress, and a typo in the host fails the same way |
| The log says `password authentication failed` | Wrong password, or an unencoded `@` / `:` / `#` in the URI |
| `/healthz` reports `"database": "unreachable"` | Postgres is not answering. The probe still returns 200 on purpose: restarting cannot fix a network path, and a restart loop is worse than the fault |
| `relation "users" does not exist` | `migrations/schema.sql` was never loaded |
| `The requested path is invalid` on upload | Bucket name is wrong, or `FIREBASE_STORAGE_BUCKET` does not match it |
| `row-level security` error | The app is connecting with the `anon` role. Use the project owner DSN |
| Upload fails with a storage error | Bucket set to Public? It must be private |
| No email ever arrives on Render | Free plan blocks ports 25/465/587. See "Two Render constraints" |
| Sign-in loops back to the login page | `REDIRECT_URI` does not match the registered redirect URI exactly |
| `AADSTS50011` | Same redirect URI mismatch |
| Sign-in fails immediately | `CLIENT_SECRET` wrong, expired, or still a placeholder |
| Port already in use | Change `PORT` in `.env`, or stop whatever is using it |
| `setup.py` cannot find `psql` | Paste `migrations/schema.sql` into the Supabase SQL Editor instead |
| `/healthz` returns 503 | Postgres unreachable. Check `DATABASE_URL` and Render's private-network access |
| Proof download returns 404 | Object missing from the bucket, or `file_path` in the database no longer matches |

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

## Project structure

```
config.py              Settings loaded from .env
setup.py               Automated first-time setup
wsgi.py                 WSGI entry point (development server, gunicorn/waitress)
requirements.txt       Pinned dependencies
Procfile               gunicorn start command for Render
render.yaml            Render blueprint (env vars, start command, health check)
app/__init__.py        Application factory, blueprints, /healthz, error handlers
app/auth/              MSAL client and sign-in routes
app/models/            Dataclasses, Postgres pool, queries
app/permissions/       Validation and orchestration (service layer)
app/student/           Student portal routes
app/faculty/           Lecturer portal, classes, attendance, proof download
app/hod/               Analytics, register, reports
app/utils/             Security helpers, Firebase Storage proofs, roster parsing, email, navigation, letter QR codes
templates/             Jinja2 templates
templates/student/letter.html    Formal A4 permission letter, one page, with its QR code
templates/auth/verify.html        Public, account-free page a letter's QR code opens
static/                CSS, Chart.js dashboard, Aditya logo assets
migrations/schema.sql  Database schema and seed accounts
verify.py              Unit harness, no database required
workflow_test.py       End-to-end over HTTP
class_features_test.py Classes, roster and attendance
sidebar_test.py        Every page for every role
```

Nothing is written to the local filesystem at runtime. Uploaded proofs live in
the private Firebase Storage bucket under `<year>/<month>/<uuid>.<ext>`, and
`proof_documents.file_path` holds that object key.

## Security notes

- No local passwords; identities come from Microsoft.
- Proof uploads are validated by extension **and** magic bytes, so a renamed
  `.exe` or script is rejected.
- Stored object keys are random UUIDs; original names are kept only in Postgres.
- Object keys are validated, refusing absolute paths, `..` segments and anything
  that would resolve outside the bucket root.
- The Firebase bucket is private. Proofs are served only through an authorised
  route, never by guessing a URL.
- Row Level Security is enabled on every table with no `anon` or `authenticated`
  policy, so roll numbers and reasons are unreachable through the Data API even
  if the publishable key leaks.
- Proof downloads are authorised: a student sees only their own, a lecturer only
  their assigned requests.
- `next` redirects are restricted to same-site relative paths.
- The service-role key is read from the environment only, never rendered.

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
| Uploads | Extension **and** magic-byte validation, 5 MB cap, random object keys, key-traversal refusal |
| Authorisation | Role guards per route; students see only their own requests, lecturers only assigned ones |
| Redirects | `next` restricted to same-site relative paths |
| Database | RLS enabled with no `anon`/`authenticated` policy on all seven tables |
| Proof storage | Private Firebase bucket, fetched through an authorised route |
| Letter verification | `/verify/<ref>.<signature>` is public and read-only; the signature is derived from `SECRET_KEY`, so a link cannot be forged and a reference number cannot be walked |

Set `SESSION_COOKIE_SECURE=true` and serve over HTTPS before going live.

## Verifying a letter by QR code

Every permission letter carries a QR code in its header. Scanning it opens
`/verify/<ref>.<signature>`, which works **without an account** — a gatekeeper, a
class representative or an employer can open it on a phone. The page shows the
PIN, the reference number, the student's name and department, the permission
type and period, the reason stated, the status, the reviewing faculty, the
decision date and any remarks. The letter states the same URL in writing, so it
still verifies if the code is smudged.

The signature in the URL is the access control, and it is there for a specific
reason: a bare `/verify/REQ-0001` would let anyone walk the whole department, and
these records carry medical reasons. The signature is produced by this app from
`SECRET_KEY` and is deliberately **not** timestamped, so a reprint of a letter
produces the same code and a letter already in circulation keeps verifying. The
trade-off is that rotating `SECRET_KEY` invalidates every letter already printed.

The page is a read-only receipt: no form, no session, and no data that only staff
can see. A link that does not verify returns 404, the same as a record that does
not exist, so the endpoint cannot be used to test which reference numbers are
real.

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
python verify.py              # 392 checks, no database required
python workflow_test.py       # end-to-end over HTTP
python class_features_test.py # classes / roster / attendance
python sidebar_test.py        # every page for every role
```

The first three all pass. The last three need the server running **and**
`DATABASE_URL` set, because they assert against real database state. Point
`BASE` in each at the server if it is not on `http://localhost:5000`.

`verify.py` covers app construction, role enforcement, sign-in failure modes,
upload security against an in-memory fake of the Firebase Storage API, request
validation, `TIME` normalisation, CSRF and logout redirects, proof-key
containment, branding assets, responsive CSS rules, deployment settings in
`render.yaml` and `Procfile`, template compilation (including the
`with context` requirement for macros and the ban on `request.path`, which child
templates shadow), and full page rendering.

`workflow_test.py` drives the running server over HTTP and then asserts database
state: real sign-in, submission with a genuine PDF upload, rejection of a `.php`
upload and of a `.pdf` containing script, lecturer approve/reject rules, blocked
double-actioning, cross-role isolation, and HOD analytics and reporting.

## Notes

- Outlook SMTP often blocks the default port, and Render's free plan blocks it
  outright. If delivery fails, the app logs it and continues rather than
  rejecting the student's request.
- `MAX_LEAVE_DAYS` in `app/permissions/service.py` caps a single request at 30 days.
- Reason categorisation for the HOD charts is keyword-based
  (`categorize_reason`) and can be swapped for a lookup table.
- Reviewer load is balanced automatically: a new request goes to the lecturer with
  the fewest pending items.
- Text searches use `ILIKE`, not `LIKE`. The MySQL build ran under a
  case-insensitive collation, so searching for `26b21cs058` matched a stored
  `26B21CS058`. Postgres `LIKE` is case-sensitive and would have returned
  nothing, so every user-facing search had to move to `ILIKE`.
- Chart and report days are bucketed with `AT TIME ZONE REPORT_TIMEZONE`
  (default `Asia/Kolkata`). Timestamps are stored as `timestamptz` in UTC, so
  without that cast a request submitted at 23:30 IST would chart under the
  following day.
- `get_stats_for_hod()` computes a `reasons` breakdown that no template renders.
  It is kept because the method is part of the HOD analytics surface, but it runs
  on every dashboard load for nothing.

## Local setup notes

There is no local database or object store to install. Supabase hosts the
database and Firebase holds the proofs, so local development uses the same two
services as production.

Importing the schema with `psql -f` works everywhere; the PowerShell `<`
redirect trap no longer applies:

```bash
Get-Content migrations\schema.sql -Raw | psql $env:DATABASE_URL -v ON_ERROR_STOP=1
```

`ON_ERROR_STOP=1` is worth keeping. Without it psql exits 0 even when an earlier
statement failed, so a broken schema looks like a successful load.
