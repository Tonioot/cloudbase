import { api, wsNodeEvents, wsNodeCommands, wsSystemStats } from './api.js';
import { badge, toast, confirm, prompt, spinner, fmtDate, timeAgo, fmtUptime, icon } from './utils.js';

const params  = new URLSearchParams(location.search);
const NODE_ID = parseInt(params.get('id'));

let node = null;
let hasLoadedLogs = false;
let lastLogsText = '';
let latestLogsRequest = 0;

// ── Chart state ───────────────────────────────────────────────────────────────
let nChartCpu = null, nChartMem = null, nChartDisk = null;
const nCpuData = [], nMemData = [], nDiskData = [];
const MAX_PTS  = 60;

export async function initNodePage() {
    if (!NODE_ID || isNaN(NODE_ID)) {
        window.location.href = '/';
        return;
    }

    try {
        const nodes = await api.listNodes();
        node = nodes.find(n => n.id === NODE_ID);
        if (!node) { window.location.href = '/'; return; }
    } catch (err) {
        toast(err.message, 'error');
        setTimeout(() => { window.location.href = '/'; }, 1500);
        return;
    }

    renderHeader();
    renderHeader_overview();
    loadLogs();
    loadHistory();
    setupWebSockets();

    document.getElementById('btn-rename').onclick = renameNode;
    document.getElementById('btn-toggle-enable').onclick = toggleEnable;
    document.getElementById('btn-delete').onclick = deleteNode;
    document.getElementById('btn-refresh-logs').onclick = loadLogs;
    setInterval(loadLogs, 12000);
    setInterval(loadHistory, 15000);

    if (!node.is_local) autoPing();

    if (node.is_local) {
        wsSystemStats(data => {
            updateMetrics({ cpu_percent: data.cpu_percent, memory_percent: data.memory_percent, disk_percent: data.disk_percent });
        });
    }
}

async function autoPing() {
    const pingBadge = document.getElementById('node-ping-badge');
    const val       = document.getElementById('node-ping-val');
    if (!pingBadge || !val) return;
    pingBadge.style.display = 'flex';

    const run = async () => {
        val.textContent = '…';
        pingBadge.style.color = 'var(--text-muted)';
        try {
            const r = await api.pingNode(NODE_ID);
            if (r.reachable) {
                val.textContent = `${r.latency_ms}ms`;
                pingBadge.style.color = r.latency_ms < 100 ? 'var(--green)' : r.latency_ms < 300 ? 'var(--yellow)' : 'var(--red)';
            } else {
                val.textContent = 'unreachable';
                pingBadge.style.color = 'var(--red)';
            }
        } catch {
            val.textContent = 'error';
            pingBadge.style.color = 'var(--text-muted)';
        }
    };

    await run();
    pingBadge.onclick = run;
    setInterval(run, 15000);
}

function renderHeader() {
    document.getElementById('node-name').textContent       = node.name;
    document.getElementById('node-name-crumb').textContent = node.name;
    document.title = `${node.name} — Node — Cloudbase`;

    const online = node.status === 'online';
    document.getElementById('node-status-badge').innerHTML =
        `<span style="font-size:11px;padding:2px 9px;border-radius:999px;background:${online ? 'var(--green-bg)' : 'var(--red-bg)'};color:${online ? 'var(--green)' : 'var(--red)'}">${node.status}</span>`;

    const roleLabel = node.is_local ? 'Primary Node' : node.role === 'hybrid' ? 'Hybrid' : 'Node';
    const connType  = node.is_local ? 'local' : node.websocket_connected ? 'WebSocket' : 'Connecting';
    document.getElementById('node-meta').textContent =
        `${roleLabel} · ${connType}${node.api_base_url ? ' · ' + node.api_base_url : ''}`;

    const toggleBtn = document.getElementById('btn-toggle-enable');
    if (node.is_local) {
        document.getElementById('btn-rename').style.display = 'none';
        toggleBtn.style.display = 'none';
        document.getElementById('btn-delete').style.display = 'none';
    } else {
        toggleBtn.textContent = node.enabled ? 'Disable Node' : 'Enable Node';
        toggleBtn.className   = `btn ${node.enabled ? 'btn-secondary' : 'btn-success'}`;
    }
}

function setupWebSockets() {
    if (node.is_local) return;

    wsNodeEvents(node.id, event => {
        if (event.type === 'node_health') {
            updateMetrics(event);
            if (node.status !== 'online') {
                node.status = 'online';
                _setChartsOfflineState(false);
            }
        }
        if (event.type === 'node_offline') {
            node.status = 'offline';
            _setChartsOfflineState(true);
        }
    });

    wsNodeCommands(node.id, msg => {
        if (msg.type === 'snapshot') renderCommands(msg.commands);
    });
}

