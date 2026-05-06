import { api, wsNodeEvents, wsNodeStats, wsLocalNodeStats, wsNodeCommands } from './api.js';
import { toast, confirm } from './utils.js';

let _detailWs = { events: null, stats: null, commands: null };

// Per-node stats buffer: nodeId → { cpuData[], memData[], diskData[] }
const _nodeStatsBuf = {};
let _statsActiveNodeId = null;
let _nodeCharts        = { cpu: null, mem: null, disk: null };
let _onStatsTabOpen    = null;   // set per-drawer-open; called when Stats tab is first activated

/* ─── Node Detail Drawer ────────────────────────────────────────────────── */
export function openNodeDetailModal(node) {
  if (!node) return;
  _closeDetailWs();

  _ensureDrawer();
  const drawer = document.getElementById('node-detail-drawer');
  drawer.style.display = 'block';
  _populateDrawer(drawer, node);
}

function _ensureDrawer() {
  if (document.getElementById('node-detail-drawer')) return;

  const drawer = document.createElement('div');
  drawer.id = 'node-detail-drawer';
  drawer.style.cssText = 'display:none;position:fixed;inset:0;z-index:400';
  drawer.innerHTML = `
    <div id="ndd-backdrop" style="position:absolute;inset:0;background:rgba(0,0,0,.5)"></div>
    <div id="ndd-panel" style="position:absolute;right:0;top:0;bottom:0;width:min(640px,95vw);background:var(--bg-surface);border-left:1px solid var(--border);display:flex;flex-direction:column;box-shadow:var(--shadow-lg)">

      <!-- Header -->
      <div style="padding:18px 20px 14px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;justify-content:space-between;flex-shrink:0;gap:12px">
        <div style="min-width:0">
          <div id="ndd-name" style="font-size:16px;font-weight:600;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
          <div id="ndd-meta" style="font-size:12px;color:var(--text-secondary);margin-top:3px"></div>
        </div>
        <button id="ndd-close" class="btn btn-secondary btn-sm btn-icon" style="flex-shrink:0">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>

      <!-- Tabs -->
      <div id="ndd-tabs" style="display:flex;padding:0 20px;border-bottom:1px solid var(--border);flex-shrink:0;gap:2px">
        ${['overview','stats','commands','history','logs','actions'].map((t, i) =>
          `<button class="ndd-tab${i===0?' ndd-tab-active':''}" data-tab="${t}"
            style="padding:10px 12px;font-size:12px;font-weight:500;border:none;background:none;cursor:pointer;color:${i===0?'var(--text-primary)':'var(--text-secondary)'};border-bottom:2px solid ${i===0?'var(--accent)':'transparent'};transition:color .15s,border-color .15s;white-space:nowrap">
            ${t.charAt(0).toUpperCase()+t.slice(1)}
          </button>`
        ).join('')}
      </div>

      <!-- Content -->
      <div id="ndd-content" style="flex:1;overflow-y:auto;padding:20px">
        <div id="ndd-tab-overview"></div>
        <div id="ndd-tab-stats" style="display:none"></div>
        <div id="ndd-tab-commands" style="display:none"></div>
        <div id="ndd-tab-history" style="display:none"></div>
        <div id="ndd-tab-logs" style="display:none"></div>
        <div id="ndd-tab-actions" style="display:none"></div>
      </div>
    </div>`;
  document.body.appendChild(drawer);

  drawer.querySelector('#ndd-close').onclick = _closeDrawer;
  drawer.querySelector('#ndd-backdrop').onclick = _closeDrawer;

  drawer.querySelectorAll('.ndd-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      drawer.querySelectorAll('.ndd-tab').forEach(b => {
        b.classList.remove('ndd-tab-active');
        b.style.color = 'var(--text-secondary)';
        b.style.borderBottomColor = 'transparent';
      });
      btn.classList.add('ndd-tab-active');
      btn.style.color = 'var(--text-primary)';
      btn.style.borderBottomColor = 'var(--accent)';
      drawer.querySelectorAll('[id^="ndd-tab-"]').forEach(t => t.style.display = 'none');
      const tab = drawer.querySelector(`#ndd-tab-${btn.dataset.tab}`);
      if (tab) tab.style.display = '';
      if (btn.dataset.tab === 'stats') _onStatsTabOpen?.(drawer);
    });
  });
}

