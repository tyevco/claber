/* mplabel PWA.
 *
 * Vanilla, no framework and no build step - the backend is deliberately
 * stdlib Python and a toolchain would be out of place next to it. Screens
 * render to strings and get swapped in; there is one user and a handful
 * of rows, so the cost of that is nothing and the cost of a bundler is
 * a whole class of thing that can rot.
 *
 * Everything user-visible goes through esc(). Item titles and buyer names
 * arrive from Facebook emails, which means their content is chosen by
 * someone else - a listing titled `<img onerror=...>` would otherwise run
 * here, inside the session that can reach every customer address.
 */

var S = {
  screen: 'ship', orders: [], pending: [], stats: null, system: null,
  detail: null, sel: [], dry: false, toast: null, undo: null,
  theme: localStorage.getItem('mp-theme') || 'dark',
  authed: false, loginError: '', busy: false
};

var app = document.getElementById('app');

/* ------------------------------------------------------------ plumbing */

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
  var res = await fetch(path, init);
  if (res.status === 401) { S.authed = false; render(); throw new Error('401'); }
  var data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

function toast(msg, opts) {
  opts = opts || {};
  S.toast = { msg: msg, bad: !!opts.bad, undo: opts.undo || null };
  render();
  clearTimeout(toast._t);
  toast._t = setTimeout(function () { S.toast = null; render(); }, 4000);
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

function setTheme(next) {
  S.theme = next;
  localStorage.setItem('mp-theme', next);
  document.documentElement.setAttribute('data-mp', next);
  var meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', next === 'light' ? '#fbf8f2' : '#100e09');
  render();
}

/* --------------------------------------------------------- hold to hold */

var hold = { key: null, raf: 0, t0: 0, ms: 800 };

function holdStart(key, fn) {
  return function (ev) {
    ev.preventDefault();
    holdCancel();
    hold.key = key; hold.t0 = Date.now();
    var el = ev.currentTarget;
    var fill = el.querySelector('.fill');
    (function step() {
      if (hold.key !== key) return;
      var p = Math.min(1, (Date.now() - hold.t0) / hold.ms);
      if (fill) fill.style.width = (p * 100).toFixed(1) + '%';
      if (p >= 1) { holdCancel(); fn(); return; }
      hold.raf = requestAnimationFrame(step);
    })();
  };
}

function holdCancel() {
  if (hold.raf) cancelAnimationFrame(hold.raf);
  hold.raf = 0; hold.key = null;
  Array.prototype.forEach.call(document.querySelectorAll('.fill'),
    function (f) { f.style.width = '0'; });
}

/* ------------------------------------------------------------- actions */

/* One mutating action at a time. A laggy connection invites a second
   tap, and two POSTs for the same sale used to collide over the same
   stamped temp file server-side. The button is also disabled while
   this is set, so the guard is visible rather than just defensive. */
async function exclusive(fn) {
  if (S.busy) return;
  S.busy = true; render();
  try { await fn(); } finally { S.busy = false; render(); }
}

async function load() {
  try {
    var r = await Promise.all([api('/api/orders'), api('/api/pending')]);
    S.orders = r[0].orders; S.pending = r[1].pending; S.authed = true;
    render();
  } catch (e) { /* 401 already re-rendered */ }
}

async function login(ev) {
  ev.preventDefault();
  var pw = document.getElementById('pw').value;
  S.loginError = '';
  try {
    await api('/api/login', { method: 'POST', body: { password: pw } });
    S.authed = true;
    await load();
  } catch (e) {
    S.loginError = e.message === '401' ? 'Wrong password.' : e.message;
    S.authed = false;
    render();
  }
}

async function openDetail(id) {
  try { S.detail = await api('/api/orders/' + id); S.screen = 'detail'; render(); }
  catch (e) { toast(e.message, { bad: true }); }
}

async function markShipped(id, code) {
  return exclusive(async function () {
  try {
    await api('/api/orders/' + id + '/ship', { method: 'POST' });
    S.screen = 'ship';
    await load();
    toast('Code ' + code + ' is shipped and free again.',
          { undo: function () { unship(id); } });
  } catch (e) { toast(e.message, { bad: true }); }
  });
}

async function unship(id) {
  try { await api('/api/orders/' + id + '/unship', { method: 'POST' }); await load(); }
  catch (e) { toast(e.message, { bad: true }); }
}

async function reprint(id) {
  return exclusive(async function () {
  try {
    var r = await api('/api/orders/' + id + '/print', { method: 'POST' });
    await load();
    toast('Label ' + r.code + ' sent to the printer.');
  } catch (e) { toast(e.message, { bad: true }); }
  });
}

async function batchPrint() {
  if (!S.sel.length) return;
  return exclusive(async function () {
  try {
    var r = await api('/api/print/pending',
                      { method: 'POST', body: { ids: S.sel, dry_run: S.dry } });
    if (S.dry) { toast(r.would_print.length + ' would print. Nothing used.'); return; }
    S.sel = []; S.screen = 'ship';
    await load();
    var n = r.printed.length;
    toast(n + (n === 1 ? ' label' : ' labels') + ' sent to the printer.' +
          (r.failed.length ? ' ' + r.failed.length + ' failed.' : ''),
          { bad: r.failed.length > 0 });
  } catch (e) {
    // The batch may have partly succeeded before the edge timed out, so
    // reload rather than trusting the selection on screen. The server
    // skips anything already printed, which makes a re-fire safe.
    S.sel = [];
    await load();
    toast(e.message + ' Re-check Pending before printing again.',
          { bad: true });
  }
  });
}

/* -------------------------------------------------------------- screens */

function loginView() {
  return '<form class="login" onsubmit="login(event)">' +
    '<div class="title">mplabel</div>' +
    '<p>The parcel queue. One password, shared.</p>' +
    '<input class="field" id="pw" type="password" autocomplete="current-password" ' +
      'placeholder="Password" autofocus>' +
    (S.loginError ? '<p class="err">' + esc(S.loginError) + '</p>' : '') +
    '<button class="hold hold--wide" type="submit"><span>Open</span></button>' +
    '</form>';
}

function queueRow(o) {
  // Not `d`: that is the order-detail object everywhere else in this file,
  // and shadowing it here is how the wrong thing ends up rendered.
  var urg = due(o.ship_by);
  var s = split(o.item);
  var alarm = !o.printed;
  return '<div class="row">' +
    '<div class="rail"></div>' +
    '<button class="card' + (alarm ? ' card--alert' : '') +
      '" aria-label="' + esc('Parcel ' + (o.code || 'no code') + ', ' +
        (o.item || 'untitled') + ', ' + (o.buyer || 'unknown buyer') +
        ', due ' + urg.label + (alarm ? ', not printed' : '')) +
      '" onclick="openDetail(' + o.id + ')">' +
      '<div class="codecol">' +
        '<div class="code' + (alarm ? ' code--alert' : '') + '">' +
          esc(o.code || '—') + '</div>' +
        '<div class="due ' + urg.cls + '">' + esc(urg.label) + '</div>' +
      '</div>' +
      '<div class="meta">' +
        (s.lead ? '<div class="lead">' + esc(s.lead) + '</div>' : '') +
        '<div class="distinct">' + esc(s.distinct || 'Untitled') + '</div>' +
        '<div class="line"><span class="who">' + esc(o.buyer || '—') + '</span>' +
          '<span class="amt">' + money(o.price) + '</span></div>' +
        (alarm ? '<div class="warn">' + (o.has_label
            ? 'No label yet — print before it can go out'
            : 'No label at all — local pickup, or it never arrived') +
          '</div>' : '') +
      '</div>' +
    '</button></div>';
}

function shipView() {
  var pend = S.pending.length;
  var out = S.orders.filter(function (o) { return o.printed; }).length;
  var rows = S.orders.length
    ? S.orders.map(queueRow).join('') +
      '<div class="foot">Codes are only unique among parcels still here. ' +
      'One frees up the moment it ships.</div>'
    : '<div class="empty"><h2>Nothing to ship</h2>' +
      '<p>The hallway is clear.</p></div>';

  return '<div class="screen">' +
    '<div class="head">' +
      '<div style="flex:1;min-width:0">' +
        '<div class="eyebrow">' + esc(new Date().toDateString()) + '</div>' +
        '<div class="title">To ship</div>' +
      '</div>' +
      '<button class="iconbtn" onclick="go(\'settings\')" aria-label="Settings">≡</button>' +
    '</div>' +
    '<div class="chips">' +
      '<button class="chip' + (pend ? ' chip--alert' : '') +
        '" onclick="go(\'pending\')"><b>' + pend + '</b>' +
        '<span>' + (pend ? 'never printed' : 'all printed') + '</span></button>' +
      '<div class="chip"><b>' + out + '</b><span>printed</span></div>' +
      '<div class="chip"><b>' + S.orders.length + '</b><span>outstanding</span></div>' +
    '</div>' +
    '<div class="scroll">' + rows + '</div>' +
    tabs('ship') + '</div>';
}

function detailView() {
  var d = S.detail;
  if (!d) return '';
  var alarm = !d.printed;
  var addr = (d.ship_to || '').split(',').map(function (l) {
    return '<span>' + esc(l.trim()) + '</span>';
  }).join('');

  return '<div class="screen">' +
    '<div class="head head--row head--rule">' +
      '<button class="iconbtn" onclick="go(\'ship\')" aria-label="Back">←</button>' +
      '<span style="font-family:var(--mv-font-mono);font-size:12px;color:var(--mut)">' +
        esc(d.order_id || '—') + '</span>' +
    '</div>' +
    '<div class="scroll" style="padding-top:15px;gap:14px;padding-bottom:200px">' +
      '<div style="display:flex;gap:13px;align-items:flex-start">' +
        '<div class="bigcode' + (alarm ? ' bigcode--alert' : '') + '">' +
          '<b>' + esc(d.code || '—') + '</b><span>TOP RIGHT</span></div>' +
        '<div style="flex:1;min-width:0">' +
          '<div class="state" style="color:var(--' + (alarm ? 'al' : 'ac') + ')">' +
            (alarm ? 'Not printed' : esc(d.status || 'printed')) + '</div>' +
          '<div style="font-size:15px;font-weight:600;line-height:1.32;margin-top:5px">' +
            esc(d.item || 'Untitled') + '</div>' +
        '</div>' +
      '</div>' +

      '<dl class="grid" style="margin:0">' +
        cell('Sold for', money(d.price), true) +
        cell('Ship by', esc(d.ship_by || '—'), true) +
        cell('Buyer', esc(d.buyer || '—')) +
        cell('Weight', esc(d.weight || '—')) +
        cell('Service', esc(d.service || '—')) +
        cell('Printed', d.print_count ? d.print_count + '×' : 'never') +
      '</dl>' +

      '<div class="panel">' +
        '<div><dt style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;' +
          'color:var(--mut);font-weight:600">Tracking</dt>' +
          '<div class="track">' + esc(d.tracking || '—') + '</div></div>' +
        '<div class="sep"></div>' +
        '<div><dt style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;' +
          'color:var(--mut);font-weight:600">Ships to</dt>' +
          '<div class="addr">' + (addr || '<span>—</span>') + '</div>' +
          '<div class="hint">The address only appears here. The queue shows a code.</div>' +
        '</div>' +
      '</div>' +

      (d.has_label
        ? '<a class="panel" style="text-align:center;font-size:13.5px;font-weight:600" ' +
          'href="/api/orders/' + d.id + '/label" target="_blank" rel="noopener">' +
          'Open the label PDF</a>'
        : '') +

      '<div class="panel"><div class="sectionhead">Fix a field</div>' +
        '<div class="note">Titles come in mangled sometimes. This corrects the ' +
          'record and the sheet follows on the next sync.</div>' +
        '<input class="field" id="fx-item" value="' + esc(d.item || '') + '">' +
        '<input class="field" id="fx-price" inputmode="decimal" value="' +
          esc(d.price === null ? '' : d.price) + '">' +
        '<textarea class="field" id="fx-notes" rows="2" placeholder="Note">' +
          esc(d.notes || '') + '</textarea>' +
        '<button class="hold hold--wide" style="height:44px" ' +
          'onclick="saveFields(' + d.id + ')"><span>Save</span></button>' +
      '</div>' +
    '</div>' +

    '<div class="dock">' +
      '<button class="hold" id="h-ship"' + (S.busy ? ' disabled' : '') +
        '><span class="fill"></span>' +
        '<span>Hold to mark shipped</span></button>' +
      '<button class="hold hold--ghost" id="h-print"' +
        (d.has_label ? '' : ' disabled') + '><span class="fill"></span>' +
        '<span>' + (d.printed ? 'Reprint' : 'Print') + '</span></button>' +
    '</div>' + tabs('ship') + '</div>';
}

function cell(label, value, mono) {
  return '<div class="cell"><dt>' + label + '</dt>' +
    '<dd' + (mono ? ' class="mono"' : '') + '>' + value + '</dd></div>';
}

async function saveFields(id) {
  var body = {
    item: document.getElementById('fx-item').value,
    notes: document.getElementById('fx-notes').value
  };
  var p = document.getElementById('fx-price').value.trim();
  if (p !== '') body.price = p;
  try {
    S.detail = await api('/api/orders/' + id + '/fields',
                         { method: 'POST', body: body });
    await load();
    toast('Saved.');
  } catch (e) { toast(e.message, { bad: true }); }
}

function pendingView() {
  var rows = S.pending.map(function (p) {
    var on = S.sel.indexOf(p.id) >= 0;
    return '<button class="card" role="checkbox" aria-checked="' + on + '" ' +
      'aria-label="' + esc((on ? 'Selected. ' : '') + 'Parcel ' +
        (p.code || 'no code') + ', ' + (p.item || 'untitled')) + '" ' +
      'onclick="toggleSel(' + p.id + ')">' +
      '<div class="check" data-on="' + (on ? 1 : 0) + '">' + (on ? '✓' : '') + '</div>' +
      '<div class="code" style="width:54px;height:40px;font-size:20px">' +
        esc(p.code || '—') + '</div>' +
      '<div class="meta"><div class="distinct">' + esc(p.item || 'Untitled') + '</div>' +
        '<div class="lead">' + money(p.price) + '</div></div>' +
      '</button>';
  }).join('');

  return '<div class="screen">' +
    '<div class="head head--row">' +
      '<button class="iconbtn" onclick="go(\'ship\')" aria-label="Back">←</button>' +
      '<div style="flex:1"><div class="eyebrow">Recovery</div>' +
        '<div class="title title--sm">Pending labels</div></div>' +
    '</div>' +
    '<div class="scroll" style="gap:10px;padding-bottom:190px">' +
      '<div class="note">These were recorded but never came out. Usually the ' +
        'printer was off. Pick the ones to run again.</div>' +
      (rows || '<div class="empty"><h2>Nothing pending</h2>' +
        '<p>Every recorded label has been printed.</p></div>') +
      (S.pending.length ? '<button class="card" style="align-items:center" ' +
        'onclick="toggleDry()"><div class="check" data-on="' + (S.dry ? 1 : 0) +
        '">' + (S.dry ? '✓' : '') + '</div><div class="meta">' +
        '<div class="distinct">Dry run</div>' +
        '<div class="lead">Show what would print. No labels used.</div>' +
        '</div></button>' : '') +
    '</div>' +
    (S.pending.length ? '<div class="dock">' +
      '<button class="hold hold--wide" id="h-batch"' +
        (S.sel.length ? '' : ' disabled') + '><span class="fill"></span><span>' +
        (S.sel.length
          ? (S.dry ? 'Hold to preview ' : 'Hold to print ') + S.sel.length
          : 'Choose the ones to run again') +
        '</span></button></div>' : '') +
    tabs('pending') + '</div>';
}

function profitView() {
  var st = S.stats;
  if (!st) return '<div class="screen"><div class="scroll">' +
    '<div class="empty"><p>Loading…</p></div></div>' + tabs('profit') + '</div>';

  var bands = (st.price_bands || []).map(function (b) {
    var pct = b.sell_through_pct || 0;
    return '<div style="display:flex;flex-direction:column;gap:5px">' +
      '<div style="display:flex;align-items:baseline;gap:8px">' +
        '<span style="font-family:var(--mv-font-mono);font-size:12.5px;' +
          'font-weight:600;width:64px;flex:none">' + esc(b.price_band) + '</span>' +
        '<span style="font-size:11.5px;color:var(--sub);flex:1">' +
          esc((b.sold || 0) + ' of ' + (b.listed || 0)) +
          (b.avg_days_to_sell ? ', ' + esc(b.avg_days_to_sell) + 'd avg' : '') +
        '</span>' +
        '<span style="font-family:var(--mv-font-mono);font-size:12.5px;' +
          'font-weight:600">' + pct + '%</span>' +
      '</div><div class="bar"><i style="width:' + Math.min(100, pct) + '%"></i></div></div>';
  }).join('');

  var aging = (st.aging || []).map(function (a) {
    return '<div style="display:flex;gap:10px;align-items:baseline;' +
      'padding-bottom:9px;border-bottom:1px solid var(--bd)">' +
      '<span style="font-size:13px;flex:1;min-width:0;line-height:1.35">' +
        esc(a.title || 'Untitled') + '</span>' +
      '<span style="font-family:var(--mv-font-mono);font-size:12px;color:var(--wa)">' +
        esc(a.days_listed) + 'd</span>' +
      '<span style="font-family:var(--mv-font-mono);font-size:12.5px;font-weight:600">' +
        money(a.price) + '</span></div>';
  }).join('');

  var m = (st.monthly || [])[0];
  return '<div class="screen">' +
    '<div class="head head--row">' +
      '<button class="iconbtn" onclick="go(\'ship\')" aria-label="Back">←</button>' +
      '<div class="eyebrow" style="flex:1">' + esc(m ? m.month : 'This month') + '</div>' +
    '</div>' +
    '<div class="scroll" style="gap:22px">' +
      '<div><div style="font-family:var(--mv-font-display);font-weight:500;' +
        'font-size:56px;letter-spacing:-.03em;line-height:1;color:var(--ac)">' +
        money(m ? m.gross : 0) + '</div>' +
        '<div class="note" style="margin-top:7px">gross across ' +
          esc(m ? m.orders : 0) + ' orders. ' +
          '<strong>Cost basis is not recorded yet</strong>, so this is not ' +
          'profit — see the note below.</div></div>' +
      '<div class="soon"><div class="sectionhead">Net and margin</div>' +
        '<div class="note">The design shows kept-after-cost. That needs a ' +
        'purchase price and postage per item, which the database does not ' +
        'hold yet. Reporting a margin without them would be a confident ' +
        'lie, so this shows gross until the cost work lands.</div></div>' +
      (bands ? '<div style="display:flex;flex-direction:column;gap:11px">' +
        '<div class="sectionhead">Sell-through by price</div>' + bands + '</div>' : '') +
      (aging ? '<div style="display:flex;flex-direction:column;gap:9px">' +
        '<div class="sectionhead">Longest listed</div>' + aging + '</div>' : '') +
    '</div>' + tabs('profit') + '</div>';
}

function settingsView() {
  var s = S.system || {};
  return '<div class="screen">' +
    '<div class="head head--row head--rule">' +
      '<button class="iconbtn" onclick="go(\'ship\')" aria-label="Back">←</button>' +
      '<div class="title title--xs" style="flex:1">Settings</div>' +
    '</div>' +
    '<div class="scroll" style="gap:18px;padding-top:15px">' +
      '<div style="display:flex;flex-direction:column;gap:9px">' +
        '<div class="sectionhead">Yours</div>' +
        '<div class="panel list" style="padding:0">' +
          '<button class="item" onclick="setTheme(\'' +
            (S.theme === 'dark' ? 'light' : 'dark') + '\')">' +
            '<span>Appearance</span><span class="val">' +
            (S.theme === 'dark' ? 'Dark' : 'Light') + '</span></button>' +
          '<button class="item" onclick="logout()">' +
            '<span>Sign out</span><span class="val">→</span></button>' +
        '</div>' +
      '</div>' +
      '<div style="display:flex;flex-direction:column;gap:9px">' +
        '<div style="display:flex;align-items:baseline;gap:8px">' +
          '<div class="sectionhead" style="flex:1">Maintenance</div>' +
          '<span style="font-size:11px;color:var(--sub)">for whoever runs the Pi</span>' +
        '</div>' +
        '<div class="panel list" style="padding:0">' +
          item('Backend', s.backend) + item('Device', s.device) +
          item('Darkness', s.darkness ? s.darkness + ' / 15' : null) +
          item('Label gap', s.gap_inches ? s.gap_inches + ' in' : null) +
          item('Mail poll', s.poll_seconds ? 'every ' + esc(s.poll_seconds) + 's' : null) +
          item('Last print', s.last_printed_at) +
        '</div>' +
        '<div class="note" style="font-family:var(--mv-font-mono);font-size:10.5px">' +
          'Printer settings are read-only here. They live in ' +
          '/etc/mplabel.conf on the Pi.</div>' +
      '</div>' +
    '</div>' + tabs('settings') + '</div>';
}

function item(label, value) {
  return '<div class="item"><span>' + esc(label) + '</span>' +
    '<span class="val">' + esc(value || '—') + '</span></div>';
}

/* The sourcing half of the design - Capture, Triage, Inventory, Add an
   item - is drawn against a backend that does not exist yet. Showing it
   with invented numbers on a system she runs real orders through would
   be worse than showing nothing. */
function soonView(title, body) {
  return '<div class="screen">' +
    '<div class="head"><div style="flex:1"><div class="eyebrow">Not built yet</div>' +
      '<div class="title">' + esc(title) + '</div></div></div>' +
    '<div class="scroll"><div class="soon"><div class="note">' + esc(body) +
      '</div></div></div>' + tabs('sourcing') + '</div>';
}

function tabs(active) {
  var defs = [
    ['ship', 'To ship', S.orders.filter(function (o) { return !o.printed; }).length],
    ['pending', 'Pending', S.pending.length],
    ['sourcing', 'Sourcing', 0],
    ['profit', 'Profit', 0]
  ];
  return '<nav class="tabs">' + defs.map(function (t) {
    return '<button class="tab" aria-current="' + (t[0] === active) +
      '" onclick="go(\'' + t[0] + '\')"><i></i><span>' + t[1] + '</span>' +
      (t[2] ? '<span class="badge">' + t[2] + '</span>' : '') + '</button>';
  }).join('') + '</nav>';
}

/* ---------------------------------------------------------------- shell */

function go(screen) {
  S.screen = screen;
  if (screen === 'profit' && !S.stats) {
    api('/api/stats').then(function (d) { S.stats = d; render(); })
      .catch(function () {});
  }
  if (screen === 'settings' && !S.system) {
    api('/api/system').then(function (d) { S.system = d; render(); })
      .catch(function () {});
  }
  render();
}

function toggleSel(id) {
  var i = S.sel.indexOf(id);
  if (i >= 0) S.sel.splice(i, 1); else S.sel.push(id);
  render();
}
function toggleDry() { S.dry = !S.dry; render(); }

async function logout() {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) {}
  S.authed = false; S.orders = []; S.pending = []; render();
}

