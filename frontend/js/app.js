import { api, wsLogs, wsStats, wsNodeEvents } from './api.js';
import { icon, typeIcon, badge, toast, confirm, spinner, fmtUptime, fmtSize, fmtDate, logClass, setBtn } from './utils.js';
import { pickGitHubToken } from './sidebar.js';

const params = new URLSearchParams(location.search);
const APP_ID = parseInt(params.get('id'));

let app = null;
let logWs  = null;
let statWs = null;
let logLines = [];
let chartCpu  = null;
let chartMem  = null;
let chartNet  = null;
let chartDisk = null;
let cpuData  = [];
let memData  = [];
let netData  = [];
let diskData = [];
let statsTabActive = false;
let lastStatStatus = null; // 'running' | 'stopped' | null (unknown/loading)
let _settingsInitialized = false;

function _isViewerRole() {
  return (document.body.dataset.role || '').toLowerCase() === 'viewer';
}

window.addEventListener('cloudbase-role-ready', (evt) => {
  if (!_settingsInitialized) return;
  const role = String(evt?.detail?.role || '').toLowerCase();
  if (role === 'viewer') _disableSettingsForViewer();
  else _enableSettingsForEditor();
});

function _updateNoWebVisibility(noWeb) {
  const hide = noWeb ? 'none' : '';
  const el = id => document.getElementById(id);

  // Settings panel
  if (el('cfg-port-field'))          el('cfg-port-field').style.display = hide;
  // Network section: parent settings-group of cfg-domains-rows
  const networkSection = el('cfg-domains-rows')?.closest('.settings-group');
  if (networkSection)                networkSection.style.display = hide;
  // Maintenance Pages section
  if (el('maintenance-pages-section')) el('maintenance-pages-section').style.display = hide;
  // Actions: nginx config editor is not applicable for no-web apps
  if (el('tile-nginx'))               el('tile-nginx').style.display = hide;

  // Header action buttons
  if (el('btn-maintenance-mode'))    el('btn-maintenance-mode').style.display = noWeb ? 'none' : '';
  if (el('btn-update-mode'))         el('btn-update-mode').style.display = noWeb ? 'none' : '';
  // separator between action buttons and mode buttons (hide when both mode buttons hidden)
  const sep = document.querySelector('.detail-actions-sep[data-admin]');
  if (sep) sep.style.display = noWeb ? 'none' : '';
}

document.addEventListener('change', e => {
  if (e.target && e.target.id === 'cfg-no-web') {
    _updateNoWebVisibility(e.target.checked);
  }
});

/* ─── Init ──────────────────────────────────────────────────────────────── */
export async function initApp() {
  if (!APP_ID || isNaN(APP_ID)) {
    window.location.href = '/';
    return;
  }

  try {
    app = await api.getApp(APP_ID);
  } catch (err) {
    document.body.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px;color:#a0a0a0">
        <div style="font-size:18px;color:#f85149">Failed to load application</div>
        <div style="font-size:13px">${err.message}</div>
        <a href="/" style="color:#c8c8c8;font-size:13px;margin-top:8px">← Back to dashboard</a>
      </div>`;
    return;
  }

  renderHeader();
  initTabs();
  startBgStats();          // Collect stats in the background from the start
  setInterval(refreshApp, 6000);
}

async function refreshApp() {
  try {
    app = await api.getApp(APP_ID);
    updateHeaderStatus();
    if (statsTabActive) {
      _syncStatsViewFromAppStatus();
    }
  } catch {}
}

function _syncStatsViewFromAppStatus() {
  if (!app) return;
  const stoppedView = document.getElementById('stats-stopped');
  const contentView = document.getElementById('stats-content');
  if (!stoppedView || !contentView) return;

  if (app.status !== 'running') {
    lastStatStatus = 'stopped';
    _removeStatsLoading();
    stoppedView.style.display = 'flex';
    contentView.style.display = 'none';
  }
}

/* ─── Header ────────────────────────────────────────────────────────────── */
function formatPortSummary(app) {
  const replicas = app.replicas || [];
  const running = replicas.filter(r => r.status === 'running');
  const total = replicas.length;
  if (total === 0) {
    return app.port ? `Port ${app.port}` : 'No instances';
  }
  const ports = running.map(r => r.external_port).filter(Boolean);
  const portStr = ports.length ? ports.map(p => `:${p}`).join(' ') : '';
  return `${running.length}/${total} instances running${portStr ? ` (${portStr})` : ''}`;
}

function renderHeader() {
  document.getElementById('app-name').textContent = app.name;
  document.getElementById('app-name-crumb').textContent = app.name;
  document.title = `${app.name} — Cloudbase`;
  document.getElementById('app-meta').textContent =
    `${app.app_type || 'unknown'} · ${formatPortSummary(app)}`;

  // App URL link (custom domain or auto-subdomain)
  const urlLink = document.getElementById('app-url-link');
  const urlText = document.getElementById('app-url-text');
  if (app.app_url && urlLink && urlText) {
    urlLink.href = app.app_url;
    urlText.textContent = app.app_url.replace(/^https?:\/\//, '');
    urlLink.style.display = 'flex';
  }

  const typeIconEl = document.getElementById('app-type-icon');
  if (typeIconEl) typeIconEl.innerHTML = typeIcon[app.app_type] || typeIcon.unknown;

  updateHeaderStatus();

  document.getElementById('btn-start').addEventListener('click',   () => quickAction('start'));
  document.getElementById('btn-stop').addEventListener('click',    async () => {
    const ok = await confirm('Stop App', `This will stop all running instances of <strong>${app.name}</strong>.`);
    if (ok) quickAction('stop');
  });
  document.getElementById('btn-restart').addEventListener('click', async () => {
    const ok = await confirm('Restart App', `This will restart all running instances of <strong>${app.name}</strong>.`);
    if (ok) quickAction('restart');
  });

  document.getElementById('btn-maintenance-mode').addEventListener('click', () => toggleMode('maintenance'));
  document.getElementById('btn-update-mode').addEventListener('click',      () => toggleMode('update'));
  _syncZeroDowntimeButton();
}

function _syncZeroDowntimeButton() {
  const zdBtn = document.getElementById('btn-zero-downtime');
  if (!zdBtn) return;

  // Base-domain routed apps can have app_url without app.nginx_enabled.
  const hasPublicRoute = !!(app?.app_url || (app?.nginx_enabled && app?.domain));
  const canUseRollingRestart = !!(app?.use_docker && !app?.no_web && hasPublicRoute);
  zdBtn.style.display = canUseRollingRestart ? '' : 'none';

  if (zdBtn.dataset.bound === '1') return;
  zdBtn.dataset.bound = '1';
  zdBtn.onclick = async () => {
    const ok = await confirm(
      'Rolling Restart',
      'Builds a new image for every running instance, starts each on a new port, verifies health, then atomically swaps nginx. Old containers stop only after the new ones are live.'
    );
    if (!ok) return;
    zdBtn.disabled = true;
    const orig = zdBtn.innerHTML;
    zdBtn.textContent = 'Restarting…';
    try {
      const res = await api.deployZeroDowntime(APP_ID);
      toast(`Rolling restart complete — instance ${res.instance_id}`, 'success');
      app = await api.getApp(APP_ID);
      updateHeaderStatus();
    } catch (e) {
      toast(e.message || 'Rolling restart failed', 'error');
    } finally {
      zdBtn.disabled = false;
      zdBtn.innerHTML = orig;
    }
  };
}

function _updateHeaderStatus_legacy() {
  document.getElementById('app-badge').innerHTML = badge(app.status);

  const s = app.status;
  const busy = (s === 'deploying');

  const btnStart   = document.getElementById('btn-start');
  const btnStop    = document.getElementById('btn-stop');
  const btnRestart = document.getElementById('btn-restart');

  btnStart.disabled   = (s === 'running') || busy;
  btnStop.disabled    = (s === 'stopped') || busy;
  btnRestart.disabled = busy;

  // Visual: dim the non-applicable button slightly
  btnStart.style.opacity   = (s === 'running') ? '0.4' : '1';
  btnStop.style.opacity    = (s === 'stopped') ? '0.4' : '1';

  // Maintenance / update mode toggle buttons
  const btnMaint  = document.getElementById('btn-maintenance-mode');
  const btnUpdate = document.getElementById('btn-update-mode');
  if (btnMaint && btnUpdate) {
    const hasNginx = !!app.nginx_enabled;
    btnMaint.disabled  = !hasNginx;
    btnUpdate.disabled = !hasNginx;
    btnMaint.title  = hasNginx ? 'Toggle maintenance mode — serves the custom downtime page via nginx'
                               : 'Requires a configured nginx domain';
    btnUpdate.title = hasNginx ? 'Toggle update mode — serves the custom update page via nginx'
                               : 'Requires a configured nginx domain';
    btnMaint.classList.toggle('active-maintenance', !!app.maintenance_mode);
    btnUpdate.classList.toggle('active-update',      !!app.update_mode);
  }
}

async function _toggleMode_legacy(type) {
  const btnId = type === 'maintenance' ? 'btn-maintenance-mode' : 'btn-update-mode';
  const btn = document.getElementById(btnId);
  const prev = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${spinner} …`;

  try {
    const fn = type === 'maintenance' ? api.toggleMaintenanceMode : api.toggleUpdateMode;
    app = await fn(APP_ID);
    updateHeaderStatus();
    _updateMaintBadges();
    const isOn = type === 'maintenance' ? app.maintenance_mode : app.update_mode;
    toast(isOn ? `${type === 'maintenance' ? 'Maintenance' : 'Update'} mode enabled`
               : `${type === 'maintenance' ? 'Maintenance' : 'Update'} mode disabled`);
  } catch (e) {
    toast(e.message, 'error');
    try { app = await api.getApp(APP_ID); updateHeaderStatus(); } catch {}
  } finally {
    btn.innerHTML = prev;
    btn.disabled = !app.nginx_enabled;
  }
}

function updateHeaderStatus() {
  const badgeEl = document.getElementById('app-badge');
  badgeEl.innerHTML = badge(app.status);

  const s = app.status;
  const busy = (s === 'deploying');

  const btnStart   = document.getElementById('btn-start');
  const btnStop    = document.getElementById('btn-stop');
  const btnRestart = document.getElementById('btn-restart');

  btnStart.disabled   = (s === 'running') || busy;
  btnStop.disabled    = (s === 'stopped') || busy;
  btnRestart.disabled = busy;

  btnStart.style.opacity = (s === 'running') ? '0.4' : '1';
  btnStop.style.opacity  = (s === 'stopped') ? '0.4' : '1';

  const btnMaint  = document.getElementById('btn-maintenance-mode');
  const btnUpdate = document.getElementById('btn-update-mode');
  if (btnMaint && btnUpdate) {
    const canToggle = canToggleMaintenanceMode();
    btnMaint.disabled = !canToggle;
    btnUpdate.disabled = !canToggle;
    btnMaint.title = canToggle
      ? 'Toggle downtime mode - serves the custom downtime page via nginx'
      : getMaintenanceToggleDisabledReason();
    btnUpdate.title = canToggle
      ? 'Toggle update mode - serves the custom update page via nginx'
      : getMaintenanceToggleDisabledReason();
    btnMaint.classList.toggle('active-maintenance', !!app.maintenance_mode);
    btnUpdate.classList.toggle('active-update', !!app.update_mode);
  }

  _syncZeroDowntimeButton();

  refreshMaintenanceUiState();
}

async function toggleMode(type) {
  const btnId = type === 'maintenance' ? 'btn-maintenance-mode' : 'btn-update-mode';
  const btn = document.getElementById(btnId);
  const prev = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${spinner} ...`;

  try {
    const fn = type === 'maintenance' ? api.toggleMaintenanceMode : api.toggleUpdateMode;
    app = await fn(APP_ID);
    updateHeaderStatus();
    _updateMaintBadges();
    const isOn = type === 'maintenance' ? app.maintenance_mode : app.update_mode;
    toast(isOn ? `${type === 'maintenance' ? 'Downtime' : 'Update'} mode enabled`
               : `${type === 'maintenance' ? 'Downtime' : 'Update'} mode disabled`);
  } catch (e) {
    toast(e.message, 'error');
    try { app = await api.getApp(APP_ID); updateHeaderStatus(); } catch {}
  } finally {
    btn.innerHTML = prev;
    btn.disabled = !canToggleMaintenanceMode();
  }
}

async function quickAction(action) {
  const btn = document.getElementById(`btn-${action}`);
  const prev = btn.innerHTML;
  const transitional = action === 'start' ? 'starting' : action === 'stop' ? 'stopping' : 'restarting';
  const labels = { start: 'Starting…', stop: 'Stopping…', restart: 'Restarting…' };

  // Lock all three buttons and show transitional badge
  ['start','stop','restart'].forEach(a => {
    const b = document.getElementById(`btn-${a}`);
    b.disabled = true;
    b.style.opacity = a === action ? '1' : '0.4';
  });
  btn.innerHTML = `${spinner} ${labels[action]}`;
  document.getElementById('app-badge').innerHTML = badge(transitional);

  // Show action banner in terminal immediately
  if (activeTab === 'logs') _logAction(action, 'begin');

  try {
    const fns = { start: api.start, stop: api.stop, restart: api.restart };
    const result = await fns[action](APP_ID);

    // Remote node: subscribe to events and wait for command completion
    const remoteNodeId = result?.node_id || (app.replicas || []).find(r => r.node_id && !r.node_is_local)?.node_id;
    if (result?.command_id && remoteNodeId) {
      btn.innerHTML = `${spinner} Pending on node…`;
      document.getElementById('app-badge').innerHTML = badge('pending');
      await _waitForRemoteCommand(result.command_id, remoteNodeId);
    } else {
      // Local: clear chart history on start/restart
      if (action === 'start' || action === 'restart') {
        cpuData = [];
        memData = [];
        await new Promise(r => setTimeout(r, 2500));
      }
    }
    app = await api.getApp(APP_ID);
    toast(`${action.charAt(0).toUpperCase() + action.slice(1)} successful`);

    if (activeTab === 'logs') _logAction(action, 'done');
  } catch (e) {
    toast(e.message, 'error');
    if (activeTab === 'logs') _logAction(action, 'fail');
    try { app = await api.getApp(APP_ID); } catch {}
  } finally {
    btn.innerHTML = prev;
    updateHeaderStatus();
    // Keep the current log stream attached so lifecycle output remains visible.
  }
}

function _waitForRemoteCommand(commandId, nodeId) {
  return new Promise((resolve, reject) => {
    let ws = null;
    let resolved = false;

    let interval = null;
    const cleanup = () => {
      resolved = true;
      if (ws) { ws.close(); ws = null; }
      if (interval) { clearInterval(interval); interval = null; }
    };

    const onDone = (cmd) => {
      if (resolved) return;
      cleanup();
      cmd.status === 'done' ? resolve(cmd) : reject(new Error(cmd.error_message || 'Command failed'));
    };

    ws = wsNodeEvents(nodeId, event => {
      if (event.type === 'command_update' && event.command_id === commandId) {
        if (event.status === 'done' || event.status === 'failed') {
          onDone(event);
        }
      }
    });

    // Immediate check + periodic fallback poll
    const check = async () => {
      if (resolved) return;
      try {
        const cmd = await api.getNodeCommandStatus(nodeId, commandId);
        if (cmd.status === 'done' || cmd.status === 'failed') {
          onDone(cmd);
        }
      } catch (e) {}
    };

    check();
    interval = setInterval(check, 3000);

    setTimeout(() => {
      clearInterval(interval);
      if (!resolved) {
        cleanup();
        resolve();
      }
    }, 60000);
  });
}

/* ─── Tabs ──────────────────────────────────────────────────────────────── */
function initTabs() {
  const tabs = ['logs', 'stats', 'files', 'instances', 'settings', 'activity'];
  tabs.forEach(t => {
    document.getElementById(`tab-${t}`).addEventListener('click', () => switchTab(t));
  });
  const saved = sessionStorage.getItem('cloudbase_active_tab');
  switchTab(tabs.includes(saved) ? saved : 'logs');
}

let activeTab = null;

function switchTab(t) {
  if (activeTab === t) return;

  // Deactivate old
  if (activeTab) {
    document.getElementById(`tab-${activeTab}`).classList.remove('active');
    document.getElementById(`panel-${activeTab}`).classList.remove('active');
    teardownTab(activeTab);
  }

  activeTab = t;
  sessionStorage.setItem('cloudbase_active_tab', t);
  document.getElementById(`tab-${t}`).classList.add('active');
  document.getElementById(`panel-${t}`).classList.add('active');
  setupTab(t);
}

function teardownTab(t) {
  if (t === 'logs')  { if (logWs) { logWs.close(); logWs = null; } _logsInitDone = false; }
  if (t === 'stats') { statsTabActive = false; } // Keep statWs alive — data keeps accumulating
  if (t === 'instances' && _instancesRefreshTimer) { clearInterval(_instancesRefreshTimer); _instancesRefreshTimer = null; }
}

function setupTab(t) {
  if (t === 'logs')      initLogs();
  if (t === 'stats')     initStats();
  if (t === 'files')     initFiles();
  if (t === 'instances') initInstances();
  if (t === 'settings')  initSettings();
  if (t === 'activity')  initActivity();
}

/* ─── LOGS ──────────────────────────────────────────────────────────────── */
let _logsInitDone = false;

function initLogs() {
  const terminal   = document.getElementById('log-terminal');
  const select     = document.getElementById('log-instance-select');
  const refreshBtn = document.getElementById('btn-log-refresh');
  const hint       = document.getElementById('log-instance-hint');

  // Populate instance picker once (idempotent)
  if (!_logsInitDone) {
    _logsInitDone = true;
    api.listInstances(APP_ID).then(instances => {
      if (!select) return;
      select.innerHTML = '<option value="primary">Live Stream (build / deploy)</option>';
      instances.forEach(r => {
        const label = `Instance #${r.id} — ${r.node_name || 'local'} :${r.external_port || '?'}`;
        const opt = document.createElement('option');
        opt.value = String(r.id);
        opt.textContent = label;
        select.appendChild(opt);
      });
    }).catch(() => {});

    select?.addEventListener('change', () => _switchLogInstance());
    refreshBtn?.addEventListener('click', () => _loadReplicaLogs(parseInt(select?.value, 10)));
  }

  _switchLogInstance();
}