function _closeDrawer() {
  _closeDetailWs();
  const drawer = document.getElementById('node-detail-drawer');
  if (drawer) drawer.style.display = 'none';
}

function _closeDetailWs() {
  if (_detailWs.events)   { _detailWs.events.close();   _detailWs.events = null; }
  if (_detailWs.stats)    { _detailWs.stats.close();    _detailWs.stats = null; }
  if (_detailWs.commands) { _detailWs.commands.close(); _detailWs.commands = null; }
  _statsActiveNodeId = null;
  _onStatsTabOpen    = null;
  _nodeCharts        = { cpu: null, mem: null, disk: null };
}

function _populateDrawer(drawer, node) {
  // Reset to overview tab
  drawer.querySelectorAll('.ndd-tab').forEach((b, i) => {
    b.classList.toggle('ndd-tab-active', i === 0);
    b.style.color = i === 0 ? 'var(--text-primary)' : 'var(--text-secondary)';
    b.style.borderBottomColor = i === 0 ? 'var(--accent)' : 'transparent';
  });
  drawer.querySelectorAll('[id^="ndd-tab-"]').forEach((t, i) => t.style.display = i === 0 ? '' : 'none');

  const connType = node.is_local ? 'Local' : node.websocket_connected ? 'WebSocket' : node.status === 'online' ? 'Connecting' : 'Offline';
  const agentV = node.agent_version ? ` · ${node.agent_version}` : '';
  drawer.querySelector('#ndd-name').textContent = node.name;
  drawer.querySelector('#ndd-meta').textContent = `${node.role} · ${connType}${agentV}`;

  _renderOverviewTab(drawer, node);
  _renderActionsTab(drawer, node);
  _setupCommandsTab(drawer, node);
  _setupHistoryTab(drawer, node);
  _setupLogsTab(drawer, node);

  // Initialise per-node stats buffer and start background collection
  if (!_nodeStatsBuf[node.id]) {
    _nodeStatsBuf[node.id] = { cpuData: [], memData: [], diskData: [] };
  }
  _onStatsTabOpen = (d) => _initNodeStatsTab(node.id, d);
  // Pre-populate the stats tab placeholder
  drawer.querySelector('#ndd-tab-stats').innerHTML =
    `<div style="font-size:12px;color:var(--text-muted);padding:20px 0">Collecting stats…</div>`;

  // Start streaming stats in the background (always, so buffer fills up)
  _detailWs.stats = node.is_local
    ? wsLocalNodeStats(data => _handleNodeStatData(node.id, data))
    : wsNodeStats(node.id, data => _handleNodeStatData(node.id, data));

  // Live events → update metrics panel
  if (!node.is_local) {
    _detailWs.events = wsNodeEvents(node.id, event => {
      if (event.type === 'node_health') _updateMetrics(drawer, event);
    });
  }
}

