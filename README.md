# CSE Permission & Leave Tracking System

Flask + Firestore permission tracker for the Department of Computer Science & Engineering,
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
| Data      | Cloud Firestore (`google-cloud-firestore`)   |
| Storage   | Cloudflare R2 (S3 API, boto3), private bucket, year/month keys |
| Email     | Outlook SMTP via Flask-Mail                   |
| Hosting   | Render (web service, gunicorn)                |

## Deploy to Render

The app runs on Render, keeps its records in Cloud Firestore and its proofs in a private Cloudflare R2 bucket.

### 1. Firebase: the database

Create a project, then **Project settings → Service accounts → Generate new
private key** and save the JSON. Record:

| Value | Used for |
|---|---|
| `FIREBASE_PROJECT_ID` | Project settings → General |
| `FIRESTORE_DATABASE` | `(default)`, unless you created a named database |
| `FIREBASE_CREDENTIALS_PATH` | Server-side only. Path to that JSON. |

The key bypasses Firestore security rules, so it is a server-side secret: never
put it in a template, a JavaScript file, or any client-side code, and rotate it if
it is ever committed or pasted somewhere it should not be.

There is no schema to apply. Firestore collections come into being when a
document is written, so create the accounts the app expects with:

```bash
python migrations/seed_firestore.py
```

The seed is idempotent — it keys on the email address and leaves anything already
there alone.

On Render there is no key file on disk, so paste the JSON itself into
`FIREBASE_CREDENTIALS_JSON` there and leave the path unset. Missing credentials do
not stop the boot: the app serves its health path and every data-backed request
reports the store as unavailable, which is the same shape as a database outage.

Two things worth knowing about how the data layer works:

- Ordering happens in Python. Firestore can sort on a single field, but
  `where(...).order_by(...)` across two fields needs a composite index created in
  the console first, and an app that errors until someone does that is a worse
  failure than one that sorts a few hundred documents.
- Each collection keeps a counter document and hands out the next integer id
  inside a transaction, so ids in URLs stay integers and two concurrent creates
  cannot collide.

### 2. Cloudflare: proof bucket

**R2 → Create bucket**, name it `proofs`, and leave **public access off**.

Proofs are medical and identity documents. A public bucket would hand every
object to anyone with the URL; the app instead fetches each object through
`/faculty/proofs/<id>/download`, which authorises the request first.

Then **R2 → Manage R2 API Tokens → Create Account API token** with *Object Read &
Write* on that bucket, and record:

| Value | Used for |
|---|---|
| `R2_ACCOUNT_ID` | R2 overview, right-hand side |
| `R2_ACCESS_KEY_ID` | Server-side only |
| `R2_SECRET_ACCESS_KEY` | Server-side only |

These are R2 credentials, not AWS ones. The token bypasses every bucket policy,
so it is a server-side secret: never put it in a template, a JavaScript file, or
any client-side code, and rotate it if it is ever committed or pasted somewhere
it should not be. Uploads fail with a plain error on the form if it is missing,
not at startup.

### 3. Storage backend

Proofs go to Cloudflare R2 by default. Set `STORAGE_BACKEND=supabase` with
`SUPABASE_URL` / `SUPABASE_SECRET_KEY` to keep using the old Supabase Storage
bucket for a deployment that still holds proofs there; those keys are otherwise
unused. Never put a service-role key in a template, a JavaScript file, or any
client-side code, and rotate it if it is ever committed or pasted somewhere it
should not be.

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
| `FIREBASE_CREDENTIALS_JSON` | The service account key, pasted in (Render has no key file) |
| `SECRET_KEY` | Render's **Generate** button |
| `CLIENT_ID`, `TENANT_ID`, `CLIENT_SECRET` | Entra ID |
| `REDIRECT_URI` | `https://permissionsystem.onrender.com/auth/callback` |
| `STORAGE_BACKEND` | `r2` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Cloudflare R2 API token |
| `R2_BUCKET` | `proofs` |
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | Only if `STORAGE_BACKEND=supabase` |
| `FIRESTORE_TIMEOUT_SECONDS` | `8` |
| `FIRESTORE_RETRIES` | `2` |
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

- `--workers 2` rather than 4: it halves the memory and the per-request store
  calls, and this workload does not need the throughput.
- `--timeout 120`: uploads run to 5 MB and the permission letter renders inline,
  which is tight against gunicorn's 30-second default.
- `/healthz` stays 200 even when Firestore is unreachable. Render restarts the
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
| A Firebase project | any | The database and the service account key |
| A Cloudflare account | any | The proof bucket and its R2 API token |

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
#    CLIENT_ID, TENANT_ID, CLIENT_SECRET,
#    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

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

### Seed the database

The app creates no collections itself; Firestore does that on first write. Seed
the accounts the app expects once, after the credentials are in `.env`:

```bash
python migrations/seed_firestore.py
```

It writes the three staff accounts and four students, skips any account whose
email already exists, and touches nothing else. Re-run it whenever you want the
sample accounts back.


Then set the connection string in `.env`:

```env
FIREBASE_PROJECT_ID=your-project-id
FIREBASE_CREDENTIALS_PATH=/path/to/service-account.json
```

### Create the proof bucket

