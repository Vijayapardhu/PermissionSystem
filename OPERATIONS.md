# Operating this system

Everything needed to run, update, recover and troubleshoot the live portal.

- **What the thing is:** [README.md](README.md)
- **Live site:** <https://permission.vijayapardhu.tech>
- **Host:** `129.225.111.22`, user `ubuntu`, SSH key `~/.ssh/permission.key`

---

## The server at a glance

| | |
|---|---|
| App | Flask behind gunicorn, systemd service `permission-system` |
| Web | nginx, Let's Encrypt certificate (auto-renews) |
| Code | `/home/ubuntu/app` |
| Data | `/home/ubuntu/app/local-dev.db` — SQLite, **not in git** |
| Logs | `journalctl -u permission-system` |
| Firewall | ufw, fail2ban |

```bash
ssh -i ~/.ssh/permission.key ubuntu@129.225.111.22
sudo systemctl status permission-system
curl -s https://permission.vijayapardhu.tech/healthz     # {"database":"ok","status":"ok"}
```

`/healthz` checks the database and returns non-200 if it is unreachable. It is
the one thing worth wiring into an alert.

### Why PowerShell users should read this

Running SSH commands from PowerShell mangles `$HOME`, `$PATH` and anything with
`$(...)`. **Write the script to a file and pipe it in:**

```powershell
Get-Content -Raw deploy.sh | ssh -i ~/.ssh/permission.key ubuntu@129.225.111.22 "tr -d '\r' | bash -s"
```

Inline `ssh host "sudo systemctl $(...)"` silently produces nonsense or a
truncated command.

---

## Deploying a change

There is **no git on the server**. Code is deployed by copying files in.

```bash
# 1. locally: commit and push first, so the server is never ahead of git
git add -A && git commit -m "..." && git push origin json

# 2. build an archive of just the changed files
tar -czf change.tar.gz app/faculty/routes.py templates/faculty/classes.html
scp change.tar.gz ubuntu@129.225.111.22:/home/ubuntu/

# 3. on the server
scp change.tar.gz ubuntu@...   # already there
ssh ...  "cd /home/ubuntu/app && tar -xzf /home/ubuntu/change.tar.gz && \
         find . -name __pycache__ -type d -exec rm -rf {} + ; \
         sudo systemctl restart permission-system"
```

Then confirm:

```bash
curl -s https://permission.vijayapardhu.tech/healthz
sudo journalctl -u permission-system --since '-3min' | grep -i error
```

### Before you deploy

The test suite is the gate. `python verify.py` should report 900+ passing.

> **Deploy after committing, not before.** The rollback copies described below
> are a single step and a single deployment, not a history. Last time this was
> not done, 19 of 22 rollback copies turned out to be versions that existed in
> no commit at all — the only undo path for that stretch of work was the copies
> themselves.

---

## Backups

**There is no automated backup of the live database.** No timer, no cron, no
copy. Everything below is manual.

This is the most serious gap in the deployment. The database holds 1,358 real
accounts and every permission and leave record. If the disk fails, it is gone.

### Do this now

```bash
ssh -i ~/.ssh/permission.key ubuntu@129.225.111.22
# A consistent copy. Never `cp` the file while the service is writing to it.
sudo systemctl stop permission-system
sudo cp /home/ubuntu/app/local-dev.db /home/ubuntu/backups/db-$(date +%F-%H%M).db
sudo systemctl start permission-system
```

Keep them somewhere off the machine. A backup on the same disk is not a backup.

### What a restore looks like

```bash
sudo systemctl stop permission-system
sudo cp /home/ubuntu/backups/db-2026-10-11-1200.db /home/ubuntu/app/local-dev.db
sudo chown ubuntu:ubuntu /home/ubuntu/app/local-dev.db
sudo rm -f /home/ubuntu/app/local-dev.db-wal /home/ubuntu/app/local-dev.db-shm
sudo systemctl start permission-system
curl -s https://permission.vijayapardhu.tech/healthz
```

The `-wal` and `-shm` files must go with it: they belong to the database being
replaced, and leaving them corrupts what you restore.

### Code rollback