/* ─── Overview tab ──────────────────────────────────────────────────────── */
function _renderOverviewTab(drawer, node) {
  const tab = drawer.querySelector('#ndd-tab-overview');
  const lastSeen = (node.is_local || node.websocket_connected)
    ? '<span style="color:var(--green)">Connected</span>'
    : (node.last_seen ? new Date(node.last_seen).toLocaleString() : 'never');

  tab.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:16px">
      <!-- Info grid -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${_pill('Status', `<span style="color:${node.status==='online'?'var(--green)':'var(--red)'}">${node.status}</span>`)}
        ${_pill('Connection', node.is_local ? '<span style="color:var(--green)">Local ✓</span>' : node.websocket_connected ? '<span style="color:var(--green)">WebSocket ✓</span>' : node.status === 'online' ? '<span style="color:var(--yellow)">Connecting...</span>' : '<span style="color:var(--text-muted)">Offline</span>')}
        ${_pill('Last seen', lastSeen)}
        ${_pill('Agent version', node.agent_version || 'unknown')}
        ${node.api_base_url ? _pill('API URL', `<span style="font-family:monospace;font-size:11px">${node.api_base_url}</span>`) : ''}
        ${node.public_host ? _pill('Public host', node.public_host) : ''}
      </div>

      <!-- Metrics -->
      <div>
        <div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);margin-bottom:10px">Resources</div>
        <div id="ndd-metrics" style="display:flex;flex-direction:column;gap:10px">
          ${node.node_metrics ? _metricsHTML(node.node_metrics) : '<div style="font-size:12px;color:var(--text-muted)">Waiting for metrics…</div>'}
        </div>
      </div>

      <!-- Ping -->
      ${!node.is_local ? `
      <div style="display:flex;align-items:center;gap:10px">
        <button class="btn btn-secondary btn-sm" id="ndd-ping-btn">Ping Node</button>
        <span id="ndd-ping-result" style="font-size:12px;color:var(--text-muted)"></span>
      </div>` : ''}
    </div>`;

  if (!node.is_local) {
    drawer.querySelector('#ndd-ping-btn')?.addEventListener('click', async () => {
      const btn = drawer.querySelector('#ndd-ping-btn');
      const result = drawer.querySelector('#ndd-ping-result');
      btn.disabled = true;
      result.textContent = 'Pinging…';
      result.style.color = 'var(--text-muted)';
      try {
        const r = await api.pingNode(node.id);
        result.style.color = r.reachable ? 'var(--green)' : 'var(--red)';
        result.textContent = r.reachable ? `${r.latency_ms}ms via ${r.transport}` : `Unreachable${r.error ? ': ' + r.error : ''}`;
      } catch (e) {
        result.style.color = 'var(--red)';
        result.textContent = e.message;
      } finally {
        btn.disabled = false;
      }
    });
  }
}

function _updateMetrics(drawer, data) {
  const container = drawer.querySelector('#ndd-metrics');
  if (!container) return;
  container.innerHTML = _metricsHTML(data);
}

function _metricsHTML(m) {
  const parts = [];
  if (m.cpu_percent != null)    parts.push(_metricBar('CPU', m.cpu_percent));
  if (m.memory_percent != null) parts.push(_metricBar('Memory', m.memory_percent));
  if (m.disk_percent != null)   parts.push(_metricBar('Disk', m.disk_percent));
  return parts.join('') || '<div style="font-size:12px;color:var(--text-muted)">No metrics available.</div>';
}

function _metricBar(label, pct) {
  const color = pct > 80 ? 'var(--red)' : pct > 60 ? 'var(--yellow)' : 'var(--green)';
  return `
    <div>
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-secondary);margin-bottom:5px">
        <span>${label}</span><span style="color:${color};font-weight:500">${pct.toFixed(1)}%</span>
      </div>
      <div style="height:5px;background:var(--border);border-radius:3px">
        <div style="height:100%;width:${Math.min(pct,100)}%;background:${color};border-radius:3px;transition:width .4s"></div>
      </div>
    </div>`;
}

function _pill(label, value) {
  return `
    <div style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px">
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:3px">${label}</div>
      <div style="font-size:13px;color:var(--text-primary)">${value}</div>
    </div>`;
}

/* ─── Actions tab ───────────────────────────────────────────────────────── */
function _renderActionsTab(drawer, node) {
  const tab = drawer.querySelector('#ndd-tab-actions');
  if (node.is_local) {
    tab.innerHTML = `<div style="font-size:13px;color:var(--text-muted)">The local node cannot be managed remotely.</div>`;
    return;
  }
  tab.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:10px;max-width:280px">
      <button class="btn ${node.enabled ? 'btn-secondary' : 'btn-success'}" id="ndd-toggle-btn">
        ${node.enabled ? 'Disable Node' : 'Enable Node'}
      </button>
      <button class="btn btn-danger" id="ndd-delete-btn">Remove Node</button>
    </div>`;

  tab.querySelector('#ndd-toggle-btn').onclick = async () => {
    try {
      if (node.enabled) await api.disableNode(node.id);
      else await api.enableNode(node.id);
      toast(`Node ${node.enabled ? 'disabled' : 'enabled'}`);
      _closeDrawer();
    } catch (e) { toast(e.message, 'error'); }
  };

  tab.querySelector('#ndd-delete-btn').onclick = async () => {
    const ok = await confirm(`Remove node "${node.name}"? This cannot be undone.`);
    if (!ok) return;
    try {
      await api.deleteNode(node.id);
      toast(`Node "${node.name}" removed`);
      _closeDrawer();
    } catch (e) { toast(e.message, 'error'); }
  };
}