In the Cloudflare dashboard: **R2 → Create bucket**, name `proofs`, public access
off, then **Manage R2 API Tokens → Create Account API token** (Object Read &
Write) and put the three values in `.env`. Nothing is written to the local
filesystem any more, so there is no `storage/proofs/` directory to create.

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
| `Firestore is not configured` | `FIREBASE_CREDENTIALS_JSON` / `_PATH` is missing or is not valid JSON |
| `could not translate host name` / `Connection refused` | The service account credentials are wrong, or the project id does not match the key |
| `password authentication failed` | Wrong password, or an unencoded `@` / `:` / `#` in the URI |
| `DeadlineExceeded` | One store call passed `FIRESTORE_TIMEOUT_SECONDS`. It is raised rather than retried past the budget on purpose |
| "The user directory is temporarily unavailable" on sign-in | Firestore is not answering. Search the log for `Firestore documents failed` — it names the call, the last driver error and how many attempts were made |
| The log says `Firestore documents failed after 2 attempts` | The project is unreachable from this host, the credentials are wrong, or Firestore is not enabled for the project |
| The log says `password authentication failed` | Wrong password, or an unencoded `@` / `:` / `#` in the URI |
| `/healthz` reports `"database": "unreachable"` | Firestore is not answering. The probe still returns 200 on purpose: restarting cannot fix a network path, and a restart loop is worse than the fault |
| The dev picker is empty, sign-in is refused | `migrations/seed_firestore.py` has not been run, or the signed-in email is not one of the seeded accounts |
| `Proof storage is not configured on this server` | The R2 credentials are missing, so no proof can be stored |
| `row-level security` error | The app is connecting with the `anon` role. Use the project owner DSN |
| Upload fails with a storage error | Bucket set to Public? It must be private |
| No email ever arrives on Render | Free plan blocks ports 25/465/587. See "Two Render constraints" |
| Sign-in loops back to the login page | `REDIRECT_URI` does not match the registered redirect URI exactly |
| `AADSTS50011` | Same redirect URI mismatch |
| Sign-in fails immediately | `CLIENT_SECRET` wrong, expired, or still a placeholder |
| Port already in use | Change `PORT` in `.env`, or stop whatever is using it |
| `setup.py` skipped the seed | It needs `FIREBASE_CREDENTIALS_PATH` (or `_JSON`) in `.env` first; then run `python migrations/seed_firestore.py` |
| Every data-backed page returns 503 | Firestore unreachable. The health path still returns 200; check the credentials and the project |
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
app/models/            Dataclasses, the Firestore store, model queries
app/permissions/       Validation and orchestration (service layer)
app/student/           Student portal routes
app/faculty/           Lecturer portal, classes, attendance, proof download
app/hod/               Analytics, register, reports
app/utils/             Security helpers, Cloudflare R2 proof storage, roster parsing, email, navigation, letter QR codes
templates/             Jinja2 templates
templates/student/letter.html    Formal A4 permission letter, one page, with its QR code
templates/auth/verify.html        Public, account-free page a letter's QR code opens
static/                CSS, Chart.js dashboard, Aditya logo assets
migrations/seed_firestore.py  Idempotent seed of the staff and student accounts
verify.py              Unit harness, no database required
workflow_test.py       End-to-end over HTTP
class_features_test.py Classes, roster and attendance
sidebar_test.py        Every page for every role
```

Nothing is written to the local filesystem at runtime. Uploaded proofs live in
the private R2 bucket under `<year>/<month>/<uuid>.<ext>`, and
`proof_documents.file_path` holds that object key.

## Security notes

- No local passwords; identities come from Microsoft.
- Proof uploads are validated by extension **and** magic bytes, so a renamed
  `.exe` or script is rejected.
- Stored object keys are random UUIDs; original names are kept only in the database.
- Object keys are validated, refusing absolute paths, `..` segments and anything
  that would resolve outside the bucket root.
- The R2 bucket is private. Proofs are served only through an authorised
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
| Data access | The service account key is server-side only, and no Firebase web config is used server-side |
| Proof storage | Private R2 bucket, fetched through an authorised route |
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
credentials set, because they assert against real store state. Point
`BASE` in each at the server if it is not on `http://localhost:5000`.

`verify.py` covers app construction, role enforcement, sign-in failure modes,
upload security against an in-memory fake of the R2 S3 API, request
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
- Text searches match roll numbers and names case-insensitively in Python
  (`needle in value.upper()`). The original MySQL build searched under a
  case-insensitive collation, so `26b21cs058` matched a stored `26B21CS058`, and
  every user-facing search has kept that behaviour since.
- Chart and report days are bucketed in `REPORT_TIMEZONE` (default
  `Asia/Kolkata`). Timestamps are stored in UTC, and the store converts them on
  the way out, so a request submitted at 23:30 IST charts under that IST day
  rather than the following UTC one.
- `get_stats_for_hod()` computes a `reasons` breakdown that no template renders.
  It is kept because the method is part of the HOD analytics surface, but it runs
  on every dashboard load for nothing.

## Local setup notes

There is no local database or object store to install. Firestore holds the
records and Cloudflare R2 holds the proofs, so local development uses the same
two services as production.

Seeding needs no client and no redirect: `python migrations/seed_firestore.py` works
the same on every platform, and it exits non-zero if the write fails.

