// inspect.js — read-only reconnaissance of whatever page the lecturer is on.
//
// It exists because the ECAP site (info.aec.edu.in) records attendance without
// knowing anything about permissions, so the extension eventually needs to match
// students by roll number and clear their tick there. The alternative to
// running this is guessing at ECAP's markup, and a wrong guess does not fail
// loudly -- it quietly marks real students absent on a system of record.
//
// So: nothing here writes to the page. It reports what it found and hands back
// JSON, and the adapter is written against that report rather than against an
// assumption. Nothing on the page is modified, submitted, or even focused.

(function () {
  if (document.getElementById('__auInspectorHost')) return;

  var MAX_ROWS_REPORTED = 6;
  var MAX_HTML_REPORTED = 1400;

  // Ordered most specific first: the first pattern that matches is the one
  // reported as the page's roll-number format. Every pattern's hit count is
  // reported too, so a wrong guess is visible instead of silent.
  var ROLL_PATTERNS = [
    { name: '25B11CS003 (session-batch-dept-number)',
      re: /\b\d{2}[A-Z]\d{2}[A-Z]{2,3}\d{3}\b/g },
    { name: '24CS01 (session-dept-number)',
      re: /\b\d{2}[A-Z]{2,4}\d{1,4}\b/g },
    { name: 'CS0031 (dept-number)',
      re: /\b[A-Z]{2,4}\d{3,6}\b/g },
    { name: '16001 (plain digits)',
      re: /\b\d{4,10}\b/g }
  ];

  // ------------------------------------------------------------- gathering

  function controls() {
    return Array.prototype.slice.call(document.querySelectorAll(
      'input[type="checkbox"], input[type="radio"], select'));
  }

  function labelFor(control) {
    if (control.id) {
      var forLabel = document.querySelector('label[for="' + cssEscape(control.id) + '"]');
      if (forLabel) return tidy(forLabel.textContent);
    }
    var wrapping = control.closest('label');
    if (wrapping) return tidy(wrapping.textContent);
    // Some pages put the caption in a sibling cell or span rather than a label.
    var cell = control.closest('td, th, li, div');
    if (cell) {
      var clone = cell.cloneNode(true);
      Array.prototype.forEach.call(clone.querySelectorAll('input, select'),
        function (n) { n.remove(); });
      var text = tidy(clone.textContent);
      if (text) return text.slice(0, 80);
    }
    return '';
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, '\\$&');
  }

  function tidy(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  // The element that represents one student's row. A table row or list item
  // first, because those are the structures a roster actually uses; failing
  // that, the nearest ancestor holding just this one control.
  function rowOf(control) {
    var found = control.closest('tr, li, [role="row"]');
    if (found) return found;
    var node = control.parentElement;
    while (node && node !== document.body) {
      var inside = node.querySelectorAll(
        'input[type="checkbox"], input[type="radio"]');
      if (inside.length === 1) return node;
      node = node.parentElement;
    }
    return control.parentElement;
  }

  function rollIn(text) {
    for (var i = 0; i < ROLL_PATTERNS.length; i++) {
      var re = new RegExp(ROLL_PATTERNS[i].re.source, 'g');
      var hit = text.match(re);
      if (hit && hit.length) {
        return { pattern: ROLL_PATTERNS[i].name, value: hit[0] };
      }
    }
    return null;
  }

  // The text a roll number might be found in.
  //
  // Not row.textContent: cells have no separator between them, so a row reading
  // "1 | 25B11CS003 | ADDAKULA SAI" arrives as "125B11CS003ADDAKULA" and the
  // pattern's trailing \b fails against the name that follows. Cell texts are
  // joined with spaces instead, and the row's own controls are folded in too --
  // on these attendance sheets the checkbox often carries the roll in its value
  // and nowhere else visible.
  // Attributes a row might carry the roll number in, when the visible text does
  // not have it. Read by name rather than by scanning every attribute: a page
  // with an unrelated data-* field full of digits would otherwise hand back a
  // confident, wrong roll number.
  var ROW_ROLL_ATTRS = [
    'data-roll', 'data-rollno', 'data-roll-number', 'data-student-roll',
    'data-usn', 'data-regno', 'data-registration', 'data-id', 'data-key', 'id'
  ];

  function haystack(row, group) {
    var parts = [];
    var cells = row.querySelectorAll('td, th');
    if (cells.length) {
      Array.prototype.forEach.call(cells, function (c) {
        parts.push(tidy(c.textContent));
      });
    } else {
      parts.push(tidy(row.textContent));
    }

    var attrs = ROW_ROLL_ATTRS.map(function (name) {
      return row.getAttribute(name);
    }).filter(Boolean);
    if (attrs.length) parts.push(attrs.join(' '));

    Array.prototype.forEach.call(group || [], function (c) {
      parts.push(c.name || '');
      parts.push(c.id || '');
      parts.push(c.value === undefined ? '' : String(c.value));
    });
    return parts.filter(Boolean).join('   ');
  }

  function describe(control) {
    return {
      type: control.type || control.tagName.toLowerCase(),
      name: control.name || '',
      id: control.id || '',
      value: control.value === undefined ? '' : String(control.value),
      checked: !!control.checked,
      disabled: !!control.disabled,
      label: labelFor(control)
    };
  }

  // ------------------------------------------------------------- the report

  function analyse() {
    var all = controls();
    var report = {
      url: location.href,
      title: tidy(document.title),
      theme: document.documentElement.getAttribute('data-bs-theme') || '',
      controls: {},
      rollNumbers: { tried: [], pattern: null, examples: [] },
      rows: { total: 0, withRoll: 0, ticked: 0, reported: [] },
      tables: [],
      note: ''
    };

    var byType = {};
    all.forEach(function (c) {
      var t = (c.type || 'select').toLowerCase();
      byType[t] = (byType[t] || 0) + 1;
    });
    report.controls = byType;

    // One entry per distinct shape of control. If every student row uses the
    // same name pattern, the adapter can rely on it; if the names vary per
    // student, the adapter has to find rows positionally instead.
    var shapes = {};
    var order = [];
    all.forEach(function (c) {
      var d = describe(c);
      var key = d.type + '|' + d.name.replace(/\d+/g, '#') + '|' + d.label;
      if (!shapes[key]) {
        shapes[key] = { type: d.type, namePattern: d.name, label: d.label,
          count: 0, example: d.name, checkedCount: 0 };
        order.push(key);
      }
      shapes[key].count += 1;
      if (d.checked) shapes[key].checkedCount += 1;
    });
    report.controlShapes = order.map(function (k) { return shapes[k]; });

    // Group the controls into rows.
    var rows = new Map();
    all.forEach(function (c) {
      var row = rowOf(c);
      if (!row) return;
      if (!rows.has(row)) rows.set(row, []);
      rows.get(row).push(c);
    });

    report.rows.total = rows.size;

    var allText = [];
    rows.forEach(function (group, row) {
      var text = tidy(row.textContent);
      var search = haystack(row, group);

      allText.push(search);
      var roll = rollIn(search);

      var ticked = group.some(function (c) { return c.checked; });
      if (ticked) report.rows.ticked += 1;
      if (!roll) return;

      report.rows.withRoll += 1;
      if (report.rows.reported.length < MAX_ROWS_REPORTED) {
        report.rows.reported.push({
          roll: roll.value,
          pattern: roll.pattern,
          rowText: text.slice(0, 200),
          ticked: ticked,
          controls: group.map(describe),
          html: row.outerHTML.slice(0, MAX_HTML_REPORTED)
        });
      }
    });

    // Which roll pattern actually describes this page. Counted over the same
    // haystacks the per-row detection used, so the format reported here and the
    // rolls reported above are guaranteed to agree.
    var joined = allText.join(' ');
    ROLL_PATTERNS.forEach(function (p) {
      var re = new RegExp(p.re.source, 'g');
      var hits = joined.match(re) || [];
      report.rollNumbers.tried.push({ pattern: p.name, matches: hits.length });
      if (!report.rollNumbers.pattern && hits.length >= 3) {
        report.rollNumbers.pattern = p.name;
        report.rollNumbers.examples = Array.from(new Set(hits)).slice(0, 5);
      }
    });

    // What the page already has ticked. This is the question that decides what
    // "fill the permission" has to undo, so it is reported in full.
    report.alreadyTicked = Array.prototype.slice.call(
      document.querySelectorAll('input:checked')).slice(0, 10).map(function (c) {
        var row = rowOf(c);
        var group = row ? rows.get(row) || [c] : [c];
        return {
          roll: row ? (rollIn(haystack(row, group)) || {}).value : null,
          type: c.type,
          name: c.name,
          value: c.value,
          label: labelFor(c)
        };
      });

    Array.prototype.forEach.call(document.querySelectorAll('table'),
      function (table, i) {
        var headers = Array.prototype.map.call(
          table.querySelectorAll('thead th'),
          function (th) { return tidy(th.textContent); });
        report.tables.push({
          index: i,
          headers: headers,
          rows: table.querySelectorAll('tbody tr').length,
          controls: table.querySelectorAll(
            'input[type=checkbox], input[type=radio], select').length
        });
      });

    if (!all.length) {
      report.note = 'No checkbox, radio or select on the page. The roster may ' +
        'be rendered as plain text, or marked absent by clicking something ' +
        'else -- look at the tables below and at the page itself.';
    } else if (!report.rollNumbers.pattern) {
      report.note = 'Controls were found but no roll number could be read ' +
        'from any row -- not the visible text, not the row attributes, not ' +
        'the controls. The roster is probably rendered by script after this ' +
        'ran, or the rolls use a format not in the list. Check the row HTML ' +
        'below and add the pattern to ROLL_PATTERNS in inspect.js.';
    }

    return report;
  }

  // ------------------------------------------------------------------- panel

  function render(report) {
    var host = document.createElement('div');
    host.id = '__auInspectorHost';
    var root = host.attachShadow({ mode: 'open' });

    var style = document.createElement('style');
    style.textContent = [
      ':host{all:initial}',
      '.p{position:fixed;top:12px;right:12px;z-index:2147483647;width:min(560px,calc(100vw - 24px));',
      'max-height:calc(100vh - 24px);display:flex;flex-direction:column;',
      'background:#111a2b;color:#e8edf5;border:1px solid #2c3a52;border-radius:10px;',
      'box-shadow:0 18px 48px -12px rgba(0,0,0,.7);',
      'font:13px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}',
      '.h{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid #2c3a52;background:#182236}',
      '.h b{font-size:13px;font-weight:650;flex:1}',
      '.h span{font-size:11px;color:#93a3ba}',
      '.b{padding:10px 12px;overflow:auto}',
      'dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0}',
      'dt{color:#93a3ba}',
      'dd{margin:0;font-variant-numeric:tabular-nums;word-break:break-word}',
      '.ok{color:#6ee7b7}.warn{color:#fbbf24}.bad{color:#fca5a5}',
      '.n{margin:9px 0 3px;font-size:10px;font-weight:700;letter-spacing:.08em;',
      'text-transform:uppercase;color:#7d8ca3;border-top:1px solid #2c3a52;padding-top:8px}',
      'pre{margin:0;padding:7px 9px;background:#0b1220;border:1px solid #232f45;border-radius:6px;',
      'font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;',
      'white-space:pre-wrap;word-break:break-word;max-height:210px;overflow:auto;color:#b9c6da}',
      '.f{display:flex;gap:7px;padding:9px 12px;border-top:1px solid #2c3a52;background:#182236}',
      'button{font:inherit;font-size:12px;font-weight:600;padding:6px 11px;border-radius:6px;',
      'border:1px solid #3a4a66;background:#243352;color:#e8edf5;cursor:pointer}',
      'button.pri{background:#2f6fd0;border-color:#2f6fd0;color:#fff}',
      'button:hover{filter:brightness(1.12)}',
      '.hint{font-size:11px;color:#93a3ba;margin:0 0 8px}'
    ].join('');

    var body = document.createElement('div');
    body.className = 'b';

    // First thing read, because it is the thing that has to be true: this
    // panel is looking, not touching.
    var hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = 'Read-only. Nothing on this page was changed.';
    body.appendChild(hint);

    var summary = document.createElement('dl');
    body.appendChild(summary);

    function row(label, value, cls) {
      var dt = document.createElement('dt');
      dt.textContent = label;
      var dd = document.createElement('dd');
      if (cls) dd.className = cls;
      dd.textContent = value;
      summary.appendChild(dt);
      summary.appendChild(dd);
    }

    function section(text) {
      var h = document.createElement('div');
      h.className = 'n';
      h.textContent = text;
      body.appendChild(h);
    }

    function pre(content) {
      var el = document.createElement('pre');
      el.textContent = content;
      body.appendChild(el);
      return el;
    }

    row('Page', report.title || '(no title)');
    row('Checkboxes', String(report.controls.checkbox || 0));
    row('Radios', String(report.controls.radio || 0));
    row('Selects', String(report.controls.select || 0));
    row('Rows found', String(report.rows.total));
    row('Rows with a roll number', String(report.rows.withRoll),
      report.rows.withRoll ? 'ok' : 'warn');
    row('Rows already ticked', String(report.rows.ticked),
      report.rows.ticked ? 'warn' : 'ok');
    row('Roll format', report.rollNumbers.pattern || 'no pattern matched',
      report.rollNumbers.pattern ? 'ok' : 'warn');
    if (report.rollNumbers.examples.length) {
      row('Examples', report.rollNumbers.examples.join(', '));
    }

    if (report.note) {
      var note = document.createElement('p');
      note.className = 'hint';
      note.style.color = '#fbbf24';
      note.textContent = report.note;
      body.appendChild(note);
    }

    if (report.controlShapes && report.controlShapes.length) {
      section('Control shapes');
      pre(report.controlShapes.map(function (s) {
        return s.count + ' x  ' + s.type +
          (s.label ? '  "' + s.label + '"' : '') +
          '\n        name: ' + (s.namePattern || '(none)') +
          '\n        example: ' + (s.example || '(none)') +
          '   ticked now: ' + s.checkedCount;
      }).join('\n'));
    }

    if (report.alreadyTicked && report.alreadyTicked.length) {
      section('Already ticked');
      pre(report.alreadyTicked.map(function (t) {
        return (t.roll || '?') + '  ' + t.type + '  ' + (t.label || t.name);
      }).join('\n'));
    }

    if (report.tables && report.tables.length) {
      section('Tables');
      pre(report.tables.map(function (t) {
        return '#' + t.index + '  ' + t.rows + ' rows, ' + t.controls +
          ' controls\n        ' + (t.headers.join(' | ') || '(no headers)');
      }).join('\n'));
    }

    if (report.rows.reported.length) {
      section('Sample rows (HTML)');
      pre(report.rows.reported.slice(0, 2).map(function (r) {
        return r.html;
      }).join('\n\n---\n\n'));
    }

    var footer = document.createElement('div');
    footer.className = 'f';

    var copy = document.createElement('button');
    copy.className = 'pri';
    copy.textContent = 'Copy report as JSON';
    copy.addEventListener('click', function () {
      var text = JSON.stringify(report, null, 2);
      var done = function () {
        copy.textContent = 'Copied -- paste it back';
        setTimeout(function () { copy.textContent = 'Copy report as JSON'; },
          2500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, showRaw);
      } else {
        showRaw();
      }

      function showRaw() {
        // Clipboard access is refused often enough (insecure origin, no
        // permission) that having a way out is worth the few lines.
        var raw = document.createElement('pre');
        raw.textContent = text;
        raw.style.maxHeight = '40vh';
        body.insertBefore(raw, footer);
      }
    });

    var close = document.createElement('button');
    close.textContent = 'Close';
    close.addEventListener('click', function () { host.remove(); });

    footer.appendChild(copy);
    footer.appendChild(close);

    root.appendChild(style);
    var head = document.createElement('div');
    head.className = 'h';
    var title = document.createElement('b');
    title.textContent = 'Faculty assistant -- page inspector';
    var sub = document.createElement('span');
    sub.textContent = 'read-only';
    head.appendChild(title);
    head.appendChild(sub);

    // The panel's own box. Everything above hangs off this, and the .p rule is
    // what positions and sizes it -- the host div alone is transparent and has
    // no layout of its own.
    var panel = document.createElement('div');
    panel.className = 'p';
    panel.appendChild(head);
    panel.appendChild(body);
    panel.appendChild(footer);

    root.appendChild(panel);
    document.documentElement.appendChild(host);
  }

  // Exposed so the jsdom tests can drive the analysis against saved pages
  // without a shadow root in the way.
  window.__auInspect = analyse;

  try {
    render(analyse());
  } catch (error) {
    // A page this hostile should still leave the user with something.
    var plain = document.createElement('pre');
    plain.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;' +
      'max-width:520px;max-height:60vh;overflow:auto;background:#111a2b;color:#e8edf5;' +
      'border:1px solid #2c3a52;border-radius:8px;padding:12px;font:12px/1.5 monospace';
    plain.textContent = 'Inspector could not render: ' + error.message +
      '\n\n' + JSON.stringify(analyse(), null, 2);
    document.documentElement.appendChild(plain);
  }
}());