function _switchLogInstance() {
  const select     = document.getElementById('log-instance-select');
  const refreshBtn = document.getElementById('btn-log-refresh');
  const hint       = document.getElementById('log-instance-hint');
  const val        = select?.value || 'primary';

  if (val === 'primary') {
    if (refreshBtn) refreshBtn.style.display = 'none';
    if (hint) hint.textContent = 'Live stream';
    _startPrimaryLogs();
  } else {
    // Stop live stream if active
    if (logWs) { logWs.close(); logWs = null; }
    if (refreshBtn) refreshBtn.style.display = '';
    if (hint) hint.textContent = 'Snapshot — last 200 lines';
    _loadReplicaLogs(parseInt(val, 10));
  }
}

function _startPrimaryLogs() {
  const terminal = document.getElementById('log-terminal');
  if (logWs) { logWs.close(); logWs = null; }

  terminal.innerHTML = `<div class="log-empty">Waiting for log output…</div>`;
  logLines = [];

  logWs = wsLogs(APP_ID, line => {
    if (terminal.querySelector('.log-empty')) terminal.innerHTML = '';
    const n = logLines.length + 1;
    logLines.push(line);

    const div = document.createElement('div');
    div.className = `log-line ${logClass(line)}`;
    div.innerHTML = `<span class="log-num">${String(n).padStart(4)}</span><span class="log-text">${escHtml(line)}</span>`;
    terminal.appendChild(div);

    const atBottom = terminal.scrollHeight - terminal.clientHeight - terminal.scrollTop < 60;
    if (atBottom) terminal.scrollTop = terminal.scrollHeight;
    if (logLines.length > 2000) terminal.removeChild(terminal.firstChild);
  });
}

async function _loadReplicaLogs(replicaId) {
  const terminal = document.getElementById('log-terminal');
  terminal.innerHTML = `<div class="log-empty">Loading logs…</div>`;
  logLines = [];
  try {
    const data = await api.getInstanceLogs(APP_ID, replicaId, 200);
    const lines = data.lines || [];
    if (!lines.length) {
      terminal.innerHTML = `<div class="log-empty">No log output available for this instance.</div>`;
      return;
    }
    terminal.innerHTML = '';
    lines.forEach((line, i) => {
      const div = document.createElement('div');
      div.className = `log-line ${logClass(line)}`;
      div.innerHTML = `<span class="log-num">${String(i + 1).padStart(4)}</span><span class="log-text">${escHtml(line)}</span>`;
      terminal.appendChild(div);
    });
    terminal.scrollTop = terminal.scrollHeight;
    logLines = lines;
  } catch (e) {
    terminal.innerHTML = `<div class="log-empty" style="color:var(--red)">Failed to load logs: ${escHtml(e.message)}</div>`;
  }
}

