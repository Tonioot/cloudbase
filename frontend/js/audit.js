import { api } from './api.js';

const LIMIT = 100;
let page = 0;
let hasMore = true;
let allLoaded = [];

const BADGE_COLOR = {
  'app.start':              'var(--green)',
  'app.stop':               'var(--red)',
  'app.restart':            'var(--yellow)',
  'app.deploy':             'var(--blue)',
  'app.pull':               '#bc8cff',
  'app.rebuild':            'var(--blue)',
  'app.config_update':      'var(--text-muted)',
  'app.delete':             'var(--red)',
  'app.zero_downtime_deploy': 'var(--green)',
  'auth.login':             'var(--text-muted)',
  'auth.logout':            'var(--text-muted)',
  'auth.change_password':   'var(--yellow)',
  'user.create':            'var(--green)',
  'user.update':            'var(--blue)',
  'user.delete':            'var(--red)',
  'node.connect':           'var(--green)',
  'node.enable':            'var(--green)',
  'node.disable':           'var(--yellow)',
  'node.rename':            'var(--blue)',
  'node.delete':            'var(--red)',
};

export async function initAuditLogs() {
  await loadPage();
  document.getElementById('btn-load-more')?.addEventListener('click', loadMore);
  document.getElementById('filter-actor')?.addEventListener('input', renderFiltered);
  document.getElementById('filter-action')?.addEventListener('change', renderFiltered);
}

async function loadPage() {
  const wrap = document.getElementById('audit-log-wrap');
  if (page === 0) wrap.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:13px">Loading…</div>';
  try {
    const entries = await api.getAuditLog(null, LIMIT, page * LIMIT);
    hasMore = entries.length === LIMIT;
    allLoaded.push(...entries);
    syncActionFilterOptions();
    renderFiltered();
    const btn = document.getElementById('btn-load-more');
    if (btn) btn.style.display = hasMore ? '' : 'none';
    page++;
  } catch (e) {
    wrap.innerHTML = `<div style="padding:20px;color:var(--red);font-size:13px">${e.message}</div>`;
  }
}

function syncActionFilterOptions() {
  const sel = document.getElementById('filter-action');
  if (!sel) return;

  const prev = sel.value || '';
  const actions = Array.from(new Set(allLoaded.map(e => e.action).filter(Boolean))).sort();

  sel.innerHTML = '<option value="">All actions</option>' +
    actions.map(a => `<option value="${a}">${a}</option>`).join('');

  if (prev && actions.includes(prev)) {
    sel.value = prev;
  }
}

async function loadMore() {
  const btn = document.getElementById('btn-load-more');
  if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
  await loadPage();
  if (btn) { btn.disabled = false; btn.textContent = 'Load more'; }
}

function renderFiltered() {
  const actor  = (document.getElementById('filter-actor')?.value  || '').toLowerCase();
  const action = document.getElementById('filter-action')?.value  || '';

  let entries = allLoaded;
  if (actor)  entries = entries.filter(e => (e.actor || '').toLowerCase().includes(actor));
  if (action) entries = entries.filter(e => e.action === action);

  const wrap = document.getElementById('audit-log-wrap');
  if (!entries.length) {
    wrap.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:13px">No events found.</div>';
    return;
  }

  const rows = entries.map(e => {
    const color   = BADGE_COLOR[e.action] || 'var(--text-muted)';
    const appName = e.detail?.name ? `<span style="color:var(--text-secondary)">${e.detail.name}</span>` : '';
    const detail  = e.detail
      ? Object.entries(e.detail).filter(([k]) => k !== 'name').map(([k, v]) => `${k}: ${v}`).join(' · ')
      : '';
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="white-space:nowrap;font-size:11px;color:var(--text-muted);padding:7px 14px">${new Date(e.timestamp).toLocaleString()}</td>
      <td style="padding:7px 14px"><span style="font-size:11px;font-weight:600;color:${color};font-family:monospace">${e.action}</span></td>
      <td style="font-size:11px;color:var(--text-muted);padding:7px 14px">${appName}</td>
      <td style="font-size:11px;color:var(--text-muted);padding:7px 14px;max-width:260px;overflow:hidden;text-overflow:ellipsis">${detail}</td>
      <td style="font-size:11px;color:var(--text-muted);padding:7px 14px;font-family:monospace">${e.actor || ''}</td>
    </tr>`;
  }).join('');

  wrap.innerHTML = `<table style="width:100%;border-collapse:collapse">
    <thead><tr style="font-size:11px;color:var(--text-muted);text-align:left;border-bottom:1px solid var(--border);background:var(--bg-secondary)">
      <th style="padding:7px 14px;font-weight:500">Time</th>
      <th style="padding:7px 14px;font-weight:500">Action</th>
      <th style="padding:7px 14px;font-weight:500">App</th>
      <th style="padding:7px 14px;font-weight:500">Detail</th>
      <th style="padding:7px 14px;font-weight:500">User</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}
