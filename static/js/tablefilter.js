// Filter the rows of any table on the page, without asking the server.
//
// Why in the browser rather than as a query parameter: the rows are already
// here. The lecturer has a table of 68 students open in front of them and wants
// to find one row in it -- that is not a database question, and answering it
// with a round trip means a page reload per keystroke and the scroll position
// lost each time. The student search had exactly that problem, which is why it
// is now a button.
//
// It is honest about its scope. A page whose server caps the rows (the HOD's
// register shows 200 of 1,138) can only be filtered across what it rendered,
// so the count always reads "N of M" rather than pretending to have searched
// everything. Those pages already carry a server-side search box for the full
// set; this is the fast path over what is on screen.
//
// Opt out per card with data-au-filter="off" -- for a print view, a letter, or
// any table where a filter would be noise.
//
// A table with fewer than MIN_ROWS body rows is left alone. A five-row table
// does not need filtering, and a box that cannot usefully narrow anything is
// worse than no box.

(function () {
  'use strict';

  var MIN_ROWS = 6;
  var DEBOUNCE_MS = 120;

  function ready(fn) {
    if (document.readyState !== 'loading') { fn(); return; }
    document.addEventListener('DOMContentLoaded', fn);
  }

  // Fold case and strip punctuation, so "cs 001" matches "25B11CS001" and a
  // name matches a roll number typed in any case.
  // Lowercase, and turn every separator into a space rather than deleting it.
  function flatten(value) {
    return (value || '')
      .toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, ' ');
  }

  // The query as separate words. Every one must be found in a row for that row
  // to match -- AND, not OR, so adding a word always narrows the result and
  // the count never goes up when you type.
  function terms(value) {
    return (value || '')
      .split(/\s+/)
      .map(flatten)
      .map(function (token) { return token.trim(); })
      .filter(Boolean);
  }

  // Text of a row, minus anything inside a cell marked data-au-filter-ignore
  // -- searching for "delete" should not match every row on the page.
  function rowText(row) {
    var clone = row.cloneNode(true);
    Array.prototype.forEach.call(
      clone.querySelectorAll('[data-au-filter-ignore]'),
      function (node) { if (node.parentNode) node.parentNode.removeChild(node); });
    return flatten(clone.textContent);
  }

  function enhance(card) {
    if (card.getAttribute('data-au-filter') === 'off') return;
    if (card.querySelector('[data-au-filter-box]')) return;   // already done

    var table = card.querySelector('table.table-au');
    if (!table) return;

    var body = table.querySelector('tbody');
    if (!body) return;
    var rows = Array.prototype.slice.call(body.rows);
    if (rows.length < MIN_ROWS) return;

    var head = card.querySelector('.card-au__head');
    if (!head) return;

    // The haystack is built once, not per keystroke: on a 200-row table,
    // re-reading the text of every row on every keypress is exactly what makes
    // a client-side filter feel slow.
    var haystack = rows.map(rowText);

    // ---- build the control ---------------------------------------------
    var box = document.createElement('div');
    box.className = 'au-tfilter';
    box.setAttribute('data-au-filter-box', '');

    var input = document.createElement('input');
    input.type = 'search';
    input.className = 'form-control form-control-sm au-tfilter__input';
    input.placeholder = 'Filter these rows';
    input.setAttribute('aria-label', 'Filter the rows in this table');
    input.autocomplete = 'off';
    input.spellcheck = false;

    var clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'au-tfilter__clear';
    clear.title = 'Clear the filter';
    clear.setAttribute('aria-label', 'Clear the filter');
    clear.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
    clear.hidden = true;

    var count = document.createElement('span');
    count.className = 'au-tfilter__count';

    box.appendChild(input);
    box.appendChild(clear);
    box.appendChild(count);
    head.appendChild(box);

    // ---- the filter itself ---------------------------------------------
    function apply() {
      var words = terms(input.value);
      var needle = words.length > 0;
      var shown = 0;
      for (var i = 0; i < rows.length; i++) {
        // AND, not OR: every word has to be on the row. Written as "give up
        // on the first miss" rather than "set true on the first hit" -- the
        // earlier version did the latter behind a !hit early exit, which made
        // two words behave as two alternatives.
        //
        // With no words at all the loop never runs and hit stays true, which is
        // what an empty query should do, so the separate needle check below is
        // only for the wording of the count.
        var hit = true;
        for (var w = 0; w < words.length; w++) {
          if (haystack[i].indexOf(words[w]) === -1) { hit = false; break; }
        }
        rows[i].hidden = !hit;
        if (hit) shown++;
      }

      count.textContent = needle
        ? (shown + ' of ' + rows.length)
        : (rows.length + ' row' + (rows.length === 1 ? '' : 's'));
      count.classList.toggle('au-tfilter__count--none',
                             !!(needle && shown === 0));
      clear.hidden = !input.value;

      // Nothing matched, so say so rather than leaving an apparently blank
      // page that looks like a loading failure.
      var empty = card.querySelector('[data-au-filter-empty]');
      if (empty) empty.hidden = !(needle && shown === 0);
    }

    var timer = null;
    input.addEventListener('input', function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(apply, DEBOUNCE_MS);
    });

    input.addEventListener('keydown', function (event) {
      // Escape clears a non-empty field. On an already-empty one it is left
      // alone, so whatever else claims Escape on the page keeps it.
      if (event.key === 'Escape' && input.value) {
        event.preventDefault();
        event.stopPropagation();
        input.value = '';
        apply();
      }
    });

    clear.addEventListener('click', function () {
      input.value = '';
      apply();
      input.focus();
    });

    apply();
  }

  ready(function () {
    Array.prototype.slice.call(
      document.querySelectorAll('.card-au')).forEach(enhance);

    // A card that arrives after load -- a tab rendered by script, say.
    if (window.MutationObserver) {
      var mo = new MutationObserver(function (records) {
        records.forEach(function (record) {
          Array.prototype.forEach.call(record.addedNodes, function (node) {
            if (node.nodeType !== 1) return;
            if (node.matches && node.matches('.card-au')) {
              enhance(node);
              return;
            }
            if (node.querySelectorAll) {
              Array.prototype.forEach.call(
                node.querySelectorAll('.card-au'), enhance);
            }
          });
        });
      });
      mo.observe(document.body, { childList: true, subtree: true });
    }
  });
}());