import { api, wsNodeEvents } from './api.js';
import { icon, typeIcon, badge, toast, confirm, spinner, setBtn } from './utils.js';
import { openDeployModal } from './modal.js';

let appsData = [];
let nodesData = [];
const _pingIntervals = new Map();

function renderPortRows(app) {
  const replicas = app.replicas || [];
  if (replicas.length) {
    const ports = replicas.map(r => r.external_port).filter(Boolean);
    const portStr = ports.length ? ports.map(p => `:${p}`).join(', ') : 'no port';
    return `<div class="app-meta-row">${icon.terminal}<span>Internal :${app.port || '?'} · ${ports.length} instance${ports.length !== 1 ? 's' : ''} (${portStr})</span></div>`;
  }
  if (app.port) {
    return `<div class="app-meta-row">${icon.terminal}<span>Port ${app.port}</span></div>`;
  }
  return '';
}

/* ─── Init ──────────────────────────────────────────────────────────────── */
export async function initDashboard() {
  document.getElementById('btn-deploy').addEventListener('click', () => {
    openDeployModal(app => {
      toast(`"${app.name}" deployed successfully`);
      window.location.href = `/app?id=${app.id}`;
    });
  });

  document.getElementById('btn-add-node')?.addEventListener('click', () => openAddNodeModal());

  await Promise.all([loadApps(), loadNodes()]);
  setInterval(loadApps, 6000);
  setInterval(loadNodes, 15000);
}

/* ─── Load apps ─────────────────────────────────────────────────────────── */
async function loadApps() {
  try {
    appsData = await api.listApps();
    renderStats();
    renderApps();
    if (nodesData.length) renderNodes();
  } catch (e) {
    console.error('Failed to load apps:', e);
  }
}

/* ─── Load nodes ─────────────────────────────────────────────────────────── */
async function loadNodes() {
  try {
    nodesData = await api.listNodes();
    renderNodes();
  } catch (e) {
    console.error('Failed to load nodes:', e);
  }
}

/* ─── Relative time ─────────────────────────────────────────────────────── */
function timeAgo(iso) {
  if (!iso) return 'never';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 5)  return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

/* ─── Nodes grid ─────────────────────────────────────────────────────────── */
function renderNodes() {
  const grid = document.getElementById('nodes-grid');
  if (!grid) return;

  if (!nodesData.length) {
    grid.innerHTML = `<div class="card" style="padding:18px;grid-column:1/-1;font-size:13px;color:var(--text-muted)">No nodes connected yet. Click <strong>Add Node</strong> to connect a remote Cloudbase installation.</div>`;
    return;
  }

  grid.innerHTML = nodesData.map(n => nodeCardHTML(n)).join('');

  grid.querySelectorAll('.node-card').forEach(card => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      const id = card.dataset.nodeId;
      if (id) window.location.href = `/node?id=${id}`;
    });
  });

  // Auto-ping all non-local nodes — clear old intervals first to avoid stacking
  _pingIntervals.forEach(id => clearInterval(id));
  _pingIntervals.clear();

  grid.querySelectorAll('[data-node-ping-badge]').forEach(async row => {
    const id  = parseInt(row.dataset.nodePingBadge, 10);
    const val = row.querySelector('.ping-val');
    const doPing = async () => {
      try {
        const r = await api.pingNode(id);
        if (r.reachable) {
          val.textContent = `${r.latency_ms}ms`;
          val.style.color = r.latency_ms < 100 ? 'var(--green)' : r.latency_ms < 300 ? 'var(--yellow)' : 'var(--red)';
        } else {
          val.textContent = 'offline';
          val.style.color = 'var(--red)';
        }
      } catch {
        val.textContent = '—';
        val.style.color = 'var(--text-muted)';
      }
    };
    await doPing();
    _pingIntervals.set(id, setInterval(doPing, 10000));
  });

  grid.querySelectorAll('[data-node-delete]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id   = parseInt(btn.dataset.nodeDelete, 10);
      const name = btn.dataset.nodeName || 'this node';
      const ok = await confirm(`Remove node "${name}"?`, 'This cannot be undone.');
      if (!ok) return;
      btn.disabled = true;
      try {
        await api.deleteNode(id);
        await loadNodes();
        toast(`Node "${name}" removed`);
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    });
  });
}