function _logAction(action, phase) {
  const terminal = document.getElementById('log-terminal');
  if (!terminal) return;
  if (terminal.querySelector('.log-empty')) terminal.innerHTML = '';

  const ts = new Date().toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const colors = { start: '#3fb950', stop: '#f85149', restart: '#d29922' };
  const color  = colors[action] || 'var(--accent)';

  const labels = {
    start:   { begin: '▶  Starting app…',              done: '▶  App started',              fail: '▶  Start failed' },
    stop:    { begin: '■  Stopping app…',               done: '■  App stopped',               fail: '■  Stop failed' },
    restart: { begin: '↺  Restarting app…',             done: '↺  App restarted',             fail: '↺  Restart failed' },
  };
  const text = labels[action]?.[phase] ?? `${action} ${phase}`;

  const sep = document.createElement('div');
  sep.className = 'log-line log-action';
  sep.innerHTML = `<span class="log-num">    </span><span class="log-text" style="color:${color};font-weight:600;letter-spacing:.02em">── ${text} ── ${ts} ──</span>`;
  terminal.appendChild(sep);
  terminal.scrollTop = terminal.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* ─── STATS — background collection ─────────────────────────────────────── */
function startBgStats() {
  statWs = wsStats(APP_ID, handleStatData);
}

function fmtMb(mb) {
  if (mb === null || mb === undefined) return '—';
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(1)} MB`;
}

let _lastInstanceCount = 0;

function _updateStatsContextBar(instanceCount) {
  const bar = document.getElementById('stats-context-bar');
  if (!bar) return;
  if (!instanceCount || instanceCount <= 0) { bar.style.display = 'none'; return; }
  _lastInstanceCount = instanceCount;
  const cpuNote = instanceCount > 1 ? 'CPU avg · memory/network/disk sum' : '';
  bar.innerHTML = `
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
    <span>${instanceCount} instance${instanceCount !== 1 ? 's' : ''}${instanceCount > 1 ? ' · aggregated' : ''}</span>
    ${cpuNote ? `<span style="color:var(--border);margin:0 2px">·</span><span style="font-style:italic">${cpuNote}</span>` : ''}`;
  bar.style.display = 'flex';
}

function handleStatData(data) {
  if (data.status === 'stopped') {
    lastStatStatus = 'stopped';
    if (statsTabActive) {
      _removeStatsLoading();
      document.getElementById('stats-stopped').style.display = 'flex';
      document.getElementById('stats-content').style.display = 'none';
      const hist = document.getElementById('stats-history-section');
      if (hist) hist.style.display = 'none';
    }
    return;
  }
  // Per-replica frame — not an aggregated app-level frame, skip for charts
  if (data.replica_id != null && data.cpu_percent == null) return;
  lastStatStatus = 'running';

  if (data._instance_count) _updateStatsContextBar(data._instance_count);

  // Always accumulate — even while on a different tab
  const now = new Date().toLocaleTimeString('nl', { hour:'2-digit', minute:'2-digit' });
  const timestamp = data.timestamp || Date.now();
  cpuData.push({ t: now, v: data.cpu_percent || 0, ts: timestamp });
  memData.push({ t: now, v: data.memory_mb   || 0, ts: timestamp });
  if (cpuData.length > 60) { cpuData.shift(); memData.shift(); }

  if (!statsTabActive) return;

  _removeStatsLoading();
  document.getElementById('stats-stopped').style.display = 'none';
  const hist = document.getElementById('stats-history-section');
  if (hist) hist.style.display = '';
  document.getElementById('stats-content').style.display = 'block';

  document.getElementById('s-cpu').textContent    = `${(data.cpu_percent || 0).toFixed(1)}%`;
  document.getElementById('s-mem').textContent    = `${(data.memory_mb   || 0).toFixed(0)} MB`;
  document.getElementById('s-vms').textContent    = fmtMb(data.memory_vms_mb);
  document.getElementById('s-uptime').textContent = fmtUptime(data.uptime_seconds || 0);
  document.getElementById('s-syscpu').textContent = `${(data.system_cpu_percent || 0).toFixed(1)}%`;

  // Rows 6-8 swap labels/values based on docker vs native
  const isDocker = !!data.docker;
  if (isDocker) {
    document.getElementById('sl-r6').textContent = 'Status';
    document.getElementById('sl-r7').textContent = 'Net RX';
    document.getElementById('sl-r8').textContent = 'Net TX';
    document.getElementById('s-r6').textContent  = data.status || '—';
    document.getElementById('s-r7').textContent  = fmtMb(data.net_rx_mb);
    document.getElementById('s-r8').textContent  = fmtMb(data.net_tx_mb);
  } else {
    document.getElementById('sl-r6').textContent = 'PID';
    document.getElementById('sl-r7').textContent = 'Threads';
    document.getElementById('sl-r8').textContent = 'Connections';
    document.getElementById('s-r6').textContent  = data.pid ?? '—';
    document.getElementById('s-r7').textContent  = data.num_threads ?? '—';
    document.getElementById('s-r8').textContent  = data.num_connections ?? '—';
  }
  document.getElementById('s-disk-read').textContent  = fmtMb(data.disk_read_mb);
  document.getElementById('s-disk-write').textContent = fmtMb(data.disk_write_mb);

  const netTotal  = (data.net_rx_mb    || 0) + (data.net_tx_mb    || 0);
  const diskTotal = (data.disk_read_mb || 0) + (data.disk_write_mb || 0);
  netData.push({ t: now, v: netTotal });
  diskData.push({ t: now, v: diskTotal });
  if (netData.length  > 60) netData.shift();
  if (diskData.length > 60) diskData.shift();

  updateChart(chartCpu,  cpuData);
  updateChart(chartMem,  memData);
  updateChart(chartNet,  netData);
  updateChart(chartDisk, diskData);
}

function initStats() {
  statsTabActive = true;


  initCharts();

  // Do not show indefinite loading when backend already knows the app is not running.
  if (app.status !== 'running') {
    lastStatStatus = 'stopped';
  }

  // Always clear any stale loading spinner before deciding what to show
  _removeStatsLoading();

  const histSection = document.getElementById('stats-history-section');
  if (lastStatStatus === 'stopped') {
    // App is known stopped — show stopped state immediately
    document.getElementById('stats-stopped').style.display = 'flex';
    document.getElementById('stats-content').style.display = 'none';
    if (histSection) histSection.style.display = 'none';
  } else if (cpuData.length > 0) {
    // We have buffered data — show it immediately
    document.getElementById('stats-stopped').style.display = 'none';
    document.getElementById('stats-content').style.display = 'block';
    if (histSection) histSection.style.display = '';
    if (_lastInstanceCount) _updateStatsContextBar(_lastInstanceCount);
    updateChart(chartCpu,  cpuData);
    updateChart(chartMem,  memData);
    updateChart(chartNet,  netData);
    updateChart(chartDisk, diskData);
  } else if (app.status === 'running') {
    // App is running but WebSocket hasn't sent data yet — show content skeleton, not spinner
    document.getElementById('stats-stopped').style.display = 'none';
    document.getElementById('stats-content').style.display = 'block';
    if (histSection) histSection.style.display = '';
  } else {
    // Status unknown — show stopped state rather than an indefinite spinner
    document.getElementById('stats-stopped').style.display = 'flex';
    document.getElementById('stats-content').style.display = 'none';
    if (histSection) histSection.style.display = 'none';
  }

  const historySelect = document.getElementById('history-hours');
  if (historySelect) {
    historySelect.onchange = e => loadStatsHistory(parseInt(e.target.value));
  }
  const exportBtn = document.getElementById('btn-export-stats');
  if (exportBtn && exportBtn.dataset.bound !== '1') {
    exportBtn.dataset.bound = '1';
    exportBtn.onclick = async () => {
      const hours = parseInt(document.getElementById('history-hours')?.value || '24', 10);
      await exportStatsCsv(hours);
    };
  }
  document.querySelectorAll('.history-open-btn').forEach(btn => {
    if (btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.onclick = () => openLargeHistoryChart(btn.dataset.historyChart || 'cpu');
  });
  // Defer slightly so canvas has layout dimensions before drawing
  setTimeout(() => loadStatsHistory(24), 100);
}

function _removeStatsLoading() {
  document.getElementById('stats-loading')?.remove();
}

function initCharts() {
  chartCpu  = createChart('chart-cpu',  '#c8c8c8', '%',   { maxLabels: 4 });
  chartMem  = createChart('chart-mem',  '#a78bfa', ' MB', { maxLabels: 4 });
  chartNet  = createChart('chart-net',  '#34d399', ' MB', { maxLabels: 4 });
  chartDisk = createChart('chart-disk', '#fbbf24', ' MB', { maxLabels: 4 });
}

let chartCpuHistory = null;
let chartMemHistory = null;
let chartNetHistory = null;
let chartDiskHistory = null;
let historySeriesCache = { hours: 24, rows: [], cpuPoints: [], memPoints: [], netPoints: [], diskPoints: [] };

function _fmtHistoryTime(ts, hours) {
  const d = new Date(ts);
  if (hours <= 6) {
    return d.toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit' });
  } else {
    const day  = d.toLocaleDateString('nl', { day: 'numeric', month: 'short' });
    const time = d.toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit' });
    return `${day} ${time}`;
  }
}

function _sampleHistoryRows(rows, maxPoints = 1200) {
  if (!rows || rows.length <= maxPoints) return rows || [];
  const step = rows.length / maxPoints;
  const sampled = [];
  for (let i = 0; i < maxPoints; i++) {
    const idx = Math.floor(i * step);
    sampled.push(rows[idx]);
  }
  const last = rows[rows.length - 1];
  if (sampled[sampled.length - 1] !== last) sampled[sampled.length - 1] = last;
  return sampled;
}

async function loadStatsHistory(hours) {
  try {
    const data = await api.getStatsHistory(APP_ID, hours);
    if (!data || !data.length) {
      historySeriesCache = { hours, rows: [], cpuPoints: [], memPoints: [], netPoints: [], diskPoints: [] };
      return;
    }
    const sampled = _sampleHistoryRows(data, 1200);
    const cpuPoints  = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.cpu_percent }));
    const memPoints  = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.memory_mb }));
    const netPoints  = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.net_mb || 0 }));
    const diskPoints = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.disk_mb || 0 }));
    historySeriesCache = { hours, rows: data, cpuPoints, memPoints, netPoints, diskPoints };
    if (!chartCpuHistory) chartCpuHistory = createChart('chart-cpu-history', '#c8c8c8', '%');
    if (!chartMemHistory) chartMemHistory = createChart('chart-mem-history', '#a78bfa', ' MB');
    if (!chartNetHistory) chartNetHistory = createChart('chart-net-history', '#34d399', ' MB');
    if (!chartDiskHistory) chartDiskHistory = createChart('chart-disk-history', '#fbbf24', ' MB');
    updateChart(chartCpuHistory, cpuPoints);
    updateChart(chartMemHistory, memPoints);
    updateChart(chartNetHistory, netPoints);
    updateChart(chartDiskHistory, diskPoints);
  } catch (e) {
    console.warn('Stats history load failed:', e);
  }
}

function _csv(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function exportStatsCsv(hours) {
  try {
    const rows = (historySeriesCache.hours === hours && historySeriesCache.rows?.length)
      ? historySeriesCache.rows
      : await api.getStatsHistory(APP_ID, hours);

    if (!rows || !rows.length) {
      toast('No stats history available in this range', 'error');
      return;
    }

    const csvRows = ['timestamp_utc,cpu_percent,memory_mb,net_mb,disk_mb'];
    rows.forEach(r => {
      csvRows.push([
        _csv(r.timestamp),
        _csv(r.cpu_percent),
        _csv(r.memory_mb),
        _csv(r.net_mb ?? 0),
        _csv(r.disk_mb ?? 0),
      ].join(','));
    });

    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `${app.name}-stats-${hours}h-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast('Stats exported as CSV', 'success');
  } catch (e) {
    toast(e.message || 'Failed to export stats', 'error');
  }
}

function openLargeHistoryChart(kind) {
  const map = {
    cpu: { title: 'CPU History', subtitle: 'Averaged per 30s interval', color: '#c8c8c8', unit: '%', points: historySeriesCache.cpuPoints },
    memory: { title: 'Memory History', subtitle: 'Averaged per 30s interval', color: '#a78bfa', unit: ' MB', points: historySeriesCache.memPoints },
    network: { title: 'Traffic History', subtitle: 'Cumulative RX+TX per 30s', color: '#34d399', unit: ' MB', points: historySeriesCache.netPoints },
    disk: { title: 'Disk I/O History', subtitle: 'Cumulative Read+Write per 30s', color: '#fbbf24', unit: ' MB', points: historySeriesCache.diskPoints },
  };

  const cfg = map[kind] || map.cpu;
  if (!cfg.points || !cfg.points.length) {
    toast('No history data to enlarge yet', 'error');
    return;
  }

  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern" style="max-width:min(1100px,96vw);width:min(1100px,96vw)">
      <div class="dialog-title">${cfg.title}</div>
      <div class="dialog-body" style="padding:14px 16px 10px;display:flex;flex-direction:column;gap:8px">
        <div style="font-size:12px;color:var(--text-muted)">${cfg.subtitle} · Range: last ${historySeriesCache.hours}h</div>
        <div style="height:min(62vh,560px)">
          <canvas id="history-large-canvas" style="width:100%;height:100%;display:block"></canvas>
        </div>
      </div>
      <div class="dialog-actions">
        <button class="btn btn-secondary" id="history-large-close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const canvas = backdrop.querySelector('#history-large-canvas');
  const draw = () => {
    canvas.width = canvas.offsetWidth * devicePixelRatio;
    canvas.height = canvas.offsetHeight * devicePixelRatio;
    drawSparkline(canvas.getContext('2d'), canvas, cfg.points, cfg.color, cfg.unit, { maxLabels: 14 });
  };
  draw();
  window.addEventListener('resize', draw, { passive: true });

  const close = () => {
    window.removeEventListener('resize', draw);
    backdrop.remove();
  };
  backdrop.querySelector('#history-large-close').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
}

function createChart(canvasId, color, unit, opts = {}) {
  const canvas = document.getElementById(canvasId);
  const ctx    = canvas.getContext('2d');

  return {
    canvas, ctx, color, unit, opts,
    draw(data) { drawSparkline(ctx, canvas, data, color, unit, opts); }
  };
}

function updateChart(chart, data) {
  if (!chart) return;
  const canvas = chart.canvas;
  canvas.width  = canvas.offsetWidth  * devicePixelRatio;
  canvas.height = canvas.offsetHeight * devicePixelRatio;
  drawSparkline(chart.ctx, canvas, data, chart.color, chart.unit, chart.opts);
}

