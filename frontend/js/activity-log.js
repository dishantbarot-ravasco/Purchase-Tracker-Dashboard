// admin.html's Activity Log tab (2026-10-01) - who signed in, what they
// changed or downloaded, and which pages they opened. The server side is
// apps/api/routers/activity_views.py over apps/services/activity_log.py.
//
// Loaded after admin-page.js, whose switchAdminTab() calls openActivityLog()
// the first time the tab opens; nothing is fetched before that. Every name
// at the top level starts with "act" - all page scripts share one global
// scope (docs/frontend.md), so a repeated name would silently override.
//
// Two parts: the People table (/api/activity/people: last sign-in, last
// seen, 30-day counts) and the Log (/api/activity, 100 rows a page, newest
// first, filtered by person / type / dates / free text). A log row opens to
// show the request it came from - path, IP, browser, time taken and the
// redacted body that was sent - built with textContent, never innerHTML,
// because that body is whatever the user typed.

let actLoaded = false;
let actPage = 1;
let actTotal = 0;
let actPageSize = 100;
let actRequestId = 0;
let actSearchTimer = null;

const ACT_TYPE_CLASS = {
  login: 'signin', logout: 'signin', sessions_revoked: 'signin', auth_failed: 'failed',
  user_created: 'account', user_updated: 'account', user_deleted: 'account', device_revoked: 'account',
  change: 'change', download: 'download', page_view: 'visit',
};

const ACT_PLANT_LABELS = { 'HRS': 'HRS', 'RTP-ACHHAD': 'Achhad', 'RTP-VAPI': 'Vapi' };

function openActivityLog() {
  if (actLoaded) return;
  actLoaded = true;
  document.getElementById('actExportBtn').onclick = actExport;
  document.getElementById('actClearBtn').onclick = actClearFilters;
  document.getElementById('actNewerBtn').onclick = () => actGo(actPage - 1);
  document.getElementById('actOlderBtn').onclick = () => actGo(actPage + 1);
  ['actActor', 'actGroup', 'actSince', 'actUntil'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => actGo(1));
  });
  document.getElementById('actQ').addEventListener('input', () => {
    clearTimeout(actSearchTimer);
    actSearchTimer = setTimeout(() => actGo(1), 300);
  });
  actLoadPeople();
  actGo(1);
}