function _connDot(node) {
  if (node.is_local)             return `<span class="conn-dot green" title="Local node"></span>`;
  if (node.websocket_connected)  return `<span class="conn-dot green" title="Connected via WebSocket"></span>`;
  if (node.status === 'online')  return `<span class="conn-dot yellow" title="Connecting..."></span>`;
  return `<span class="conn-dot muted" title="Offline"></span>`;
}

function _connLabel(node) {
  if (node.is_local)            return 'local';
  if (node.websocket_connected) return 'WS';
  if (node.status === 'online') return 'Connecting';
  return 'Offline';
}

function _metricsHTML(node) {
  const online = node.status === 'online';
  if (!online) return '';
  const cpuVal = node.node_metrics?.cpu_percent;
  const memVal = node.node_metrics?.memory_percent;

  const cpu = cpuVal != null ? cpuVal.toFixed(0) : '—';
  const mem = memVal != null ? memVal.toFixed(0) : '—';
  const cpuColor = cpuVal > 80 ? 'var(--red)' : cpuVal > 60 ? 'var(--yellow)' : 'var(--green)';
  const memColor = memVal > 80 ? 'var(--red)' : memVal > 60 ? 'var(--yellow)' : 'var(--green)';
  return `
    <div style="margin-top:10px;display:flex;gap:10px;font-size:11px;color:var(--text-secondary)">
      <div style="flex:1">
        <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span>CPU</span><span style="color:${cpuColor}">${cpu}%</span></div>
        <div style="height:3px;background:var(--border);border-radius:2px"><div style="height:100%;width:${Math.min(cpu, 100)}%;background:${cpuColor};border-radius:2px;transition:width .4s"></div></div>
      </div>
      <div style="flex:1">
        <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span>RAM</span><span style="color:${memColor}">${mem}%</span></div>
        <div style="height:3px;background:var(--border);border-radius:2px"><div style="height:100%;width:${Math.min(mem, 100)}%;background:${memColor};border-radius:2px;transition:width .4s"></div></div>
      </div>
    </div>`;
}