`/home/ubuntu/prev` is a copy of the files a deploy replaced. It is overwritten
each time. A tarball of the last one is kept alongside:

```
/home/ubuntu/prev-rollback-YYYYMMDD-HHMM.tar.gz
```

To roll back: extract over `/home/ubuntu/app`, clear `__pycache__`, restart.
Prefer `git checkout` to the previous commit where the file exists there.

---

## Data imports

Scripts live in `migrations/` and are run by hand, never by the app. All of them
are dry-run first unless told otherwise.

### Student names and phone numbers

```bash
ECAP_EXPORT_DIR=<folder holding the exports> SQLITE_PATH=/home/ubuntu/app/local-dev.db \
  DATABASE_BACKEND=sql .venv/bin/python migrations/import_student_details.py

# then, if the dry run looks right:
# ... --apply
```

Reads the college's ECAP exports. **The `.xls` extension on those files is a
lie** — they are Excel "Save as Web Page" output, i.e. HTML tables. Read as a
workbook they yield nothing.

It only ever updates roll numbers already in the system. The export is the whole
college; the 11,000+ rolls not in the system are skipped and nothing in the
script can create a user. A name is replaced only when it currently holds the
student's own roll number (a placeholder), and a phone only when the account has
none and it parses as a ten-digit Indian mobile. Safe to re-run: a second run
writes nothing.

### College register, CSE students, Dean

```
migrations/import_college_data.py     the faculty register
migrations/import_cse_students.py     the student roster
migrations/add_dean.py                 the Dean account
migrations/check_college_data.py       inspect before importing
```

### Auditing what is already there

```
DATABASE_BACKEND=sql .venv/bin/python migrations/audit_data.py
```

Read-only. It calls nothing but `store.documents`, and the database file is
byte-identical afterwards — worth knowing before you point it at production.

It reports, in order of severity:

| | |
|---|---|
| `undecidable` | a record whose approver cannot exist. **Permanent** until the data or the workflow changes. Exits non-zero. |
| `stranded` | a non-faculty applicant, which can never enter the workflow |
| `stuck` | non-terminal and untouched for longer than `--stuck-days` (default 14) |
| `empty` | a queue nobody is working through — including the HOD one a Deputy HOD works from |
| `orphaned` | a reference to a record that is not there |

Useful flags: `--stuck-days 30` to be less noisy, `--verbose` to list every
record rather than only the problems.

**Why it exists.** `verify.py` runs against synthetic fixtures, so it cannot
tell you about the records you actually have. The live audit that prompted this
found application #171 — the one Dean's own application, at `DEAN_PENDING`,
which nobody could ever approve because the only person who can sign that stage
is the applicant. It was written by a script calling `LeaveModel.create`
directly, which takes an id and cannot tell who is applying, so neither the page
grants nor `create_leave` could have stopped it. `submit_to_hod` now refuses
non-teaching staff, which is the last gate before the workflow. The audit is
what finds anything that still gets past it.

Note the two together: the fix stops new records, the audit catches the old
ones. Neither replaces the other.

---

## Day-to-day tasks

| Task | Where |
|---|---|
| Promote a lecturer to Deputy HOD | HOD → Teaching Staff → **Make deputy** |
| Add a routing desk | HOD → Routing → Review Desks → Add |
| Load a class roster | Lecturer → My Classes → open → **Class settings** |
| Record classwork | Lecturer → Classwork → **Record classwork** |
| Answer an objection | Lecturer → Objections |
| Load faculty from a spreadsheet | HOD → Teaching Staff → Import |
| Check who holds permission today | Deputy → Permitted Today |

### Configuring email

Notifications are **not being sent.** `MAIL_USERNAME` is empty on the live
server, so every send is logged as suppressed and silently dropped. To fix:

```bash
sudo nano /home/ubuntu/app/.env      # MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD
sudo systemctl restart permission-system
```

Until then, students must open the site to see a decision. Decisions are
recorded correctly either way — this only affects notification.

### Turning on real sign-in

`DEV_MODE` enables a local sign-in that bypasses Microsoft. It is currently on,
because that is how the demo accounts work. **Turn it off for real use**, and
confirm `CLIENT_ID`, `TENANT_ID` and `CLIENT_SECRET` are set first.