/* ─── Commands tab (live) ───────────────────────────────────────────────── */
function _setupCommandsTab(drawer, node) {
  const tab = drawer.querySelector('#ndd-tab-commands');
  if (node.is_local) {
    tab.innerHTML = `<div style="font-size:13px;color:var(--text-muted)">Local node has no command queue.</div>`;
    return;
  }

  tab.innerHTML = `
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);margin-bottom:10px">Live command queue</div>
    <div id="ndd-cmd-list" style="display:flex;flex-direction:column;gap:6px">
      <div style="font-size:12px;color:var(--text-muted)">Connecting…</div>
    </div>`;

  _detailWs.commands = wsNodeCommands(node.id, msg => {
    const list = tab.querySelector('#ndd-cmd-list');
    if (!list) return;
    if (msg.type === 'snapshot') {
      list.innerHTML = msg.commands.length
        ? msg.commands.map(_cmdRow).join('')
        : `<div style="font-size:12px;color:var(--text-muted)">No active commands.</div>`;
    } else if (msg.type === 'command_updated' && msg.command) {
      list.innerHTML = list.innerHTML; // trigger re-render on next snapshot
    }
  });
}

function _cmdRow(c) {
  const statusColor = { done:'var(--green)', failed:'var(--red)', in_progress:'var(--accent)', queued:'var(--yellow)' }[c.status] || 'var(--text-muted)';
  const ts = c.created_at ? new Date(c.created_at).toLocaleTimeString() : '';
  return `<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px">
    <div>
      <span style="color:var(--text-primary);font-weight:500">${c.command_type}</span>
      ${c.app_id ? `<span style="color:var(--text-muted);margin-left:6px">app #${c.app_id}</span>` : ''}
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:var(--text-muted)">${ts}</span>
      <span style="color:${statusColor};font-weight:500">${c.status}</span>
    </div>
  </div>`;
}

/* ─── History tab ───────────────────────────────────────────────────────── */
async function _setupHistoryTab(drawer, node) {
  const tab = drawer.querySelector('#ndd-tab-history');
  tab.innerHTML = `<div style="font-size:12px;color:var(--text-muted)">Loading…</div>`;
  try {
    const commands = await api.listNodeCommands(node.id);
    if (!commands.length) {
      tab.innerHTML = `<div style="font-size:12px;color:var(--text-muted)">No command history.</div>`;
      return;
    }
    tab.innerHTML = `
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);margin-bottom:10px">Last ${Math.min(commands.length, 100)} commands</div>
      <div style="display:flex;flex-direction:column;gap:6px">
        ${commands.slice(0, 100).map(c => {
          const statusColor = { done:'var(--green)', failed:'var(--red)', in_progress:'var(--accent)', queued:'var(--yellow)' }[c.status] || 'var(--text-muted)';
          const ts = c.created_at ? new Date(c.created_at).toLocaleString() : '';
          const dur = (c.dispatched_at && c.completed_at)
            ? `${Math.round((new Date(c.completed_at) - new Date(c.dispatched_at)) / 1000)}s`
            : '';
          return `<div style="padding:10px 12px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm)">
            <div style="display:flex;justify-content:space-between;margin-bottom:3px">
              <span style="font-size:12px;font-weight:500;color:var(--text-primary)">${c.command_type}</span>
              <span style="font-size:12px;color:${statusColor};font-weight:500">${c.status}${dur ? ' · ' + dur : ''}</span>
            </div>
            <div style="font-size:11px;color:var(--text-muted)">${ts}${c.app_id ? ' · app #' + c.app_id : ''}</div>
            ${c.error_message ? `<div style="font-size:11px;color:var(--red);margin-top:4px">${c.error_message}</div>` : ''}
          </div>`;
        }).join('')}
      </div>`;
  } catch (e) {
    tab.innerHTML = `<div style="font-size:12px;color:var(--red)">Failed to load: ${e.message}</div>`;
  }
}

/* ─── Logs tab ──────────────────────────────────────────────────────────── */
async function _setupLogsTab(drawer, node) {
  const tab = drawer.querySelector('#ndd-tab-logs');
  tab.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)">Agent logs</div>
      <button class="btn btn-secondary btn-sm" id="ndd-refresh-logs">Refresh</button>
    </div>
    <div id="ndd-logs-content" style="font-family:var(--font-mono, monospace);font-size:11px;line-height:1.7;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;max-height:420px;overflow-y:auto;white-space:pre-wrap;color:var(--text-secondary)">Loading…</div>`;

  const refresh = async () => {
    const el = tab.querySelector('#ndd-logs-content');
    if (!el) return;
    el.textContent = 'Loading…';
    try {
      const r = await api.getNodeAgentLogs(node.id, 300);
      el.textContent = (r.lines || []).join('\n') || '(no logs)';
      el.scrollTop = el.scrollHeight;
    } catch (e) {
      el.textContent = `Error: ${e.message}`;
    }
  };

  tab.querySelector('#ndd-refresh-logs').onclick = refresh;
  await refresh();
}

