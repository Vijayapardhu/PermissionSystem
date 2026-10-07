/* Print-document header filler.
 *
 * Every page behind the app shell shares one print header (see base.html):
 * the university letterhead, the page's own title, and the generation date.
 * The title is read from the page heading at print time rather than stored
 * per page, so a renamed page cannot print under its old name.
 *
 * Dependency-free on purpose: this has to run even when the CDN bundle fails,
 * or the one thing standing between the user and their document is a blocked
 * third-party script.
 */
(function () {
  function fillPrintHeader() {
    var title = document.querySelector('.print-doc-head__title');
    if (title) {
      var heading = document.querySelector('main .page-head__title');
      var text = heading
        ? heading.textContent.replace(/\s+/g, ' ').trim()
        : (document.title || '').split('\u00B7')[0].trim();
      title.textContent = text;
    }
    var stamp = new Date().toLocaleDateString('en-GB', {
      day: '2-digit', month: 'long', year: 'numeric',
    });
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-print-date]'),
      function (el) { el.textContent = stamp; }
    );
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fillPrintHeader);
  } else {
    fillPrintHeader();
  }
  window.addEventListener('beforeprint', fillPrintHeader);
})();
