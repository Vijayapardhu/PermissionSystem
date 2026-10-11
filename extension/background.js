// Adds the attendance panel to a class's attendance page.
//
// Injected only on a URL that is both a portal attendance page and an origin the
// lecturer has explicitly granted. The origin is asked for once, from the popup,
// rather than being declared in the manifest: a lecturer's portal is one host --
// localhost while developing, the deployed address in use -- and blanket
// "http://*/*" access to every site they visit would be a great deal of trust to
// ask for in exchange for reading a table.

var ATTENDANCE_PATH =
  /\/faculty\/classes\/\d+\/attendance(\/|[?#]|$)/;

function originPattern(url) {
  try {
    return new URL(url).origin + '/*';
  } catch (error) {
    return null;
  }
}

chrome.tabs.onUpdated.addListener(function (tabId, info, tab) {
  if (info.status !== 'complete' || !tab || !tab.url) return;
  if (!ATTENDANCE_PATH.test(tab.url)) return;

  var pattern = originPattern(tab.url);
  if (!pattern) return;

  chrome.permissions.contains({ origins: [pattern] }, function (granted) {
    if (!granted) return;

    chrome.scripting.insertCSS({
      target: { tabId: tabId },
      files: ['panel.css']
    }, function () {
      // A failure to inject the stylesheet must not stop the panel: it works
      // without them, just unstyled.
    });

    chrome.scripting.executeScript({
      target: { tabId: tabId },
      files: ['attendance.js']
    });
  });
});
