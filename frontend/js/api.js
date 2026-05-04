const BASE = '/api';

function redirectToLogin() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/login.html?next=${next}`;
}

async function request(method, path, body) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opts);
  if (res.status === 401) { redirectToLogin(); return; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

export const api = {
  health:        ()          => request('GET',    '/health'),
  checkAuth:     ()          => request('GET',    '/auth/check'),
  getSession:    ()          => request('GET',    '/auth/session'),
  logout:        ()          => request('POST',   '/auth/logout'),
  changePassword:(newPwd)    => request('POST',   '/auth/change-password', { password: newPwd }),
  listUsers:     ()          => request('GET',    '/users'),
  createUser:    (data)      => request('POST',   '/users', data),
  updateUser:    (id, data)  => request('PUT',    `/users/${id}`, data),
  deleteUser:    (id)        => request('DELETE', `/users/${id}`),
  listApps: ()         => request('GET',    '/apps'),
  listNodes:()         => request('GET',    '/nodes'),
  createNodeInvite:(payload) => request('POST', '/nodes/invites', payload),
  listNodeCommands:(nodeId) => request('GET', `/nodes/${nodeId}/commands`),
  getNodeCommandStatus:(nodeId, cmdId) => request('GET', `/nodes/${nodeId}/commands/${cmdId}`),
  updateNode:(nodeId, data) => request('PATCH', `/nodes/${nodeId}`, data),
  enableNode:(nodeId) => request('POST', `/nodes/${nodeId}/enable`),
  disableNode:(nodeId) => request('POST', `/nodes/${nodeId}/disable`),
  deleteNode:(nodeId) => request('DELETE', `/nodes/${nodeId}`),
  pingNode:(nodeId) => request('POST', `/nodes/${nodeId}/ping`),
  getNodeConnectionStatus:(nodeId) => request('GET', `/nodes/${nodeId}/connection-status`),
  getNodeAgentLogs:(nodeId, limit) => request('GET', `/nodes/${nodeId}/agent-logs?limit=${limit || 200}`),
  getApp:   (id)       => request('GET',    `/apps/${id}`),
  deploy:   (payload)  => request('POST',   '/apps', payload),
  updateApp:(id, data) => request('PUT',    `/apps/${id}`, data),
  deleteApp:(id)       => request('DELETE', `/apps/${id}`),
  exportApps:(appIds)  => request('POST',   '/apps/export', { app_ids: appIds }),
  importApps:(apps, targetNodeId) => request('POST', '/apps/import', { apps, target_node_id: targetNodeId }),
  moveApp:  (id, targetNodeId, port) => request('POST', `/apps/${id}/move`, { target_node_id: targetNodeId, ...(port != null ? { port } : {}) }),
  start:    (id)       => request('POST',   `/apps/${id}/start`),
  stop:     (id)       => request('POST',   `/apps/${id}/stop`),
  restart:  (id)       => request('POST',   `/apps/${id}/restart`),
  rebuild:  (id)       => request('POST',   `/apps/${id}/rebuild`),
  pull:     (id, body) => request('POST',   `/apps/${id}/pull`, body),
  listCommits:(id, limit) => request('GET', `/apps/${id}/commits?limit=${limit || 20}`),

  /**
   * Consume a Server-Sent Events action stream (pull/rebuild).
   * Calls onLine(string) for every log line as it arrives.
   * Resolves with the final result object, or rejects on error.
   */
  async streamAction(path, body, onLine) {
    const opts = { method: 'POST', headers: {}, credentials: 'same-origin' };
    if (body) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(BASE + path, opts);
    if (res.status === 401) { redirectToLogin(); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let result = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const events = buf.split('\n\n');
      buf = events.pop();
      for (const ev of events) {
        if (!ev.trim()) continue;
        let evType = 'message', data = '';
        for (const line of ev.split('\n')) {
          if (line.startsWith('event: ')) evType = line.slice(7).trim();
          else if (line.startsWith('data: ')) data = line.slice(6);
        }
        if (evType === 'result') { result = JSON.parse(data); }
        else if (data === '__DONE__') { return result; }
        else if (data === '__FAILED__') { throw new Error(result?.error || 'Action failed'); }
        else { try { onLine(JSON.parse(data)); } catch { onLine(data); } }
      }
    }
    return result;
  },
  getStats: (id)       => request('GET',    `/apps/${id}/stats`),
  listFiles:(id, path) => request('GET',    `/apps/${id}/files?path=${encodeURIComponent(path || '')}`),
  fileContent:(id, p)  => request('GET',    `/apps/${id}/files/content?path=${encodeURIComponent(p)}`),
  serviceFile:()       => request('GET',    '/apps/system/service-file'),
  discoverCerts:()     => request('GET',    '/apps/system/certs'),
  discoverAppCerts:(id)=> request('GET',    `/apps/${id}/certs`),
  uploadSystemCert: (file) => {
    const fd = new FormData(); fd.append('file', file);
    return fetch(BASE + '/system/certs/upload', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(r => { if (r.status === 401) { redirectToLogin(); return; } return r.json().then(d => { if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`); return d; }); });
  },
  uploadAppCert: (id, file) => {
    const fd = new FormData(); fd.append('file', file);
    return fetch(BASE + `/apps/${id}/certs/upload`, { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(r => { if (r.status === 401) { redirectToLogin(); return; } return r.json().then(d => { if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`); return d; }); });
  },
  nginxRefresh:   (id)       => request('POST',  `/apps/${id}/nginx-refresh`),
  getNginxConfig: (id) => request('GET',    `/apps/${id}/nginx-config`),
  saveNginxConfig:(id, content) => request('PUT', `/apps/${id}/nginx-config`, { content }),
  getMaintenancePages:   (id)       => request('GET',  `/apps/${id}/maintenance-pages`),
  saveMaintenancePages:  (id, data) => request('PUT',  `/apps/${id}/maintenance-pages`, data),
  toggleMaintenanceMode: (id)       => request('POST', `/apps/${id}/maintenance-mode/toggle`),
  toggleUpdateMode:      (id)       => request('POST', `/apps/${id}/update-mode/toggle`),
  getAuditLog: (appId, limit, offset) => request('GET', `/audit-log?limit=${limit || 100}&offset=${offset || 0}${appId ? `&app_id=${appId}` : ''}`),
  getStatsHistory:(id, hours) => request('GET', `/apps/${id}/stats/history?hours=${hours || 24}`),
  deployZeroDowntime: (id) => request('POST', `/apps/${id}/deploy-zero-downtime`),
  listReplicas:   (id)             => request('GET',    `/apps/${id}/replicas`),
  listInstances:  (id)             => request('GET',    `/apps/${id}/instances`),
  getInstanceStats:(id)            => request('GET',    `/apps/${id}/instances/stats`),
  scaleApp:       (id, body)       => request('POST',   `/apps/${id}/scale`, body),
  removeReplica:  (id, replicaId)  => request('DELETE', `/apps/${id}/replicas/${replicaId}`),
  deleteInstance: (id, instanceId) => request('DELETE', `/apps/${id}/instances/${instanceId}`),
  restartInstance:(id, instanceId) => request('POST',   `/apps/${id}/instances/${instanceId}/restart`),
  getInstanceLogs:(id, repId, lines) => request('GET',  `/apps/${id}/replicas/${repId}/logs?lines=${lines || 200}`),
  getAppLogsTail: (id, lines)      => request('GET',    `/apps/${id}/logs/tail?limit=${lines || 200}`),
  getServerLogs: (lines) => request('GET', `/system/logs?lines=${lines || 500}`),
  getPDManagerNginx: () => request('GET',  '/system/nginx-config'),
  applyPDManagerNginx:(data)    => request('POST', '/system/nginx-config', data),
  listGitHubTokens:  ()         => request('GET',    '/system/github-tokens'),
  saveGitHubToken:   (label, token) => request('POST', '/system/github-tokens', { label, token }),
  deleteGitHubToken: (id)       => request('DELETE', `/system/github-tokens/${id}`),
};

export function wsLogs(appId, onLine) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/ws/apps/${appId}/logs`);
    ws.onmessage = e => onLine(e.data);
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; if (ws) ws.close(); } };
}

export function wsStats(appId, onData) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/ws/apps/${appId}/stats`);
    ws.onmessage = e => onData(JSON.parse(e.data));
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; ws.close(); } };
}

export function wsSystemStats(onData) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/ws/system/stats`);
    ws.onmessage = e => onData(JSON.parse(e.data));
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; ws.close(); } };
}

export function wsNodeEvents(nodeId, onEvent) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/api/nodes/${nodeId}/events`);
    ws.onmessage = e => { try { onEvent(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; if (ws) ws.close(); } };
}

export function wsNodeStats(nodeId, onData) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/api/nodes/${nodeId}/stats`);
    ws.onmessage = e => { try { onData(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; if (ws) ws.close(); } };
}

export function wsLocalNodeStats(onData) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/ws/system/stats`);
    ws.onmessage = e => { try { onData(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; if (ws) ws.close(); } };
}

export function wsServerLogs(onLine) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/ws/system/server-logs`);
    ws.onmessage = e => onLine(e.data);
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; if (ws) ws.close(); } };
}

export function wsNodeCommands(nodeId, onUpdate) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws, closed = false;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/api/nodes/${nodeId}/commands/live`);
    ws.onmessage = e => { try { onUpdate(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => { if (!closed) setTimeout(connect, 3000); };
  }
  connect();
  return { close() { closed = true; if (ws) ws.close(); } };
}
