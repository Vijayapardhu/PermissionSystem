// The universal search box's behaviour, shared by every page that has one.
//
// The bar submits itself: typing is debounced and the list below updates as you
// type, so the search is a control rather than a form to be submitted. That is
// also why the Go button is removed rather than left in place -- see the macro
// in _macros.html.
//
// Everything is opt-in per box through [data-au-search]. A page with a search
// field that must NOT submit itself (a typeahead inside a form, say) simply
// leaves the attribute off.

(function () {
  var DEBOUNCE_MS = 320;

  function ready(fn) {
    if (document.readyState !== 'loading') { fn(); return; }
    document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    var boxes = Array.prototype.slice.call(
      document.querySelectorAll('[data-au-search]'));

    boxes.forEach(function (box) {
      var input = box.querySelector('.au-search__input');
      if (!input) return;

      var form = box.closest('form');
      var go = box.querySelector('.au-search__go');
      var clear = box.querySelector('.au-search__clear');
      var timer = null;

      function submit() {
        if (timer) { clearTimeout(timer); timer = null; }
        if (!form) return;
        // Disabled briefly so a slow page cannot be submitted twice from a
        // fast typist. `requestSubmit` rather than `submit` where available, so
        // the button's own name/value would still be posted if one were needed.
        input.disabled = true;
        if (typeof form.requestSubmit === 'function') {
          form.requestSubmit();
        } else {
          form.submit();
        }
      }

      input.addEventListener('input', function () {
        // Long enough to skip most of a pasted or fast-typed word, short enough
        // that the list does not feel like it is ignoring the keyboard.
        if (timer) clearTimeout(timer);
        timer = setTimeout(submit, DEBOUNCE_MS);
      });

      input.addEventListener('keydown', function (event) {
        // Enter submits now rather than waiting out the debounce, and must not
        // add a newline to a single-line field.
        if (event.key === 'Enter') {
          event.preventDefault();
          submit();
          return;
        }
        // Escape clears, and leaves the field if it was already empty, so it
        // behaves like Escape does everywhere else rather than being a dead key.
        if (event.key === 'Escape' && input.value) {
          event.preventDefault();
          input.value = '';
          input.focus();
          submit();
        }
      });

      if (clear) {
        clear.addEventListener('click', function () {
          input.value = '';
          input.focus();
          submit();
        });
      }

      // The dropdowns sharing this form apply themselves too.
      //
      // They used to carry `onchange="this.form.submit()"` in the markup. That
      // works, but it only exists on the selects that remembered it, so a filter
      // bar is one forgotten attribute away from silently doing nothing -- and
      // it cannot be seen from the template that forgot it. Driven from here
      // instead, every select in a self-submitting form behaves the same way.
      Array.prototype.forEach.call(form ? form.querySelectorAll('select') : [],
        function (select) {
          select.addEventListener('change', submit);
        });

      // Without scripting the Go button is the only way to search. With
      // scripting it is a second, louder way to do something the field already
      // does, so it is removed rather than left to compete with the input.
      if (go) go.hidden = true;
    });
  });
}());