function actWhen(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** "5 min ago", "3 h ago", "2 days ago" - the People table's last seen. */
function actAgo(iso) {
  if (!iso) return 'Never';
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return mins + ' min ago';
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + ' h ago';
  const days = Math.round(hours / 24);
  return days + (days === 1 ? ' day ago' : ' days ago');
}

function actFilters() {
  const params = new URLSearchParams();
  const pairs = { actor: 'actActor', group: 'actGroup', since: 'actSince', until: 'actUntil', q: 'actQ' };
  Object.keys(pairs).forEach(key => {
    const value = document.getElementById(pairs[key]).value.trim();
    if (value) params.set(key, value);
  });
  return params;
}

function actClearFilters() {
  ['actActor', 'actGroup', 'actSince', 'actUntil', 'actQ'].forEach(id => { document.getElementById(id).value = ''; });
  actGo(1);
}

function actExport() {
  // Same pattern as the stock snapshot export: the session cookie rides
  // along on a plain navigation, and the server sends an attachment.
  const qs = actFilters().toString();
  window.open('/api/activity/export' + (qs ? '?' + qs : ''), '_blank');
}

// ── People ───────────────────────────────────────────────────────

async function actLoadPeople() {
  const area = document.getElementById('actPeopleArea');
  area.innerHTML = '<div class="no-data-note">Loading&hellip;</div>';
  let people;
  try {
    const res = await authFetch('/api/activity/people', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    people = (await res.json()).people || [];
  } catch (e) {
    console.error('activity: people failed:', e);
    area.innerHTML = '<div class="no-data-note">Couldn\'t load right now.</div>';
    return;
  }
  const select = document.getElementById('actActor');
  const current = select.value;
  select.innerHTML = '<option value="">Everyone</option>' + people.map(p =>
    '<option value="' + p.id + '">' + escapeHtml(p.name ? p.name + ' (' + p.email + ')' : p.email) + '</option>'
  ).join('');
  select.value = current;

  if (!people.length) {
    area.innerHTML = '<div class="no-data-note">No accounts yet.</div>';
    return;
  }
  const head = '<tr><th>Person</th><th>Role</th><th>Last sign-in</th><th>Last active</th>' +
    '<th class="num">Sign-ins</th><th class="num">Changes</th><th class="num">Downloads</th>' +
    '<th class="num">Page visits</th><th class="num">Refused sign-ins</th></tr>';
  const body = people.map(p => {
    const who = '<button type="button" class="act-person" data-act-person="' + p.id + '">' +
      escapeHtml(p.name || p.email) + '</button>' +
      (p.name ? '<div class="act-sub">' + escapeHtml(p.email) + '</div>' : '') +
      (p.active ? '' : '<span class="plants-pill">Inactive</span>');
    return '<tr><td>' + who + '</td><td>' + escapeHtml(p.role) + '</td>' +
      '<td>' + escapeHtml(actWhen(p.lastLogin)) + '</td>' +
      '<td title="' + escapeHtml(actWhen(p.lastSeen)) + '">' + escapeHtml(actAgo(p.lastSeen)) + '</td>' +
      '<td class="num">' + p.signins + '</td><td class="num">' + p.changes + '</td>' +
      '<td class="num">' + p.downloads + '</td><td class="num">' + p.visits + '</td>' +
      '<td class="num' + (p.failed ? ' act-warn' : '') + '">' + p.failed + '</td></tr>';
  }).join('');
  area.innerHTML = '<table class="admin-activity-table act-table"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  area.querySelectorAll('[data-act-person]').forEach(btn => btn.addEventListener('click', () => {
    document.getElementById('actActor').value = btn.dataset.actPerson;
    actGo(1);
    document.getElementById('actLogArea').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
}

// ── Log ──────────────────────────────────────────────────────────

async function actGo(page) {
  if (page < 1) return;
  const requestId = ++actRequestId;
  const params = actFilters();
  params.set('page', String(page));
  const area = document.getElementById('actLogArea');
  const summary = document.getElementById('actSummary');
  summary.textContent = 'Loading...';
  let data;
  try {
    const res = await authFetch('/api/activity?' + params.toString(), { credentials: 'same-origin' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    data = body;
  } catch (e) {
    if (requestId !== actRequestId) return;
    console.error('activity: log failed:', e);
    summary.textContent = 'Couldn\'t load the log: ' + e.message;
    area.innerHTML = '';
    return;
  }
  // A newer filter change has already been sent - drop this response.
  if (requestId !== actRequestId) return;

  actPage = data.page;
  actTotal = data.total;
  actPageSize = data.pageSize || actPageSize;
  const groupSelect = document.getElementById('actGroup');
  if (groupSelect.options.length <= 1) {
    (data.groups || []).forEach(g => {
      const opt = document.createElement('option');
      opt.value = g.key;
      opt.textContent = g.label;
      groupSelect.appendChild(opt);
    });
  }
  actRenderRows(data.rows || []);
  const first = actTotal ? (actPage - 1) * actPageSize + 1 : 0;
  const last = Math.min(actPage * actPageSize, actTotal);
  summary.textContent = actTotal ? (first + '-' + last + ' of ' + actTotal.toLocaleString('en-IN') + ' entries') : 'No activity matches.';
  document.getElementById('actPageInfo').textContent = actTotal ? 'Page ' + actPage + ' of ' + Math.max(1, Math.ceil(actTotal / actPageSize)) : '';
  document.getElementById('actNewerBtn').disabled = actPage <= 1;
  document.getElementById('actOlderBtn').disabled = last >= actTotal;
}

function actResult(row) {
  if (row.status == null) return '';
  if (row.status < 400) return '<span class="act-ok">OK</span>';
  return '<span class="act-refused">Refused (' + row.status + ')</span>';
}

function actRenderRows(rows) {
  const area = document.getElementById('actLogArea');
  if (!rows.length) {
    area.innerHTML = '';
    return;
  }
  const head = '<tr><th>When</th><th>Who</th><th>What</th><th>Type</th><th>Plant</th><th>Result</th></tr>';
  const body = rows.map((r, i) => {
    const who = r.actorName ? escapeHtml(r.actorName) + '<div class="act-sub">' + escapeHtml(r.actorEmail) + '</div>'
      : escapeHtml(r.actorEmail || 'Unknown');
    return '<tr class="act-row" data-act-row="' + i + '" tabindex="0" aria-expanded="false">' +
      '<td class="act-when">' + escapeHtml(actWhen(r.at)) + '</td>' +
      '<td>' + who + '</td>' +
      '<td>' + escapeHtml(r.detail) + '</td>' +
      '<td><span class="act-pill act-' + (ACT_TYPE_CLASS[r.action] || 'change') + '">' + escapeHtml(r.actionLabel) + '</span></td>' +
      '<td>' + escapeHtml(ACT_PLANT_LABELS[r.plant] || r.plant || '') + '</td>' +
      '<td>' + actResult(r) + '</td></tr>';
  }).join('');
  area.innerHTML = '<table class="admin-activity-table act-table"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  area.querySelectorAll('[data-act-row]').forEach(tr => {
    const toggle = () => actToggleDetail(tr, rows[Number(tr.dataset.actRow)]);
    tr.addEventListener('click', toggle);
    tr.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });
}

/** Opens (or closes) the request behind a log row, under that row. */
function actToggleDetail(tr, row) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('act-detail-row')) {
    next.remove();
    tr.setAttribute('aria-expanded', 'false');
    return;
  }
  tr.setAttribute('aria-expanded', 'true');
  const detailRow = document.createElement('tr');
  detailRow.className = 'act-detail-row';
  const cell = document.createElement('td');
  cell.colSpan = 6;
  const facts = [
    ['Request', row.method && row.path ? row.method + ' ' + row.path : row.path],
    ['IP address', row.ip],
    ['Browser', row.userAgent],
    ['Took', row.durationMs != null ? row.durationMs + ' ms' : ''],
  ];
  const list = document.createElement('dl');
  list.className = 'act-facts';
  facts.filter(f => f[1]).forEach(([label, value]) => {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    list.appendChild(dt);
    list.appendChild(dd);
  });
  cell.appendChild(list);
  if (row.payload != null) {
    const label = document.createElement('div');
    label.className = 'act-sub';
    label.textContent = 'What was sent (passwords, codes and tokens are masked):';
    const pre = document.createElement('pre');
    pre.className = 'act-payload';
    pre.textContent = JSON.stringify(row.payload, null, 2);
    cell.appendChild(label);
    cell.appendChild(pre);
  }
  detailRow.appendChild(cell);
  tr.after(detailRow);
}
