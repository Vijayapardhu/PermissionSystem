// The popup: point it at the portal, read the lecturer's classes off the page
// they are already on, and open one class's attendance sheet.
//
// Everything runs through `activeTab` + `scripting` on the tab the lecturer is
// looking at, so the extension cannot read a tab it has not been pointed at, and
// it works against localhost and the deployed host without editing the manifest.

var portalInput = document.getElementById('portal');
var list = document.getElementById('classes');
var empty = document.getElementById('empty');
var emptyText = document.getElementById('empty-text');
var listTitle = document.getElementById('list-title');
var setupHint = document.getElementById('setup-hint');
var setupHost = document.getElementById('setup-host');
var setup = document.getElementById('setup');

var lastTabUrl = '';
var lastClasses = [];

// ---------------------------------------------------------------- helpers

function say(text, tone) {
  setupHint.textContent = text || '';
  setupHint.className = 'hint' + (tone ? ' is-' + tone : '');
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch (error) {
    return '';
  }
}

function stored(key, fallback) {
  return new Promise(function (resolve) {
    chrome.storage.local.get({ [key]: fallback }, function (items) {
      resolve(items[key]);
    });
  });
}

function remember(key, value) {
  var patch = {};
  patch[key] = value;
  chrome.storage.local.set(patch);
}

function portal() {
  return portalInput.value.trim().replace(/\/+$/, '');
}

function onPortal(url) {
  var base = portal();
  return !!base && url.indexOf(base) === 0;
}

function activeTab() {
  return new Promise(function (resolve) {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      resolve(tabs && tabs[0]);
    });
  });
}

function inject(tabId, files) {
  return chrome.scripting.executeScript({
    target: { tabId: tabId }, files: files
  });
}

// ------------------------------------------------------------------ grant

function granted() {
  var base = portal();
  if (!base) return Promise.resolve(false);
  return new Promise(function (resolve) {
    chrome.permissions.contains({ origins: [base + '/*'] },
      function (ok) { resolve(ok); });
  });
}

function paintGrant() {
  granted().then(function (ok) {
    var button = document.getElementById('grant');
    button.innerHTML = ok
      ? '<i class="bi bi-shield-check"></i> Allowed on this site'
      : '<i class="bi bi-shield-check"></i> Allow on this site';
    button.disabled = ok;
    button.classList.toggle('btn-quiet', !ok);
    if (ok) say('The panel will appear on attendance pages here.', 'ok');
  });
}

// ------------------------------------------------------------------- load

async function loadClasses() {
  var tab = await activeTab();
  if (!tab || !tab.url) return say('Open the portal in this tab first.', 'warn');
  if (!/^https?:/.test(tab.url)) {
    return say('This tab is not a web page.', 'warn');
  }
  if (!portal()) {
    return say('Set the portal address first.', 'warn');
  }
  if (!onPortal(tab.url)) {
    return say('This tab is not on ' + portal() + '.', 'warn');
  }
  if (tab.url.indexOf('/faculty/classes') === -1) {
    return say('Open My Classes in the portal, then press the refresh button.',
      'warn');
  }

  try {
    var results = await inject(tab.id, ['classes.js']);
    var classes = (results && results[0] && results[0].result) || [];
    lastClasses = classes;
    render(classes);
    if (!classes.length) {
      emptyText.innerHTML = 'Nothing on that page looks like a class list.';
    }
  } catch (error) {
    say('Could not read the page: ' + error.message, 'error');
  }
}

function render(classes) {
  list.innerHTML = '';
  empty.hidden = classes.length > 0;
  listTitle.textContent = classes.length
    ? classes.length + ' class' + (classes.length === 1 ? '' : 'es')
    : 'Your classes';

  classes.forEach(function (item) {
    var li = document.createElement('li');
    var button = document.createElement('button');
    button.type = 'button';

    var left = document.createElement('span');
    var name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.name || ('Class ' + item.id);
    left.appendChild(name);

    var bits = [item.section, item.year].filter(Boolean).join(' · ');
    if (bits) {
      var meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = bits;
      left.appendChild(meta);
    }

    var count = document.createElement('span');
    count.className = 'count';
    count.textContent = item.students || '';

    button.appendChild(left);
    button.appendChild(count);
    button.title = 'Open attendance for ' + (item.name || '');
    button.addEventListener('click', function () {
      var base = portal().replace(/\/+$/, '');
      chrome.tabs.create({
        url: base + '/faculty/classes/' + item.id + '/attendance'
      });
    });

    li.appendChild(button);
    list.appendChild(li);
  });
}

// ---------------------------------------------------------------- wiring

document.addEventListener('DOMContentLoaded', async function () {
  portalInput.value = await stored('portal', '');

  var tab = await activeTab();
  if (tab && tab.url && /^https?:/.test(tab.url)) {
    lastTabUrl = tab.url;
    if (!portalInput.value) {
      // First run: adopt the tab the user is already sitting on.
      portalInput.value = new URL(tab.url).origin;
      remember('portal', portalInput.value);
    }
  }

  setupHost.textContent = hostOf(portal()) || 'set up';
  paintGrant();
});

document.getElementById('save-portal').addEventListener('click', function () {
  var value = portalInput.value.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(value)) {
    return say('That does not look like an address.', 'error');
  }
  portalInput.value = value;
  remember('portal', value);
  setupHost.textContent = hostOf(value) || 'set up';
  paintGrant();
  say('Saved.', 'ok');
});

document.getElementById('detect').addEventListener('click', async function () {
  var tab = await activeTab();
  if (!tab || !tab.url || !/^https?:/.test(tab.url)) {
    return say('This tab is not a web page.', 'warn');
  }
  var origin = new URL(tab.url).origin;
  portalInput.value = origin;
  remember('portal', origin);
  setupHost.textContent = hostOf(origin) || 'set up';
  paintGrant();
  say('Portal set from this tab.', 'ok');
});

document.getElementById('grant').addEventListener('click', function () {
  var base = portal();
  if (!/^https?:\/\//.test(base)) {
    return say('Set the portal address first.', 'error');
  }
  chrome.permissions.request({ origins: [base + '/*'] }, function (ok) {
    if (!ok) say('Permission was declined.', 'warn');
    paintGrant();
  });
});

document.getElementById('reload').addEventListener('click', loadClasses);

// ------------------------------------------------------------- the inspector

// Runs on whatever the active tab is, including the college's own attendance
// system, which is the whole point: that site is what the extension will
// eventually need to understand, and it cannot be understood from the portal's
// markup. Read-only, and it reports back here rather than acting.
document.getElementById('inspect').addEventListener('click', async function () {
  var tab = await activeTab();
  if (!tab || !tab.url || !/^https?:/.test(tab.url)) {
    return say('Open the page you want inspected in this tab.', 'warn');
  }
  try {
    await inject(tab.id, ['inspect.js']);
    window.close();
  } catch (error) {
    say('Could not read the page: ' + error.message, 'error');
  }
});

document.getElementById('open-classes').addEventListener('click', function () {
  var base = portal();
  if (!base) return say('Set the portal address first.', 'warn');
  chrome.tabs.create({ url: base + '/faculty/classes' });
});

portalInput.addEventListener('keydown', function (event) {
  if (event.key === 'Enter') document.getElementById('save-portal').click();
});
