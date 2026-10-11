// The attendance panel.
//
// The portal's attendance page gives every student three states -- Present,
// Absent, On Permission -- as a radio group, one input per state. This reads the
// page, tells you who the portal already has a permission for, and sets the
// radios to match.
//
// It deliberately does not submit the form. Ticking the boxes is the mechanical
// part of marking attendance; deciding what it means is the lecturer's, and
// nothing is written to the portal until they press Save themselves.

(function () {
  if (window.__auAssistantLoaded) return;
  window.__auAssistantLoaded = true;

  var PANEL_ID = 'au-assistant-panel';

  // ---------------------------------------------------------------- reading

  function attendanceRows() {
    var out = [];
    var rows = document.querySelectorAll('tr');

    Array.prototype.forEach.call(rows, function (row) {
      var group = row.querySelector('.btn-group[aria-label^="Attendance for"]');
      if (!group) return;

      var roll = (group.getAttribute('aria-label') || '')
        .replace('Attendance for', '').trim();
      var nameInput = row.querySelector('input[name^="att_"]');
      if (!nameInput) return;

      // The permission column is the only cell in the row that can hold a
      // badge saying "Approved", so the badge is scoped to the row rather than
      // matched page-wide, where the "linked" column uses the same class.
      var badge = row.querySelector('.badge-au--approved');
      var onPermission = !!badge &&
        /approved/i.test(badge.textContent || '');

      out.push({
        roll: roll,
        group: nameInput.name,
        onPermission: onPermission
      });
    });

    return out;
  }

  function classTitle() {
    // The page heading is "Attendance — T3-ES-II-6840", so the class is what
    // follows the dash. The breadcrumb was the obvious place to look and gives
    // the wrong answer: its active item is "Attendance", the crumb before it is
    // the class, and an item's activeness says which page you are on rather
    // than which class it is about.
    var heading = document.querySelector('.page-head__title');
    if (heading) {
      var text = heading.textContent.trim();
      var dash = text.lastIndexOf('—');
      if (dash > 0) return text.slice(dash + 1).trim();
      return text;
    }

    var crumbs = document.querySelectorAll('.breadcrumb-item');
    if (crumbs.length > 1) {
      return crumbs[crumbs.length - 2].textContent.trim();
    }
    return 'This class';
  }

  function viewDate() {
    var input = document.querySelector('input[name="date"]');
    var raw = (input && input.value) || '';
    if (!raw) {
      var match = (location.search || '').match(/[?&]date=(\d{4}-\d{2}-\d{2})/);
      raw = match ? match[1] : '';
    }
    return prettyDate(raw);
  }

  function prettyDate(iso) {
    if (!iso) return '';
    var parts = iso.split('-');
    if (parts.length !== 3) return iso;
    var date = new Date(Number(parts[0]), Number(parts[1]) - 1,
      Number(parts[2]));
    if (isNaN(date.getTime())) return iso;

    var days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
      'Sep', 'Oct', 'Nov', 'Dec'];
    // Spelled the way the page itself spells it, rather than left as the raw
    // ISO value the URL happens to carry.
    return days[date.getDay()] + ' ' + date.getDate() + ' ' +
      months[date.getMonth()] + ' ' + date.getFullYear();
  }

  // ---------------------------------------------------------------- applying

  function choose(row, option) {
    var input = document.querySelector(
      'input[name="' + row.group + '"][value="' + option + '"]');
    if (!input) return false;
    if (!input.checked) {
      input.checked = true;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return true;
  }

  // Ticking the input is the whole job. Bootstrap's `btn-check` styles the
  // button from `:checked + label`, and the portal already gives each label the
  // right variant (`btn-outline-success` / `-danger` / `-warning`), so the
  // checked radio lights up the correct button on its own.
  //
  // An earlier version recoloured the label here as well. It did nothing --
  // the replacement was the class already there -- and it was one more thing
  // that would break the moment the portal's own markup changed.
  function fillPermissions(rows) {
    var done = 0;
    rows.forEach(function (row) {
      // Only the students the portal itself marks as excused. Everybody else
      // is left exactly as the page had them. The extension's job is to carry
      // the permission across and nothing else, and a control that resets two
      // hundred students to "Present" is a much larger thing to be wrong about
      // than one that marks the three the portal already agrees are excused.
      if (!row.onPermission) return;
      if (choose(row, 'ON_PERMISSION')) done += 1;
    });
    return done;
  }

  function apply() {
    var rows = attendanceRows();
    var counts = { PRESENT: 0, ABSENT: 0, ON_PERMISSION: 0 };
    var done = fillPermissions(rows);

    // Counted from the inputs themselves rather than from what was asked for,
    // so the summary reports the page's real state and not an intention.
    document.querySelectorAll('input[name^="att_"]:checked').forEach(
      function (input) {
        if (counts[input.value] !== undefined) counts[input.value] += 1;
      });

    return { total: rows.length, done: done, counts: counts };
  }

  // ------------------------------------------------------------------ panel

  function build() {
    if (document.getElementById(PANEL_ID)) return;

    var rows = attendanceRows();
    if (!rows.length) return;

    var excused = rows.filter(function (r) { return r.onPermission; }).length;

    var panel = document.createElement('section');
    panel.id = PANEL_ID;
    // Whitespace between the blocks is deliberate. Without it the panel's
    // textContent runs the date into the sentence after it -- "2026-10-103
    // students have" -- which is what a screen reader would read out.
    panel.innerHTML =
      '<div class="au-a__bar">' +
      '<span class="au-a__title"><i class="bi bi-lightning-charge-fill" ' +
      'aria-hidden="true"></i> ' + escapeHtml(classTitle()) + '</span>' +
      ' <span class="au-a__meta">' + rows.length + ' students &middot; ' +
      escapeHtml(viewDate() || 'today') + '</span>' +
      ' </div> ' +
      '<div class="au-a__grid"> ' +
      '<div class="au-a__facts"> ' +
      '<p class="au-a__lede">' +
      (excused
        ? '<span class="au-a__pill au-a__pill--warn">' + excused + '</span> ' +
          'student' + (excused === 1 ? ' has' : 's have') +
          ' an approved permission on this date.'
        : '<span class="au-a__pill au-a__pill--quiet">0</span> ' +
          'students have an approved permission for this date.') +
      '</p> ' +
      '<p class="au-a__outcome" data-au-summary>' + previewOf() + '</p>' +
      ' </div> ' +
      '<div class="au-a__actions">' +
      '<button type="button" class="au-a__btn au-a__btn--primary" ' +
      'data-au-fill>Mark the excused students</button> ' +
      ' </div> ' +
      '</div> ' +
      '<p class="au-a__foot"><i class="bi bi-info-circle" aria-hidden="true">' +
      '</i> This sets the permission students only &mdash; everyone else is ' +
      'left as you had them. Nothing is saved until you press ' +
      '<strong>Save attendance</strong> on the page below.</p>';

    var anchor = document.querySelector('.page-head');
    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(panel, anchor.nextSibling);
    } else {
      document.body.insertBefore(panel, document.body.firstChild);
    }

    var button = panel.querySelector('[data-au-fill]');
    if (excused) button.addEventListener('click', function (event) {
      event.preventDefault();

      var result = apply();
      var summary = panel.querySelector('[data-au-summary]');
      summary.innerHTML =
        'Marked <strong>' + result.done + '</strong> of ' + result.total +
        ' students &mdash; ' + result.counts.PRESENT + ' present, ' +
        result.counts.ABSENT + ' absent, ' + result.counts.ON_PERMISSION +
        ' on permission. Press <strong>Save attendance</strong> below to keep it.';
      // Once it has been done the line reports rather than predicts, and the
      // preview wording has done its job.
      summary.classList.add('is-done');
      button.disabled = true;
      button.textContent = 'Marked';
      button.blur();
    });
    else button.disabled = true;
  }

  // What Fill is about to do, stated before it is done. Counted from the same
  // read the button uses, so the promise on screen and the result on the page
  // cannot disagree.
  function previewOf() {
    var rows = attendanceRows();
    var excused = rows.filter(function (row) {
      return row.onPermission;
    }).length;

    return 'Will mark <strong>' + excused + '</strong> of ' + rows.length +
      ' students as excused. The other ' + (rows.length - excused) +
      ' are left alone.';
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
        "'": '&#39;' }[c];
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