/* ─── Overview tab ─────────────────────────────────────────────────────── */
function renderHeader_overview() {
    if (node.node_metrics) updateMetrics(node.node_metrics);

    const meta   = node.metadata || {};
    const connLabel = node.is_local ? 'Local' : node.websocket_connected ? 'WebSocket' : 'Connecting';

    const grid = document.getElementById('node-info-grid');
    grid.innerHTML = [
        _detailItem('Connection', connLabel),
        _detailItem('Agent', node.agent_version || '\u2014'),
        _detailItem('Last Seen', (node.is_local || node.websocket_connected) ? 'Connected' : timeAgo(node.last_seen), {
            valueClass: (node.is_local || node.websocket_connected) ? 'node-detail-value-live' : ''
        }),
        _detailItem('Uptime', `<span id="n-uptime">—</span>`),
        meta.hostname ? _detailItem('Hostname', meta.hostname) : '',
        meta.ip ? _detailItem('IP Address', meta.ip, { valueClass: 'node-detail-value-mono' }) : '',
        meta.os ? _detailItem('OS', meta.os_short || meta.os) : '',
        meta.arch ? _detailItem('Architecture', meta.arch) : '',
        meta.cpu_count ? _detailItem('CPU Cores', `${meta.cpu_count}P / ${meta.cpu_count_logical || meta.cpu_count}L`) : '',
        meta.ram_total_mb ? _detailItem('RAM', `${Math.round(meta.ram_total_mb / 1024)}GB`) : '',
        meta.disk_total_gb ? _detailItem('Storage', `${meta.disk_total_gb}GB`) : '',
        _detailItem('Node ID', `#${node.id}`, { valueClass: 'node-detail-value-mono' }),
    ].join('');

    if (meta.uptime_secs) { const el = document.getElementById('n-uptime'); if (el) el.textContent = fmtUptime(meta.uptime_secs); }

    renderCommands([]);
    _initNodeCharts();
}

function _detailItem(label, val, { valueClass = '' } = {}) {
    const classes = ['node-detail-value', valueClass].filter(Boolean).join(' ');
    return `<div class="node-detail-item"><span class="node-detail-label">${label}</span><span class="${classes}">${val}</span></div>`;
}


function updateMetrics(m) {
    const _set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    if (m.cpu_percent    != null) _set('n-cpu',    `${m.cpu_percent.toFixed(1)}%`);
    if (m.memory_percent != null) _set('n-mem',    `${m.memory_percent.toFixed(1)}%`);
    if (m.disk_percent   != null) _set('n-disk',   `${m.disk_percent.toFixed(1)}%`);
    if (m.uptime_secs    != null) _set('n-uptime', fmtUptime(m.uptime_secs));

    // Feed charts
    const t = Date.now();
    if (m.cpu_percent    != null) { nCpuData.push({t, v: m.cpu_percent});    if (nCpuData.length  > MAX_PTS) nCpuData.shift(); }
    if (m.memory_percent != null) { nMemData.push({t, v: m.memory_percent}); if (nMemData.length  > MAX_PTS) nMemData.shift(); }
    if (m.disk_percent   != null) { nDiskData.push({t, v: m.disk_percent});  if (nDiskData.length > MAX_PTS) nDiskData.shift(); }
    _updateNodeChart(nChartCpu,  nCpuData);
    _updateNodeChart(nChartMem,  nMemData);
    _updateNodeChart(nChartDisk, nDiskData);
}

function _metricBar(label, pct) {
    if (pct == null) return '';
    const color = pct > 80 ? 'var(--red)' : pct > 60 ? 'var(--yellow)' : 'var(--accent)';
    return `
        <div>
            <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-secondary);margin-bottom:5px">
                <span>${label}</span><span style="font-weight:600;color:${color}">${pct.toFixed(1)}%</span>
            </div>
            <div style="height:4px;background:var(--bg-muted);border-radius:3px">
                <div style="height:100%;width:${Math.min(pct, 100)}%;background:${color};border-radius:3px;transition:width .4s"></div>
            </div>
        </div>`;
}

// ── Node sparkline charts ─────────────────────────────────────────────────────
function _initNodeCharts() {
    nChartCpu  = _nodeChart('node-chart-cpu',  '#c8c8c8');
    nChartMem  = _nodeChart('node-chart-mem',  '#a78bfa');
    nChartDisk = _nodeChart('node-chart-disk', '#fbbf24');

    const offline = node.status !== 'online';
    _setChartsOfflineState(offline);
}