function nodeCardHTML(n) {
  const online  = n.status === 'online';
  const offline = n.status === 'offline';
  const statusColor = online ? 'var(--green)' : offline ? 'var(--red)' : 'var(--yellow)';
  const statusBg    = online ? 'var(--green-bg)' : offline ? 'var(--red-bg)' : 'var(--bg-muted)';

  const instanceCount = appsData.reduce((acc, a) => acc + (a.replicas || []).filter(r =>
    n.is_local ? (r.node_id === n.id || r.node_id === null) : r.node_id === n.id
  ).length, 0);

  const serverSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`;
  const clockSvg  = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
  const appSvg    = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>`;
  const pingSvg   = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/></svg>`;

  const connDot   = _connDot(n);
  const connLabel = _connLabel(n);
  const agentVersion = n.agent_version ? ` · v${n.agent_version}` : '';

  const deleteBtn = n.is_local ? '' : `
    <button class="btn btn-sm btn-danger node-card-action-btn" data-admin
      data-node-delete="${n.id}" data-node-name="${n.name}"
      title="Remove node" style="padding:4px 8px">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
    </button>`;

  return `
    <div class="card node-card" data-node-id="${n.id}" style="cursor:pointer">
      <div class="node-card-header">
        <div style="display:flex;align-items:center;gap:8px;min-width:0">
          <span style="color:var(--text-secondary);flex-shrink:0">${serverSvg}</span>
          <span class="node-card-name" title="${n.name}">${n.name}</span>
          ${n.is_local ? `<span style="font-size:10px;padding:2px 6px;border-radius:999px;background:var(--accent-bg);color:var(--accent);flex-shrink:0">primary</span>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
          <span style="font-size:11px;padding:2px 8px;border-radius:999px;background:${statusBg};color:${statusColor}">${n.status}</span>
          ${deleteBtn}
        </div>
      </div>
      <div class="node-card-meta">
        <div class="node-meta-row">${connDot}<span style="font-size:11px;color:var(--text-muted)">${connLabel}${agentVersion}</span></div>
        <div class="node-meta-row" style="margin-top:2px">
          ${clockSvg}<span>Last seen: ${timeAgo(n.last_seen)}</span>
        </div>
        
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border-muted);display:flex;flex-direction:column;gap:6px">
          <div class="node-meta-row" style="font-size:11px;opacity:0.8">
            ${icon.cpu}<span>${n.metadata?.cpu_model || (n.metadata?.cpu_count ? `${n.metadata.cpu_count}-Core Processor` : 'System CPU')}</span>
          </div>
          <div style="display:flex;gap:12px;margin-top:2px">
             ${n.metadata?.ram_total_mb ? `<div class="node-meta-row" style="font-size:11px">${icon.memory}<span>${Math.round(n.metadata.ram_total_mb / 1024)}GB RAM</span></div>` : ''}
             ${n.metadata?.disk_total_gb ? `<div class="node-meta-row" style="font-size:11px">${icon.server}<span>${n.metadata.disk_total_gb}GB SSD</span></div>` : ''}
          </div>
        </div>

        <div class="node-meta-row" style="margin-top:8px">${appSvg}<span>${instanceCount} instance${instanceCount !== 1 ? 's' : ''}</span></div>
        ${(!n.is_local && n.status === 'online') ? `<div class="node-meta-row" data-node-ping-badge="${n.id}">
          ${pingSvg}<span class="ping-val" style="font-size:11px;color:var(--text-muted)">…</span>
        </div>` : ''}
      </div>
      ${_metricsHTML(n)}
    </div>`;
}

/* ─── Add Node modal ────────────────────────────────────────────────────── */
let _nodeModalPollTimer = null;
let _nodeModalCountdown = null;

function _clearNodeModalTimers() {
  clearInterval(_nodeModalPollTimer);
  clearInterval(_nodeModalCountdown);
  _nodeModalPollTimer = null;
  _nodeModalCountdown = null;
}

function openAddNodeModal() {
  _clearNodeModalTimers();

  let modal = document.getElementById('add-node-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'add-node-modal';
    modal.className = 'dialog-backdrop';
    modal.innerHTML = `
      <div class="dialog" style="max-width:520px;width:94%">
        <div class="dialog-title" id="node-modal-title">Connect a Node</div>
        <div class="dialog-body" style="padding-top:4px">

          <p style="margin:0 0 18px;font-size:13px;color:var(--text-secondary);line-height:1.6">
            Run this command on your remote server. The agent will automatically register and appear in your dashboard.
          </p>

          <div style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border)">
              <span style="font-size:11px;font-weight:600;text-transform:uppercase;color:var(--text-muted);letter-spacing:0.06em">Install Command</span>
              <span id="node-countdown" style="font-size:11px;font-family:var(--font-mono);color:var(--yellow)">30:00</span>
            </div>
            <pre id="node-invite-cmd" style="margin:0;padding:14px;font-family:var(--font-mono);font-size:12px;color:var(--text-primary);white-space:pre-wrap;word-break:break-all;line-height:1.6;min-height:52px">Generating…</pre>
            <div style="padding:10px 14px;border-top:1px solid var(--border)">
              <button class="btn btn-secondary btn-sm" id="node-copy-cmd" style="width:100%;justify-content:center;gap:6px">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                Copy Command
              </button>
            </div>
          </div>

          <div id="node-status-waiting" style="display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--bg-muted);border:1px solid var(--border);border-radius:8px">
            <div class="node-spinner"></div>
            <span style="font-size:13px;color:var(--text-secondary)">Waiting for node to connect…</span>
          </div>
          <div id="node-status-success" style="display:none;align-items:center;gap:12px;padding:12px 16px;background:var(--green-bg);border:1px solid var(--green-border);border-radius:8px">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>
            <span style="font-size:13px;color:var(--green);font-weight:600">Node connected successfully!</span>
          </div>
          <div id="node-create-error" style="display:none;margin-top:12px;font-size:12px;padding:10px 14px;border-radius:8px;background:var(--red-bg);border:1px solid var(--red-border);color:var(--red)"></div>

        </div>
        <div class="dialog-actions">
          <button class="btn btn-secondary" id="node-modal-cancel">Close</button>
          <button class="btn btn-primary" id="node-modal-done" style="display:none">View Node</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    const closeHandler = () => { _clearNodeModalTimers(); modal.style.display = 'none'; };
    modal.querySelector('#node-modal-cancel').onclick = closeHandler;
    modal.querySelector('#node-modal-done').onclick = () => {
      closeHandler();
      if (modal._connectedId) window.location.href = `/node?id=${modal._connectedId}`;
    };
    modal.querySelector('#node-copy-cmd').onclick = () => {
      const cmd = modal.querySelector('#node-invite-cmd').textContent || '';
      navigator.clipboard.writeText(cmd);
      toast('Command copied');
    };
    modal.addEventListener('click', e => { if (e.target === modal) closeHandler(); });
  }

  modal.style.display = 'flex';
  modal.querySelector('#node-invite-cmd').textContent = 'Generating…';
  modal.querySelector('#node-countdown').textContent = '30:00';
  modal.querySelector('#node-countdown').style.color = 'var(--yellow)';
  modal.querySelector('#node-status-waiting').style.display = 'flex';
  modal.querySelector('#node-status-success').style.display = 'none';
  modal.querySelector('#node-modal-done').style.display = 'none';
  modal.querySelector('#node-modal-cancel').style.display = '';
  modal.querySelector('#node-create-error').style.display = 'none';
  modal.querySelector('#node-modal-title').textContent = 'Connect a Node';
  modal._connectedId = null;

  (async () => {
    try {
      const prevIds = new Set(nodesData.map(n => n.id));
      const invite = await api.createNodeInvite({ note: 'New Node', ttl_minutes: 30 });

      const cmd = `cloudbase connect --main-url ${location.origin} --invite-code ${invite.code} --mode node-only`;
      modal.querySelector('#node-invite-cmd').textContent = cmd;

      const expiresAt = new Date(invite.expires_at);
      const tick = () => {
        const secs = Math.max(0, Math.floor((expiresAt.getTime() - Date.now()) / 1000));
        const m = Math.floor(secs / 60).toString().padStart(2, '0');
        const s = (secs % 60).toString().padStart(2, '0');
        const el = modal.querySelector('#node-countdown');
        if (el) {
          el.textContent = `${m}:${s}`;
          el.style.color = secs < 120 ? 'var(--red)' : 'var(--yellow)';
        }
        if (secs === 0) {
          _clearNodeModalTimers();
          const errEl = modal.querySelector('#node-create-error');
          errEl.textContent = 'Invite expired. Please close and reopen to generate a new one.';
          errEl.style.display = 'block';
        }
      };
      tick();
      _nodeModalCountdown = setInterval(tick, 1000);

      _nodeModalPollTimer = setInterval(async () => {
        try {
          const fresh = await api.listNodes();
          const newNode = fresh.find(n => !prevIds.has(n.id));
          if (newNode) {
            _clearNodeModalTimers();
            modal._connectedId = newNode.id;
            nodesData = fresh;
            renderNodes();
            modal.querySelector('#node-status-waiting').style.display = 'none';
            modal.querySelector('#node-status-success').style.display = 'flex';
            modal.querySelector('#node-modal-done').style.display = '';
            modal.querySelector('#node-modal-cancel').textContent = 'Close';
            modal.querySelector('#node-modal-title').textContent = 'Node Connected';
          }
        } catch {}
      }, 3000);

    } catch (e) {
      modal.querySelector('#node-create-error').textContent = e.message;
      modal.querySelector('#node-create-error').style.display = 'block';
      modal.querySelector('#node-invite-cmd').textContent = 'Failed to generate invite.';
    }
  })();
}

/* ─── Stat strip ────────────────────────────────────────────────────────── */
function renderStats() {
  const total    = appsData.length;
  const running  = appsData.filter(a => a.status === 'running').length;
  const stopped  = appsData.filter(a => a.status === 'stopped').length;
  const errors   = appsData.filter(a => a.status === 'error').length;

  document.getElementById('stat-total').textContent   = total;
  document.getElementById('stat-running').textContent = running;
  document.getElementById('stat-stopped').textContent = stopped;
  document.getElementById('stat-errors').textContent  = errors;
}

/* ─── Apps grid ─────────────────────────────────────────────────────────── */
function renderApps() {
  const grid = document.getElementById('apps-grid');

  if (appsData.length === 0) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <div class="empty-icon">${icon.server}</div>
        <div class="empty-title">No applications deployed</div>
        <div class="empty-sub">Deploy your first application from a GitHub repository to get started.</div>
        <button class="btn btn-primary" id="empty-deploy-btn">${icon.plus} Deploy Application</button>
      </div>`;
    document.getElementById('empty-deploy-btn')?.addEventListener('click', () => {
      openDeployModal(app => { window.location.href = `/app?id=${app.id}`; });
    });
    return;
  }

  grid.innerHTML = appsData.map(app => appCardHTML(app)).join('');

  appsData.forEach(app => {
    const card = document.getElementById(`card-${app.id}`);
    if (!card) return;

    card.addEventListener('click', () => {
      window.location.href = `/app?id=${app.id}`;
    });

    card.querySelector('.btn-start')?.addEventListener('click', e => {
      e.stopPropagation();
      appAction(app, 'start', card);
    });

    card.querySelector('.btn-stop')?.addEventListener('click', e => {
      e.stopPropagation();
      appAction(app, 'stop', card);
    });

    card.querySelector('.btn-restart')?.addEventListener('click', e => {
      e.stopPropagation();
      appAction(app, 'restart', card);
    });
  });
}

function appCardHTML(app) {
  const busy      = app.status === 'deploying';
  const isRunning = app.status === 'running';

  const primaryBtn = `
    <button class="btn btn-success btn-sm btn-start" data-admin
      ${isRunning || busy ? 'disabled' : ''}
      style="opacity:${isRunning ? '.4' : '1'}">${icon.play} Start</button>
    <button class="btn btn-danger btn-sm btn-stop" data-admin
      ${!isRunning || busy ? 'disabled' : ''}
      style="opacity:${!isRunning ? '.4' : '1'}">${icon.stop} Stop</button>`;

  const repoShort = (app.repo_url || '').replace('https://github.com/', '');

  return `
    <div class="card app-card" id="card-${app.id}">
      <div class="app-card-top">
        <div class="app-card-identity">
          <div class="app-type-icon">${typeIcon[app.app_type] || typeIcon.unknown}</div>
          <div>
            <div class="app-name">${app.name}</div>
            <div class="app-type-label">${app.app_type || 'unknown'}</div>
          </div>
        </div>
        ${badge(app.status)}
      </div>

      <div class="app-card-meta">
        ${app.domain ? `<div class="app-meta-row">${icon.globe}<span>${app.domain}</span></div>` : ''}
        ${renderPortRows(app)}
        <div class="app-meta-row">${icon.link}<span>${repoShort}</span></div>
      </div>

      <div class="app-card-actions">
        ${primaryBtn}
        <button class="btn btn-secondary btn-sm btn-icon btn-restart" data-admin ${busy ? 'disabled' : ''} title="Restart">${icon.restart}</button>
      </div>
    </div>`;
}

/* ─── Quick actions ─────────────────────────────────────────────────────── */
async function appAction(app, action, card) {
  const btnEl = card.querySelector(`.btn-${action === 'start' ? 'start' : action === 'stop' ? 'stop' : 'restart'}`);
  if (btnEl) btnEl.disabled = true;

  try {
    const fns = { start: api.start, stop: api.stop, restart: api.restart };
    const res = await fns[action](app.id);

    if (res?.command_id) {
      const b = card.querySelector('.app-card-top > span:last-child');
      if (b) b.textContent = 'pending…';
    }

    await loadApps();
    toast(`${action.charAt(0).toUpperCase() + action.slice(1)} successful`);
  } catch (e) {
    toast(e.message, 'error');
    if (btnEl) btnEl.disabled = false;
  }
}

function _waitForCommand(commandId, nodeId) {
  return new Promise((resolve, reject) => {
    const ws = wsNodeEvents(nodeId, event => {
      if (event.type === 'command_update' && event.command_id === commandId) {
        ws.close();
        event.status === 'done' ? resolve(event) : reject(new Error(event.error_message || 'Command failed'));
      }
    });
    // Check current status immediately in case we missed the event
    api.getNodeCommandStatus(nodeId, commandId).then(cmd => {
      if (cmd.status === 'done' || cmd.status === 'failed') {
        ws.close();
        cmd.status === 'done' ? resolve(cmd) : reject(new Error(cmd.error_message || 'Command failed'));
      }
    }).catch(() => {});
    setTimeout(() => { ws.close(); resolve(); }, 60000);
  });
}