function render() {
  document.documentElement.setAttribute('data-mp', S.theme);
  var body;
  if (!S.authed) body = loginView();
  else if (S.screen === 'detail') body = detailView();
  else if (S.screen === 'pending') body = pendingView();
  else if (S.screen === 'profit') body = profitView();
  else if (S.screen === 'settings') body = settingsView();
  else if (S.screen === 'sourcing') body = soonView('Sourcing',
    'Receipt capture, triage and the cost basis they feed are designed but ' +
    'not built. Until the database can hold what an item cost, this screen ' +
    'would only show invented numbers.');
  else body = shipView();

  if (S.toast) {
    body += '<div class="toast' + (S.toast.bad ? ' bad' : '') + '"><i></i>' +
      '<p>' + esc(S.toast.msg) + '</p>' +
      (S.toast.undo ? '<button onclick="doUndo()">Undo</button>' : '') + '</div>';
  }
  app.innerHTML = body;
  wireHolds();
}

function doUndo() {
  var fn = S.toast && S.toast.undo;
  S.toast = null;
  if (fn) fn();
}

/* Bound after each render rather than inline, because a hold needs
   pointerdown/up/leave together and inline attributes for three events on
   one element get unreadable fast. */
function wireHolds() {
  var d = S.detail;
  var pairs = [
    ['h-ship', function () { markShipped(d.id, d.code); }],
    ['h-print', function () { reprint(d.id); }],
    ['h-batch', batchPrint]
  ];
  pairs.forEach(function (p) {
    var el = document.getElementById(p[0]);
    if (!el || el.disabled) return;
    el.addEventListener('pointerdown', holdStart(p[0], p[1]));
    ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (ev) {
      el.addEventListener(ev, holdCancel);
    });
  });
}

setTheme(S.theme);
api('/api/session').then(function (d) {
  S.authed = !!d.authenticated;
  if (S.authed) load(); else render();
}).catch(function () { render(); });