function drawSparkline(ctx, canvas, data, color, unit, opts = {}) {
  const W = canvas.width, H = canvas.height;
  const dpr = devicePixelRatio;

  ctx.clearRect(0, 0, W, H);
  if (data.length < 2) return;

  const vals  = data.map(d => d.v);
  const rawMax = Math.max(...vals);
  // Nice round ceiling: % → nearest 5, MB → nearest 50
  const yMax = rawMax === 0 ? 10
    : unit === ' MB' ? Math.max(Math.ceil(rawMax / 50) * 50, 50)
    : Math.max(Math.ceil(rawMax / 5) * 5, 5);

  // Dynamic left padding: MB labels are wider
  const pL = unit === ' MB' ? 46 * dpr : 38 * dpr;
  const pR = 10 * dpr;   // right
  const pT = 24 * dpr;   // top   — current-value label
  const pB = 22 * dpr;   // bottom — time labels

  const cW = W - pL - pR;
  const cH = H - pT - pB;
  const xStp = data.length > 1 ? cW / (data.length - 1) : cW;
  const yS   = v => pT + cH - Math.min(Math.max(v, 0) / yMax, 1) * cH;

  // Grid lines + Y-axis labels (0 / 25 / 50 / 75 / 100 % of max)
  ctx.font = `${10 * dpr}px Inter, sans-serif`;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const frac = i / 4;
    const y    = pT + cH - frac * cH;
    const val  = yMax * frac;

    ctx.strokeStyle = 'rgba(255,255,255,0.04)';
    ctx.lineWidth   = dpr;
    ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(W - pR, y); ctx.stroke();

    const lbl = unit === ' MB'
      ? (val >= 1000 ? (val / 1024).toFixed(1) + 'G' : val.toFixed(0) + 'M')
      : val.toFixed(0) + '%';
    ctx.fillStyle = 'rgba(130,145,165,0.65)';
    ctx.fillText(lbl, pL - 5 * dpr, y);
  }

  // X-axis time labels — calculate max labels that fit without overlap
  const approxCharPx = 6.5 * dpr;
  const sampleLbl = data[0].t || '';
  const lblPx = sampleLbl.length * approxCharPx + 20 * dpr; // label width + generous gap
  const fittingMax = Math.max(2, Math.floor(cW / lblPx));
  const hardMax    = (opts && opts.maxLabels) ? opts.maxLabels : fittingMax;
  const labelCount = Math.min(hardMax, fittingMax, data.length);
  const step = Math.max(1, Math.floor((data.length - 1) / (labelCount - 1)));
  ctx.fillStyle = 'rgba(130,145,165,0.5)';
  ctx.font = `${9.5 * dpr}px Inter, sans-serif`;
  ctx.textBaseline = 'top';
  const drawnX = new Set();
  for (let i = 0; i < data.length; i += step) {
    if (!data[i].t) continue;
    const x = pL + i * xStp;
    ctx.textAlign = i === 0 ? 'left' : 'center';
    ctx.fillText(data[i].t, x, pT + cH + 5 * dpr);
    drawnX.add(i);
  }
  // Ensure the last point's time is shown, but only if it won't overlap the previous label
  const last = data.length - 1;
  if (!drawnX.has(last) && data[last].t) {
    const lastX = pL + last * xStp;
    const prevIdx = drawnX.size > 0 ? Math.max(...drawnX) : 0;
    const prevX   = pL + prevIdx * xStp;
    if (lastX - prevX > lblPx) {
      ctx.textAlign = 'right';
      ctx.fillText(data[last].t, lastX, pT + cH + 5 * dpr);
    }
  }

  // Gradient fill
  const grad = ctx.createLinearGradient(0, pT, 0, pT + cH);
  grad.addColorStop(0, color + '20');
  grad.addColorStop(1, color + '00');
  ctx.beginPath();
  ctx.moveTo(pL, yS(vals[0]));
  for (let i = 1; i < vals.length; i++) ctx.lineTo(pL + i * xStp, yS(vals[i]));
  ctx.lineTo(pL + (vals.length - 1) * xStp, pT + cH);
  ctx.lineTo(pL, pT + cH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  ctx.moveTo(pL, yS(vals[0]));
  for (let i = 1; i < vals.length; i++) ctx.lineTo(pL + i * xStp, yS(vals[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth   = 1.5 * dpr;
  ctx.lineJoin    = 'round';
  ctx.lineCap     = 'round';
  ctx.stroke();

  // Current value — top right, coloured
  const cur = vals[vals.length - 1];
  const curLbl = unit === ' MB'
    ? (cur >= 100 ? `${cur.toFixed(0)} MB` : `${cur.toFixed(1)} MB`)
    : `${cur.toFixed(1)}%`;
  ctx.font         = `600 ${12 * dpr}px Inter, sans-serif`;
  ctx.textAlign    = 'right';
  ctx.textBaseline = 'top';
  ctx.fillStyle    = color;
  ctx.fillText(curLbl, W - pR, 4 * dpr);
}

/* ─── FILES ─────────────────────────────────────────────────────────────── */
let currentFilePath = '';

async function initFiles() {
  await loadDir('');
}

async function loadDir(path) {
  currentFilePath = path;
  const list = document.getElementById('files-list');
  const bcrumb = document.getElementById('files-breadcrumb');
  const isRemote = (app.replicas || []).some(r => r.node_id && !r.node_is_local);
  list.innerHTML = `<div style="padding:16px;color:var(--text-muted);font-size:12px">${isRemote && path === '' ? 'Fetching from node…' : 'Loading…'}</div>`;

  try {
    const data = await api.listFiles(APP_ID, path);

    // Breadcrumb
    const parts = data.path === '.' ? [] : data.path.split('/').filter(Boolean);
    bcrumb.innerHTML = renderBreadcrumb(parts);
    bcrumb.querySelectorAll('.crumb-btn').forEach(btn => {
      btn.addEventListener('click', () => loadDir(btn.dataset.path));
    });

    console.log('[Files] API Response:', data);
    // Entries
    list.innerHTML = '';

    if (path !== '' && path !== '.') {
      const up = document.createElement('div');
      up.className = 'file-entry';
      up.innerHTML = `${icon.folder} ..`;
      up.addEventListener('click', () => loadDir(parts.slice(0,-1).join('/')));
      list.appendChild(up);
    }

    if (!data.entries || data.entries.length === 0) {
      const empty = document.createElement('div');
      empty.style.padding = '32px 16px';
      empty.style.textAlign = 'center';
      empty.style.color = 'var(--text-muted)';
      empty.style.fontSize = '12px';
      empty.innerHTML = `<div style="margin-bottom:8px;opacity:0.3">${icon.server}</div>This directory is empty`;
      list.appendChild(empty);
    } else {
      data.entries.forEach(entry => {
        const el = document.createElement('div');
        el.className = 'file-entry';
        el.innerHTML = entry.is_dir
          ? `<span class="dir-icon">${icon.folder}</span><span>${entry.name}</span>`
          : `<span class="file-icon">${icon.file}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${entry.name}</span><span class="file-size">${fmtSize(entry.size)}</span>`;

        el.addEventListener('click', () => {
          if (entry.is_dir) loadDir(entry.path);
          else openFile(entry, el);
        });
        list.appendChild(el);
      });
    }

  } catch (e) {
    console.error('[Files] Load failed:', e);
    list.innerHTML = `<div style="padding:16px;color:var(--red);font-size:12px">
      <strong>Error loading files:</strong><br>${e.message}
    </div>`;
  }
}

function renderBreadcrumb(parts) {
  const items = [{ label:'~', path:'' }, ...parts.map((p, i) => ({ label:p, path:parts.slice(0,i+1).join('/') }))];
  return items.map((item, i) => `
    ${i > 0 ? '<span class="sep">/</span>' : ''}
    <button class="crumb-btn" data-path="${item.path}">${item.label}</button>
  `).join('');
}

async function openFile(entry, el) {
  document.querySelectorAll('.file-entry.active').forEach(e => e.classList.remove('active'));
  el.classList.add('active');

  const header  = document.getElementById('file-viewer-header');
  const hint    = document.getElementById('file-empty-hint');
  const content = document.getElementById('file-content');

  header.innerHTML = `
    ${icon.file}
    <span class="file-path">${entry.path}</span>
    <span class="file-mime">Loading…</span>`;

  hint.style.display    = 'none';
  content.style.display = 'block';
  content.textContent   = '';

  try {
    const data = await api.fileContent(APP_ID, entry.path);
    header.querySelector('.file-mime').textContent = data.mime || '';

    if (data.binary) {
      content.textContent = '[Binary file — cannot display]';
    } else {
      content.textContent = data.content || '';
    }
  } catch (e) {
    content.textContent = `Error: ${e.message}`;
  }
}

/* ─── SETTINGS ──────────────────────────────────────────────────────────── */function showCertPicker(inputEl, items, label, displayEl) {
  document.querySelectorAll('.cert-picker').forEach(p => p.remove());
  if (!items.length) { toast(`No ${label} found in app folder`, 'warn'); return; }

  const picker = document.createElement('div');
  picker.className = 'cert-picker';
  picker.style.cssText = 'position:absolute;z-index:9999;background:#141414;border:1px solid #2e2e2e;border-radius:6px;max-height:200px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.6);font-size:12px;';

  items.forEach(path => {
    const row = document.createElement('div');
    row.textContent = path;
    row.style.cssText = 'padding:8px 12px;cursor:pointer;color:#f0f0f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    row.addEventListener('mouseenter', () => row.style.background = '#222222');
    row.addEventListener('mouseleave', () => row.style.background = '');
    row.addEventListener('click', () => {
      inputEl.value = path;
      if (displayEl) { displayEl.textContent = path.split('/').pop(); displayEl.classList.add('has-value'); }
      picker.remove();
    });
    picker.appendChild(row);
  });

  // Anchor to the visible row container (cert-upload-row), not the hidden input
  const anchorEl = displayEl ? displayEl.closest('.cert-upload-row') || displayEl : inputEl;
  const rect = anchorEl.getBoundingClientRect();
  picker.style.top   = `${rect.bottom + window.scrollY + 4}px`;
  picker.style.left  = `${rect.left + window.scrollX}px`;
  picker.style.width = `${Math.max(rect.width, 280)}px`;
  document.body.appendChild(picker);

  const close = e => { if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}
async function initActivity() {
  const wrap = document.getElementById('audit-log-table-wrap');
  try {
    const entries = await api.getAuditLog(APP_ID, 100);
    if (!entries || !entries.length) {
      wrap.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:13px">No activity recorded yet.</div>';
      return;
    }
    const badgeColor = {
      'app.start':         'var(--green)',
      'app.stop':          'var(--red)',
      'app.restart':       'var(--yellow)',
      'app.deploy':        'var(--blue)',
      'app.pull':          '#bc8cff',
      'app.rebuild':       'var(--blue)',
      'app.config_update': 'var(--text-muted)',
      'app.delete':        'var(--red)',
      'app.zero_downtime_deploy': 'var(--green)',
      'auth.login':        'var(--text-muted)',
      'auth.logout':       'var(--text-muted)',
      'auth.change_password': 'var(--yellow)',
    };
    const rows = entries.map(e => {
      const color = badgeColor[e.action] || 'var(--text-muted)';
      const detail = e.detail ? Object.entries(e.detail).filter(([k]) => k !== 'name').map(([k,v]) => `${k}: ${v}`).join(' · ') : '';
      return `<tr>
        <td style="white-space:nowrap;font-size:11px;color:var(--text-muted);padding:6px 10px">${new Date(e.timestamp).toLocaleString()}</td>
        <td style="padding:6px 10px"><span style="font-size:11px;font-weight:600;color:${color};font-family:monospace">${e.action}</span></td>
        <td style="font-size:11px;color:var(--text-muted);padding:6px 10px">${detail}</td>
        <td style="font-size:11px;color:var(--text-muted);padding:6px 10px;font-family:monospace">${e.actor || ''}</td>
      </tr>`;
    }).join('');
    wrap.innerHTML = `<table style="width:100%;border-collapse:collapse">
      <thead><tr style="font-size:11px;color:var(--text-muted);text-align:left;border-bottom:1px solid var(--border)">
        <th style="padding:6px 10px;font-weight:500">Time</th>
        <th style="padding:6px 10px;font-weight:500">Action</th>
        <th style="padding:6px 10px;font-weight:500">Detail</th>
        <th style="padding:6px 10px;font-weight:500">User</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--red);padding:20px;font-size:13px">${e.message}</div>`;
  }
}

function initSettings() {
  const isViewer = _isViewerRole();
  _settingsInitialized = true;

  // Info rows
  document.getElementById('si-name').textContent  = app.name;
  document.getElementById('si-repo').textContent  = app.repo_url;
  document.getElementById('si-type').textContent  = app.app_type || '—';
  document.getElementById('si-date').textContent  = fmtDate(app.created_at);

  // Form fields
  document.getElementById('cfg-cmd').value          = app.start_command  || '';
  document.getElementById('cfg-port').value         = app.port           || '';

  // Domains list (primary first, then extras)
  const domainsContainer = document.getElementById('cfg-domains-rows');
  domainsContainer.innerHTML = '';
  const allDomains = [app.domain, ...(app.extra_domains || [])].filter(Boolean);
  if (allDomains.length === 0) addDomainRow(domainsContainer, '');
  else allDomains.forEach(d => addDomainRow(domainsContainer, d));
  document.getElementById('cfg-add-domain').onclick = () => addDomainRow(domainsContainer, '');

  // Redirect domains
  const redirectContainer = document.getElementById('cfg-redirect-domains-rows');
  redirectContainer.innerHTML = '';
  (app.redirect_domains || []).forEach(d => addDomainRow(redirectContainer, d));
  document.getElementById('cfg-add-redirect-domain').onclick = () => addDomainRow(redirectContainer, '');

  document.getElementById('cfg-no-web').checked = !!app.no_web;
  _updateNoWebVisibility(!!app.no_web);
  document.getElementById('cfg-autostart').checked  = !!app.auto_start;
  document.getElementById('cfg-restart-policy').value = app.restart_policy || 'no';
  document.getElementById('cfg-docker-cpu').value = app.docker_cpu_limit || '';
  document.getElementById('cfg-docker-memory').value = app.docker_memory_limit_mb || '';
  document.getElementById('cfg-docker-readonly').checked = !!app.docker_read_only_root;
  document.getElementById('cfg-docker-tmpfs-enabled').checked = !!app.docker_tmpfs_enabled;
  document.getElementById('cfg-docker-tmpfs-size').value = app.docker_tmpfs_size_mb || '';
  const dockerSection = document.getElementById('docker-runtime-section');
  if (dockerSection) dockerSection.style.display = '';

  // Cert/key hidden inputs + filename display
  function setCertDisplay(inputId, nameId, path) {
    document.getElementById(inputId).value = path || '';
    const nameEl = document.getElementById(nameId);
    if (path) { nameEl.textContent = path.split('/').pop(); nameEl.classList.add('has-value'); }
    else      { nameEl.textContent = 'No file selected'; nameEl.classList.remove('has-value'); }
  }
  setCertDisplay('cfg-cert', 'cfg-cert-name', app.ssl_cert_path || '');
  setCertDisplay('cfg-key',  'cfg-key-name',  app.ssl_key_path  || '');

  // Env vars
  const envContainer = document.getElementById('cfg-env-rows');
  envContainer.innerHTML = '';
  Object.entries(app.env_vars || {}).forEach(([k, v]) => addEnvRow(envContainer, k, v));

  document.getElementById('cfg-add-env').onclick = () => addEnvRow(envContainer, '', '');

  // Pick saved GitHub token
  document.getElementById('cfg-token-pick').onclick = () => {
    pickGitHubToken(document.getElementById('cfg-token'), document.getElementById('cfg-token-id'));
  };

  // Save
  document.getElementById('btn-save').onclick = saveSettings;

  // DNS Setup modal (from Settings header)
  const dnsBtn = document.getElementById('btn-dns-setup');
  const dnsModal = document.getElementById('dns-setup-modal');
  const dnsModalClose = document.getElementById('dns-modal-close');
  if (dnsBtn && dnsModal) {
    dnsBtn.onclick = async () => {
      const ipEl = document.getElementById('dns-server-ip');
      const domainEl = document.getElementById('dns-app-domain');

      if (domainEl) {
        const domain = app && app.domain ? app.domain : '(no domain configured)';
        domainEl.textContent = domain;
        domainEl.style.color = app && app.domain ? 'var(--text-primary)' : 'var(--text-muted)';
      }

      let serverIp = null;
      if (ipEl) {
        ipEl.textContent = 'Loading…';
        ipEl.style.cursor = 'default';
        ipEl.onclick = null;
      }

      try {
        const nodes = await api.listNodes();
        const primaryNode = nodes.find(n => n.is_local) || nodes.find(n => n.role === 'main') || nodes[0] || null;
        if (primaryNode) {
          const meta = primaryNode.metadata || {};
          const parsedHost = primaryNode.api_base_url ? (() => {
            try { return new URL(primaryNode.api_base_url).hostname; } catch { return null; }
          })() : null;
          const isPublicIpAddress = value => {
            if (!value || typeof value !== 'string') return false;
            const v = value.trim();

            if (/^(\d{1,3}\.){3}\d{1,3}$/.test(v)) {
              const parts = v.split('.').map(Number);
              if (parts.some(n => Number.isNaN(n) || n < 0 || n > 255)) return false;
              if (parts[0] === 10) return false;
              if (parts[0] === 127) return false;
              if (parts[0] === 0) return false;
              if (parts[0] === 169 && parts[1] === 254) return false;
              if (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) return false;
              if (parts[0] === 192 && parts[1] === 168) return false;
              if (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127) return false;
              return true;
            }

            if (/^[0-9a-fA-F:]+$/.test(v) && v.includes(':')) {
              const low = v.toLowerCase();
              if (low === '::1') return false;
              if (low.startsWith('fe80:')) return false;
              if (low.startsWith('fc') || low.startsWith('fd')) return false;
              return true;
            }

            return false;
          };

          const candidates = [meta.public_ip, primaryNode.public_host, parsedHost];
          serverIp = candidates.find(isPublicIpAddress) || null;
        }
      } catch {
        // Keep fallback text below
      }

      if (ipEl) {
        if (serverIp) {
          ipEl.textContent = serverIp;
          ipEl.style.cursor = 'pointer';
          ipEl.onclick = () => {
            navigator.clipboard.writeText(serverIp).then(() => toast('IP copied', 'success')).catch(() => {});
          };
        } else {
          ipEl.textContent = 'IP not available';
          ipEl.style.cursor = 'default';
        }
      }

      dnsModal.style.display = 'flex';
    };
    if (dnsModalClose) dnsModalClose.onclick = () => { dnsModal.style.display = 'none'; };
    dnsModal.onclick = e => { if (e.target === dnsModal) dnsModal.style.display = 'none'; };
  }

  // Action tiles
  document.getElementById('tile-pull').onclick = () => tileAction('pull', 'Pull');
  document.getElementById('tile-nginx').onclick = openNginxModal;

  const pullTitle = document.getElementById('tile-pull-title');
  const pullSub = document.getElementById('tile-pull-sub');
  if (pullTitle) pullTitle.textContent = 'Pull + Rebuild';
  if (pullSub) pullSub.textContent = 'Pick a commit, sync code, and rebuild without stop/restart';

  // Cert scan buttons (search within app folder only)
  document.getElementById('cfg-scan-cert').onclick = async () => {
    const btn = document.getElementById('cfg-scan-cert');
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const { certs } = await api.discoverAppCerts(APP_ID);
      showCertPicker(document.getElementById('cfg-cert'), certs, 'certificates', document.getElementById('cfg-cert-name'));
    } catch { toast('Scan failed', 'error'); }
    finally { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Scan'; }
  };
  document.getElementById('cfg-scan-key').onclick = async () => {
    const btn = document.getElementById('cfg-scan-key');
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const { keys } = await api.discoverAppCerts(APP_ID);
      showCertPicker(document.getElementById('cfg-key'), keys, 'private keys', document.getElementById('cfg-key-name'));
    } catch { toast('Scan failed', 'error'); }
    finally { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Scan'; }
  };

  // Cert upload buttons
  document.getElementById('cfg-upload-cert').onclick = () => document.getElementById('cfg-cert-file').click();
  document.getElementById('cfg-cert-file').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    document.getElementById('cfg-upload-cert').disabled = true;
    try {
      const res = await api.uploadAppCert(APP_ID, file);
      setCertDisplay('cfg-cert', 'cfg-cert-name', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { document.getElementById('cfg-upload-cert').disabled = false; e.target.value = ''; }
  };
  document.getElementById('cfg-upload-key').onclick = () => document.getElementById('cfg-key-file').click();
  document.getElementById('cfg-key-file').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    document.getElementById('cfg-upload-key').disabled = true;
    try {
      const res = await api.uploadAppCert(APP_ID, file);
      setCertDisplay('cfg-key', 'cfg-key-name', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { document.getElementById('cfg-upload-key').disabled = false; e.target.value = ''; }
  };

  // Delete
  document.getElementById('btn-delete').onclick = async () => {
    const ok = await confirm('Delete Application', `This will permanently remove "${app.name}" and all its files. This action cannot be undone.`);
    if (!ok) return;
    try {
      await api.deleteApp(APP_ID);
      window.location.href = '/';
    } catch (e) {
      toast(e.message, 'error');
    }
  };

  // Maintenance pages section
  initMaintenanceSettings();
  _initMaintModal();

  if (isViewer) _disableSettingsForViewer();
  else _enableSettingsForEditor();
}

function _disableSettingsForViewer() {
  const panel = document.getElementById('panel-settings');
  if (!panel) return;
  if (panel.dataset.viewerLocked === '1') return;

  // Disable all inputs, selects, textareas, buttons
  panel.querySelectorAll('input, select, textarea, button').forEach(el => {
    el.disabled = true;
  });

  // Add a notice banner inside the settings bar
  const bar = panel.querySelector('.settings-bar');
  if (bar && !bar.querySelector('.viewer-notice')) {
    const notice = document.createElement('span');
    notice.className = 'viewer-notice';
    notice.style.cssText = 'font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:5px';
    notice.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg> Read-only`;
    bar.appendChild(notice);
  }
  panel.dataset.viewerLocked = '1';
}

function _enableSettingsForEditor() {
  const panel = document.getElementById('panel-settings');
  if (!panel) return;
  if (panel.dataset.viewerLocked !== '1') return;

  panel.querySelectorAll('input, select, textarea, button').forEach(el => {
    el.disabled = false;
  });
  panel.querySelector('.viewer-notice')?.remove();
  delete panel.dataset.viewerLocked;
}

/* ─── Maintenance pages settings ────────────────────────────────────────── */
let _maintModalType = 'downtime'; // currently open card type
let _maintLogoData  = null;       // base64 data-URL or null (no logo)

function hasMaintenanceDomain() {
  return !!(app.domain && app.port);
}

function isRemoteAppNode() {
  return (app.replicas || []).some(r => r.node_id && !r.node_is_local);
}

function canToggleMaintenanceMode() {
  if (isRemoteAppNode()) return hasMaintenanceDomain();
  return !!app.nginx_enabled;
}

function getMaintenanceToggleDisabledReason() {
  if (!hasMaintenanceDomain()) return 'Requires a configured nginx domain';
  if (!isRemoteAppNode() && !app.nginx_enabled) return 'Requires nginx to be configured for this app';
  return 'Maintenance mode is unavailable';
}

function refreshMaintenanceUiState() {
  const canServeMaintenance = canToggleMaintenanceMode();
  const noNginxWarn = document.getElementById('maint-no-nginx-warn');
  if (noNginxWarn) noNginxWarn.style.display = canServeMaintenance ? 'none' : '';

  const openButtons = [
    ['btn-open-downtime-modal', 'Edit Downtime Page'],
    ['btn-open-update-modal', 'Edit Update Page'],
    ['btn-open-restart-modal', 'Edit Restart Page'],
    ['btn-open-starting-modal', 'Edit Starting Page'],
  ];
  openButtons.forEach(([id, enabledTitle]) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = false;
    btn.title = canServeMaintenance
      ? enabledTitle
      : 'You can edit these pages now; they will be served once nginx/domain routing is configured';
  });
}

function initMaintenanceSettings() {
  // Status badges
  _updateMaintBadges();
  refreshMaintenanceUiState();

  // Wire open buttons
  document.getElementById('btn-open-downtime-modal').addEventListener('click',  () => openMaintModal('downtime'));
  document.getElementById('btn-open-update-modal').addEventListener('click',    () => openMaintModal('update'));
  document.getElementById('btn-open-restart-modal').addEventListener('click',   () => openMaintModal('restart'));
  document.getElementById('btn-open-starting-modal').addEventListener('click',  () => openMaintModal('starting'));
}

function _openMaintModal_legacy(type) {
  _maintModalType = type;
  let cfg;
  if (type === 'downtime')      cfg = app.downtime_page || {};
  else if (type === 'restart')  cfg = app.restart_page  || {};
  else                          cfg = app.update_page   || {};
  const isDown    = type === 'downtime';
  const isRestart = type === 'restart';

  const backdrop = document.getElementById('maint-modal-backdrop');
  backdrop.style.display = '';

  document.getElementById('maint-modal-title').textContent = isDown ? 'Downtime Page' : isRestart ? 'Restart Page' : 'Update Page';
  document.getElementById('maint-modal-sub').textContent   = isDown
    ? 'Shown automatically on 502/503 (crash or stop) and when Maintenance mode is on'
    : isRestart
    ? 'Shown automatically whenever the Restart button is pressed — clears when the app is back up'
    : 'Shown when Update Mode is manually enabled — ideal for planned deployments';

  const color = cfg.color || (isDown ? '#f85149' : isRestart ? '#a0a0a0' : '#f0883e');
  document.getElementById('maint-modal-title-input').value  = cfg.title   || '';
  document.getElementById('maint-modal-message').value      = cfg.message || '';
  document.getElementById('maint-modal-status-url').value   = cfg.status_url || '';
  document.getElementById('maint-modal-color').value        = color;
  document.getElementById('maint-modal-color-picker').value = color;

  // Logo
  _maintLogoData = cfg.logo_data || null;
  const logoPreview = document.getElementById('maint-modal-logo-preview');
  const btnLogoClr  = document.getElementById('btn-maint-logo-clear');
  if (_maintLogoData) {
    document.getElementById('maint-modal-logo-img').src = _maintLogoData;
    logoPreview.style.display = '';
    btnLogoClr.style.display  = '';
  } else {
    logoPreview.style.display = 'none';
    btnLogoClr.style.display  = 'none';
  }

  const hasCustom = !!cfg.custom_html;
  document.getElementById('maint-modal-custom-toggle').checked     = hasCustom;
  document.getElementById('maint-modal-custom-wrap').style.display = hasCustom ? '' : 'none';
  document.getElementById('maint-modal-custom-html').value         = cfg.custom_html || '';
}

function openMaintModal(type) {
  _maintModalType = type;
  let cfg;
  if (type === 'downtime')       cfg = app.downtime_page  || {};
  else if (type === 'restart')   cfg = app.restart_page   || {};
  else if (type === 'starting')  cfg = app.starting_page  || {};
  else                           cfg = app.update_page    || {};
  const isDown     = type === 'downtime';
  const isRestart  = type === 'restart';
  const isStarting = type === 'starting';

  const backdrop = document.getElementById('maint-modal-backdrop');
  backdrop.style.display = '';

  document.getElementById('maint-modal-title').textContent =
    isDown ? 'Downtime Page' : isRestart ? 'Restart Page' : isStarting ? 'Starting Page' : 'Update Page';
  document.getElementById('maint-modal-sub').textContent = isDown
    ? 'Shown automatically on 502/503 (crash or stop) and when Downtime mode is on'
    : isRestart
    ? 'Shown automatically whenever the Restart button is pressed - clears when the app is back up'
    : isStarting
    ? 'Shown automatically whenever the Start button is pressed - clears when the app is online'
    : 'Shown when Update mode is manually enabled - ideal for planned deployments';

  const color = cfg.color || (isDown ? '#f85149' : (isRestart || isStarting) ? '#a0a0a0' : '#f0883e');
  document.getElementById('maint-modal-title-input').value  = cfg.title   || '';
  document.getElementById('maint-modal-message').value      = cfg.message || '';
  document.getElementById('maint-modal-status-url').value   = cfg.status_url || '';
  document.getElementById('maint-modal-color').value        = color;
  document.getElementById('maint-modal-color-picker').value = color;

  _maintLogoData = cfg.logo_data || null;
  const logoPreview = document.getElementById('maint-modal-logo-preview');
  const btnLogoClr  = document.getElementById('btn-maint-logo-clear');
  if (_maintLogoData) {
    document.getElementById('maint-modal-logo-img').src = _maintLogoData;
    logoPreview.style.display = '';
    btnLogoClr.style.display  = '';
  } else {
    logoPreview.style.display = 'none';
    btnLogoClr.style.display  = 'none';
  }

  const hasCustom = !!cfg.custom_html;
  document.getElementById('maint-modal-custom-toggle').checked     = hasCustom;
  document.getElementById('maint-modal-custom-wrap').style.display = hasCustom ? '' : 'none';
  document.getElementById('maint-modal-custom-html').value         = cfg.custom_html || '';
}

function _initMaintModal() {
  const backdrop = document.getElementById('maint-modal-backdrop');
  if (!backdrop) return;

  // Close on backdrop click or cancel button
  const close = () => { backdrop.style.display = 'none'; };
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  document.getElementById('maint-modal-close').addEventListener('click', close);
  document.getElementById('maint-modal-cancel').addEventListener('click', close);

  // Color picker ↔ hex sync
  const picker = document.getElementById('maint-modal-color-picker');
  const hex    = document.getElementById('maint-modal-color');
  picker.addEventListener('input', () => { hex.value = picker.value; });
  hex.addEventListener('input', () => {
    if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) picker.value = hex.value;
  });

  // Custom HTML toggle
  document.getElementById('maint-modal-custom-toggle').addEventListener('change', e => {
    document.getElementById('maint-modal-custom-wrap').style.display = e.target.checked ? '' : 'none';
  });

  // Preview
  document.getElementById('maint-modal-preview').addEventListener('click', () => {
    window.open(`/api/apps/${APP_ID}/maintenance-pages/preview/${_maintModalType}`, '_blank');
  });

  // Logo upload
  document.getElementById('btn-maint-logo-upload').addEventListener('click', () => {
    document.getElementById('maint-modal-logo-file').click();
  });
  document.getElementById('maint-modal-logo-file').addEventListener('change', e => {
    const file = e.target.files[0];
    if (!file) return;
    if (file.size > 512 * 1024) { toast('Logo must be under 512 KB', 'error'); return; }
    const reader = new FileReader();
    reader.onload = evt => {
      _maintLogoData = evt.target.result;
      document.getElementById('maint-modal-logo-img').src = _maintLogoData;
      document.getElementById('maint-modal-logo-preview').style.display = '';
      document.getElementById('btn-maint-logo-clear').style.display = '';
    };
    reader.readAsDataURL(file);
    e.target.value = ''; // allow re-selecting same file
  });
  document.getElementById('btn-maint-logo-clear').addEventListener('click', () => {
    _maintLogoData = null;
    document.getElementById('maint-modal-logo-preview').style.display = 'none';
    document.getElementById('btn-maint-logo-clear').style.display = 'none';
  });

  // Save
  document.getElementById('maint-modal-save').addEventListener('click', () => saveMaintenancePage(_maintModalType));
}

function _updateMaintBadges() {
  const downtimeBadge  = document.getElementById('maint-downtime-badge');
  const updateBadge    = document.getElementById('maint-update-badge');
  const restartBadge   = document.getElementById('maint-restart-badge');
  const startingBadge  = document.getElementById('maint-starting-badge');
  if (!downtimeBadge || !updateBadge) return;

  const isMaint  = !!app.maintenance_mode;
  const isUpdate = !!app.update_mode;

  downtimeBadge.textContent = isMaint  ? 'On' : 'Off';
  downtimeBadge.className   = `maint-row-badge ${isMaint  ? 'maint-badge--red'    : 'maint-badge--off'}`;

  updateBadge.textContent   = isUpdate ? 'On' : 'Off';
  updateBadge.className     = `maint-row-badge ${isUpdate ? 'maint-badge--orange' : 'maint-badge--off'}`;

  if (restartBadge) {
    restartBadge.textContent = 'Auto';
    restartBadge.className   = 'maint-row-badge maint-badge--blue';
  }
  if (startingBadge) {
    startingBadge.textContent = 'Auto';
    startingBadge.className   = 'maint-row-badge maint-badge--blue';
  }
}

async function saveMaintenancePage(type) {
  // Required field validation (skip when custom HTML is used)
  const isCustom = document.getElementById('maint-modal-custom-toggle')?.checked;
  if (!isCustom) {
    const titleVal = document.getElementById('maint-modal-title-input').value.trim();
    const msgVal   = document.getElementById('maint-modal-message').value.trim();
    if (!titleVal) {
      document.getElementById('maint-modal-title-input').focus();
      toast('Title is required', 'error');
      return;
    }
    if (!msgVal) {
      document.getElementById('maint-modal-message').focus();
      toast('Message is required', 'error');
      return;
    }
  }

  const btn  = document.getElementById('maint-modal-save');
  const prev = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${spinner} Saving…`;

  const getVal = id => document.getElementById(id)?.value ?? '';

  const pageData = {
    title:      getVal('maint-modal-title-input').trim()  || null,
    message:    getVal('maint-modal-message').trim()      || null,
    color:      getVal('maint-modal-color').trim()        || null,
    status_url: getVal('maint-modal-status-url').trim()   || null,
    logo_data:  _maintLogoData,
    custom_html: document.getElementById('maint-modal-custom-toggle')?.checked
                   ? getVal('maint-modal-custom-html') || null
                   : null,
  };

  // Build full settings payload (preserve the other page's existing data)
  const currentDt = app.downtime_page  || {};
  const currentUp = app.update_page    || {};
  const currentRe = app.restart_page   || {};
  const currentSt = app.starting_page  || {};
  const _pick = (o) => ({ title: o.title, message: o.message, color: o.color, status_url: o.status_url, custom_html: o.custom_html, logo_data: o.logo_data });
  const payload = {
    downtime_page:  type === 'downtime'  ? pageData : _pick(currentDt),
    update_page:    type === 'update'    ? pageData : _pick(currentUp),
    restart_page:   type === 'restart'   ? pageData : _pick(currentRe),
    starting_page:  type === 'starting'  ? pageData : _pick(currentSt),
  };

  try {
    const res = await api.saveMaintenancePages(APP_ID, payload);
    if (res.ok) {
      app = await api.getApp(APP_ID);
      _updateMaintBadges();
      toast(`${{ downtime: 'Downtime', restart: 'Restart', starting: 'Starting', update: 'Update' }[type]} page saved`);
      document.getElementById('maint-modal-backdrop').style.display = 'none';
    } else {
      toast(res.message || 'Save failed', 'error');
    }
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = prev;
  }
}

function addDomainRow(container, value = '') {
  const row = document.createElement('div');
  row.className = 'env-row';
  row.innerHTML = `
    <input class="input" placeholder="sub.example.com" value="${escAttr(value)}" data-domain-val style="flex:1" />
    <button type="button" class="btn-remove" title="Remove">${icon.trash}</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

function addEnvRow(container, key = '', value = '') {  const row = document.createElement('div');
  row.className = 'env-row';
  row.innerHTML = `
    <input class="input input-mono" placeholder="KEY"   value="${escAttr(key)}"   data-env-key />
    <input class="input input-mono" placeholder="value" value="${escAttr(value)}" data-env-val />
    <button type="button" class="btn-remove" title="Remove">${icon.trash}</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

function escAttr(s) {
  return (s || '').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

/* ─── INSTANCES TAB ─────────────────────────────────────────────────────── */
let _instancesRefreshTimer = null;

const _sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function _waitForInstanceSync(predicate, timeoutMs = 15000, intervalMs = 700) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const instances = await api.listInstances(APP_ID);
      if (predicate(instances || [])) return true;
    } catch {
      // Keep polling; transient API hiccups should not fail sync wait immediately.
    }
    await _sleep(intervalMs);
  }
  return false;
}

async function initInstances() {
  const wrap = document.getElementById('instances-table-wrap');
  if (!wrap) return;
  const pendingRemovals = new Set();
  let pendingCreate = false;

  async function renderInstances() {
    let instances = [], instStats = {};
    try {
      [instances, instStats] = await Promise.all([
        api.listInstances(APP_ID),
        api.getInstanceStats(APP_ID).catch(() => ({})),
      ]);
    } catch (e) {
      wrap.innerHTML = `<div style="color:var(--red);padding:12px;font-size:13px">Failed to load instances: ${e.message}</div>`;
      return;
    }

    if (!instances.length) {
      wrap.innerHTML = '<div style="color:var(--text-muted);padding:12px;font-size:13px">No instances found.</div>';
      return;
    }

    const cards = instances.map((inst, idx) => {
      const removePending = pendingRemovals.has(inst.id);
      const isRunning  = inst.status === 'running';
      const isStarting = inst.status === 'starting';
      const isError    = inst.status === 'error';

      const statusColor = isRunning ? 'var(--green)' : isError ? 'var(--red)' : isStarting ? 'var(--yellow)' : 'var(--text-muted)';
      const statusBg    = isRunning ? 'var(--green-bg)' : isError ? 'var(--red-bg)' : isStarting ? 'var(--yellow-bg)' : 'var(--bg-muted)';
      const statusDot   = isRunning ? 'var(--green)' : isError ? 'var(--red)' : isStarting ? 'var(--yellow)' : 'var(--text-muted)';

      const nodeName = inst.node_name || 'Primary Node';

      // Uptime — DB timestamps are UTC without Z suffix; append Z so browser parses as UTC
      let uptimeStr = '—';
      const uptimeSrc = isRunning ? (inst.updated_at || inst.created_at) : inst.created_at;
      if (uptimeSrc) {
        const ts = uptimeSrc.endsWith('Z') || uptimeSrc.includes('+') ? uptimeSrc : uptimeSrc + 'Z';
        const diffMs = Date.now() - new Date(ts).getTime();
        if (diffMs > 0) uptimeStr = fmtUptime(Math.floor(diffMs / 1000));
      }

      // Connection info: local / tunnel with port / no tunnel
      let connHtml;
      if (inst.node_is_local) {
        connHtml = `<span style="font-size:12px;color:var(--text-muted)">local</span>`;
      } else if (inst.tunnel_connected && inst.tunnel_port) {
        connHtml = `<span style="font-size:12px;color:var(--green);display:flex;align-items:center;gap:4px">
          <svg width="7" height="7" viewBox="0 0 7 7"><circle cx="3.5" cy="3.5" r="3.5" fill="currentColor"/></svg>
          tunnel :${inst.tunnel_port}
        </span>`;
      } else if (inst.tunnel_connected) {
        connHtml = `<span style="font-size:12px;color:var(--green)">tunnel</span>`;
      } else {
        connHtml = `<span style="font-size:12px;color:var(--red)">no tunnel</span>`;
      }

      // Live metrics
      const snap = instStats[String(inst.id)];
      let metricsHtml = '';
      if (snap && isRunning) {
        const cpu = snap.cpu_percent != null ? snap.cpu_percent : null;
        const mem = snap.memory_mb   != null ? Math.round(snap.memory_mb) : null;
        const cpuColor = cpu > 80 ? 'var(--red)' : cpu > 60 ? 'var(--yellow)' : 'var(--green)';
        const memPct = inst.docker_memory_limit_mb && mem ? Math.min((mem / inst.docker_memory_limit_mb) * 100, 100) : null;
        const memColor = memPct > 80 ? 'var(--red)' : memPct > 60 ? 'var(--yellow)' : 'var(--accent)';

        metricsHtml = `
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:8px;padding-top:8px;border-top:1px solid var(--border-muted)">
            ${cpu != null ? `<div>
              <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-muted);margin-bottom:2px">
                <span>CPU</span><span style="color:${cpuColor};font-weight:600">${cpu.toFixed(1)}%</span>
              </div>
              <div style="height:3px;background:var(--border);border-radius:2px;overflow:hidden">
                <div style="height:100%;width:${Math.min(cpu,100)}%;background:${cpuColor};border-radius:2px;transition:width .5s"></div>
              </div>
            </div>` : ''}
            ${mem != null ? `<div>
              <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-muted);margin-bottom:2px">
                <span>Mem</span><span style="font-weight:600;color:var(--text-secondary)">${mem >= 1024 ? (mem/1024).toFixed(1)+'GB' : mem+'MB'}</span>
              </div>
              <div style="height:3px;background:var(--border);border-radius:2px;overflow:hidden">
                <div style="height:100%;width:${memPct ?? 0}%;background:${memColor};border-radius:2px;transition:width .5s"></div>
              </div>
            </div>` : ''}
          </div>`;
      } else if (isRunning) {
        metricsHtml = `<div style="margin-top:8px;padding-top:7px;border-top:1px solid var(--border-muted);font-size:10px;color:var(--text-muted)">Collecting metrics…</div>`;
      }

      const cpuLimit = inst.docker_cpu_limit != null ? `${inst.docker_cpu_limit} CPU` : null;
      const memLimit = inst.docker_memory_limit_mb != null ? `${inst.docker_memory_limit_mb}MB` : null;
      const limitsText = [cpuLimit, memLimit].filter(Boolean).join(' · ');
      const containerShort = inst.container_id ? inst.container_id.slice(0, 12) : null;

      return `
      <div class="card" style="padding:0;overflow:hidden">
        <div style="padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:8px;border-bottom:1px solid var(--border-muted)">
          <div style="display:flex;align-items:center;gap:7px;min-width:0">
            <div style="width:6px;height:6px;border-radius:50%;background:${statusDot};flex-shrink:0"></div>
            <span style="font-size:12px;font-weight:600;color:var(--text-primary)">Instance ${idx + 1}</span>
            <span style="font-size:11px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${nodeName}</span>
          </div>
          <span style="padding:1px 7px;border-radius:999px;font-size:10px;font-weight:600;background:${statusBg};color:${statusColor};white-space:nowrap;flex-shrink:0">${inst.status}</span>
        </div>
        <div style="padding:9px 12px">
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">
            <div>
              <div style="font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);margin-bottom:2px">Port</div>
              <div style="font-size:12px;font-weight:600;font-family:var(--font-mono);color:var(--text-primary)">:${inst.external_port || '—'}</div>
            </div>
            <div>
              <div style="font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);margin-bottom:2px">Uptime</div>
              <div style="font-size:12px;font-weight:600;color:var(--text-primary)">${uptimeStr}</div>
            </div>
            <div>
              <div style="font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);margin-bottom:2px">Connection</div>
              <div>${connHtml}</div>
            </div>
          </div>

          ${metricsHtml}

          ${inst.last_error ? `<div style="margin-top:7px;padding:5px 7px;background:var(--red-bg);border:1px solid var(--red-border);border-radius:4px;font-size:10px;color:var(--red);font-family:var(--font-mono);word-break:break-all">${escHtml(inst.last_error)}</div>` : ''}

          <div style="margin-top:8px;padding-top:7px;border-top:1px solid var(--border-muted);display:flex;align-items:center;justify-content:space-between;gap:6px">
            <div style="font-size:10px;color:var(--text-muted);display:flex;gap:7px;align-items:center;min-width:0;overflow:hidden">
              ${limitsText ? `<span>${limitsText}</span>` : ''}
              ${containerShort ? `<span style="font-family:var(--font-mono)" title="${escHtml(inst.container_id || '')}">${containerShort}</span>` : ''}
            </div>
            <div style="display:flex;gap:4px;flex-shrink:0">
              <button class="btn btn-secondary btn-sm inst-restart-btn" data-id="${inst.id}" style="font-size:10px;padding:2px 8px" ${removePending ? 'disabled' : ''}>Restart</button>
              <button class="btn btn-danger btn-sm inst-remove-btn" data-id="${inst.id}" style="font-size:10px;padding:2px 8px" ${removePending ? 'disabled' : ''}>${removePending ? `${spinner} Removing…` : 'Remove'}</button>
            </div>
          </div>
        </div>
      </div>`;
    }).join('');

    wrap.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px">${cards}</div>`;

    wrap.querySelectorAll('.inst-restart-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const instanceId = parseInt(btn.dataset.id, 10);
        const ok = await confirm('Restart instance?', 'The instance container will be stopped and restarted.');
        if (!ok) return;
        btn.disabled = true;
        btn.innerHTML = `${spinner} Restarting…`;
        try {
          await api.restartInstance(APP_ID, instanceId);
          toast('Instance restarting…');
          await renderInstances();
        } catch (e) {
          toast(e.message, 'error');
          btn.disabled = false;
          btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg> Restart`;
        }
      });
    });

    wrap.querySelectorAll('.inst-remove-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const instanceId = parseInt(btn.dataset.id, 10);
        if (pendingRemovals.has(instanceId)) return;
        const ok = await confirm('Remove instance?', 'The instance container will be stopped and removed.');
        if (!ok) return;
        pendingRemovals.add(instanceId);
        await renderInstances();
        try {
          await api.deleteInstance(APP_ID, instanceId);
          const removed = await _waitForInstanceSync(instances => !instances.some(i => i.id === instanceId));
          pendingRemovals.delete(instanceId);
          app = await api.getApp(APP_ID);
          updateHeaderStatus();
          renderHeader();
          await renderInstances();
          if (removed) {
            toast('Instance removed');
          } else {
            toast('Instance removal duurt langer dan verwacht');
          }
        } catch (e) {
          pendingRemovals.delete(instanceId);
          await renderInstances();
          toast(e.message, 'error');
        }
      });
    });

  }

  await renderInstances();
  _instancesRefreshTimer = setInterval(renderInstances, 5000);

  const refreshBtn = document.getElementById('btn-instances-refresh');
  if (refreshBtn) refreshBtn.onclick = renderInstances;

  const addBtn = document.getElementById('btn-add-instance');
  if (addBtn) {
    addBtn.onclick = async () => {
      let nodes = [];
      try {
        nodes = await api.listNodes();
      } catch {
        toast('Failed to load nodes', 'error');
        return;
      }
      const available = nodes.filter(n => n.enabled && n.status === 'online');

      const _field = (id, label, type, placeholder, hint) =>
        `<div style="margin-bottom:10px">
          <div style="font-size:12px;font-weight:500;color:var(--text-secondary);margin-bottom:4px">${label}</div>
          <input id="${id}" type="${type}" placeholder="${placeholder}"
            style="width:100%;padding:7px 10px;background:#111111;color:#f0f0f0;border:1px solid #2e2e2e;border-radius:6px;font-size:13px;box-sizing:border-box" />
          ${hint ? `<div style="font-size:11px;color:var(--text-muted);margin-top:3px">${hint}</div>` : ''}
        </div>`;

      const result = await new Promise(resolve => {
        const backdrop = document.createElement('div');
        backdrop.className = 'dialog-backdrop';
        backdrop.innerHTML = `
          <div class="dialog" style="max-width:440px">
            <div class="dialog-title">Add Instance</div>
            <div class="dialog-body" style="color:var(--text-secondary);font-size:13px;line-height:1.5">
              <div style="margin-bottom:12px;font-size:12px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">Node</div>
              <select id="inst-node-select" style="width:100%;padding:7px 10px;background:#111111;color:#f0f0f0;border:1px solid #2e2e2e;border-radius:6px;font-size:13px;margin-bottom:16px">
                <option value="">Primary node</option>
                ${available.filter(n => !n.is_local).map(n => `<option value="${n.id}">${n.name} (${n.public_host || n.status})</option>`).join('')}
              </select>
              <div style="margin-bottom:8px;font-size:12px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">Docker Runtime <span style="font-weight:400;text-transform:none;letter-spacing:0">(optional overrides)</span></div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                ${_field('inst-cpu', 'CPU Limit', 'number', app.docker_cpu_limit || '1.0', 'Max CPUs')}
                ${_field('inst-mem', 'Memory Limit (MB)', 'number', app.docker_memory_limit_mb || '512', 'Hard memory cap')}
              </div>
              <div style="display:flex;align-items:center;gap:16px;margin-top:4px">
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
                  <input type="checkbox" id="inst-readonly" ${app.docker_read_only_root ? 'checked' : ''} style="accent-color:#c8c8c8" />
                  Read-only root fs
                </label>
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
                  <input type="checkbox" id="inst-tmpfs" ${app.docker_tmpfs_enabled ? 'checked' : ''} style="accent-color:#c8c8c8" />
                  Tmpfs /tmp
                </label>
                <input id="inst-tmpfs-size" type="number" placeholder="${app.docker_tmpfs_size_mb || 64}"
                  style="width:70px;padding:5px 8px;background:#111111;color:#f0f0f0;border:1px solid #2e2e2e;border-radius:6px;font-size:12px" />
                <span style="font-size:11px;color:var(--text-muted)">MB</span>
              </div>
            </div>
            <div class="dialog-actions">
              <button class="btn btn-secondary" id="inst-dlg-cancel">Cancel</button>
              <button class="btn btn-primary" id="inst-dlg-ok">Start Instance</button>
            </div>
          </div>`;
        document.body.appendChild(backdrop);
        const ok = () => {
          const nodeVal = backdrop.querySelector('#inst-node-select').value;
          const cpu = parseFloat(backdrop.querySelector('#inst-cpu').value);
          const mem = parseInt(backdrop.querySelector('#inst-mem').value, 10);
          const readonly = backdrop.querySelector('#inst-readonly').checked;
          const tmpfs = backdrop.querySelector('#inst-tmpfs').checked;
          const tmpfsSz = parseInt(backdrop.querySelector('#inst-tmpfs-size').value, 10);
          backdrop.remove();
          resolve({
            nodeId: nodeVal ? parseInt(nodeVal, 10) : null,
            cpu: Number.isFinite(cpu) ? cpu : null,
            mem: Number.isInteger(mem) ? mem : null,
            readonly,
            tmpfs,
            tmpfsSz: Number.isInteger(tmpfsSz) ? tmpfsSz : null,
          });
        };
        backdrop.querySelector('#inst-dlg-ok').onclick = ok;
        backdrop.querySelector('#inst-dlg-cancel').onclick = () => {
          backdrop.remove();
          resolve(undefined);
        };
        backdrop.addEventListener('click', e => {
          if (e.target === backdrop) {
            backdrop.remove();
            resolve(undefined);
          }
        });
      });

      if (result === undefined) return;

      if (pendingCreate) return;
      pendingCreate = true;
      addBtn.disabled = true;
      addBtn.textContent = 'Starting...';
      try {
        const beforeInstances = await api.listInstances(APP_ID).catch(() => []);
        const beforeIds = new Set((beforeInstances || []).map(i => i.id));
        await api.scaleApp(APP_ID, {
          node_id: result.nodeId,
          docker_cpu_limit: result.cpu,
          docker_memory_limit_mb: result.mem,
          docker_read_only_root: result.readonly,
          docker_tmpfs_enabled: result.tmpfs,
          docker_tmpfs_size_mb: result.tmpfsSz,
        });
        const started = await _waitForInstanceSync(instances => instances.some(i => !beforeIds.has(i.id)));
        app = await api.getApp(APP_ID);
        updateHeaderStatus();
        renderHeader();
        await renderInstances();
        if (started) {
          toast('Instance started');
        } else {
          toast('Instance is still provisioning');
        }
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        pendingCreate = false;
        addBtn.disabled = false;
        addBtn.textContent = 'Add Instance';
      }
    };
  }
}



async function saveSettings() {
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  btn.innerHTML = `${spinner} Saving…`;

  const env_vars = {};
  document.querySelectorAll('#cfg-env-rows .env-row').forEach(row => {
    const k = row.querySelector('[data-env-key]').value.trim();
    const v = row.querySelector('[data-env-val]').value;
    if (k) env_vars[k] = v;
  });

  const tokenId = document.getElementById('cfg-token-id')?.value?.trim();
  const token   = document.getElementById('cfg-token')?.value?.trim();
  const dockerCpu = parseFloat(document.getElementById('cfg-docker-cpu').value);
  const dockerMemory = parseInt(document.getElementById('cfg-docker-memory').value, 10);
  const dockerTmpfsSize = parseInt(document.getElementById('cfg-docker-tmpfs-size').value, 10);

  const allDomainInputs = [...document.querySelectorAll('#cfg-domains-rows [data-domain-val]')]
                            .map(i => i.value.trim()).filter(Boolean);
  const payload = {
    start_command:  document.getElementById('cfg-cmd').value.trim()    || null,
    port:           parseInt(document.getElementById('cfg-port').value) || null,
    domain:         allDomainInputs[0] || null,
    extra_domains:  allDomainInputs.slice(1),
    redirect_domains: [...document.querySelectorAll('#cfg-redirect-domains-rows [data-domain-val]')]
                        .map(i => i.value.trim()).filter(Boolean),
    ssl_cert_path:  document.getElementById('cfg-cert').value.trim()   || null,
    ssl_key_path:   document.getElementById('cfg-key').value.trim()    || null,
    no_web:         document.getElementById('cfg-no-web').checked,
    auto_start:     document.getElementById('cfg-autostart').checked,
    restart_policy: document.getElementById('cfg-restart-policy').value,
    docker_cpu_limit: Number.isFinite(dockerCpu) ? dockerCpu : null,
    docker_memory_limit_mb: Number.isInteger(dockerMemory) ? dockerMemory : null,
    docker_read_only_root: document.getElementById('cfg-docker-readonly').checked,
    docker_tmpfs_enabled: document.getElementById('cfg-docker-tmpfs-enabled').checked,
    docker_tmpfs_size_mb: Number.isInteger(dockerTmpfsSize) ? dockerTmpfsSize : null,
    env_vars,
    ...(tokenId ? { github_token_id: tokenId } : token ? { github_token: token } : {}),
  };

  try {
    app = await api.updateApp(APP_ID, payload);
    if (app?.pending_sync) {
      toast(app.message || 'Settings saved and queued for node sync');
    } else {
      toast('Settings saved');
    }
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `${icon.save} Save Settings`;
  }
}

async function openNginxModal() {
  const modal    = document.getElementById('nginx-modal');
  const textarea = document.getElementById('nginx-config-textarea');
  const pathEl   = document.getElementById('nginx-config-path');
  const badge    = document.getElementById('nginx-status-badge');
  const msgEl    = document.getElementById('nginx-save-msg');
  msgEl.style.display = 'none';
  textarea.value = 'Loading…';
  modal.style.display = 'flex';

  try {
    const data = await api.getNginxConfig(APP_ID);
    pathEl.textContent = data.path;
    badge.textContent  = data.active ? '● Active' : data.exists ? '○ Inactive' : '○ Not created';
    badge.style.color  = data.active ? 'var(--green)' : 'var(--text-muted)';
    textarea.value = data.content || '# No config yet — fill in domain/port in Settings and save to generate one';
  } catch (e) {
    textarea.value = `Error: ${e.message}`;
  }

  const saveBtn = document.getElementById('nginx-save');
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    msgEl.style.display = 'none';
    try {
      const res = await api.saveNginxConfig(APP_ID, textarea.value);
      msgEl.textContent = res.ok ? 'Saved & nginx reloaded successfully.' : `Error: ${res.message}`;
      msgEl.style.display = 'block';
      msgEl.style.color = res.ok ? 'var(--green)' : 'var(--red)';
      if (res.ok) { badge.textContent = '● Active'; badge.style.color = 'var(--green)'; }
    } catch (e) {
      msgEl.textContent = e.message; msgEl.style.display = 'block'; msgEl.style.color = 'var(--red)';
    } finally {
      saveBtn.disabled = false;
    }
  };

  document.getElementById('nginx-close').onclick = () => { modal.style.display = 'none'; };
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
}

async function tileAction(endpoint, label) {
  const tileId = endpoint === 'pull' ? 'tile-pull' : 'tile-rebuild';
  const tile = document.getElementById(tileId);
  tile.disabled = true;
  const origIcon = tile.querySelector('.action-tile-icon').innerHTML;
  tile.querySelector('.action-tile-icon').innerHTML = spinner;
  const logsTitle = endpoint === 'pull' ? 'Pull + Rebuild Logs' : 'Rebuild Logs';
  const logDialog = openActionLogsDialog(logsTitle);

  try {
    let streamPath, body = null;
    if (endpoint === 'pull') {
      const selectedCommit = await openCommitPicker();
      if (selectedCommit === null) {
        logDialog.append('[Action] Cancelled by user.');
        logDialog.setStatus('Cancelled');
        return;
      }
      streamPath = `/apps/${APP_ID}/pull/stream`;
      body = selectedCommit ? { commit: selectedCommit } : null;
    } else {
      streamPath = `/apps/${APP_ID}/rebuild/stream`;
    }

    await api.streamAction(streamPath, body, line => logDialog.append(line));
    logDialog.setStatus('Done');
  } catch (e) {
    logDialog.append(`[Error] ${e.message}`);
    logDialog.setStatus('Failed');
    toast(e.message, 'error');
  } finally {
    tile.disabled = false;
    tile.querySelector('.action-tile-icon').innerHTML = origIcon;
  }
}

function openActionLogsDialog(title) {
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern action-log-dialog" style="max-width:760px;width:min(760px,92vw)">
      <div class="dialog-title">${escHtml(title)}</div>
      <div class="dialog-body action-log-body">
        <pre class="action-log-pre" id="action-log-pre"></pre>
      </div>
      <div class="dialog-actions action-log-actions">
        <div class="action-log-status" id="action-log-status">Running…</div>
        <button class="btn btn-primary" id="action-log-close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const pre = backdrop.querySelector('#action-log-pre');
  const status = backdrop.querySelector('#action-log-status');
  const close = () => backdrop.remove();
  backdrop.querySelector('#action-log-close').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });

  return {
    append(line) {
      pre.textContent += `${line}\n`;
      pre.scrollTop = pre.scrollHeight;
    },
    setStatus(text) {
      status.textContent = text;
    },
  };
}

async function openCommitPicker() {
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern commit-picker-dialog" style="max-width:760px;width:min(760px,92vw)">
      <div class="dialog-title">Select Commit</div>
      <div class="dialog-body commit-picker-body">
        <div class="commit-picker-intro">
          Choose the latest commit or pin this app to a specific recent commit before rebuilding.
        </div>
        <div class="commit-picker-toolbar">
          <button class="btn btn-secondary btn-sm" id="commit-latest">Latest on current branch</button>
          <span id="commit-picker-sync" class="commit-picker-sync">Loading recent commits...</span>
        </div>
        <div class="commit-picker-list" id="commit-picker-list">
          <div class="commit-picker-loading">Loading commits…</div>
        </div>
      </div>
      <div class="dialog-actions">
        <button class="btn btn-secondary" id="commit-cancel">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  let resolvePicker = null;
  const renderCommitRows = (listEl, commits) => {
    if (!commits.length) {
      listEl.innerHTML = '<div class="commit-picker-empty">No recent commits found.</div>';
      return;
    }

    listEl.innerHTML = commits.map(commit => `
      <button class="commit-row" data-commit="${commit.hash}">
        <div class="commit-row-top">
          <span class="commit-hash">${commit.short_hash}</span>
          <span class="commit-time">${commit.relative_time}</span>
        </div>
        <div class="commit-subject">${escHtml(commit.subject)}</div>
        <div class="commit-author">${escHtml(commit.author)}</div>
      </button>`).join('');

    listEl.scrollTop = 0;
    listEl.querySelectorAll('.commit-row').forEach(row => {
      row.onclick = () => {
        if (resolvePicker) resolvePicker(close(row.dataset.commit));
      };
    });
  };

  let closed = false;
  const close = (value) => {
    closed = true;
    backdrop.remove();
    return value;
  };

  return await new Promise(async resolve => {
    resolvePicker = resolve;
    backdrop.addEventListener('click', e => { if (e.target === backdrop) resolve(close(null)); });
    backdrop.querySelector('#commit-cancel').onclick = () => resolve(close(null));
    backdrop.querySelector('#commit-latest').onclick = () => resolve(close(''));

    try {
      const list = backdrop.querySelector('#commit-picker-list');
      const sync = backdrop.querySelector('#commit-picker-sync');

      // Fast path: local HEAD history without fetch so the picker opens immediately.
      const localData = await api.listCommits(APP_ID, 40, false);
      if (!closed) {
        renderCommitRows(list, localData.commits || []);
        sync.textContent = 'Syncing latest from remote...';
      }

      // Slow path: fetch origin and render latest remote commits when available.
      const freshData = await api.listCommits(APP_ID, 40, true);
      if (!closed) {
        renderCommitRows(list, freshData.commits || []);
        sync.textContent = `Showing ${freshData.ref || 'latest'} commits`;
      }
    } catch (e) {
      if (!closed) {
        backdrop.querySelector('#commit-picker-list').innerHTML = `<div class="commit-picker-empty" style="color:var(--red)">${escHtml(e.message)}</div>`;
        const sync = backdrop.querySelector('#commit-picker-sync');
        if (sync) sync.textContent = 'Failed to load commits';
      }
    }
  });
}