function _setChartsOfflineState(offline) {
    const offlineSvg = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/></svg>`;
    const pairs = [
        ['node-chart-cpu',  'node-chart-cpu-empty'],
        ['node-chart-mem',  'node-chart-mem-empty'],
        ['node-chart-disk', 'node-chart-disk-empty'],
    ];
    pairs.forEach(([canvasId, overlayId]) => {
        const canvas  = document.getElementById(canvasId);
        const overlay = document.getElementById(overlayId);
        if (!overlay) return;
        if (offline) {
            overlay.innerHTML = `${offlineSvg}<span>Node offline</span>`;
            overlay.style.display = 'flex';
            if (canvas) canvas.style.opacity = '0';
        } else {
            overlay.style.display = 'none';
            if (canvas) canvas.style.opacity = '1';
        }
    });

    const logsOverlay = document.getElementById('node-logs-empty');
    const logsContent = document.getElementById('node-logs-content');
    if (logsOverlay) {
        if (offline) {
            logsOverlay.innerHTML = `${offlineSvg}<span>Node offline</span>`;
            logsOverlay.style.display = 'flex';
            if (logsContent) logsContent.style.opacity = '0';
            hasLoadedLogs = false;
            lastLogsText = '';
        } else {
            logsOverlay.style.display = 'none';
            if (logsContent) logsContent.style.opacity = '1';
        }
    }
}

function _nodeChart(id, color) {
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    return { canvas, ctx: canvas.getContext('2d'), color };
}

function _updateNodeChart(chart, data) {
    if (!chart) return;
    const { canvas, ctx, color } = chart;
    canvas.width  = canvas.offsetWidth  * devicePixelRatio;
    canvas.height = canvas.offsetHeight * devicePixelRatio;
    const W = canvas.width, H = canvas.height, dpr = devicePixelRatio;
    ctx.clearRect(0, 0, W, H);
    if (data.length < 2) return;

    const vals   = data.map(d => d.v);
    const rawMax = Math.max(...vals);
    const yMax   = rawMax === 0 ? 10 : Math.max(Math.ceil(rawMax / 10) * 10, 10);

    const pL = 26 * dpr;   // left — y labels
    const pR =  6 * dpr;   // right
    const pT = 16 * dpr;   // top  — current value label
    const pB =  4 * dpr;   // bottom (no time labels — too small)

    const cW = W - pL - pR;
    const cH = H - pT - pB;
    const xStp = cW / (vals.length - 1);
    const yS   = v => pT + cH - Math.min(Math.max(v, 0) / yMax, 1) * cH;

    // Grid lines + Y labels: 0 / 50 / 100 % of max
    ctx.font = `${9 * dpr}px Inter, sans-serif`;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 2; i++) {
        const frac = i / 2;
        const y    = pT + cH - frac * cH;
        ctx.strokeStyle = 'rgba(255,255,255,0.05)';
        ctx.lineWidth   = dpr;
        ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(W - pR, y); ctx.stroke();
        ctx.fillStyle = 'rgba(130,145,165,0.6)';
        ctx.fillText((yMax * frac).toFixed(0), pL - 4 * dpr, y);
    }

    // Gradient fill
    const grad = ctx.createLinearGradient(0, pT, 0, pT + cH);
    grad.addColorStop(0, color + '22');
    grad.addColorStop(1, color + '00');
    ctx.beginPath();
    ctx.moveTo(pL, yS(vals[0]));
    vals.forEach((v, i) => { if (i > 0) ctx.lineTo(pL + i * xStp, yS(v)); });
    ctx.lineTo(pL + (vals.length - 1) * xStp, pT + cH);
    ctx.lineTo(pL, pT + cH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(pL, yS(vals[0]));
    vals.forEach((v, i) => { if (i > 0) ctx.lineTo(pL + i * xStp, yS(v)); });
    ctx.strokeStyle = color;
    ctx.lineWidth   = 1.5 * dpr;
    ctx.lineJoin    = 'round';
    ctx.stroke();

    // Current value — top right
    const cur = vals[vals.length - 1];
    ctx.font         = `600 ${10 * dpr}px Inter, sans-serif`;
    ctx.textAlign    = 'right';
    ctx.textBaseline = 'top';
    ctx.fillStyle    = color;
    ctx.fillText(`${cur.toFixed(1)}%`, W - pR, 3 * dpr);
}


function _emptyState(svgPath, label) {
    const svg = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">${svgPath}</svg>`;
    return `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;height:100%;color:var(--text-muted)">${svg}<span style="font-size:11px;font-weight:500;opacity:0.5">${label}</span></div>`;
}