/* ─── Node Stats (background buffering + tab) ───────────────────────────── */

function _handleNodeStatData(nodeId, data) {
  const buf = _nodeStatsBuf[nodeId];
  if (!buf) return;

  const now = new Date().toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  buf.cpuData.push ({ t: now, v: data.cpu_percent    || 0 });
  buf.memData.push ({ t: now, v: data.memory_percent || 0 });
  buf.diskData.push({ t: now, v: data.disk_percent   || 0 });
  if (buf.cpuData.length > 60) { buf.cpuData.shift(); buf.memData.shift(); buf.diskData.shift(); }

  // If the stats tab is currently open for this node, update charts live
  if (_statsActiveNodeId === nodeId && _nodeCharts.cpu) {
    _updateNodeChart(_nodeCharts.cpu,  buf.cpuData);
    _updateNodeChart(_nodeCharts.mem,  buf.memData);
    _updateNodeChart(_nodeCharts.disk, buf.diskData);
    // Update the live value readouts
    const drawer = document.getElementById('node-detail-drawer');
    if (drawer) {
      drawer.querySelector('#ndd-s-cpu')?.textContent  && (drawer.querySelector('#ndd-s-cpu').textContent  = `${(data.cpu_percent    || 0).toFixed(1)}%`);
      drawer.querySelector('#ndd-s-mem')?.textContent  && (drawer.querySelector('#ndd-s-mem').textContent  = `${(data.memory_percent || 0).toFixed(1)}%`);
      drawer.querySelector('#ndd-s-disk')?.textContent && (drawer.querySelector('#ndd-s-disk').textContent = `${(data.disk_percent   || 0).toFixed(1)}%`);
    }
  }
}

