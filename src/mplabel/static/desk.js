/* mplabel desk — the laptop portal.
 *
 * The phone app is the shipping tool and stays that way. This is the work
 * a phone is bad at: bulk-editing forty shelf rows, writing listing copy
 * with the photographs big enough to judge, reading month-end numbers
 * side by side, and getting old sales in from a spreadsheet.
 *
 * Same server, same origin, same session cookie, same `/api` surface -
 * and esc()/api()/money()/due()/split() come from common.js, because the
 * two clients render the same rows out of the same endpoints and a second
 * copy of the escape function is one that gets fixed in only one place.
 *
 * Three departures from app.js, all because this screen is denser:
 *
 * 1. A screen registry, not an if/else chain. The sidebar, the router
 *    and render() all read SCREENS, so a screen cannot exist in one and
 *    not the others - which is a nav row that highlights and shows the
 *    wrong body.
 * 2. Region rendering. app.js replaces the whole body and restores focus
 *    by id afterwards. Here there is a search box, a bulk field and a
 *    description textarea being typed into while data reloads behind
 *    them, so the shell is built once and each region owns its node.
 * 3. Hash routes, so the back button works and a screen can be
 *    bookmarked. There is one window and no tab bar to lose your place in.
 *
 * Loop variables are single letters on purpose: the escaping test finds
 * unescaped interpolation with a regex over [odpabstm], and naming one
 * `order` does not fail that test, it makes it stop looking.
 */

var S = {
  screen: 'home',
  theme: savedTheme(),
  authed: false, loginError: '', busy: false,
  error: '',

  /* Today */
  orders: null, pending: null, trips: null, stats: null, drafts: null,
  system: null,

  /* To ship */
  pick: null, detail: null, labelUrl: null,

  /* Print a label. The file never leaves the page until she asks: it is
     the one thing here that cannot be re-fetched, and a half-remembered
     one across a reload would print something she did not pick. */
  send: null, sendUrl: null, sendShot: null,

  /* Inventory */
  inv: null, invQ: '', invTotal: 0, sort: 'title', dir: 1, sel: [],
  bulk: null, bulkVal: '', bins: null,

  /* Listing writer */
  writerId: null, draft: null, shots: null,
  draftTitle: '', draftBody: '', dirty: false,

  /* Import. The file stays here: the server parses it twice rather than
     holding a half-finished upload between two requests. */
  csvStep: 0, csvText: '', csvName: '', csvMapping: null, csvPlan: null,

  /* Overlays */
  confirm: null, toast: null
};

var root = document.getElementById('desk');

MP.onUnauthorized = function () {
  S.authed = false;
  render();
};

/* ------------------------------------------------------------- screens */

/* One list. The sidebar reads it for its rows, the router reads it for
   which slugs are real, and render() reads it for the body. */
var SCREENS = [
  { key: 'home',      slug: 'today',     label: 'Today',          view: viewToday },
  { key: 'queue',     slug: 'queue',     label: 'To ship',        view: viewQueue },
  { key: 'send',      slug: 'print',     label: 'Print a label',  view: viewSend },
  { key: 'inventory', slug: 'inventory', label: 'Inventory',      view: viewInventory },
  { key: 'writer',    slug: 'writer',    label: 'Listing writer', view: viewWriter },
  { key: 'review',    slug: 'review',    label: 'Month-end',      view: viewReview },
  { key: 'import',    slug: 'import',    label: 'Import sales',   view: viewImport }
];

function screenFor(key) {
  for (var i = 0; i < SCREENS.length; i++) {
    if (SCREENS[i].key === key) return SCREENS[i];
  }
  return SCREENS[0];
}

function slugToKey(slug) {
  for (var i = 0; i < SCREENS.length; i++) {
    if (SCREENS[i].slug === slug) return SCREENS[i].key;
  }
  return 'home';
}

/* ------------------------------------------------------------ plumbing */

function toast(msg) {
  S.toast = msg;
  renderOverlays();
  clearTimeout(toast._t);
  toast._t = setTimeout(function () { S.toast = null; renderOverlays(); }, 4000);
}

/* Printing and marking shipped ask first. The phone holds a button for
   800ms; a mouse makes press-and-hold awkward, and both actions are hard
   to undo - paper is spent, and a wrongly-shipped flag hides a parcel
   that still has to go out. */
function ask(title, body, cta, fn) {
  S.confirm = { title: title, body: body, cta: cta, fn: fn };
  renderOverlays();
}

function runConfirm() {
  var c = S.confirm;
  S.confirm = null;
  renderOverlays();
  if (c && c.fn) c.fn();
}

function cancelConfirm() {
  S.confirm = null;
  renderOverlays();
}

/* One mutation at a time. Two prints from a double-click is two labels. */
async function once(fn) {
  if (S.busy) return;
  S.busy = true;
  try {
    await fn();
    S.error = '';
  } catch (e) {
    if (e.message !== '401') { S.error = e.message; toast(e.message); }
  } finally {
    S.busy = false;
    render();
  }
}

function toggleTheme() {
  S.theme = S.theme === 'dark' ? 'light' : 'dark';
  applyTheme(S.theme);
  /* The button offers the theme you are not in, and the mark is drawn in
     ink that has to invert with it - so the sidebar has to be redrawn.
     Leaving it out left a button in light mode still saying "Light". */
  renderNav();
}

/* -------------------------------------------------------------- format */

function dash(v) { return (v === null || v === undefined || v === '') ? '—' : v; }

/* Her titles run long and share their opening, so a cut with nothing
   marking it reads as a broken string rather than as a truncation. */
function clip(text, n) {
  var t = String(text || '');
  return t.length > n ? t.slice(0, n - 1).replace(/[ ,]+$/, '') + '…' : t;
}

/* "1 trips have no till total" is the kind of sentence that makes a
   screen read as generated rather than written, and she is the only
   person who ever sees it. */
function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : (many || one + 's'));
}

function days(v) {
  return (v === null || v === undefined) ? '—' : v + 'd';
}

function whole(n) {
  if (n === null || n === undefined || n === '') return '—';
  return '$' + Math.round(Number(n)).toLocaleString();
}

function monthName(ym) {
  if (!ym) return '';
  var parts = String(ym).split('-');
  var names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return names[Number(parts[1]) - 1] || ym;
}

function today() {
  var now = new Date();
  var wd = ['Sunday', 'Monday', 'Tuesday', 'Wednesday',
            'Thursday', 'Friday', 'Saturday'][now.getDay()];
  var mo = ['January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November',
            'December'][now.getMonth()];
  return wd + ', ' + mo + ' ' + now.getDate();
}

/* ---------------------------------------------------------- data loads */

async function loadQueue() {
  var d = await api('/api/orders');
  S.orders = d.orders || [];
  if (S.pick === null && S.orders.length) S.pick = S.orders[0].id;
}

async function loadPending() {
  var d = await api('/api/pending');
  S.pending = d.pending || [];
}

async function loadTrips() {
  var d = await api('/api/trips');
  S.trips = d.trips || [];
}

async function loadStats() {
  S.stats = await api('/api/stats');
}

async function loadInventory() {
  var d = await api('/api/inventory?limit=500&q=' + encodeURIComponent(S.invQ));
  S.inv = d.items || [];
  /* The server does the filtering, so a filtered response cannot say how
     many there are in total - and "6 of 6 matching" is a count comparing
     a number with itself. Remember the total from whenever the query was
     last empty, which includes the first load. */
  if (!S.invQ) S.invTotal = S.inv.length;
}

async function loadDrafts() {
  var d = await api('/api/inventory?state=draft&limit=200');
  /* Oldest first. The list endpoint orders by the newest date a row
     has, and a draft has none of them - so it falls through to title,
     descending, and the queue she works through comes out in reverse
     alphabetical order for no reason anyone could see. Row id is
     creation order, which is the order they were photographed in. */
  S.drafts = (d.items || []).sort(function (x, y) { return x.id - y.id; });
  if (S.writerId === null && S.drafts.length) S.writerId = S.drafts[0].id;
}

async function loadDraftPhotos(id) {
  var d = await api('/api/inventory/' + id + '/photos');
  S.shots = d.photos || [];
}

async function loadBins() {
  var d = await api('/api/bins');
  S.bins = d.bins || [];
}

async function loadSystem() {
  S.system = await api('/api/system');
}

async function loadDetail(id) {
  S.detail = await api('/api/orders/' + id);
  releaseLabel();
  /* The PDF needs the session, and a <embed src> cannot carry one - it
     would open to a 401. Fetch it and hand the viewer a blob instead. */
  if (S.detail.has_label) {
    try {
      var res = await fetch('/api/orders/' + id + '/label',
                            { credentials: 'same-origin' });
      if (res.ok) S.labelUrl = URL.createObjectURL(await res.blob());
    } catch (e) { /* the panel says "no label" on its own */ }
  }
}

function releaseLabel() {
  if (S.labelUrl) { URL.revokeObjectURL(S.labelUrl); S.labelUrl = null; }
}

/* What a screen needs before it can draw anything. Kept beside the
   registry rather than inside each view, because a view that fetches
   while rendering fetches on every keystroke. */
async function loadScreen(key) {
  if (key === 'home') {
    await Promise.all([loadQueue(), loadPending(), loadTrips(), loadStats(),
                       loadDrafts()]);
  } else if (key === 'queue') {
    await loadQueue();
    if (S.pick !== null) await loadDetail(S.pick);
  } else if (key === 'inventory') {
    await Promise.all([loadInventory(), loadBins()]);
  } else if (key === 'writer') {
    await loadDrafts();
    if (S.writerId !== null) await openDraft(S.writerId);
  } else if (key === 'review') {
    await Promise.all([loadStats(), loadTrips()]);
  }
}

/* The draft being edited is read fresh rather than taken out of the
   list: the list query does not carry `description`, and showing a blank
   body for a draft that has one would read as the text having been
   lost. */
async function openDraft(id) {
  var d = await api('/api/inventory/' + id);
  S.draft = d.item;
  S.draftTitle = S.draft.title || '';
  S.draftBody = S.draft.description || '';
  S.dirty = false;
  await loadDraftPhotos(id);
}