---

## Troubleshooting

### The site is down

```bash
sudo systemctl status permission-system
sudo journalctl -u permission-system -n 50 --no-pager
sudo nginx -t && sudo systemctl reload nginx
curl -s https://permission.vijayapardhu.tech/healthz
```

### A page is 500 after a deploy

Almost always a template or JS edit. The traceback is in the journal:

```bash
sudo journalctl -u permission-system --since '-10min' | grep -A15 Traceback
```

`local-dev.db-wal` errors after a restore mean stale write-ahead files — see
the restore steps above.

### A route returns 403 for everyone

The route is not in the page-permission table. `app/permissions/pages.py` raises
at start-up for an *unmapped* route, but a **mismatched key** — a route named
`classwork_delete` looking for a grant called `delete_classwork` — passes
start-up and then 403s everyone. The key must equal the endpoint name exactly.

### Icons or Bootstrap not loading, red CSP errors in the console

The CSP lives in nginx, not the app. Check both:

```bash
sudo nginx -T | grep Content-Security-Policy
curl -s https://permission.vijayapardhu.tech/static/vendor/css/bootstrap-icons.min.css | head -c 200
```

Assets are served from `/static/vendor`. If a font 404s the icons render as
nothing, and the console will not mention the CSP at all.

### A student cannot sign in

Almost always because their account has no email address. Only a handful of the
1,138 student accounts have one, and the count rises as students sign in for the
first time. Check:

```bash
cd /home/ubuntu/app && SQLITE_PATH=local-dev.db DATABASE_BACKEND=sql .venv/bin/python -c "
from app import create_app; from app.models import UserRole
from app.models.user import UserModel
a = create_app()
with a.app_context():
    s = UserModel._by_role(UserRole.STUDENT.value, 'name')
    print('students:', len(s), 'with an email:', sum(1 for u in s if u.email))"
```

Names and phone numbers are loaded; the mailboxes are the missing piece. That is
an IT task at the college, not a code change.

### Routing sends everything to the wrong place

Almost always because almost nobody is on a proctor list. Proctoring is the
first rule, so with 3 of 1,138 students allocated, there is very little for the
desks to catch.

### Disk filling up

```bash
df -h /
du -sh /home/ubuntu/app/local-dev.db /home/ubuntu/ecap /var/cache/apt
```

`/home/ubuntu/ecap` (212MB) is the ECAP export, kept so the import can be re-run
against a fresher one. Safe to delete. `apt-get clean` reclaims the package
cache.

---

## Security posture

| Measure | State |
|---|---|
| SSH | Root login disabled, key-only, `MaxAuthTries 3`, `AllowUsers ubuntu` |
| fail2ban | Active, bans on repeated auth failure |
| Unattended upgrades | Enabled |
| sysctl hardening | Applied |
| nginx | `server_tokens off`, rate limits on sign-in and callback |
| TLS | Let's Encrypt, auto-renews |
| RPC | Disabled |

Check what is actually in force rather than trusting this table:

```bash
sudo fail2ban-client status sshd
sudo systemctl list-timers | grep certbot
sudo sshd -T | grep -E 'permitrootlogin|maxauthtries|passwordauthentication'
```

### Secrets

`.env` on the server holds the client secret and database credentials. It is
`chmod 600`, gitignored, and must never be committed or pasted into a chat.

The SSH private key lives only on the workstation at `~/.ssh/permission.key`.
There is a copy of its *public* half in `Downloads` from a previous session —
harmless, but it is a stray file that can be deleted.

---

## Rotating the SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/permission.new -C "permission"
ssh-copy-id -i ~/.ssh/permission.new.pub ubuntu@129.225.111.22
ssh -i ~/.ssh/permission.new ubuntu@129.225.111.22 "echo works"
# only once that works:
ssh -i ~/.ssh/permission.key ubuntu@129.225.111.22 \
  "sed -i '/permission$/d' ~/.ssh/authorized_keys"
mv ~/.ssh/permission ~/.ssh/permission.old
```

Keep the old file until the new one is known to work.