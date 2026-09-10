/* mplabel — what the phone app and the desk portal both need.
 *
 * There are two front ends now and one server. These are the pieces where
 * having two copies would be a real bug rather than a tidiness complaint:
 *
 *   esc()  is the XSS guard. Item titles and buyer names arrive from
 *          Facebook emails, so their content is chosen by someone else -
 *          a listing titled `<img onerror=...>` would otherwise run
 *          inside the session that can reach every customer address. Two
 *          copies means one of them gets fixed.
 *   api()  carries the `X-Mplabel: 1` header the server requires on every
 *          authenticated non-GET, and turns a 401 into a login screen. A
 *          second copy that forgets the header is a client where every
 *          mutation fails with a 400 and nothing says why.
 *
 * money(), due() and split() are here because both clients render the
 * same rows out of the same endpoints, and a parcel that reads OVERDUE on
 * one screen and TOMORROW on the other is worse than either.
 *
 * Loaded before app.js / desk.js, which is why these are plain globals:
 * there is no build step and no module loader, deliberately.
 */

/* Each client fills these in before its first api() call. Kept as one
   object rather than as loose globals so a missing hook is a no-op
   rather than a ReferenceError halfway through a fetch. */
var MP = {
  /* What to do when the server says the session is gone. The phone flips
     to its login view; the desk does the same. */
  onUnauthorized: function () {}
};

function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
             '"': '&quot;', "'": '&#39;' }[c];
  });
}

function money(n) {
  if (n === null || n === undefined || n === '') return '—';
  return '$' + Number(n).toFixed(2);
}

async function api(path, opts) {
  opts = opts || {};
  var init = {
    method: opts.method || 'GET',
    headers: { 'X-Mplabel': '1' },
    credentials: 'same-origin'
  };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers['Content-Type'] = 'application/json';
  }
  /* Raw bytes with a real type, for the routes that take a file: a
     photograph and a label PDF. Deliberately not multipart - one file
     and no other fields, so the query string carries the rest and there
     is no parser on either end to get wrong. */
  if (opts.raw !== undefined) {
    init.body = opts.raw;
    init.headers['Content-Type'] = opts.type || 'application/octet-stream';
  }
  var res = await fetch(path, init);
  if (res.status === 401) { MP.onUnauthorized(); throw new Error('401'); }
  var data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

/* Ship-by is a hard Facebook commitment, so the queue sorts by urgency
   and says it in words rather than making her subtract dates. */
function due(shipBy) {
  if (!shipBy) return { label: '—', cls: '' };
  var today = new Date(); today.setHours(0, 0, 0, 0);
  var d = new Date(shipBy + 'T00:00:00');
  var days = Math.round((d - today) / 86400000);
  if (days < 0) return { label: 'OVERDUE', cls: 'due--now' };
  if (days === 0) return { label: 'TODAY', cls: 'due--now' };
  if (days === 1) return { label: 'TOMORROW', cls: 'due--soon' };
  return { label: days + ' DAYS', cls: '' };
}

/* Her titles run long and share their first sixty characters, so the
   shared opening becomes a quiet lead line and the part that actually
   tells two listings apart gets the weight. */
function split(title) {
  var t = title || '';
  var cut = t.indexOf(' — ');
  if (cut > 0 && cut < 60) {
    return { lead: t.slice(0, cut + 2), distinct: t.slice(cut + 3) };
  }
  return { lead: '', distinct: t };
}

/* The attribute, the store and the status-bar colour - everything about
   a theme change except re-rendering, which only the client knows how to
   do. `data-mp` is read by tokens.css and by nothing else. */
function applyTheme(next) {
  localStorage.setItem('mp-theme', next);
  document.documentElement.setAttribute('data-mp', next);
  var meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', next === 'light' ? '#fbf8f2' : '#100e09');
}

function savedTheme() {
  return localStorage.getItem('mp-theme') || 'dark';
}