/* ------------------------------------------------------------- routing */

function go(key) {
  var slug = screenFor(key).slug;
  if (location.hash !== '#/' + slug) {
    location.hash = '#/' + slug;   /* onhashchange picks it up */
    return;
  }
  enter(key);
}

function enter(key) {
  S.screen = key;
  S.bulk = null;
  S.confirm = null;
  render();
  once(function () { return loadScreen(key); });
}

function onHash() {
  var slug = (location.hash || '').replace(/^#\/?/, '');
  var key = slugToKey(slug);
  if (key !== S.screen || S.inv === null) enter(key);
}

/* ---------------------------------------------------------- the shell */

function render() {
  document.documentElement.setAttribute('data-mp', S.theme);
  if (!S.authed) { root.innerHTML = viewLogin(); return; }
  if (!root.querySelector('.shell')) {
    root.innerHTML =
      '<div class="shell">' +
        '<nav class="side" id="side"></nav>' +
        '<div class="main" id="main"></div>' +
        '<div id="over"></div>' +
      '</div>';
  }
  renderNav();
  renderMain();
  renderOverlays();
}

/* innerHTML drops focus and the caret with it, and this screen has three
   fields that are typed into while data reloads behind them - the search
   box, the bulk field and the description. Restoring here rather than in
   each caller is what stops a later, broader render() undoing a restore
   an earlier one had already done: the search box lost its focus that
   way, which reads as a keyboard that stops after one letter. */
function renderMain() {
  var el = document.getElementById('main');
  if (!el) return;
  var was = document.activeElement;
  var id = was && was.id;
  var at = null;
  try { at = was && was.selectionStart; } catch (e) { at = null; }

  el.innerHTML = screenFor(S.screen).view();

  if (!id) return;
  var back = document.getElementById(id);
  if (!back || back === document.activeElement) return;
  back.focus();
  if (at !== null && back.setSelectionRange) {
    try { back.setSelectionRange(at, at); } catch (e) { /* not a text field */ }
  }
}

function mark(size) {
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 48 48"' +
    ' style="flex:none"><rect width="48" height="48" rx="10" fill="' +
    (S.theme === 'dark' ? '#f4efe6' : '#1b180f') + '"></rect>' +
    '<circle cx="12" cy="24" r="3.2" fill="#3fa148"></circle>' +
    '<path d="M12 24 L35 24 M28 17 L35 24 L28 31" stroke="' +
    (S.theme === 'dark' ? '#1b180f' : '#fbf8f2') + '" stroke-width="3"' +
    ' stroke-linecap="round" stroke-linejoin="round" fill="none"></path></svg>';
}

function badges() {
  var open = S.orders ? S.orders.length : 0;
  var unlabeled = unlabeledOrders().length;
  var drafts = S.drafts ? S.drafts.length : 0;
  var owed = homeless().length;
  return {
    home: { n: (unlabeled ? 1 : 0) + (owed ? 1 : 0) + (drafts ? 1 : 0),
            cls: unlabeled ? 'badge--alarm' : 'badge--warn' },
    queue: { n: open, cls: unlabeled ? 'badge--alarm' : '' },
    writer: { n: drafts, cls: '' }
  };
}

function renderNav() {
  var el = document.getElementById('side');
  if (!el) return;
  var b = badges();
  var rows = SCREENS.map(function (s) {
    var key = s.key;
    var on = key === S.screen;
    var badge = b[key];
    return '<button class="nav' + (on ? ' nav--on' : '') +
      '" onclick="go(\'' + key + '\')">' +
      '<span class="nav__label">' + esc(s.label) + '</span>' +
      (badge && badge.n
        ? '<span class="badge ' + badge.cls + '">' + badge.n + '</span>'
        : '') +
      '</button>';
  }).join('');

  el.innerHTML =
    '<div class="side__mark">' + mark(22) +
      '<span class="side__name">mplabel</span></div>' +
    rows +
    '<div class="side__foot">' + printerFoot() +
      '<button class="btn btn--quiet" onclick="toggleTheme()" ' +
        'style="padding:6px 10px;font-size:12px">' +
        (S.theme === 'dark' ? 'Light' : 'Dark') + '</button>' +
      /* `?ui=phone` and not `/`. On a laptop the bare link is a button
         that bounces straight back here, because the auto-route would
         send this browser to the desk again. The parameter is the
         person overruling it, and it is remembered. */
      '<a href="/?ui=phone" style="font-size:12px">The phone app →</a>' +
    '</div>';
}

/* The design draws a green dot reading "Printer ready". This printer
   cannot say that: the G4 is write-only, so a failed print cannot be
   detected in software and there is no readiness to report. What there
   is, is when something last came out - and, on a split deployment,
   whether printd answers at all. */
function printerFoot() {
  var s = S.system;
  if (!s) return '<p class="muted">Asking the printer…</p>';
  var known = s.printer_reachable !== undefined;
  var cls = !known ? '' : (s.printer_reachable ? 'dot--ok' : 'dot--bad');
  var word = !known
    ? esc(s.backend || 'printer')
    : (s.printer_reachable ? 'printd answers' : 'printd is not answering');
  return '<div class="side__state"><span class="dot ' + cls + '"></span>' +
    '<span style="font-size:12px;color:var(--mut)">' + word + '</span></div>' +
    '<div class="side__meta">' +
      (s.last_printed_at
        ? 'last printed ' + esc(String(s.last_printed_at).slice(0, 16))
        : 'nothing printed yet') +
      '<br>poll ' + esc(String(s.poll_seconds || '—')) + 's</div>';
}

/* --------------------------------------------------------- the overlays */

function renderOverlays() {
  var el = document.getElementById('over');
  if (!el) return;
  var out = '';
  if (S.confirm) {
    out +=
      '<div class="scrim" onclick="cancelConfirm()">' +
        '<div class="dialog" onclick="event.stopPropagation()">' +
          '<h2>' + esc(S.confirm.title) + '</h2>' +
          '<p>' + esc(S.confirm.body) + '</p>' +
          '<div class="dialog__acts">' +
            '<button class="btn btn--quiet" onclick="cancelConfirm()">Cancel</button>' +
            '<button class="btn btn--primary" onclick="runConfirm()">' +
              esc(S.confirm.cta) + '</button>' +
          '</div>' +
        '</div>' +
      '</div>';
  }
  if (S.toast) {
    out += '<div class="toast"><div class="toast__box">' +
      '<span class="dot dot--ok"></span>' + esc(S.toast) + '</div></div>';
  }
  el.innerHTML = out;
}

/* ---------------------------------------------------------------- login */

function viewLogin() {
  return '<div class="login"><form onsubmit="submitLogin(event)">' +
    '<div style="display:flex;align-items:center;gap:10px">' + mark(28) +
      '<span class="display display--sm" style="margin:0">mplabel — desk</span>' +
    '</div>' +
    (S.loginError ? '<div class="err">' + esc(S.loginError) + '</div>' : '') +
    '<input class="input" id="pw" type="password" autocomplete="current-password" ' +
      'placeholder="Password" autofocus>' +
    '<button class="btn btn--primary" type="submit">Sign in</button>' +
    '</form></div>';
}

function submitLogin(ev) {
  ev.preventDefault();
  var pw = document.getElementById('pw').value;
  once(async function () {
    try {
      await api('/api/login', { method: 'POST', body: { password: pw } });
      S.authed = true;
      S.loginError = '';
      onHash();
    } catch (e) {
      S.loginError = 'That password was not right.';
    }
  });
}

/* ----------------------------------------------------------- shared bits */

/* An open parcel with no label cannot ship, and that is the loudest fact
   on any screen it appears on. `has_label` and `printed` are different
   questions: a local pickup has no file and never will, a
   recorded-but-unprinted one has a file waiting. */
function unlabeledOrders() {
  return (S.orders || []).filter(function (o) { return !o.printed; });
}

/* Money that came out of a till and has not been attached to anything.
   `unassigned` is null rather than zero when nobody wrote the receipt
   total down - "nothing left to attribute" and "we never recorded what
   the till said" are different answers and only one of them is a job. */
function homeless() {
  return (S.trips || []).filter(function (t) {
    return t.unassigned !== null && t.unassigned !== undefined &&
           Number(t.unassigned) > 0;
  });
}

function untotalled() {
  return (S.trips || []).filter(function (t) {
    return t.unassigned === null || t.unassigned === undefined;
  });
}

/* How much of what sold has a cost against it. Every profit figure on
   this portal is qualified by this, because `net` over two costed
   listings out of ninety is not a month's profit. */
function costCover() {
  var c = S.stats && S.stats.cost;
  if (!c || !c.sold) return { sold: 0, costed: 0, any: false, frac: 0 };
  return { sold: c.sold, costed: c.costed || 0, any: (c.costed || 0) > 0,
           frac: (c.costed || 0) / c.sold };
}

/* --------------------------------------------------------------- today */

function viewToday() {
  if (S.orders === null) return '<div class="pane"><p class="muted">Loading…</p></div>';

  var unlabeled = unlabeledOrders();
  var dueNow = (S.orders || []).filter(function (o) {
    return o.ship_by && due(o.ship_by).cls === 'due--now';
  });
  var owed = homeless();
  var owedTotal = owed.reduce(function (n, t) { return n + Number(t.unassigned); }, 0);
  var drafts = S.drafts || [];

  var cards = [];

  cards.push(debtCard({
    n: 1,
    tone: unlabeled.length ? 'alarm' : '',
    done: !unlabeled.length && !dueNow.length,
    title: dueNow.length
      ? plural(dueNow.length, 'parcel') + ' due today'
      : (S.orders.length ? plural(S.orders.length, 'parcel') + ' open'
                         : 'Nothing waiting to go out'),
    body: unlabeled.length
      ? (unlabeled.length === 1
          ? 'One open parcel still has no label. Print before anything else.'
          : unlabeled.length + ' open parcels still have no label. ' +
            'Print before anything else.')
      : 'Everything open has been printed. Pack and mark them out.',
    cta: 'Open the queue', key: 'queue',
    items: (S.orders || []).slice(0, 3).map(function (o) {
      return { code: o.code, text: o.item, bad: !o.printed };
    })
  }));

  var blind = untotalled().length;
  cards.push(debtCard({
    n: 2,
    tone: owed.length ? 'warn' : '',
    done: !owed.length,
    title: owed.length ? money(owedTotal) + ' with no home'
                       : 'Every receipt is attributed',
    body: owed.length
      ? plural(owed.length, 'trip has', 'trips have') +
        ' money the receipt paid for that is not on any item yet. ' +
        'Until it lands, every profit number here is optimistic.'
      : (blind
          ? plural(blind, 'trip has', 'trips have') + ' no till total ' +
            'recorded, so there is nothing to attribute against — which is ' +
            'not the same as nothing being owed.'
          : 'Nothing is waiting to be attributed.'),
    cta: owed.length ? 'Assign it' : 'Month-end', key: 'review',
    items: []
  }));

  cards.push(debtCard({
    n: 3,
    tone: '',
    done: !drafts.length,
    title: drafts.length ? plural(drafts.length, 'draft') + ' to finish'
                         : 'No drafts waiting',
    body: drafts.length
      ? 'Photographed and costed, but never written up. They earn nothing ' +
        'sitting here.'
      : 'Nothing has been photographed and left unwritten.',
    cta: 'Start writing', key: 'writer',
    items: drafts.slice(0, 3).map(function (d) {
      return { code: d.bin_code || '—', text: d.title, bad: false };
    })
  }));

  return '<div class="pane">' +
    (S.error ? '<div class="err">' + esc(S.error) + '</div>' : '') +
    '<div class="today__head">' +
      '<div style="flex:1;min-width:0">' +
        '<div class="eyebrow">' + esc(today()) + '</div>' +
        '<h1 class="display display--xl">' + esc(headline(cards.length)) + '</h1>' +
      '</div>' + keptTile() +
    '</div>' +
    '<div class="debts">' + cards.join('') + '</div>' +
    monthStrip() +
  '</div>';
}

function headline(n) {
  var jobs = 0;
  if (unlabeledOrders().length) jobs++;
  if (homeless().length) jobs++;
  if ((S.drafts || []).length) jobs++;
  if (!jobs) return 'Nothing needs you';
  if (jobs === 1) return 'One thing needs you';
  return (jobs === 2 ? 'Two' : 'Three') + ' things need you';
}

/* The design puts a "kept this month" figure at the top. It is only an
   honest number when the month's sales are actually costed, so the tile
   says how many are rather than showing a net that is really a subtotal
   of two rows. */
function keptTile() {
  var m = S.stats && S.stats.monthly && S.stats.monthly[0];
  var cover = costCover();
  if (!m) return '';
  if (!cover.any) {
    return '<div class="today__kept"><b style="color:var(--mut)">' +
      whole(m.gross) + '</b><span>gross this month · nothing costed</span></div>';
  }
  return '<div class="today__kept"><b>' + whole(m.net) + '</b>' +
    '<span>kept on ' + (m.costed || 0) + ' of ' +
    plural(m.orders || 0, 'sale') + '</span></div>';
}

function debtCard(c) {
  var tone = c.tone ? ' debt--' + c.tone : '';
  /* A card with nothing owing keeps its button, quietly. Removing it
     would make the row jump around as the day goes; making it loud
     would send her to a screen with nothing on it. */
  var cls = c.tone === 'alarm' ? 'btn btn--alarm'
          : c.done ? 'btn btn--quiet' : 'btn';
  return '<div class="debt' + tone + '">' +
    '<div class="debt__top">' +
      '<div class="debt__n">' + c.n + '</div>' +
      '<div class="debt__body"><h3>' + esc(c.title) + '</h3>' +
        '<p>' + esc(c.body) + '</p></div>' +
      '<button class="' + cls + '" onclick="go(\'' + c.key + '\')">' +
        esc(c.cta) + '</button>' +
    '</div>' +
    (c.items.length
      ? '<div class="debt__list">' + c.items.map(function (a) {
          return '<span><b style="color:' +
            (a.bad ? 'var(--al)' : 'var(--mut)') + '">' + esc(a.code) +
            '</b> ' + esc(clip(a.text, 34)) + '</span>';
        }).join('') + '</div>'
      : '') +
  '</div>';
}

function monthStrip() {
  var m = S.stats && S.stats.monthly && S.stats.monthly[0];
  if (!m) return '';
  /* Not `aging.length`. That view is served with a LIMIT, so its row
     count is the limit and not a fact about the shelf - a number that
     looks like data and is really a query parameter. The bands are a
     full GROUP BY over every listing, so they can be counted. */
  var bands = S.stats.price_bands || [];
  var listed = bands.reduce(function (n, b) { return n + Number(b.listed || 0); }, 0);
  var sold = bands.reduce(function (n, b) { return n + Number(b.sold || 0); }, 0);
  return '<div style="margin-top:30px;max-width:860px"><div class="card">' +
    '<div class="label">This month</div><div class="figs">' +
      fig(m.orders, m.orders === 1 ? 'sold' : 'sold') +
      fig(m.avg_days_to_sell === null ? '—' : Math.round(m.avg_days_to_sell) + 'd',
          'avg to sell') +
      fig(whole(m.gross), 'gross') +
      fig(listed - sold, 'still listed') +
    '</div></div></div>';
}

function fig(value, label) {
  return '<div class="fig"><b>' + esc(String(value)) + '</b>' +
    '<span>' + esc(label) + '</span></div>';
}

/* ------------------------------------------------------------- to ship */

function viewQueue() {
  if (S.orders === null) return '<div class="pane"><p class="muted">Loading…</p></div>';
  var unlabeled = unlabeledOrders();

  var rows = (S.orders || []).map(function (o) {
    var bad = !o.printed;
    var on = o.id === S.pick;
    var u = due(o.ship_by);
    return '<div class="qrow"><div class="qrow__rail"></div>' +
      '<button class="qrow__btn' + (on ? ' qrow__btn--on' : '') +
        (bad && !on ? ' qrow__btn--bad' : '') +
        '" onclick="pickOrder(' + o.id + ')">' +
        '<span class="code code--row' + (bad ? ' code--bad' : '') + '">' +
          esc(o.code || '—') + '</span>' +
        /* Two lines for the title and the price down on the second row.
           Her titles run to a hundred characters - "Original 1944 WWII
           Army Air Forces Officer Candidate School Panoramic Photograph
           Miami Beach Florida" - so a single line next to a price is a
           line with no room to say which parcel this is. */
        '<span class="qrow__text">' +
          '<span class="qrow__title">' + esc(o.item) + '</span>' +
          '<span class="qrow__sub">' +
            '<span class="qrow__buyer">' + esc(dash(o.buyer)) + '</span>' +
            '<span class="qrow__price">' + money(o.price) + '</span>' +
            '<span class="qrow__due ' + u.cls + '">' + esc(u.label) + '</span>' +
          '</span>' +
        '</span>' +
      '</button></div>';
  }).join('');

  return '<div class="pane--flush split">' +
    '<div class="split__list">' +
      '<div class="split__head">' +
        '<div class="eyebrow">Open parcels</div>' +
        '<h1 class="display display--md">To ship</h1>' +
        (unlabeled.length
          ? '<button class="btn btn--alarm btn--wide" style="margin-top:12px" ' +
            'onclick="printAll()">Print all ' + unlabeled.length +
            ' missing labels</button>'
          : '') +
      '</div>' +
      '<div class="split__rows">' +
        (rows || '<p class="empty">Nothing is waiting to go out.</p>') +
      '</div>' +
    '</div>' +
    '<div class="split__detail">' + viewOrderDetail() + '</div>' +
  '</div>';
}

function pickOrder(id) {
  S.pick = id;
  S.detail = null;
  render();
  once(function () { return loadDetail(id); });
}

function viewOrderDetail() {
  var d = S.detail;
  if (!d) return '<p class="muted">Pick a parcel.</p>';
  var bad = !d.printed;

  return '<div class="detail__head">' +
      '<span class="code code--big' + (bad ? ' code--bad' : '') + '">' +
        esc(d.code || '—') + '<small>TOP RIGHT</small></span>' +
      '<div style="flex:1;min-width:0">' +
        '<div class="detail__state' + (bad ? ' detail__state--bad' : '') + '">' +
          (bad ? 'No label printed yet' : 'Label printed') + '</div>' +
        '<h2 class="display display--sm" style="line-height:1.25;margin-top:6px">' +
          esc(d.item) + '</h2>' +
      '</div>' +
    '</div>' +
    '<div class="stats">' +
      cell('Sold for', money(d.price)) +
      cell(d.postage_source === 'confirmed' ? 'Postage' : 'Postage, est.',
           money(d.postage)) +
      cell('You keep', money(d.kept), 'keep') +
      cell('Ship by', dash(d.ship_by), 'small') +
      cell('Weight', dash(d.weight), 'small') +
      cell('Order', dash(d.order_id), 'small') +
    '</div>' +
    postageNote(d) +
    '<div class="detail__mid">' +
      '<div class="card detail__addr">' +
        '<div class="label">Ships to</div>' +
        '<div style="margin-top:6px">' +
          (d.ship_to
            ? String(d.ship_to).split(',').map(function (a) {
                return '<span>' + esc(a.trim()) + '</span>';
              }).join('')
            : '<span class="muted">Not recorded</span>') +
        '</div>' +
        '<div class="rule"></div>' +
        '<div class="label">Tracking</div>' +
        '<div class="detail__track">' + esc(dash(d.tracking)) + '</div>' +
      '</div>' +
      '<div class="detail__label">' +
        (S.labelUrl
          ? '<embed src="' + esc(S.labelUrl) + '" type="application/pdf">'
          : '<div style="flex:1"></div>') +
        '<span>' + (d.has_label ? 'Label PDF' : 'No label on file') + '</span>' +
      '</div>' +
    '</div>' +
    '<div class="detail__acts">' +
      '<button class="btn btn--primary" onclick="doShip()">Mark shipped</button>' +
      '<button class="btn btn--fixed" onclick="doPrint()"' +
        (d.has_label ? '' : ' disabled') + '>' +
        (bad ? 'Print label' : 'Reprint label') + '</button>' +
    '</div>' +
    '<p class="detail__note">Both ask first — a mouse makes press-and-hold ' +
      'awkward, and neither is easy to take back.</p>';
}

function cell(label, value, cls) {
  return '<div><div class="label">' + esc(label) + '</div>' +
    '<b' + (cls ? ' class="' + cls + '"' : '') + '>' + esc(value) + '</b></div>';
}

/* An estimate must not be able to pass for a fact, so the screen says in
   words where the figure came from rather than leaving "(est.)" to carry
   it alone. */
function postageNote(d) {
  if (d.postage === null || d.postage === undefined) {
    return '<p class="detail__note">No postage recorded, and there is no ' +
      'basis to estimate one — so "You keep" is unknown rather than the ' +
      'whole price.</p>';
  }
  if (d.postage_source === 'confirmed') return '';
  return '<p class="detail__note">That postage is an estimate, worked out ' +
    'from parcels of a similar weight whose charge was actually confirmed. ' +
    'Typing the real figure marks it confirmed.</p>';
}

function doPrint() {
  var d = S.detail;
  if (!d) return;
  /* Read out first: the dialog escapes what it is handed, and leaving
     `d.code` inline here is the shape the escaping test looks for. */
  var code = d.code;
  ask('Print label ' + code + '?',
      'One 4×6 goes to the printer. It cannot report back, so if nothing ' +
      'comes out the paper is the only thing that knows.',
      'Print', function () {
    once(async function () {
      await api('/api/orders/' + d.id + '/print', { method: 'POST', body: {} });
      toast('Label ' + code + ' sent to the printer.');
      await loadQueue();
      await loadDetail(d.id);
    });
  });
}

function doShip() {
  var d = S.detail;
  if (!d) return;
  var code = d.code;
  ask('Mark ' + code + ' shipped?',
      'The code frees up for the next parcel immediately, and this sale ' +
      'lands in this month\'s numbers.',
      'Mark shipped', function () {
    once(async function () {
      await api('/api/orders/' + d.id + '/ship', { method: 'POST', body: {} });
      toast(code + ' is out. That code is free again.');
      S.pick = null;
      S.detail = null;
      releaseLabel();
      await loadQueue();
      if (S.pick !== null) await loadDetail(S.pick);
    });
  });
}

function printAll() {
  var n = unlabeledOrders().length;
  ask('Print ' + n + ' label' + (n === 1 ? '' : 's') + '?',
      'Every open parcel that has not printed goes in one pass. Check the ' +
      'roll first — nothing here can tell you it ran out.',
      'Print all', function () {
    once(async function () {
      var d = await api('/api/print/pending',
                        { method: 'POST', body: { dry_run: false } });
      var okCount = (d.printed || []).length;
      var bad = (d.failed || []).length;
      toast(okCount + ' sent' + (bad ? ', ' + bad + ' failed' : '') + '.');
      await loadQueue();
      await loadPending();
    });
  });
}

/* ----------------------------------------------------------- inventory */

var COLS = [
  { key: 'title',       label: 'Item',   flex: true },
  { key: 'era',         label: 'Era',    w: 82 },
  { key: 'bin',         label: 'Bin',    w: 68 },
  { key: 'price',       label: 'Asking', w: 78, right: true },
  { key: 'paid',        label: 'Paid',   w: 66, right: true },
  { key: 'margin',      label: 'Margin', w: 74, right: true },
  { key: 'days_listed', label: 'Listed', w: 68, right: true },
  { key: 'state',       label: 'Status', w: 82 }
];

function marginOf(t) {
  if (t.price === null || t.price === undefined) return null;
  if (t.paid === null || t.paid === undefined) return null;
  return Number(t.price) - Number(t.paid);
}

function sortedInv() {
  var rows = (S.inv || []).slice();
  var key = S.sort;
  rows.sort(function (x, y) {
    var a = key === 'margin' ? marginOf(x) : x[key];
    var b = key === 'margin' ? marginOf(y) : y[key];
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    if (typeof a === 'string') return a.localeCompare(b) * S.dir;
    return (a - b) * S.dir;
  });
  return rows;
}

function viewInventory() {
  if (S.inv === null) return '<div class="pane"><p class="muted">Loading…</p></div>';
  var rows = sortedInv();

  var head = '<span class="chk"></span><span style="width:38px;flex:none"></span>' +
    COLS.map(function (b) {
      var key = b.key, width = b.w;
      var style = b.flex ? 'flex:1;min-width:0' : 'width:' + width + 'px;flex:none';
      if (b.right) style += ';text-align:right';
      return '<button class="' + (S.sort === key ? 'on' : '') +
        '" style="' + style + '" onclick="sortBy(\'' + key + '\')">' +
        esc(b.label) + (S.sort === key ? (S.dir === 1 ? ' ↑' : ' ↓') : '') +
        '</button>';
    }).join('');

  var body = rows.map(function (t) {
    var on = S.sel.indexOf(t.id) !== -1;
    var m = marginOf(t);
    return '<div class="tbl__row' + (on ? ' tbl__row--on' : '') + '">' +
      '<button class="chk" onclick="toggleRow(' + t.id + ')">' +
        '<i class="' + (on ? 'on' : '') + '">' + (on ? '✓' : '') + '</i></button>' +
      '<span class="tbl__thumb"></span>' +
      '<button class="tbl__title" onclick="openWriter(' + t.id + ')">' +
        esc(t.title) + '</button>' +
      '<span class="tbl__cell" style="width:82px">' + esc(dash(t.era)) + '</span>' +
      '<span class="tbl__cell mono" style="width:68px">' +
        esc(dash(t.bin || t.bin_code)) + '</span>' +
      '<span class="tbl__cell--strong" style="width:78px">' + money(t.price) + '</span>' +
      '<span class="tbl__cell tbl__cell--num" style="width:66px">' +
        money(t.paid) + '</span>' +
      '<span class="tbl__cell tbl__cell--num" style="width:74px">' +
        (m === null ? '—' : money(m)) + '</span>' +
      '<span class="tbl__cell tbl__cell--num' +
        (t.days_listed > 45 ? ' tbl__cell--old' : '') + '" style="width:68px">' +
        esc(days(t.days_listed)) + '</span>' +
      '<span style="width:82px;flex:none">' + stateTag(t.state) + '</span>' +
    '</div>';
  }).join('');

  return '<div class="pane--col">' +
    '<div class="inv__head">' +
      '<div style="flex:1;min-width:0">' +
        '<div class="eyebrow">' + esc(invHead(rows.length)) + '</div>' +
        '<h1 class="display display--lg">Inventory</h1>' +
      '</div>' +
      '<div class="find"><span>Find</span>' +
        '<input id="invq" value="' + esc(S.invQ) + '" oninput="setSearch(this.value)" ' +
          'placeholder="title, era, or bin"></div>' +
      '<button class="btn btn--quiet" onclick="selectAll()">Select all</button>' +
    '</div>' +
    '<div class="tbl">' +
      '<div class="tbl__head">' + head + '</div>' +
      (body || '<p class="empty">Nothing matches that. Try a bin name, or ' +
                'an era like 1950s.</p>') +
      '<div style="height:90px"></div>' +
    '</div>' +
  '</div>' + viewBulk();
}

/* Not "on the shelf". Sold items are in this table too - a sale is
   reconciled back onto its listing - and a count that calls them shelf
   stock is wrong about the one thing the number is for. */
function invHead(shown) {
  var all = (S.inv || []).length;
  if (S.invQ) {
    return S.invTotal
      ? shown + ' of ' + S.invTotal + ' matching'
      : plural(shown, 'match', 'matches');
  }
  /* "Still listed" means listed. It used to mean "not sold", which was
     the same thing while `active` and `sold` were the only states that
     could hold anything - and stopped being when `draft` and `acquired`
     arrived, both of which are things she owns that nobody can buy.
     Counting them here reported stock as shopfront. */
  var live = (S.inv || []).filter(function (t) {
    return t.state === 'active';
  }).length;
  return plural(all, 'item') + ' · ' + live + ' still listed';
}

function stateTag(state) {
  var cls = state === 'sold' ? 'tag--ok'
          : (state === 'draft' || state === 'acquired') ? 'tag--warn' : '';
  /* `acquired` is spelled out rather than title-cased, because the word
     the database uses is not the one a person would reach for: what she
     wants to know about one of these rows is that it is *not listed*,
     which is a thing to do, and "Acquired" is a thing that happened. */
  var word = state === 'active' ? 'Listed'
           : state === 'acquired' ? 'Not listed'
           : state ? state.charAt(0).toUpperCase() + state.slice(1) : '—';
  return '<span class="tag ' + cls + '">' + esc(word) + '</span>';
}

function sortBy(key) {
  if (S.sort === key) S.dir = -S.dir;
  else { S.sort = key; S.dir = (key === 'title' || key === 'era' ||
                                key === 'bin' || key === 'state') ? 1 : -1; }
  renderMain();
}

/* Deliberately not through once(). That wrapper re-renders everything
   when it finishes, which is right for a print or a bulk edit and wrong
   here: the query changes on every keystroke and the whole screen does
   not need to. renderMain() puts the caret back. */
function setSearch(v) {
  S.invQ = v;
  clearTimeout(setSearch._t);
  setSearch._t = setTimeout(async function () {
    try {
      await loadInventory();
      S.error = '';
    } catch (e) {
      if (e.message === '401') return;
      S.error = e.message;
    }
    renderMain();
  }, 200);
}

function toggleRow(id) {
  var at = S.sel.indexOf(id);
  if (at === -1) S.sel.push(id); else S.sel.splice(at, 1);
  renderMain();
}

function selectAll() {
  S.sel = sortedInv().map(function (t) { return t.id; });
  renderMain();
}

function clearSel() {
  S.sel = [];
  S.bulk = null;
  renderMain();
}

var BULKS = [
  { key: 'bin',   label: 'Move to bin',       field: 'Bin name', ph: 'B4' },
  { key: 'era',   label: 'Change era',        field: 'Era',      ph: '1950s' },
  { key: 'sold',  label: 'Mark sold',         field: null },
  { key: 'draft', label: 'Back to draft',     field: null },
  { key: 'active', label: 'Mark ready to post', field: null },
  { key: 'export', label: 'Export CSV',       field: null }
];

function viewBulk() {
  if (!S.sel.length) return '';
  var chosen = BULKS.filter(function (b) { return b.key === S.bulk; })[0];
  var acts = BULKS.map(function (b) {
    var key = b.key;
    return '<button class="pill' + (S.bulk === key ? ' pill--on' : '') +
      '" onclick="pickBulk(\'' + key + '\')">' + esc(b.label) + '</button>';
  }).join('');

  return '<div class="bulk"><div class="bulk__box">' +
    '<div class="bulk__row">' +
      '<b>' + S.sel.length + (S.sel.length === 1 ? ' item' : ' items') +
        ' selected</b>' +
      '<span class="bulk__sep"></span>' +
      '<div class="bulk__acts">' + acts + '</div>' +
      '<span class="bulk__sep"></span>' +
      '<button class="btn btn--quiet" onclick="clearSel()">Clear</button>' +
    '</div>' +
    (chosen
      ? '<div class="bulk__form">' +
          (chosen.field
            ? '<div class="field"><label class="label">' + esc(chosen.field) +
              '</label><input class="input" id="bulkval" value="' +
              esc(S.bulkVal) + '" oninput="S.bulkVal=this.value" placeholder="' +
              esc(chosen.ph) + '"></div>'
            : '') +
          '<button class="btn btn--primary" onclick="applyBulk()">Apply</button>' +
        '</div>'
      : '') +
  '</div></div>';
}

function pickBulk(key) {
  S.bulk = S.bulk === key ? null : key;
  S.bulkVal = '';
  renderMain();
}

function applyBulk() {
  var chosen = BULKS.filter(function (b) { return b.key === S.bulk; })[0];
  if (!chosen) return;
  var n = S.sel.length;
  var noun = n + (n === 1 ? ' item' : ' items');

  if (chosen.key === 'export') return exportSelection();

  ask('Apply to ' + noun + '?',
      chosen.label + ' — this changes every selected row at once and there ' +
      'is no undo.',
      'Apply', function () {
    once(async function () {
      var set = {};
      if (chosen.key === 'bin') set.bin = S.bulkVal;
      else if (chosen.key === 'era') set.era = S.bulkVal;
      else set.state = chosen.key;
      var d = await api('/api/inventory/bulk',
                        { method: 'POST', body: { ids: S.sel, set: set } });
      toast((d.changed || 0) + ' updated.');
      S.sel = [];
      S.bulk = null;
      S.bulkVal = '';
      await loadInventory();
    });
  });
}

/* A download, not a round trip: these rows are already on screen and the
   server has nothing to add to them. */
function exportSelection() {
  var chosenIds = S.sel;
  var rows = (S.inv || []).filter(function (t) {
    return chosenIds.indexOf(t.id) !== -1;
  });
  var head = 'code,title,era,bin,asking,paid,state\n';
  var body = rows.map(function (t) {
    return [t.inventory_code, t.title, t.era, t.bin || t.bin_code,
            t.price, t.paid, t.state]
      .map(function (v) {
        return '"' + String(v === null || v === undefined ? '' : v)
          .replace(/"/g, '""') + '"';
      }).join(',');
  }).join('\n');
  /* utf-8-sig, like `mplabel inventory`: her titles carry accents and
     curly quotes, and Excel on Windows reads a plain utf-8 CSV as
     mojibake. */
  var blob = new Blob(['﻿' + head + body], { type: 'text/csv' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'inventory.csv';
  a.click();
  URL.revokeObjectURL(a.href);
  toast(rows.length + ' rows exported.');
}

/* -------------------------------------------------- print a stray label
 *
 * A 4x6 that did not come from a Marketplace email: eBay, PirateShip, a
 * carrier's own site, a return. `POST /api/print/label` takes the PDF
 * and records nothing - there is no order for it to belong to, and
 * inventing one would put a parcel that is not a sale into revenue and
 * into the Sheet.
 *
 * The laptop is where these actually arrive - emailed by a buyer,
 * downloaded from a seller's site - so this screen gets the affordances
 * the phone cannot have: drag a file onto it, and see the page you are
 * cropping from at a size where a wrong crop is obvious.
 *
 * Check-only is on by default, and that is not a nicety. This printer
 * cannot report a failure, so a wrong crop costs a label and says
 * nothing; a PDF from a seller nobody has printed before is exactly
 * where a wrong crop comes from, and checking first is free.
 */

function viewSend() {
  var pick = S.send;
  var r = pick && pick.result;

  var chooser =
    '<div class="drop' + (pick ? ' drop--has' : '') + '" id="senddrop" ' +
        'ondragover="sendOver(event, true)" ' +
        'ondragleave="sendOver(event, false)" ondrop="sendDrop(event)">' +
      '<h3>' + (pick ? esc(pick.name) : 'Drop a label PDF here') + '</h3>' +
      '<p>' + (pick
        ? 'Drop another to replace it.'
        : 'Whatever the seller emailed, or a carrier’s own download. ' +
          'Nothing about it is recorded — there is no order for it to ' +
          'belong to.') + '</p>' +
      '<input type="file" id="sendfile" accept="application/pdf,.pdf" ' +
        'style="display:none" onchange="pickSendFile(this)">' +
      '<button class="btn" style="margin-top:18px" ' +
        'onclick="document.getElementById(\'sendfile\').click()">' +
        (pick ? 'Choose another' : 'Choose a file') + '</button>' +
    '</div>';

  if (!pick) {
    return '<div class="pane"><div class="wiz">' + sendHead() + chooser +
      '</div></div>';
  }

  var turns = [['', 'Work it out'], ['0', '0°'], ['90', '90°'],
               ['180', '180°'], ['270', '270°']];
  var turnBtns = turns.map(function (t) {
    return '<button class="pill' + (pick.rotate === t[0] ? ' pill--on' : '') +
      '" onclick="sendOpt(\'rotate\', \'' + t[0] + '\')">' +
      esc(t[1]) + '</button>';
  }).join('');

  var regions = '';
  if (r && r.regions_found > 1) {
    var buttons = '';
    for (var i = 1; i <= r.regions_found; i++) {
      var on = String(pick.region) === String(i) ||
               (pick.region === '' && r.region === i);
      buttons += '<button class="pill' + (on ? ' pill--on' : '') +
        '" onclick="sendOpt(\'region\', \'' + i + '\')">' + i + '</button>';
    }
    regions = '<div class="sendrow"><span class="label">Which block</span>' +
      '<span class="sendrow__opts">' + buttons + '</span></div>' +
      '<p class="prose muted" style="font-size:12px;margin:0">This page has ' +
      esc(r.regions_found) + ' things on it that could be the label. Check ' +
      'each one before printing.</p>';
  }

  return '<div class="pane"><div class="send">' +
    '<div class="send__left">' + sendHead() + chooser +
      '<div class="card send__opts">' +
        '<div class="sendrow"><span class="label">Check only</span>' +
          '<span class="sendrow__opts">' +
            '<button class="pill' + (pick.dry ? ' pill--on' : '') +
              '" onclick="sendOpt(\'dry\', true)">On</button>' +
            '<button class="pill' + (pick.dry ? '' : ' pill--on') +
              '" onclick="sendOpt(\'dry\', false)">Off</button>' +
          '</span></div>' +
        '<div class="sendrow"><span class="label">Turn</span>' +
          '<span class="sendrow__opts">' + turnBtns + '</span></div>' +
        '<div class="sendrow"><span class="label">Page</span>' +
          '<span class="sendrow__opts">' +
            '<button class="pill" onclick="sendOpt(\'page\', ' +
              Math.max(1, (pick.page || 1) - 1) + ')">−</button>' +
            '<b class="mono" style="padding:0 10px">' +
              esc(pick.page || 1) + '</b>' +
            '<button class="pill" onclick="sendOpt(\'page\', ' +
              ((pick.page || 1) + 1) + ')">+</button>' +
          '</span></div>' +
        regions +
      '</div>' +
      /* Actions above the measurement, not below it. The thing this
         screen exists to do is print; the size and the rotation are
         reference, and reference under the button is how the add-item
         and order screens both ended up with their primary action below
         a fold. */
      '<div style="display:flex;gap:11px;margin-top:18px">' +
        '<button class="btn ' + (pick.dry ? 'btn--primary' : 'btn--alarm') +
          '" style="flex:1" onclick="doSendLabel()"' +
          (pick.busy ? ' disabled' : '') + '>' +
          (pick.busy ? 'Working…'
                     : (pick.dry ? 'Check the crop' : 'Print it')) +
          '</button>' +
        '<button class="btn btn--quiet" onclick="clearSend()">Clear</button>' +
      '</div>' +
      sendAnswer(pick, r) +
    '</div>' +
    '<div class="send__right">' +
      '<div class="label">' +
        (S.sendShot ? 'What will print' : 'The file you chose') + '</div>' +
      (S.sendShot
        ? '<img class="send__page" src="' + esc(S.sendShot) + '" alt="">'
        : (S.sendUrl
            ? '<embed src="' + esc(S.sendUrl) + '" type="application/pdf">'
            : '<div class="send__blank"></div>')) +
      sendLegend(r) +
    '</div>' +
  '</div></div>';
}

/* The outline is drawn by the server, from the same call that does the
   cropping - so it cannot show a rectangle the printer will not use.
   This only has to say what the colours mean. */
function sendLegend(r) {
  if (!S.sendShot) {
    return '<p class="prose muted" style="font-size:11.5px;margin:0">' +
      'The page going in. Check the crop to see the 4×6 marked on it.</p>';
  }
  var extra = (r && r.regions_found > 1)
    ? ' The red outlines are the other blocks on this page it could have ' +
      'taken — a packing slip, usually. If it has boxed the wrong one, ' +
      'say which under <b>Which block</b>.'
    : '';
  return '<p class="prose muted" style="font-size:11.5px;margin:0">' +
    '<b style="color:var(--ac)">Green</b> is the 4×6 that will print, ' +
    'drawn by the same code that does the cropping.' + extra + '</p>';
}

function sendHead() {
  return '<div class="eyebrow">Not a Marketplace order</div>' +
    '<h1 class="display display--lg" style="margin-bottom:20px">' +
      'Print a label</h1>';
}

function sendAnswer(pick, r) {
  if (pick.error) {
    /* The server's sentence, not a status code. "This may not be a
       shipping label" and "say which with region" are the whole point of
       the message - only the person holding the file can act on either,
       and neither survives being turned into "failed". */
    return '<div class="err" style="margin:16px 0 0">' +
      esc(pick.error) + '</div>';
  }
  if (!r) {
    return '<p class="prose muted" style="margin-top:14px;font-size:13px">' +
      'Nothing measured yet. <b>Check only</b> converts it and reports ' +
      'what it found without opening the printer.</p>';
  }
  var rows =
    cell('Size', r.size_in[0] + ' × ' + r.size_in[1] + ' in') +
    cell('Turned', r.rotation + '°', 'small') +
    cell('Page', r.page, 'small');
  var note = '';
  if (r.rotation_source === 'aspect') {
    note = '<p class="prose muted" style="font-size:12px;margin:10px 0 0">' +
      'There is no text on this label to read an orientation from, so ' +
      'that is its shape talking. It knows the label is on its side and ' +
      'not which way up — look at it, and use <b>Turn</b> if it is ' +
      'upside down.</p>';
  } else if (r.rotation_source === 'forced') {
    note = '<p class="prose muted" style="font-size:12px;margin:10px 0 0">' +
      'You chose that turn.</p>';
  } else {
    note = '<p class="prose muted" style="font-size:12px;margin:10px 0 0">' +
      'Read off the text on the label.</p>';
  }
  if (r.dry_run) {
    note += '<p class="prose muted" style="font-size:12px;margin:8px 0 0">' +
      'Nothing printed and no stock used. Turn <b>Check only</b> off when ' +
      'it looks right.</p>';
  } else {
    note += '<p class="prose muted" style="font-size:12px;margin:8px 0 0">' +
      'Job <span class="mono">' + esc(r.job) + '</span>, recorded in ' +
      esc(r.recorded) + '.</p>';
  }
  return '<div class="stats" style="margin-top:14px;max-width:none">' +
    rows + '</div>' + note;
}

function pickSendFile(input) {
  var f = input.files && input.files[0];
  if (f) takeSendFile(f);
}

function sendOver(ev, on) {
  ev.preventDefault();
  var el = document.getElementById('senddrop');
  if (el) el.classList.toggle('drop--over', on);
}

function sendDrop(ev) {
  ev.preventDefault();
  sendOver(ev, false);
  var files = ev.dataTransfer && ev.dataTransfer.files;
  if (files && files.length) takeSendFile(files[0]);
}

function takeSendFile(f) {
  releaseSend();
  S.send = { name: f.name, file: f, dry: true, rotate: '', page: 1,
             region: '', result: null, error: '', busy: false };
  S.sendUrl = URL.createObjectURL(f);
  renderMain();
  /* Draw it straight away. The outline is the whole reason this screen
     is worth having on a laptop, and making her press a button to see
     the page she just chose is a step with nothing behind it. */
  refreshSendShot();
}

function clearSend() {
  releaseSend();
  S.send = null;
  renderMain();
}

function releaseSend() {
  if (S.sendUrl) { URL.revokeObjectURL(S.sendUrl); S.sendUrl = null; }
  releaseShot();
}

function sendOpt(key, value) {
  if (!S.send) return;
  S.send[key] = value;
  /* Any option changes what the answer would be, so the old one stops
     being about this - the picture included. A stale outline beside a
     changed rotation is worse than no outline: it is a drawing of a
     crop that is not going to happen, and it looks exactly like one
     that is. */
  S.send.result = null;
  S.send.error = '';
  releaseShot();
  renderMain();
  /* Check-only is a toggle about *printing*, not about looking. Turning
     it off should not stop the page being drawn, and must never make
     this print. */
  if (key !== 'dry') refreshSendShot();
}

function releaseShot() {
  if (S.sendShot) { URL.revokeObjectURL(S.sendShot); S.sendShot = null; }
}

/* Always a dry run and never a print. This fires on its own - choosing a
   file, changing the turn - so it has to be incapable of spending
   stock whatever the toggle says. */
function refreshSendShot() {
  var pick = S.send;
  if (!pick || !pick.file) return;
  var q = [];
  if (pick.rotate !== '') q.push('rotate=' + pick.rotate);
  if (pick.page > 1) q.push('page=' + pick.page);
  if (pick.region !== '') q.push('region=' + pick.region);
  var query = q.length ? '?' + q.join('&') : '';

  var mine = ++sendShotSeq;
  (async function () {
    try {
      var body = await pick.file.arrayBuffer();
      var res = await fetch('/api/label/preview' + query, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-Mplabel': '1', 'Content-Type': 'application/pdf' },
        body: body
      });
      /* A slow answer to a question she has already changed must not
         land. Without this the outline can flick back to the previous
         rotation after the new one has drawn. */
      if (mine !== sendShotSeq || S.send !== pick) return;
      if (!res.ok) return;         /* the JSON call reports the reason */
      releaseShot();
      S.sendShot = URL.createObjectURL(await res.blob());
      renderMain();
    } catch (e) { /* the measurement below says what went wrong */ }
  })();
}

var sendShotSeq = 0;

function doSendLabel() {
  var pick = S.send;
  if (!pick || !pick.file || pick.busy) return;
  if (pick.dry) return runSendLabel();
  /* Spending stock asks first, like printing a parcel does. The phone
     holds a button; a mouse makes that awkward. */
  ask('Print ' + clip(pick.name, 40) + '?',
      'One 4×6 goes to the printer and nothing about it is recorded — ' +
      'there is no order for it to belong to. It cannot report back, so ' +
      'if nothing comes out the paper is the only thing that knows.',
      'Print', runSendLabel);
}

function runSendLabel() {
  var pick = S.send;
  if (!pick || !pick.file) return;
  pick.busy = true;
  pick.error = '';
  renderMain();

  var q = [];
  if (pick.rotate !== '') q.push('rotate=' + pick.rotate);
  if (pick.page > 1) q.push('page=' + pick.page);
  if (pick.region !== '') q.push('region=' + pick.region);
  if (pick.dry) q.push('dry_run=1');

  (async function () {
    try {
      var body = await pick.file.arrayBuffer();
      var d = await api('/api/print/label' + (q.length ? '?' + q.join('&') : ''),
                        { method: 'POST', raw: body,
                          type: 'application/pdf' });
      pick.result = d.label;
      if (!pick.dry) toast('Sent ' + pick.name + ' to the printer.');
    } catch (e) {
      if (e.message !== '401') pick.error = e.message;
    }
    pick.busy = false;
    renderMain();
  })();
}

/* -------------------------------------------------------------- writer */

function openWriter(id) {
  S.writerId = id;
  go('writer');
}

function pickDraft(id) {
  if (id === S.writerId) return;
  S.writerId = id;
  once(function () { return openDraft(id); });
}

function stepDraft(by) {
  var at = draftIndex();
  var next = (S.drafts || [])[at + by];
  if (next) pickDraft(next.id);
}

function draftIndex() {
  var ids = (S.drafts || []).map(function (d) { return d.id; });
  return ids.indexOf(S.writerId);
}

function viewWriter() {
  if (S.drafts === null) {
    return '<div class="pane"><p class="muted">Loading…</p></div>';
  }
  if (!S.drafts.length) {
    return '<div class="pane">' +
      '<div class="eyebrow">Nothing waiting</div>' +
      '<h1 class="display display--lg">Listing writer</h1>' +
      '<p class="prose muted" style="max-width:520px;margin-top:14px">' +
        'A draft is something photographed and costed that has never been ' +
        'written up. Nothing is in that state right now.</p>' +
      '<p class="prose muted" style="max-width:520px;margin-top:12px">' +
        'Select rows on the Inventory screen and choose <b>Back to draft</b> ' +
        'to put them here. Drafts are kept out of the sell-through figures ' +
        'on Month-end — they were never for sale, so counting them would ' +
        'drag the percentage down.</p>' +
    '</div>';
  }

  var at = draftIndex();
  var d = S.draft;
  var shots = S.shots || [];

  /* No thumbnail here, deliberately. The list endpoint does not carry
     photographs, so every strip item would draw the same "nothing
     photographed" placeholder - including for the drafts that have
     three. A placeholder that is wrong about the thing it stands for is
     worse than no placeholder, and the title is what tells two drafts
     apart anyway. */
  var film = S.drafts.map(function (d) {
    var on = d.id === S.writerId;
    return '<button class="film__item' + (on ? ' film__item--on' : '') +
      '" onclick="pickDraft(' + d.id + ')">' +
      '<span>' + esc(clip(d.title, 60)) + '</span></button>';
  }).join('');

  return '<div class="pane--col">' +
    '<div class="writer__head">' +
      '<div style="flex:1;min-width:0">' +
        '<div class="eyebrow">Draft ' + (at + 1) + ' of ' +
          S.drafts.length + (S.dirty ? ' · unsaved' : '') + '</div>' +
        '<h2>' + esc((d && d.title) || '') + '</h2>' +
      '</div>' +
      '<div style="display:flex;gap:8px;flex:none">' +
        '<button class="btn btn--quiet" onclick="stepDraft(-1)"' +
          (at <= 0 ? ' disabled' : '') + '>← Previous</button>' +
        '<button class="btn btn--quiet" onclick="stepDraft(1)"' +
          (at >= S.drafts.length - 1 ? ' disabled' : '') + '>Next →</button>' +
      '</div>' +
    '</div>' +
    '<div class="writer__body">' +
      '<div class="film">' +
        '<div class="label" style="padding:0 6px 4px">' +
          plural(S.drafts.length, 'draft') + '</div>' + film +
      '</div>' +
      '<div class="writer__photos">' +
        '<div class="writer__cover">' +
          (shots.length
            ? '<img src="/api/photos/' + shots[0].id + '" alt="">' +
              '<b>COVER</b>'
            : '') +
        '</div>' +
        '<div class="writer__strip">' +
          shots.slice(1, 5).map(function (p) {
            return '<span class="shot"><img src="/api/photos/' + p.id +
              '" alt=""></span>';
          }).join('') +
        '</div>' +
        '<p class="prose muted" style="font-size:11.5px;margin:0">' +
          (shots.length
            ? 'The first is the cover buyers see in the feed. Photographs ' +
              'are attached on the phone, where they were taken.'
            : 'No photographs attached. They are attached on the phone, ' +
              'from the untriaged pile.') + '</p>' +
      '</div>' +
      '<div class="writer__form">' +
        '<div class="writer__facts">' +
          fact('Era', dash(d && d.era)) +
          fact('Bin', dash(d && (d.bin || d.bin_code)), true) +
          fact('Paid', money(d && d.paid), true) +
          fact('Asking', money(d && d.price), true) +
        '</div>' +
        '<div class="field">' +
          '<label class="label" for="draft-title">Listing title</label>' +
          '<input class="writer__title" id="draft-title" value="' +
            esc(S.draftTitle) + '" oninput="editDraft(\'draftTitle\', this.value)">' +
        '</div>' +
        '<div class="writer__bodyfield">' +
          '<div style="display:flex;align-items:baseline;gap:8px">' +
            '<label class="label" style="flex:1" for="draft-body">' +
              'Description</label>' +
            '<span class="draftmark">DRAFT</span>' +
          '</div>' +
          '<textarea id="draft-body" ' +
            'oninput="editDraft(\'draftBody\', this.value)">' +
            esc(S.draftBody) + '</textarea>' +
          '<p class="prose muted" style="font-size:11.5px;margin:0">' +
            'Nothing here reaches Facebook. Copy it across when it reads ' +
            'right, then mark the thing listed.</p>' +
        '</div>' +
        '<div style="display:flex;gap:10px;flex:none">' +
          '<button class="btn btn--primary" style="flex:1" ' +
            'onclick="copyDraft()">Copy for Marketplace</button>' +
          '<button class="btn" onclick="saveDraft()"' +
            (S.dirty ? '' : ' disabled') + '>Save</button>' +
          '<button class="btn" onclick="publishDraft()">Mark listed</button>' +
        '</div>' +
      '</div>' +
    '</div>' +
  '</div>';
}

function fact(label, value, mono) {
  return '<div><div class="label">' + esc(label) + '</div>' +
    '<b' + (mono ? ' class="mono"' : '') + '>' + esc(value) + '</b></div>';
}

/* Typing does not save. The screen is a place to write badly first, and
   an autosave would put every half-sentence into the row the analytics
   read. `dirty` is what the Save button and the header both watch. */
function editDraft(key, value) {
  S[key] = value;
  if (S.dirty) return;
  S.dirty = true;
  renderMain();
}

function saveDraft() {
  if (!S.draft) return;
  var id = S.draft.id;
  once(async function () {
    await api('/api/inventory/' + id + '/fields', {
      method: 'POST',
      body: { title: S.draftTitle, description: S.draftBody }
    });
    S.dirty = false;
    toast('Saved.');
    await loadDrafts();
    await openDraft(id);
  });
}

function copyDraft() {
  var text = S.draftTitle + '\n\n' + S.draftBody;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(function () {
      toast('Title and description copied.');
    }, function () { toast('The browser would not let go of the clipboard.'); });
  } else {
    toast('This browser has no clipboard access.');
  }
}

function publishDraft() {
  if (!S.draft) return;
  var id = S.draft.id;
  var name = S.draftTitle;
  ask('Mark "' + clip(name, 40) + '" listed?',
      'It stops being a draft and starts counting towards sell-through, ' +
      'dated today. Anything unsaved is saved with it.',
      'Mark listed', function () {
    once(async function () {
      await api('/api/inventory/' + id + '/fields', {
        method: 'POST',
        body: { title: S.draftTitle, description: S.draftBody,
                state: 'active' }
      });
      toast('Listed. It is in the numbers from today.');
      S.writerId = null;
      S.draft = null;
      S.dirty = false;
      await loadDrafts();
      if (S.writerId !== null) await openDraft(S.writerId);
    });
  });
}

/* ----------------------------------------------------------- month-end */

function viewReview() {
  if (!S.stats) return '<div class="pane"><p class="muted">Loading…</p></div>';
  var months = (S.stats.monthly || []).slice(0, 7).slice().reverse();
  var m = (S.stats.monthly || [])[0];
  var cover = costCover();

  var peak = months.reduce(function (n, a) {
    return Math.max(n, Number(a.net || 0), 1);
  }, 1);

  var bars = months.map(function (a) {
    var on = m && a.month === m.month;
    return '<div><em class="' + (on ? 'on' : '') + '">' + whole(a.net) + '</em>' +
      '<i class="' + (on ? 'on' : '') + '" style="height:' +
      Math.round(Math.max(0, Number(a.net || 0)) / peak * 100) + '%"></i>' +
      '<span>' + esc(monthName(a.month)) + '</span></div>';
  }).join('');

  var bands = (S.stats.price_bands || []).map(function (b) {
    var pct = b.sell_through_pct === null ? 0 : Number(b.sell_through_pct);
    var colour = pct > 55 ? 'var(--ac)' : pct > 35 ? 'var(--wa)' : 'var(--sub)';
    return '<div class="band"><div class="band__top">' +
      '<span class="band__name">' + esc(b.price_band) + '</span>' +
      '<span class="band__line">' + (b.sold || 0) + ' of ' + (b.listed || 0) +
        (b.avg_days_to_sell === null
          ? ''
          : ' · ' + Math.round(b.avg_days_to_sell) + 'd avg') + '</span>' +
      '<span class="band__pct">' + Math.round(pct) + '%</span></div>' +
      '<div class="band__track"><i style="width:' + Math.round(pct) +
        '%;background:' + colour + '"></i></div></div>';
  }).join('');

  var aging = (S.stats.aging || []).map(function (a) {
    return '<div class="aging__row"><span>' + esc(a.title) + '</span>' +
      '<span class="days">' + esc(days(a.days_listed)) + '</span>' +
      '<span class="num">' + (a.inquiries || 0) + ' asks</span>' +
      '<span class="ask">' + money(a.price) + '</span></div>';
  }).join('');

  return '<div class="pane">' +
    '<div class="rev__head">' +
      '<div style="flex:1;min-width:260px">' +
        '<div class="eyebrow">' + esc(m ? monthLabel(m.month) : 'No sales yet') +
          '</div>' + netHero(m, cover) +
      '</div>' + monthCells(m) +
    '</div>' +
    '<div class="rev__cols">' +
      '<div class="card"><div class="label">Net by month</div>' +
        '<div class="bars">' + bars + '</div>' +
        '<p class="prose muted" style="font-size:11.5px;margin:14px 0 0">' +
          'Only sales with a cost against them are in these bars.</p></div>' +
      '<div class="card"><div class="label">Sell-through by price</div>' +
        '<div class="bands">' + (bands || '<p class="muted">No sales yet.</p>') +
        '</div></div>' +
    '</div>' +
    advice() + owedCallout() +
    '<div class="aging"><div class="label" style="margin-bottom:11px">' +
      'Longest listed</div>' +
      (aging || '<p class="muted">Nothing has a listed date on it.</p>') +
    '</div>' +
  '</div>';
}

function monthLabel(ym) {
  if (!ym) return '';
  var parts = String(ym).split('-');
  var names = ['January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November',
               'December'];
  return (names[Number(parts[1]) - 1] || '') + ' ' + parts[0];
}

/* The design puts a hero net figure here and calls it "kept, after gross,
   cost, fees and postage". Two of those are not in this number: no
   Facebook fee has ever been confirmed against a real payout, and postage
   lives per parcel on the sale. And `net` only covers sales that have a
   cost recorded, so the sentence has to follow the data rather than
   stay the same while the data changes underneath it. */
function netHero(m, cover) {
  if (!m) {
    return '<h1 class="rev__net" style="color:var(--mut)">—</h1>' +
      '<p class="prose muted" style="margin-top:9px">Nothing has sold yet.</p>';
  }
  if (!cover.any) {
    return '<h1 class="rev__net" style="color:var(--fg)">' + whole(m.gross) +
      '</h1><p class="prose muted" style="margin-top:9px">gross. Not one of ' +
      'the ' + plural(m.orders || 0, 'sale') + ' this month has a cost ' +
      'recorded against it, so there is no profit figure to show — only ' +
      'what came in.</p>';
  }
  return '<h1 class="rev__net">' + whole(m.net) + '</h1>' +
    '<p class="prose muted" style="margin-top:9px">kept on the ' +
    (m.costed || 0) + ' of ' + plural(m.orders || 0, 'sale') + ' with a ' +
    'cost recorded, out of ' + whole(m.gross) + ' gross. Postage is per ' +
    'parcel and is not in it, and Facebook\'s fee has never been confirmed ' +
    'against a real payout, so neither is counted.</p>';
}

function monthCells(m) {
  if (!m) return '';
  return '<div class="cells">' +
    '<div><b>' + (m.orders || 0) + '</b><span>sold</span></div>' +
    '<div><b>' + (m.avg_days_to_sell === null
      ? '—' : Math.round(m.avg_days_to_sell) + '<small>d</small>') +
      '</b><span>avg to sell</span></div>' +
    '<div><b>' + (m.avg_order === null ? '—' : whole(m.avg_order)) +
      '</b><span>avg order</span></div>' +
  '</div>';
}

/* Only said when the bands actually support it. "Buy more in the $25-50
   band" off two sales is a recommendation this system invented. */
function advice() {
  var bands = (S.stats.price_bands || []).filter(function (b) {
    return b.price_band !== 'unknown' && (b.listed || 0) >= 5;
  });
  if (bands.length < 2) {
    return '<div class="callout callout--warn">Not enough sold in any one ' +
      'price band yet to say what to buy more of. Five listings in a band ' +
      'is the least this will read anything into.</div>';
  }
  var best = bands.slice().sort(function (x, y) {
    return Number(y.sell_through_pct || 0) - Number(x.sell_through_pct || 0);
  })[0];
  return '<div class="callout">The ' + esc(best.price_band) + ' band clears ' +
    'fastest — ' + (best.sold || 0) + ' of ' + (best.listed || 0) +
    (best.avg_days_to_sell === null
      ? ''
      : ' in about ' + Math.round(best.avg_days_to_sell) + ' days') +
    '. That is sell-through only; what it earns depends on what was paid, ' +
    'which is recorded for ' + costCover().costed + ' sales.</div>';
}

function owedCallout() {
  var owed = homeless();
  if (!owed.length) return '';
  var total = owed.reduce(function (n, t) { return n + Number(t.unassigned); }, 0);
  return '<div class="callout callout--warn">' + money(total) + ' from ' +
    (owed.length === 1 ? 'one trip is' : owed.length + ' trips are') +
    ' still not attached to anything. Every figure above is optimistic by ' +
    'up to that much.</div>';
}

/* -------------------------------------------------------------- import */

var CSV_STEPS = ['Choose a file', 'Match the columns', 'Check and import'];

function viewImport() {
  var at = S.csvStep;
  var plan = S.csvPlan;

  var steps = CSV_STEPS.map(function (label, i) {
    var cls = i === at ? 'on' : (i < at ? 'done' : '');
    return '<div><i class="' + cls + '">' + (i + 1) + '</i>' +
      '<span>' + esc(label) + '</span></div>';
  }).join('');

  return '<div class="pane"><div class="wiz">' +
    '<div class="eyebrow">Bring old sales in</div>' +
    '<h1 class="display display--lg">Import a spreadsheet</h1>' +
    (S.error ? '<div class="err" style="margin-top:16px">' + esc(S.error) +
               '</div>' : '') +
    '<div class="steps">' + steps + '</div>' +
    (at === 0 ? viewCsvPick() : '') +
    (at === 1 && plan ? viewCsvMap(plan) : '') +
    (at === 2 && plan ? viewCsvCheck(plan) : '') +
    '<div style="display:flex;gap:11px;margin-top:20px">' +
      (at === 2
        ? '<button class="btn btn--primary" onclick="csvCommit()">Import ' +
          plural(plan ? plan.rows.length - plan.skipped : 0, 'row') +
          '</button>'
        : (at === 1
            ? '<button class="btn btn--primary" onclick="csvStep(2)">' +
              'Continue</button>'
            : '')) +
      (at > 0
        ? '<button class="btn btn--quiet" onclick="csvStep(' + (at - 1) +
          ')">Back</button>'
        : '') +
    '</div>' +
  '</div></div>';
}

function viewCsvPick() {
  return '<div class="drop" id="csvdrop" ondragover="csvOver(event, true)" ' +
      'ondragleave="csvOver(event, false)" ondrop="csvDrop(event)">' +
    '<h3>Drop a CSV here</h3>' +
    '<p>Anything with a title and what it sold for. Column names do not ' +
      'matter — you match them next. Rows become listings, not orders: ' +
      'there is no parcel and no label behind them.</p>' +
    '<input type="file" id="csvfile" accept=".csv,text/csv" ' +
      'style="display:none" onchange="csvChose(this.files)">' +
    '<button class="btn" style="margin-top:20px" ' +
      'onclick="document.getElementById(\'csvfile\').click()">' +
      'Choose a file</button>' +
    (S.csvName
      ? '<p style="margin-top:14px;color:var(--ac)">' + esc(S.csvName) +
        '</p>'
      : '') +
  '</div>';
}

function viewCsvMap(plan) {
  var options = Object.keys(plan.fields);
  var rows = plan.headers.map(function (header) {
    var chosen = plan.mapping[header] || '';
    var opts = ['<option value="">— ignore this column —</option>'].concat(
      options.map(function (key) {
        return '<option value="' + esc(key) + '"' +
          (key === chosen ? ' selected' : '') + '>' +
          esc(plan.fields[key]) + '</option>';
      })).join('');
    return '<div class="sheet__row">' +
      '<span class="sheet__from">' + esc(header) + '</span>' +
      '<span class="sheet__arrow">→</span>' +
      '<span class="sheet__to"><select onchange="csvMap(this.value, ' +
        JSON.stringify(header).replace(/"/g, '&quot;') + ')">' +
        opts + '</select></span>' +
    '</div>';
  }).join('');

  return '<div class="sheet">' +
    '<div class="sheet__head"><span style="flex:1">Your column</span>' +
      '<span style="width:24px;flex:none"></span>' +
      '<span style="flex:1">Becomes</span></div>' + rows +
    '<div class="sheet__foot">Guessed from the header names. Anything left ' +
      'on “ignore” is not imported at all — including postage, which is ' +
      'the one figure nothing else in this system has ever been able to ' +
      'tell you.</div>' +
  '</div>';
}

function viewCsvCheck(plan) {
  var rows = plan.rows.map(function (p) {
    var bad = p.warnings.length;
    return '<div class="sheet__row' + (bad ? ' sheet__row--warn' : '') + '">' +
      '<span class="mono" style="width:88px;flex:none;font-size:12px;' +
        'color:var(--mut)">' + esc(dash(p.row.sold_at)) + '</span>' +
      '<span style="flex:1;min-width:0;font-size:13px;overflow:hidden;' +
        'text-overflow:ellipsis;white-space:nowrap">' +
        esc(dash(p.row.title)) + '</span>' +
      '<span class="mono" style="width:74px;flex:none;text-align:right;' +
        'font-size:12.5px">' + money(p.row.price) + '</span>' +
      '<span class="mono" style="width:64px;flex:none;text-align:right;' +
        'font-size:12.5px;color:var(--mut)">' + money(p.row.paid) + '</span>' +
      '<span class="mono" style="width:74px;flex:none;text-align:right;' +
        'font-size:12.5px">' + money(p.row.postage) + '</span>' +
      '<span class="tag ' + (p.outcome === 'created' ? 'tag--ok'
                            : p.outcome === 'skipped' ? 'tag--alarm'
                            : 'tag--warn') + '" style="width:78px;flex:none">' +
        esc(p.outcome) + '</span>' +
    '</div>' +
    (bad
      ? '<div class="sheet__foot sheet__foot--warn" style="padding-top:0">' +
        esc(p.warnings.join(' · ')) + '</div>'
      : '');
  }).join('');

  return '<div class="sheet">' +
    '<div class="sheet__head">' +
      '<span style="width:88px;flex:none">Date</span>' +
      '<span style="flex:1">Title</span>' +
      '<span style="width:74px;flex:none;text-align:right">Sold</span>' +
      '<span style="width:64px;flex:none;text-align:right">Paid</span>' +
      '<span style="width:74px;flex:none;text-align:right">Postage</span>' +
      '<span style="width:78px;flex:none"></span>' +
    '</div>' + rows +
    '<div class="sheet__foot">' + esc(csvSummary(plan)) + '</div>' +
  '</div>';
}

/* Not "5 rows will be imported". `upsert_listing` fills blanks and never
   overwrites, so re-importing a corrected spreadsheet reports five rows
   and changes nothing - which is the one thing a summary has to say. */
function csvSummary(plan) {
  var parts = [];
  if (plan.created) parts.push(plan.created + ' new');
  if (plan.enriched) parts.push(plan.enriched + ' filled in');
  if (plan.unchanged) {
    parts.push(plan.unchanged + ' already here and left alone');
  }
  if (plan.skipped) parts.push(plan.skipped + ' skipped');
  return parts.length
    ? parts.join(', ') + '. Existing figures are never overwritten — a ' +
      'correction has to be typed on the item itself.'
    : 'Nothing to import.';
}

function csvOver(ev, on) {
  ev.preventDefault();
  var el = document.getElementById('csvdrop');
  if (el) el.classList.toggle('drop--over', on);
}

function csvDrop(ev) {
  ev.preventDefault();
  csvOver(ev, false);
  csvChose(ev.dataTransfer && ev.dataTransfer.files);
}

function csvChose(files) {
  if (!files || !files.length) return;
  var file = files[0];
  var reader = new FileReader();
  reader.onload = function () {
    S.csvText = String(reader.result || '');
    S.csvName = file.name;
    csvPreview(1);
  };
  reader.readAsText(file);
}

function csvPreview(step) {
  once(async function () {
    S.csvPlan = await api('/api/import/preview', {
      method: 'POST',
      body: { csv: S.csvText, mapping: S.csvMapping }
    });
    S.csvMapping = S.csvPlan.mapping;
    if (step !== undefined) S.csvStep = step;
  });
}

function csvMap(field, header) {
  S.csvMapping = S.csvMapping || {};
  S.csvMapping[header] = field || null;
  csvPreview();
}

function csvStep(n) {
  S.csvStep = n;
  renderMain();
}

function csvCommit() {
  var plan = S.csvPlan;
  if (!plan) return;
  ask('Import ' + plural(plan.rows.length - plan.skipped, 'row') + '?',
      csvSummary(plan) + ' They arrive as listings — nothing here creates ' +
      'an order or a parcel code.',
      'Import', function () {
    once(async function () {
      var d = await api('/api/import/commit', {
        method: 'POST',
        body: { csv: S.csvText, mapping: S.csvMapping }
      });
      toast([d.created + ' new', d.enriched + ' filled in',
             d.unchanged + ' left alone'].join(', ') + '.');
      S.csvStep = 0;
      S.csvPlan = null;
      S.csvText = '';
      S.csvMapping = null;
      S.csvName = '';
      S.stats = null;
      go('review');
    });
  });
}

/* ---------------------------------------------------------------- boot */

window.addEventListener('hashchange', onHash);

applyTheme(S.theme);
(async function () {
  try {
    var d = await api('/api/session');
    S.authed = !!d.authenticated;
  } catch (e) { S.authed = false; }
  if (!S.authed) { render(); return; }
  render();
  loadSystem().then(renderNav, function () {});
  onHash();
})();
