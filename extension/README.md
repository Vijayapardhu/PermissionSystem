# Aditya Faculty Assistant

A Chrome extension for the faculty side of the permission portal. It lists your
classes and fills the attendance page from the permissions the portal has
already recorded.

## What it does

**Popup — your classes.** Set the portal address once. Open *My Classes*, press
**Load my classes**, and the popup lists what you teach with student counts.
Clicking one opens that class's attendance sheet for today.

**Attendance page — Mark the excused students.** On any class's attendance page a
panel appears above the table with a single button:

| Button | What it sets |
|---|---|
| **Mark the excused students** | The students the portal shows as **On permission** become *On permission*. Nobody else is touched. |

Before the button is pressed the panel says what pressing it will do — *"Will
mark 3 of 215 students as excused. The other 212 are left alone."* — read from
the same pass over the page the button uses, so the promise and the result
cannot disagree. Afterwards it reports what it actually set — *"Marked 3 of 215
students — 212 present, 0 absent, 3 on permission"*, counting what is now
ticked on the page rather than what it was asked to set.

If no one on the roster has a permission, the button is disabled rather than
offering a no-op.

## What it deliberately does not do

**It only marks permissions.** It has one action, and that action touches only
the students the portal already records as excused. An earlier version also
offered "all present" and "permissions as absent"; both were removed. Setting two
hundred students to *Present* is a much larger thing to be wrong about than
setting the three the portal already agrees are excused, and the panel's own
page buttons already cover the bulk cases.

**It never saves attendance.** Filling only sets the buttons on the form.
Nothing reaches the portal until you read the list and press **Save attendance**
yourself. Deciding that a student was there, and recording that they were, are
two different acts, and an automated second one is not worth the convenience.

**It does not invent a permission.** The panel only ever sets students the
portal itself marks as having an approved permission on that date. If the portal
is wrong, the fix is a permission, not a tick.

## Inspecting a page

The popup has an **Inspect this page** button. It works on any site — including
the college's own attendance system — and is **strictly read-only**: it changes
no checkbox, submits nothing, and says so at the top of its own panel.

It exists because the college's ECAP site (`info.aec.edu.in`) is a system of
record that knows nothing about permissions. To carry a permission across to it,
the extension has to match students by roll number and clear their tick there.
Writing that against an assumption about ECAP's markup would fail silently and
in the worst direction — it would mark real students absent on an official
record, and nothing would look broken.

So: point the extension at the ECAP attendance page, press **Inspect this
page**, and press **Copy report as JSON**. The report says

- how many checkboxes / radios / selects there are, and their shared `name`
- how many rows it found, and how many of those a roll number could be read from
- **which rows are ticked right now**, matched back to roll numbers
- the table headers, and sample row HTML

That is enough to write the ECAP adapter against what the page actually is. Paste
the JSON back and the adapter gets built from it. It reads roll numbers from the
row's visible cells, from common attributes (`data-roll`, `data-usn`, `id`, …)
and from the controls' own `value`, because on these sheets the checkbox often
carries the roll number and nothing visible does.

If it cannot find a roll format it says so and names where it looked, rather
than guessing.

## Installing

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → choose the `extension` folder in this repository.
4. Click the extension's icon. Press **Detect from this tab** while a portal
   page is open, then **Allow on this site**.

That last step is the only permission it ever asks for. Chrome shows which site;
the extension gets nothing on any other one.

## Why it looks at the page instead of calling an API

The portal has no JSON endpoint for a lecturer's classes, and adding one would
widen what a logged-in session can be made to return for the sake of a personal
tool. The class list and the attendance table are already rendered; the
extension reads them.

The trade is that it depends on the page's markup. The selectors it relies on
are:

- `.btn-group[aria-label^="Attendance for"]` — one per student; the label
  carries the roll number.
- `input[name^="att_"]` — the three radios for a student, valued `PRESENT`,
  `ABSENT`, `ON_PERMISSION`.
- `.badge-au--approved` inside a student's row — the portal's own "Approved"
  marker for a permission on that date.
- `.page-head__title` and the breadcrumb — to name the class.

If the attendance page is redesigned, the extension needs a matching change.
There is a test that runs this code against a saved copy of the real page, so a
breakage shows up there rather than in a lecture.

## Files

| File | |
|---|---|
| `manifest.json` | MV3. `activeTab`, `storage`, `scripting`, and one *optional* host permission the user grants. |
| `popup.html` / `popup.css` / `popup.js` | The class list and the permission request. |
| `background.js` | Adds the panel when an attendance page finishes loading, on a granted origin only. |
| `attendance.js` | The panel. Reads the table, sets radios, reports counts. |
| `classes.js` | Reads the class list off the *My Classes* page. |
| `panel.css` | The panel's styling, scoped under its own id, and theme-aware. |

## Permissions, and why each one is needed

| | |
|---|---|
| `activeTab` | Read the portal page in the tab you are looking at. |
| `scripting` | Add the panel and read the class list on that page. |
| `storage` | Remember the portal address you typed. |
| *(optional) host* | Only ever requested for the one portal you type in, and only so the panel can appear on attendance pages without you opening the popup first. |

There is no background script that does anything on a timer, no data sent
anywhere, and nothing stored beyond the portal address.