function _initNodeStatsTab(nodeId, drawer) {
  _statsActiveNodeId = nodeId;
  const buf = _nodeStatsBuf[nodeId] || { cpuData: [], memData: [], diskData: [] };
  const tab = drawer.querySelector('#ndd-tab-stats');
  if (!tab) return;

  tab.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:20px">
      ${_nodeStatCard('CPU Usage',    'ndd-s-cpu',  'ndd-chart-cpu',  buf.cpuData)}
      ${_nodeStatCard('Memory',       'ndd-s-mem',  'ndd-chart-mem',  buf.memData)}
      ${_nodeStatCard('Disk',         'ndd-s-disk', 'ndd-chart-disk', buf.diskData)}
    </div>`;

  _nodeCharts.cpu  = _createNodeChart('ndd-chart-cpu',  '#c8c8c8', '%');
  _nodeCharts.mem  = _createNodeChart('ndd-chart-mem',  '#a78bfa', '%');
  _nodeCharts.disk = _createNodeChart('ndd-chart-disk', '#34d399', '%');

  if (buf.cpuData.length > 0) {
    _updateNodeChart(_nodeCharts.cpu,  buf.cpuData);
    _updateNodeChart(_nodeCharts.mem,  buf.memData);
    _updateNodeChart(_nodeCharts.disk, buf.diskData);
    const last = buf.cpuData.length - 1;
    tab.querySelector('#ndd-s-cpu') .textContent = `${buf.cpuData[last].v.toFixed(1)}%`;
    tab.querySelector('#ndd-s-mem') .textContent = `${buf.memData[last].v.toFixed(1)}%`;
    tab.querySelector('#ndd-s-disk').textContent = `${buf.diskData[last].v.toFixed(1)}%`;
  }
}

function _nodeStatCard(label, valueId, canvasId, _buf) {
  return `
    <div style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px">
        <span style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)">${label}</span>
        <span id="${valueId}" style="font-size:18px;font-weight:600;color:var(--text-primary)">—</span>
      </div>
      <canvas id="${canvasId}" style="width:100%;height:70px;display:block"></canvas>
    </div>`;
}

function _createNodeChart(canvasId, color, unit) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  const ctx = canvas.getContext('2d');
  return { canvas, ctx, color, unit, draw(data) { _drawNodeSparkline(ctx, canvas, data, color, unit); } };
}

function _updateNodeChart(chart, data) {
  if (!chart) return;
  const canvas = chart.canvas;
  canvas.width  = canvas.offsetWidth  * devicePixelRatio;
  canvas.height = canvas.offsetHeight * devicePixelRatio;
  _drawNodeSparkline(chart.ctx, canvas, data, chart.color, chart.unit);
}

function _drawNodeSparkline(ctx, canvas, data, color, unit) {
  const W = canvas.width, H = canvas.height;
  const dpr = devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  if (data.length < 2) return;

  const vals = data.map(d => d.v);
  const lo = 0, hi = 100;  // percentages always 0-100
  const pad = 6 * dpr;
  const xStep  = (W - pad * 2) / (data.length - 1);
  const yScale = v => H - pad - ((v - lo) / (hi - lo)) * (H - pad * 2);

  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, color + '55');
  grad.addColorStop(1, color + '00');

  ctx.beginPath();
  ctx.moveTo(pad, yScale(vals[0]));
  vals.forEach((v, i) => { if (i > 0) ctx.lineTo(pad + i * xStep, yScale(v)); });
  ctx.lineTo(pad + (vals.length - 1) * xStep, H);
  ctx.lineTo(pad, H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(pad, yScale(vals[0]));
  vals.forEach((v, i) => { if (i > 0) ctx.lineTo(pad + i * xStep, yScale(v)); });
  ctx.strokeStyle = color;
  ctx.lineWidth   = 2 * dpr;
  ctx.lineJoin    = 'round';
  ctx.stroke();

  const last = vals[vals.length - 1];
  ctx.fillStyle = '#e6edf3';
  ctx.font      = `${12 * dpr}px Inter, sans-serif`;
  ctx.textAlign = 'right';
  ctx.fillText(`${last.toFixed(1)}${unit}`, W - pad, pad + 12 * dpr);
}
