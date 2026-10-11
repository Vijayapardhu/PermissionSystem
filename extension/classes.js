// Reads the class list off the portal's own "My Classes" page.
//
// Injected rather than fetched. The portal has no JSON endpoint for this, and
// adding one would widen what a logged-in session can be made to return for the
// sake of a personal tool. The page already renders the list; this reads it.
//
// Returns [] rather than throwing when the page is not the class list, so the
// popup can say "open your classes page first" instead of showing an error.

function harvestClasses() {
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr'));

  var out = [];
  var seen = {};

  rows.forEach(function (row) {
    // The detail link, but not the edit link that now lives beside it.
    var link = row.querySelector('a[href*="/faculty/classes/"]');
    if (!link) return;

    var match = link.getAttribute('href').match(
      /\/faculty\/classes\/(\d+)(\/|$)/);
    if (!match) return;

    var id = match[1];
    if (seen[id]) return;
    seen[id] = true;

    var cells = Array.prototype.slice.call(row.querySelectorAll('td'));
    var text = function (index) {
      var cell = cells[index];
      return cell ? cell.textContent.trim() : '';
    };

    out.push({
      id: id,
      name: cells.length ? (cells[1] || cells[0]).textContent.trim() : '',
      section: text(2),
      year: text(3),
      students: text(4)
    });
  });

  return out;
}

harvestClasses();