function renderCommands(cmds) {
    const list = document.getElementById('node-cmd-list');
    if (!list) return;
    if (!cmds.length) {
        list.innerHTML = `<div style="font-size:12px;color:var(--text-muted)">No active commands.</div>`;
        return;
    }
    list.innerHTML = cmds.map(c => `
        <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px">
            <div>
                <div style="font-size:13px;font-weight:500;color:var(--text-primary)">${c.command_type}</div>
                ${c.app_id ? `<div style="font-size:11px;color:var(--text-muted)">App #${c.app_id}</div>` : ''}
            </div>
            <div style="text-align:right">
                <div style="font-size:12px;font-weight:600">${c.status}</div>
                <div style="font-size:11px;color:var(--text-muted)">${new Date(c.created_at).toLocaleTimeString()}</div>
            </div>
        </div>
    `).join('');
}

/* ─── Logs & History tab ────────────────────────────────────────────────── */
async function loadLogs() {
    const content = document.getElementById('node-logs-content');
    if (!content) return;
    if (!node || node.status !== 'online') {
        _setChartsOfflineState(true);
        return;
    }
    const requestId = ++latestLogsRequest;
    const shouldAutoScroll = !hasLoadedLogs || _isNearBottom(content);
    const prevScrollTop = content.scrollTop;
    const prevScrollHeight = content.scrollHeight;

    if (!hasLoadedLogs && !content.textContent.trim()) {
        content.textContent = 'Loading…';
    }
    try {
        const r = await api.getNodeAgentLogs(NODE_ID, 500);
        if (requestId !== latestLogsRequest) return;

        const nextText = r.lines.join('\n') || '(no logs found)';
        if (!hasLoadedLogs || nextText !== lastLogsText) {
            content.textContent = nextText;
            lastLogsText = nextText;

            if (shouldAutoScroll) {
                content.scrollTop = content.scrollHeight;
            } else {
                const nextScrollHeight = content.scrollHeight;
                content.scrollTop = prevScrollTop + Math.max(0, nextScrollHeight - prevScrollHeight);
            }
        }
        hasLoadedLogs = true;
    } catch (e) {
        if (requestId !== latestLogsRequest) return;
        content.textContent = `Error: ${e.message}`;
        hasLoadedLogs = false;
        lastLogsText = '';
    }
}

function _isNearBottom(el, threshold = 28) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

async function loadHistory() {
    const list = document.getElementById('node-history-list');
    if (!list) return;
    list.style.alignItems = '';
    list.style.justifyContent = '';
    list.innerHTML = `<div style="padding:12px 0;font-size:12px;color:var(--text-muted)">${spinner} Loading…</div>`;
    try {
        const history = await api.listNodeCommands(NODE_ID);
        if (!history.length) {
            list.style.alignItems = 'center';
            list.style.justifyContent = 'center';
            list.innerHTML = _emptyState('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>', 'No command history');
            return;
        }
        list.style.alignItems = '';
        list.style.justifyContent = '';
        list.innerHTML = history.slice(0, 50).map(c => `
            <div class="card" style="padding:12px 16px">
                <div style="display:flex;justify-content:space-between">
                    <span style="font-weight:600;font-size:13px;color:var(--text-primary)">${c.command_type}</span>
                    <span style="font-size:12px;font-weight:600;color:${c.status === 'done' ? 'var(--green)' : 'var(--red)'}">${c.status}</span>
                </div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:3px">
                    ${fmtDate(c.created_at)}${c.app_id ? ' · App #' + c.app_id : ''}
                </div>
                ${c.error_message ? `<div style="font-size:11px;color:var(--red);margin-top:5px">${c.error_message}</div>` : ''}
            </div>
        `).join('');
    } catch (e) {
        list.innerHTML = `<div style="color:var(--red);font-size:12px">${e.message}</div>`;
    }
}

/* ─── Actions ───────────────────────────────────────────────────────────── */
async function renameNode() {
    const newName = await prompt('Rename Node', 'New display name for this node', node.name);
    if (!newName || newName === node.name) return;

    try {
        const updated = await api.updateNode(NODE_ID, { name: newName.trim() });
        node.name = updated.name;
        document.getElementById('node-name').textContent = updated.name;
        document.getElementById('node-name-crumb').textContent = updated.name;
        document.title = `${updated.name} — Node — Cloudbase`;
        toast('Node name updated');
    } catch (e) {
        toast(e.message, 'error');
    }
}

async function toggleEnable() {
    const btn = document.getElementById('btn-toggle-enable');
    btn.disabled = true;
    try {
        if (node.enabled) await api.disableNode(NODE_ID);
        else              await api.enableNode(NODE_ID);
        node.enabled = !node.enabled;
        renderHeader();
        toast(`Node ${node.enabled ? 'enabled' : 'disabled'}`);
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

async function deleteNode() {
    const ok = await confirm(`Remove node "${node.name}"?`, 'This cannot be undone.');
    if (!ok) return;
    try {
        await api.deleteNode(NODE_ID);
        toast('Node removed');
        window.location.href = '/';
    } catch (e) {
        toast(e.message, 'error');
    }
}

