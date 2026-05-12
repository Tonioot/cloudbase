import { api } from './api.js';
import { toast, confirm } from './utils.js';

const STATUS_DOT = {
  running:  'var(--green)',
  stopped:  'var(--text-muted)',
  error:    'var(--red)',
  deploying:'var(--yellow)',
  starting: 'var(--yellow)',
  stopping: 'var(--yellow)',
};

function esc(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Helper: set a cert filename display + hidden input ───────────────────────
function setCertDisplay(modal, nameId, hiddenId, path) {
  modal.querySelector(hiddenId).value = path || '';
  const nameEl = modal.querySelector(nameId);
  if (path) {
    nameEl.textContent = path.split('/').pop();
    nameEl.classList.add('has-value');
  } else {
    nameEl.textContent = 'No file selected';
    nameEl.classList.remove('has-value');
  }
}

export function initSidebar() {
  loadSidebarTree();
  setInterval(loadSidebarTree, 10000);
  wireNodesButton();
  wirePDManagerNginxButton();
  initSessionTimer();
  wireGitHubTokensButton();
  wireExportImportButton();
  initRoleBasedUI();
  wireSystemSettingsButton();
}

async function loadSidebarTree() {
  const container = document.getElementById('sidebar-nodes');
  if (!container) return;

  try {
    const [nodes, apps] = await Promise.all([
      api.listNodes(),
      api.listApps(),
    ]);

    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    const localNode = nodes.find(n => n.is_local);

    const onAppPage     = location.pathname.startsWith('/app');
    const onNodePage    = location.pathname.startsWith('/node');
    const currentAppId  = onAppPage  ? parseInt(new URLSearchParams(location.search).get('id')) : NaN;
    const currentNodeId = onNodePage ? parseInt(new URLSearchParams(location.search).get('id')) : NaN;

    // ── Nodes section (only when multi-node) ──────────────────────────────
    let nodesHtml = '';
    const remoteNodes = nodes.filter(n => !n.is_local);
    if (remoteNodes.length) {
      nodesHtml = `
        <div class="sidebar-section-label" style="margin-top:10px">Nodes</div>
        ${nodes.map(n => {
          const dot = n.status === 'online' ? 'var(--green)' : n.status === 'offline' ? 'var(--red)' : 'var(--yellow)';
          const active = n.id === currentNodeId ? ' active' : '';
          const label = n.is_local ? 'Primary Node' : n.name;
          return `<a href="/node?id=${n.id}" class="sidebar-app-item${active}">
            <span class="sidebar-app-dot" style="background:${dot}"></span>
            <span class="sidebar-app-name">${label}</span>
          </a>`;
        }).join('')}
        <div class="sidebar-section-label" style="margin-top:10px">Apps</div>`;
    }

    // ── Flat apps list ────────────────────────────────────────────────────
    const appsHtml = apps.length
      ? apps.map(app => {
          const appDot = STATUS_DOT[app.status] || 'var(--text-muted)';
          const active = app.id === currentAppId ? ' active' : '';
          const replicas = app.replicas || [];
          const instanceLabel = remoteNodes.length && replicas.length
            ? (() => {
                const nodeIds = [...new Set(replicas.map(r => r.node_id).filter(Boolean))];
                const names = nodeIds.map(id => {
                  const n = nodeMap.get(id);
                  return n ? (n.is_local ? 'local' : n.name) : 'local';
                });
                const label = names.length ? names.join(', ') : 'local';
                return `<span style="font-size:10px;color:var(--text-muted);margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60px" title="${label}">${label}</span>`;
              })()
            : '';

          return `<a href="/app?id=${app.id}" class="sidebar-app-item${active}">
            <span class="sidebar-app-dot" style="background:${appDot}"></span>
            <span class="sidebar-app-name">${app.name}</span>
            ${instanceLabel}
          </a>`;
        }).join('')
      : `<div class="sidebar-apps-empty">No apps yet</div>`;

    container.innerHTML = nodesHtml + appsHtml;

    const appsContainer = document.getElementById('sidebar-apps');
    if (appsContainer) {
      appsContainer.innerHTML = '';
      appsContainer.style.display = 'none';
    }
  } catch {}
}

function wireNodesButton() {
  const btn = document.getElementById('btn-nodes');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const section = document.getElementById('nodes-section');
    if (section) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      // On non-dashboard pages, navigate to the dashboard nodes section
      window.location.href = '/#nodes-section';
    }
  });
}

function wirePDManagerNginxButton() {
  const btn   = document.getElementById('btn-pdm-nginx');
  const modal = document.getElementById('pdm-nginx-modal');
  if (!btn || !modal) return;

  btn.addEventListener('click', async () => {
    modal.style.display = 'flex';
    const msg = modal.querySelector('#pdm-nginx-msg');
    msg.style.display = 'none';

    // Pre-fill existing values
    try {
      const data = await api.getPDManagerNginx();
      // Cloudbase panel section
      if (data.exists && data.content) {
        const m = data.content.match(/server_name\s+([^\s;]+)/);
        if (m) modal.querySelector('#pdm-domain').value = m[1];
        const c = data.content.match(/ssl_certificate\s+([^\s;]+)/);
        if (c) setCertDisplay(modal, '#pdm-cert-name', '#pdm-cert', c[1]);
        const k = data.content.match(/ssl_certificate_key\s+([^\s;]+)/);
        if (k) setCertDisplay(modal, '#pdm-key-name', '#pdm-key', k[1]);
      }
      // App subdomains section
      if (data.base_domain) modal.querySelector('#pdm-base-domain').value = data.base_domain;
      if (data.base_ssl_cert_path) setCertDisplay(modal, '#pdm-base-cert-name', '#pdm-base-cert', data.base_ssl_cert_path);
      if (data.base_ssl_key_path)  setCertDisplay(modal, '#pdm-base-key-name',  '#pdm-base-key',  data.base_ssl_key_path);
    } catch {}
  });

  modal.querySelector('#pdm-nginx-close').onclick = () => { modal.style.display = 'none'; };
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });

  // Cloudbase panel cert/key uploads
  modal.querySelector('#pdm-upload-cert').addEventListener('click', () => modal.querySelector('#pdm-cert-file').click());
  modal.querySelector('#pdm-cert-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    if (!file) return;
    modal.querySelector('#pdm-upload-cert').disabled = true;
    try {
      const res = await api.uploadSystemCert(file);
      setCertDisplay(modal, '#pdm-cert-name', '#pdm-cert', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { modal.querySelector('#pdm-upload-cert').disabled = false; e.target.value = ''; }
  });

  modal.querySelector('#pdm-upload-key').addEventListener('click', () => modal.querySelector('#pdm-key-file').click());
  modal.querySelector('#pdm-key-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    if (!file) return;
    modal.querySelector('#pdm-upload-key').disabled = true;
    try {
      const res = await api.uploadSystemCert(file);
      setCertDisplay(modal, '#pdm-key-name', '#pdm-key', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { modal.querySelector('#pdm-upload-key').disabled = false; e.target.value = ''; }
  });

  // App subdomains cert/key uploads
  modal.querySelector('#pdm-upload-base-cert').addEventListener('click', () => modal.querySelector('#pdm-base-cert-file').click());
  modal.querySelector('#pdm-base-cert-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    if (!file) return;
    modal.querySelector('#pdm-upload-base-cert').disabled = true;
    try {
      const res = await api.uploadSystemCert(file);
      setCertDisplay(modal, '#pdm-base-cert-name', '#pdm-base-cert', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { modal.querySelector('#pdm-upload-base-cert').disabled = false; e.target.value = ''; }
  });

  modal.querySelector('#pdm-upload-base-key').addEventListener('click', () => modal.querySelector('#pdm-base-key-file').click());
  modal.querySelector('#pdm-base-key-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    if (!file) return;
    modal.querySelector('#pdm-upload-base-key').disabled = true;
    try {
      const res = await api.uploadSystemCert(file);
      setCertDisplay(modal, '#pdm-base-key-name', '#pdm-base-key', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { modal.querySelector('#pdm-upload-base-key').disabled = false; e.target.value = ''; }
  });

  modal.querySelector('#pdm-nginx-apply').addEventListener('click', async () => {
    const domain     = modal.querySelector('#pdm-domain').value.trim();
    const cert       = modal.querySelector('#pdm-cert').value.trim()      || null;
    const key        = modal.querySelector('#pdm-key').value.trim()       || null;
    const baseDomain = modal.querySelector('#pdm-base-domain').value.trim() || null;
    const baseCert   = modal.querySelector('#pdm-base-cert').value.trim() || null;
    const baseKey    = modal.querySelector('#pdm-base-key').value.trim()  || null;
    const msg        = modal.querySelector('#pdm-nginx-msg');

    if (!domain) { showMsg(msg, 'Cloudbase panel domain is required', false); return; }

    const applyBtn = modal.querySelector('#pdm-nginx-apply');
    applyBtn.disabled = true;
    applyBtn.textContent = 'Saving…';

    try {
      const res = await api.applyPDManagerNginx({
        domain,
        ssl_cert_path:      cert,
        ssl_key_path:       key,
        base_domain:        baseDomain,
        base_ssl_cert_path: baseCert,
        base_ssl_key_path:  baseKey,
      });
      if (res.ok) {
        const proto = cert ? 'https' : 'http';
        let detail = `Cloudbase reachable at ${proto}://${domain}`;
        if (baseDomain) detail += `. App subdomains enabled on ${baseDomain}`;
        showMsg(msg, detail, true);
      } else {
        showMsg(msg, res.message, false);
      }
    } catch (e) {
      showMsg(msg, e.message, false);
    } finally {
      applyBtn.disabled = false;
      applyBtn.textContent = 'Save & Apply';
    }
  });
}

function showMsg(el, text, success) {
  el.textContent = text;
  el.style.display = 'block';
  el.style.background = success ? 'var(--green-bg)'  : 'var(--red-bg)';
  el.style.color      = success ? 'var(--green)'     : 'var(--red)';
  el.style.border     = `1px solid ${success ? 'var(--green-border)' : 'var(--red-border)'}`;
}

function wireServiceButton() {
  const btn = document.getElementById('btn-install-service');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    let modal = document.getElementById('service-modal-global');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'service-modal-global';
      modal.className = 'dialog-backdrop';
      modal.innerHTML = `
        <div class="dialog" style="max-width:560px;width:90%">
          <div class="dialog-title">Enable Cloudbase Auto Start</div>
          <div class="dialog-body" style="font-size:13px;line-height:1.6">
            <p style="margin:0 0 10px">Run this command to make Cloudbase start automatically on boot:</p>
            <pre id="service-pre-global" style="background:var(--bg-muted);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;overflow-x:auto;white-space:pre;margin:0 0 12px">Loading…</pre>
            <p style="margin:0;color:var(--text-muted);font-size:12px">Requires <code>sudo</code>. Run once on your Linux server.</p>
          </div>
          <div class="dialog-actions">
            <button class="btn btn-secondary" id="service-copy-global">Copy Commands</button>
            <button class="btn btn-primary" id="service-close-global">Close</button>
          </div>
        </div>`;
      document.body.appendChild(modal);

      modal.querySelector('#service-close-global').onclick = () => { modal.style.display = 'none'; };
      modal.querySelector('#service-copy-global').onclick  = () => {
        navigator.clipboard.writeText(modal.querySelector('#service-pre-global').textContent);
        toast('Copied to clipboard');
      };
      modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
    }

    modal.style.display = 'flex';
    const pre = modal.querySelector('#service-pre-global');
    pre.textContent = 'Loading…';

    try {
      const data = await api.serviceFile();
      pre.textContent = [
        `# Fastest option`,
        `cloudbase enable`,
        ``,
        `# Manual systemd setup`,
        `sudo tee ${data.path} << 'EOF'`,
        data.content.trim(),
        `EOF`,
        ``,
        `sudo systemctl daemon-reload`,
        `sudo systemctl enable --now cloudbase`,
      ].join('\n');
    } catch (e) {
      pre.textContent = `Error: ${e.message}`;
    }
  });
}

// ── Session timer ─────────────────────────────────────────────────────────────
function fmtSeconds(s) {
  if (s <= 0) return 'Expired';
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

async function initSessionTimer() {
  const bar = document.getElementById('session-timer-bar');
  if (!bar) return;

  const fill  = bar.querySelector('.session-timer-fill');
  const label = bar.querySelector('.session-timer-label');

  let remaining = 3600; // fallback
  try {
    const data = await api.getSession();
    remaining = data.expires_in;
  } catch { return; }

  const total = 3600; // fixed token lifetime — percentage relative to full session

  function tick() {
    if (remaining <= 0) {
      label.textContent = 'Session expired — please log in again';
      fill.style.width  = '0%';
      fill.style.background = 'var(--red)';
      return;
    }

    label.textContent = `Session: ${fmtSeconds(remaining)} remaining`;
    const pct = Math.max(0, (remaining / total) * 100);
    fill.style.width = `${pct}%`;

    if (pct < 15) {
      fill.style.background = 'var(--red)';
    } else if (pct < 35) {
      fill.style.background = 'var(--yellow)';
    } else {
      fill.style.background = 'var(--accent)';
    }

    remaining--;
  }

  tick();
  setInterval(tick, 1000);
}

// ── GitHub token vault ────────────────────────────────────────────────────────
function wireGitHubTokensButton() {
  const btn = document.getElementById('btn-github-tokens');
  if (!btn) return;

  btn.addEventListener('click', () => openGitHubTokensModal());
}

function openGitHubTokensModal() {
  let modal = document.getElementById('github-tokens-modal-global');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'github-tokens-modal-global';
    modal.className = 'dialog-backdrop';
    modal.innerHTML = `
      <div class="dialog dialog-modern" style="max-width:500px;width:90%">
        <div class="dialog-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="margin-right:6px;vertical-align:-2px"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
          GitHub Tokens
        </div>
        <div class="dialog-body">
          <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px">
            Save tokens here so you can quickly pick them when deploying apps.
          </p>
          <div id="gh-tokens-list" style="margin-bottom:14px"></div>
          <div style="display:flex;gap:8px;margin-bottom:8px">
            <input class="input" id="gh-token-label" placeholder="Label (e.g. my-org)" style="flex:1;min-width:0" />
            <input class="input input-mono" id="gh-token-value" type="password" placeholder="ghp_..." autocomplete="current-password" style="flex:2;min-width:0" />
          </div>
          <div id="gh-token-err" style="display:none;color:var(--red);font-size:12px;margin-bottom:8px"></div>
        </div>
        <div class="dialog-actions">
          <button class="btn btn-secondary" id="gh-tokens-close">Close</button>
          <button class="btn btn-primary" id="gh-token-add">Save Token</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    modal.querySelector('#gh-tokens-close').onclick = () => { modal.style.display = 'none'; };
    modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });

    modal.querySelector('#gh-token-add').addEventListener('click', async () => {
      const label = modal.querySelector('#gh-token-label').value.trim();
      const token = modal.querySelector('#gh-token-value').value.trim();
      const err   = modal.querySelector('#gh-token-err');
      err.style.display = 'none';
      if (!label) { err.textContent = 'Label is required'; err.style.display = 'block'; return; }
      if (!token) { err.textContent = 'Token is required'; err.style.display = 'block'; return; }
      try {
        await api.saveGitHubToken(label, token);
        modal.querySelector('#gh-token-label').value = '';
        modal.querySelector('#gh-token-value').value = '';
        await renderTokenList(modal);
        toast(`Token "${label}" saved`);
      } catch (e) {
        err.textContent = e.message;
        err.style.display = 'block';
      }
    });
  }

  modal.style.display = 'flex';
  renderTokenList(modal);
}

async function renderTokenList(modal) {
  const list = modal.querySelector('#gh-tokens-list');
  list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">Loading…</div>';
  try {
    const tokens = await api.listGitHubTokens();
    if (tokens.length === 0) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">No saved tokens yet.</div>';
      return;
    }
    list.innerHTML = tokens.map(t => `
      <div class="gh-token-row" data-id="${t.id}">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="color:var(--text-muted);flex-shrink:0"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
        <span class="gh-token-row-label">${esc(t.label)}</span>
        <span class="gh-token-row-hint">••••${esc(t.token_hint)}</span>
        <button class="btn btn-danger btn-sm gh-token-delete" style="padding:3px 8px;font-size:11px">Delete</button>
      </div>`).join('');

    list.querySelectorAll('.gh-token-delete').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.closest('[data-id]').dataset.id;
        await api.deleteGitHubToken(id);
        await renderTokenList(modal);
        toast('Token deleted');
      });
    });
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);font-size:12px">${e.message}</div>`;
  }
}

// Exported so the deploy modal and settings page can call it.
// tokenInput    – the visible password <input> (used for display only when a vault token is chosen)
// tokenIdInput  – a hidden <input> that stores the vault token ID (sent to backend instead of raw value)
export async function pickGitHubToken(tokenInput, tokenIdInput) {
  let tokens = [];
  try { tokens = await api.listGitHubTokens(); } catch { return; }
  if (!tokens.length) { toast('No saved tokens — save one via the GitHub Tokens button', 'warn'); return; }

  document.querySelectorAll('.gh-token-picker').forEach(p => p.remove());

  const picker = document.createElement('div');
  picker.className = 'gh-token-picker cert-picker';
  picker.style.cssText = `position:absolute;z-index:9999;background:#141414;border:1px solid #2e2e2e;
    border-radius:6px;max-height:200px;overflow-y:auto;min-width:260px;
    box-shadow:0 8px 24px rgba(0,0,0,.6);font-size:12px;`;

  tokens.forEach(t => {
    const row = document.createElement('div');
    row.style.cssText = 'padding:8px 12px;cursor:pointer;color:#f0f0f0;display:flex;justify-content:space-between;gap:12px;';
    row.innerHTML = `<span style="font-weight:500">${esc(t.label)}</span><span style="color:#a0a0a0;font-family:monospace">••••${esc(t.token_hint)}</span>`;
    row.addEventListener('mouseenter', () => row.style.background = '#222222');
    row.addEventListener('mouseleave', () => row.style.background = '');
    row.addEventListener('click', () => {
      picker.remove();
      // Store only the vault ID server-side; show a non-editable label in the input
      if (tokenIdInput) tokenIdInput.value = t.id;
      // Show the label as a visual indicator — placeholder style
      tokenInput.value = '';
      tokenInput.placeholder = `🔑 ${t.label} (••••${t.token_hint})`;
      tokenInput.dataset.vaultLabel = t.label;
      // Clear the vault selection when the user starts typing a new token manually
      const clearVault = () => {
        if (tokenIdInput) tokenIdInput.value = '';
        tokenInput.placeholder = tokenInput.dataset.origPlaceholder || '';
        delete tokenInput.dataset.vaultLabel;
        tokenInput.removeEventListener('input', clearVault);
      };
      tokenInput.addEventListener('input', clearVault);
    });
    picker.appendChild(row);
  });

  const rect = tokenInput.getBoundingClientRect();
  picker.style.top  = `${rect.bottom + window.scrollY + 4}px`;
  picker.style.left = `${rect.left + window.scrollX}px`;
  picker.style.width = `${Math.max(rect.width, 260)}px`;
  document.body.appendChild(picker);

  const close = e => { if (!picker.contains(e.target) && e.target !== tokenInput) { picker.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}

// ── Export / Import Apps ──────────────────────────────────────────────────────
function wireExportImportButton() {
  const btn = document.getElementById('btn-export-import');
  if (!btn) return;
  btn.addEventListener('click', () => openExportImportModal());
}

function openExportImportModal() {
  let modal = document.getElementById('export-import-modal-global');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'export-import-modal-global';
    modal.className = 'dialog-backdrop';
    modal.innerHTML = `
      <style>
        .export-import-dialog { max-width: 500px; width: 90%; display: flex; flex-direction: column; padding: 0 !important; overflow: hidden; }
        .export-import-dialog .dialog-title { padding: 18px 20px 12px; margin-bottom: 0; border-bottom: 1px solid var(--border-muted); }
        .export-import-dialog .dialog-body { padding: 0; margin-bottom: 0; flex: 1; overflow: hidden; display: flex; flex-direction: column; }
        .export-import-dialog .dialog-actions { padding: 12px 20px 18px; border-top: 1px solid var(--border-muted); display: flex; gap: 8px; }
        .export-import-dialog .btn-full { width: 100%; justify-content: center; padding: 9px 12px; font-size: 13px; }
        
        .ei-tabs { display: flex; background: var(--bg-surface); }
        .ei-tab { flex: 1; padding: 10px; text-align: center; font-size: 12px; font-weight: 600; color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent; transition: all var(--transition); }
        .ei-tab:hover { color: var(--text-primary); background: var(--bg-muted); }
        .ei-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
        
        .ei-content { padding: 14px 20px; flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
        
        .custom-check { display: flex; align-items: center; gap: 12px; cursor: pointer; padding: 10px 14px; border-radius: 8px; transition: all var(--transition); user-select: none; border: 1px solid transparent; }
        .custom-check:hover { background: var(--bg-muted); border-color: var(--border); }
        .custom-check input { display: none; }
        .custom-check .box { width: 20px; height: 20px; border: 2px solid var(--border); border-radius: 6px; background: var(--bg-base); display: flex; align-items: center; justify-content: center; transition: all var(--transition); flex-shrink: 0; }
        .custom-check input:checked + .box { background: var(--accent-dark); border-color: var(--accent-dark); }
        .custom-check .box::after { content: ''; width: 5px; height: 10px; border: 2px solid #fff; border-top: 0; border-left: 0; transform: rotate(45deg) scale(0); transition: transform 0.15s cubic-bezier(0.175, 0.885, 0.32, 1.275); margin-top: -2px; }
        .custom-check input:checked + .box::after { transform: rotate(45deg) scale(1); }
        .custom-check .label-text { font-size: 13px; font-weight: 500; color: var(--text-primary); flex: 1; }
        .custom-check .label-sub { font-size: 11px; color: var(--text-muted); }
        
        #export-apps-list { display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow-y: auto; padding-right: 4px; }
        #export-apps-list::-webkit-scrollbar { width: 4px; }
        #export-apps-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
      </style>
      <div class="dialog dialog-modern export-import-dialog">
        <div class="dialog-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="margin-right:10px;color:var(--accent)"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Export / Import Apps
        </div>
        
        <div class="ei-tabs">
          <div class="ei-tab active" id="tab-export-apps">Export</div>
          <div class="ei-tab" id="tab-import-apps">Import</div>
        </div>

        <div class="dialog-body">
          <!-- EXPORT PANEL -->
          <div id="panel-export-apps" class="ei-content">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom: 4px;">
              <span style="font-size:12px; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px;">Select Apps</span>
              <label class="custom-check" style="padding: 4px 8px; border:none; background:none;">
                <input type="checkbox" id="export-select-all" checked>
                <div class="box" style="width:16px; height:16px; border-width:1.5px;"></div>
                <span style="font-size:12px; font-weight:600; color:var(--accent);">Select All</span>
              </label>
            </div>
            
            <div id="export-apps-list">
              <div style="color:var(--text-muted);font-size:12px;padding:20px;text-align:center;">Loading apps...</div>
            </div>
          </div>
          
          <!-- IMPORT PANEL -->
          <div id="panel-import-apps" class="ei-content" style="display:none">
            <div class="field">
              <label class="field-label">Exported JSON File</label>
              <div class="cert-upload-row">
                <span class="cert-filename" id="import-file-name" style="padding: 10px 14px;">No file selected</span>
                <button type="button" class="btn-scan" id="btn-import-upload" style="padding: 10px 14px;">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                  Browse
                </button>
                <input type="file" id="import-file-input" accept=".json" style="display:none">
              </div>
            </div>
            
            <div class="field">
              <label class="field-label">Target Node <span class="hint">(Optional override)</span></label>
              <select class="input" id="import-target-node" style="height: 42px;">
                <option value="">-- Keep Original Node --</option>
              </select>
            </div>
            
            <div id="import-msg" style="display:none;font-size:12px;padding:12px;border-radius:8px;line-height:1.5;"></div>
          </div>
        </div>

        <div class="dialog-actions">
          <button class="btn btn-primary btn-full" id="btn-do-action">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>Download JSON</span>
          </button>
          <button class="btn btn-secondary btn-full" id="export-import-close">Close</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    const closeBtn = modal.querySelector('#export-import-close');
    const actionBtn = modal.querySelector('#btn-do-action');
    const actionText = actionBtn.querySelector('span');
    const actionIcon = actionBtn.querySelector('svg');

    closeBtn.onclick = () => { modal.style.display = 'none'; };
    modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
    
    const tabExport = modal.querySelector('#tab-export-apps');
    const tabImport = modal.querySelector('#tab-import-apps');
    const panelExport = modal.querySelector('#panel-export-apps');
    const panelImport = modal.querySelector('#panel-import-apps');
    
    const switchToExport = () => {
      tabExport.classList.add('active');
      tabImport.classList.remove('active');
      panelExport.style.display = 'flex';
      panelImport.style.display = 'none';
      actionText.textContent = 'Download JSON';
      actionIcon.innerHTML = '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>';
      actionBtn.onclick = doExport;
      loadExportAppsList(modal);
    };
    
    const switchToImport = () => {
      tabImport.classList.add('active');
      tabExport.classList.remove('active');
      panelImport.style.display = 'flex';
      panelExport.style.display = 'none';
      actionText.textContent = 'Import Apps';
      actionIcon.innerHTML = '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>';
      actionBtn.onclick = doImport;
      loadImportNodesList(modal);
    };

    tabExport.onclick = switchToExport;
    tabImport.onclick = switchToImport;

    // Export Logic
    const selectAll = modal.querySelector('#export-select-all');
    selectAll.onchange = (e) => {
      const checks = modal.querySelectorAll('.export-app-check');
      checks.forEach(c => c.checked = e.target.checked);
    };

    async function doExport() {
      const checks = Array.from(modal.querySelectorAll('.export-app-check'));
      const selectedIds = checks.filter(c => c.checked).map(c => parseInt(c.value));
      
      if (checks.length > 0 && selectedIds.length === 0) {
        toast('Please select at least one app to export', 'error');
        return;
      }
      
      actionBtn.disabled = true;
      const oldHtml = actionBtn.innerHTML;
      actionBtn.innerHTML = '<div class="spinner"></div> Exporting...';
      
      try {
        const payload = selectAll.checked && selectedIds.length === checks.length ? null : selectedIds;
        const res = await api.exportApps(payload);
        
        const blob = new Blob([JSON.stringify(res.exported_apps, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `cloudbase_apps_${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        
        toast('Apps exported successfully');
        modal.style.display = 'none';
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        actionBtn.disabled = false;
        actionBtn.innerHTML = oldHtml;
      }
    }
    
    // Import Logic
    const fileInput = modal.querySelector('#import-file-input');
    const uploadBtn = modal.querySelector('#btn-import-upload');
    const fileName = modal.querySelector('#import-file-name');
    const importMsg = modal.querySelector('#import-msg');
    let importedAppsData = null;
    
    uploadBtn.onclick = () => fileInput.click();
    fileInput.onchange = (e) => {
      const file = e.target.files[0];
      if (!file) return;
      fileName.textContent = file.name;
      fileName.classList.add('has-value');
      
      const reader = new FileReader();
      reader.onload = (ev) => {
        try {
          importedAppsData = JSON.parse(ev.target.result);
          if (!Array.isArray(importedAppsData)) throw new Error('Invalid format: expected a JSON array of apps');
          importMsg.style.display = 'block';
          importMsg.style.background = 'var(--bg-elevated)';
          importMsg.style.color = 'var(--text-primary)';
          importMsg.style.border = '1px solid var(--border)';
          importMsg.innerHTML = `<div style="font-weight:600;margin-bottom:4px">Ready to import</div><div style="color:var(--text-muted)">Found ${importedAppsData.length} applications in file.</div>`;
        } catch (err) {
          importedAppsData = null;
          importMsg.style.display = 'block';
          importMsg.style.background = 'var(--red-bg)';
          importMsg.style.color = 'var(--red)';
          importMsg.style.border = '1px solid var(--red-border)';
          importMsg.textContent = `Error reading JSON: ${err.message}`;
        }
      };
      reader.readAsText(file);
    };

    async function doImport() {
      if (!importedAppsData) {
        toast('Please select a valid JSON file first', 'error');
        return;
      }
      
      const targetNodeId = modal.querySelector('#import-target-node').value;
      const nodeId = targetNodeId ? parseInt(targetNodeId) : null;
      
      actionBtn.disabled = true;
      const oldHtml = actionBtn.innerHTML;
      actionBtn.innerHTML = '<div class="spinner"></div> Importing...';
      
      importMsg.style.display = 'block';
      importMsg.style.background = 'var(--bg-elevated)';
      importMsg.style.color = 'var(--accent)';
      importMsg.style.border = '1px solid var(--accent)';
      importMsg.textContent = 'Importing apps... Please wait.';
      
      try {
        const res = await api.importApps(importedAppsData, nodeId);
        toast('Apps imported successfully');
        modal.style.display = 'none';
        if (typeof loadSidebarApps === 'function') loadSidebarApps();
      } catch (err) {
        importMsg.style.background = 'var(--red-bg)';
        importMsg.style.color = 'var(--red)';
        importMsg.style.border = '1px solid var(--red-border)';
        importMsg.textContent = `Import failed: ${err.message}`;
      } finally {
        actionBtn.disabled = false;
        actionBtn.innerHTML = oldHtml;
      }
    }

    actionBtn.onclick = doExport; // Initial
  }

  modal.style.display = 'flex';
  const tabExport = modal.querySelector('#tab-export-apps');
  tabExport.click(); // Always start on export
}

async function loadExportAppsList(modal) {
  const list = modal.querySelector('#export-apps-list');
  list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:40px;text-align:center;"><div class="spinner" style="margin:0 auto 10px"></div>Loading apps...</div>';
  try {
    const apps = await api.listApps();
    if (apps.length === 0) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:40px;text-align:center;">No applications found.</div>';
      return;
    }
    list.innerHTML = apps.map(app => `
      <label class="custom-check">
        <input type="checkbox" class="export-app-check" value="${app.id}" checked>
        <div class="box"></div>
        <div class="label-text">
          ${app.name}
          <div class="label-sub">${(app.replicas || []).length} instance${(app.replicas || []).length !== 1 ? 's' : ''}</div>
        </div>
      </label>
    `).join('');
    
    const checks = modal.querySelectorAll('.export-app-check');
    const selectAll = modal.querySelector('#export-select-all');
    selectAll.checked = true;
    checks.forEach(c => {
      c.addEventListener('change', () => {
        selectAll.checked = Array.from(checks).every(chk => chk.checked);
      });
    });
    
  } catch (err) {
    list.innerHTML = `<div style="color:var(--red);font-size:12px;padding:20px;text-align:center;">Error: ${err.message}</div>`;
  }
}

async function loadImportNodesList(modal) {
  const select = modal.querySelector('#import-target-node');
  select.innerHTML = '<option value="">-- Keep Original Node --</option>';
  try {
    const nodes = await api.listNodes();
    nodes.forEach(n => {
      const opt = document.createElement('option');
      opt.value = n.id;
      opt.textContent = n.name + (n.is_local ? ' (Primary)' : '');
      select.appendChild(opt);
    });
  } catch (err) {
    console.error('Failed to load nodes for import', err);
  }
}

// ── Role-based UI ─────────────────────────────────────────────────────────────
async function initRoleBasedUI() {
  try {
    const data = await api.checkAuth();
    document.body.dataset.role = data.role;
    // Store permissions on body for other scripts
    document.body.dataset.permissions = JSON.stringify(data.permissions || []);
    window.dispatchEvent(new CustomEvent('cloudbase-role-ready', { detail: { role: data.role, permissions: data.permissions || [] } }));

    // Hide [data-perm="X"] elements if user lacks that permission
    const perms = new Set(data.permissions || []);
    if (!data.is_superadmin) {
      document.querySelectorAll('[data-perm]').forEach(el => {
        if (!perms.has(el.dataset.perm)) el.style.display = 'none';
      });
      // Legacy support: hide [data-admin] for users without system.manage
      if (!perms.has('system.manage')) {
        document.querySelectorAll('[data-admin]').forEach(el => { el.style.display = 'none'; });
        const s = document.createElement('style');
        s.textContent = '[data-admin]{display:none!important}';
        document.head.appendChild(s);
      }
    }

    if (data.is_superadmin) {
      const btn = document.getElementById('btn-manage-users');
      if (btn) {
        btn.style.display = '';
        btn.addEventListener('click', () => openManageUsersModal());
      }
    }
  } catch {}
}

// ── Users & Roles management modal (superadmin only) ─────────────────────────
let _cachedRoles = null;

async function loadRoles(force = false) {
  if (!_cachedRoles || force) {
    _cachedRoles = await api.listRoles();
  }
  return _cachedRoles;
}

function buildRoleSelect(selectedRoleId) {
  // Builds role <option> elements; resolved asynchronously
  return `<option value="">Loading…</option>`;
}

function openManageUsersModal() {
  // The modal has two tabs: Users and Roles
  let modal = document.getElementById('manage-users-modal-global');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'manage-users-modal-global';
    modal.className = 'dialog-backdrop';
    modal.innerHTML = `
      <div class="dialog dialog-modern" style="max-width:600px;width:95%">
        <div class="dialog-title" style="display:flex;align-items:center;gap:0">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="margin-right:8px;vertical-align:-2px"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          <span style="flex:1">Users &amp; Roles</span>
          <div style="display:flex;gap:0;border:1px solid var(--border-muted);border-radius:6px;overflow:hidden;margin-left:auto">
            <button class="btn btn-secondary tab-btn active-tab" data-tab="users" style="border-radius:0;border:none;padding:4px 14px;font-size:12px">Users</button>
            <button class="btn btn-secondary tab-btn" data-tab="roles" style="border-radius:0;border:none;border-left:1px solid var(--border-muted);padding:4px 14px;font-size:12px">Roles</button>
          </div>
        </div>
        <div class="dialog-body" style="padding:0">

          <!-- ── Users tab ────────────────────────────────── -->
          <div id="tab-users" style="padding:16px">
            <div id="users-list" style="margin-bottom:16px"></div>
            <div style="border-top:1px solid var(--border-muted);padding-top:14px">
              <div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px">Add User</div>
              <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
                <input class="input" id="new-user-name" placeholder="Username" style="flex:1;min-width:140px" />
                <input class="input input-mono" id="new-user-pwd" type="password" placeholder="Password (min 8)" autocomplete="new-password" style="flex:1;min-width:140px" />
                <select class="input" id="new-user-role-id" style="flex:1;min-width:140px;height:42px">
                  <option value="">Loading roles…</option>
                </select>
              </div>
              <div id="users-err" style="display:none;color:var(--red);font-size:12px;margin-bottom:8px"></div>
              <button class="btn btn-primary" id="manage-users-add" style="width:100%">Add User</button>
            </div>
          </div>

          <!-- ── Roles tab ────────────────────────────────── -->
          <div id="tab-roles" style="display:none;padding:16px">
            <div id="roles-list" style="margin-bottom:16px"></div>
            <div style="border-top:1px solid var(--border-muted);padding-top:14px">
              <div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px">Create Role</div>
              <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
                <input class="input" id="new-role-name" placeholder="Role name" style="flex:1;min-width:140px" />
                <input class="input" id="new-role-desc" placeholder="Description (optional)" style="flex:2;min-width:200px" />
              </div>
              <div id="new-role-perms" style="margin-bottom:10px"></div>
              <div id="roles-err" style="display:none;color:var(--red);font-size:12px;margin-bottom:8px"></div>
              <button class="btn btn-primary" id="manage-roles-add" style="width:100%">Create Role</button>
            </div>
          </div>

        </div>
        <div class="dialog-actions">
          <button class="btn btn-secondary" id="manage-users-close">Close</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    modal.querySelector('#manage-users-close').onclick = () => { modal.style.display = 'none'; };
    modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });

    // Tab switching
    modal.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        modal.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active-tab'));
        btn.classList.add('active-tab');
        const tab = btn.dataset.tab;
        modal.querySelector('#tab-users').style.display = tab === 'users' ? 'block' : 'none';
        modal.querySelector('#tab-roles').style.display = tab === 'roles' ? 'block' : 'none';
        if (tab === 'roles') renderRolesList(modal);
      });
    });

    // Add user
    modal.querySelector('#manage-users-add').addEventListener('click', async () => {
      const username = modal.querySelector('#new-user-name').value.trim();
      const password = modal.querySelector('#new-user-pwd').value;
      const roleId   = parseInt(modal.querySelector('#new-user-role-id').value);
      const err      = modal.querySelector('#users-err');
      err.style.display = 'none';
      if (!username) { err.textContent = 'Username is required'; err.style.display = 'block'; return; }
      if (password.length < 8) { err.textContent = 'Password must be at least 8 characters'; err.style.display = 'block'; return; }
      if (!roleId) { err.textContent = 'Please select a role'; err.style.display = 'block'; return; }
      const btn = modal.querySelector('#manage-users-add');
      btn.disabled = true;
      try {
        await api.createUser({ username, password, role_id: roleId });
        modal.querySelector('#new-user-name').value = '';
        modal.querySelector('#new-user-pwd').value = '';
        await renderUsersList(modal);
        toast(`User "${username}" created`);
      } catch (e) {
        err.textContent = e.message;
        err.style.display = 'block';
      } finally {
        btn.disabled = false;
      }
    });

    // Create role
    modal.querySelector('#manage-roles-add').addEventListener('click', async () => {
      const name  = modal.querySelector('#new-role-name').value.trim();
      const desc  = modal.querySelector('#new-role-desc').value.trim();
      const permIds = [...modal.querySelectorAll('#new-role-perms input[type=checkbox]:checked')].map(c => parseInt(c.value));
      const err   = modal.querySelector('#roles-err');
      err.style.display = 'none';
      if (name.length < 2) { err.textContent = 'Role name must be at least 2 characters'; err.style.display = 'block'; return; }
      const btn = modal.querySelector('#manage-roles-add');
      btn.disabled = true;
      try {
        await api.createRole({ name, description: desc || null, permission_ids: permIds });
        _cachedRoles = null;
        modal.querySelector('#new-role-name').value = '';
        modal.querySelector('#new-role-desc').value = '';
        await renderRolesList(modal);
        await populateRoleSelect(modal);
        toast(`Role "${name}" created`);
      } catch (e) {
        err.textContent = e.message;
        err.style.display = 'block';
      } finally {
        btn.disabled = false;
      }
    });
  }

  modal.style.display = 'flex';
  renderUsersList(modal);
  populateRoleSelect(modal);
  loadPermissionsCheckboxes(modal.querySelector('#new-role-perms'));
}

async function populateRoleSelect(modal) {
  const sel = modal.querySelector('#new-user-role-id');
  if (!sel) return;
  try {
    const roles = await loadRoles(true);
    sel.innerHTML = roles.map(r => `<option value="${r.id}">${esc(r.name)}${r.description ? ' — ' + esc(r.description) : ''}</option>`).join('');
  } catch {
    sel.innerHTML = '<option value="">Failed to load roles</option>';
  }
}

async function loadPermissionsCheckboxes(container) {
  if (!container) return;
  try {
    const perms = await api.listPermissions();
    container.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Permissions</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
        ${perms.map(p => `
          <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;user-select:none">
            <input type="checkbox" value="${p.id}" style="accent-color:var(--accent)" />
            <span title="${esc(p.description || '')}">${esc(p.name)}</span>
          </label>`).join('')}
      </div>`;
  } catch {
    container.innerHTML = '<div style="color:var(--text-muted);font-size:12px">Failed to load permissions</div>';
  }
}

async function renderUsersList(modal) {
  const list = modal.querySelector('#users-list');
  list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">Loading…</div>';
  try {
    const [users, roles] = await Promise.all([api.listUsers(), loadRoles()]);
    const roleMap = Object.fromEntries(roles.map(r => [r.id, r]));
    if (!users.length) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">No users found.</div>';
      return;
    }
    list.innerHTML = users.map(u => {
      const isSuperadmin = u.username === 'admin';
      const roleName = isSuperadmin ? 'superadmin' : (u.role || 'unknown');
      const badgeColor = isSuperadmin ? 'rgba(210,153,34,.15)' : 'var(--accent-dark)';
      const badgeText  = isSuperadmin ? '#e3b341' : 'var(--accent)';
      const badgeHtml  = `<span class="user-role-badge" style="padding:2px 8px;border-radius:4px;font-size:11px;background:${badgeColor};color:${badgeText}">${esc(roleName)}</span>`;

      const roleOptions = isSuperadmin ? '' : roles.map(r =>
        `<option value="${r.id}" ${r.id === u.role_id ? 'selected' : ''}>${esc(r.name)}</option>`
      ).join('');

      const editFormHtml = isSuperadmin ? '' : `
        <div class="user-edit-form" style="display:none;background:var(--bg-elevated);border-radius:6px;padding:10px;flex-direction:column;gap:8px">
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <input class="input user-edit-username" placeholder="Username" style="flex:1;min-width:140px" value="${esc(u.username)}" />
            <input class="input input-mono user-edit-pwd" type="password" placeholder="New password (leave blank to keep)" autocomplete="new-password" style="flex:1;min-width:140px" />
            <select class="input user-edit-role-id" style="flex:1;min-width:140px;height:38px">${roleOptions}</select>
          </div>
          <div class="user-edit-err" style="display:none;color:var(--red);font-size:12px"></div>
          <div style="display:flex;gap:6px;justify-content:flex-end">
            <button class="btn btn-secondary btn-sm user-edit-cancel" style="font-size:11px">Cancel</button>
            <button class="btn btn-primary btn-sm user-edit-save" style="font-size:11px">Save</button>
          </div>
        </div>`;

      return `
        <div class="gh-token-row" data-id="${u.id}" data-username="${esc(u.username)}" data-role-id="${u.role_id || ''}" data-superadmin="${isSuperadmin}" style="flex-direction:column;align-items:stretch;gap:6px">
          <div style="display:flex;align-items:center;gap:8px">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="color:var(--text-muted);flex-shrink:0"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
            <span class="gh-token-row-label">${esc(u.username)}</span>
            ${badgeHtml}
            <div style="margin-left:auto;display:flex;gap:6px">
              ${isSuperadmin ? '' : `<button class="btn btn-secondary btn-sm user-edit-btn" style="padding:3px 8px;font-size:11px">Edit</button>`}
              ${isSuperadmin ? '' : `<button class="btn btn-danger btn-sm user-delete-btn" style="padding:3px 8px;font-size:11px">Delete</button>`}
            </div>
          </div>
          ${editFormHtml}
        </div>`;
    }).join('');

    list.querySelectorAll('[data-id]').forEach(row => {
      const id          = parseInt(row.dataset.id);
      const username    = row.dataset.username;
      const isSuperadmin = row.dataset.superadmin === 'true';
      const form        = row.querySelector('.user-edit-form');

      row.querySelector('.user-edit-btn')?.addEventListener('click', () => {
        const isOpen = form.style.display === 'flex';
        form.style.display = isOpen ? 'none' : 'flex';
        if (!isOpen) row.querySelector('.user-edit-err').style.display = 'none';
      });

      row.querySelector('.user-edit-cancel')?.addEventListener('click', () => { form.style.display = 'none'; });

      row.querySelector('.user-edit-save')?.addEventListener('click', async () => {
        const newUsername = row.querySelector('.user-edit-username').value.trim();
        const pwd        = row.querySelector('.user-edit-pwd').value;
        const roleId     = parseInt(row.querySelector('.user-edit-role-id').value);
        const errEl      = row.querySelector('.user-edit-err');
        const saveBtn    = row.querySelector('.user-edit-save');
        errEl.style.display = 'none';
        if (newUsername.length < 2) { errEl.textContent = 'Username must be at least 2 characters'; errEl.style.display = 'block'; return; }
        if (pwd && pwd.length < 8) { errEl.textContent = 'Password must be at least 8 characters'; errEl.style.display = 'block'; return; }
        const payload = { role_id: roleId };
        if (newUsername !== username) payload.username = newUsername;
        if (pwd) payload.password = pwd;
        saveBtn.disabled = true;
        try {
          const updated = await api.updateUser(id, payload);
          row.dataset.username = updated.username;
          row.dataset.roleId   = updated.role_id;
          row.querySelector('.gh-token-row-label').textContent = updated.username;
          row.querySelector('.user-role-badge').textContent = updated.role;
          form.style.display = 'none';
          toast(`User "${updated.username}" updated`);
        } catch (e) {
          errEl.textContent = e.message;
          errEl.style.display = 'block';
        } finally {
          saveBtn.disabled = false;
        }
      });

      row.querySelector('.user-delete-btn')?.addEventListener('click', async () => {
        if (!await confirm(`Delete user "${username}"?`, 'This cannot be undone.')) return;
        try {
          await api.deleteUser(id);
          await renderUsersList(modal);
          toast(`User "${username}" deleted`);
        } catch (e) {
          toast(e.message, 'error');
        }
      });
    });
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);font-size:12px">${e.message}</div>`;
  }
}

async function renderRolesList(modal) {
  const list = modal.querySelector('#roles-list');
  list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">Loading…</div>';
  try {
    const [roles, allPerms] = await Promise.all([loadRoles(true), api.listPermissions()]);
    const permMap = Object.fromEntries(allPerms.map(p => [p.id, p]));
    const builtIn = new Set(['admin', 'viewer']);

    list.innerHTML = roles.map(r => {
      const isBuiltIn = builtIn.has(r.name);
      const permTags = r.permissions.map(p =>
        `<span style="padding:1px 7px;border-radius:3px;font-size:10px;background:var(--bg-muted);color:var(--text-secondary)">${esc(p.name)}</span>`
      ).join(' ');

      const editFormHtml = isBuiltIn && r.name === 'admin' ? '' : `
        <div class="role-edit-form" style="display:none;background:var(--bg-elevated);border-radius:6px;padding:10px;flex-direction:column;gap:8px">
          ${!isBuiltIn ? `<div style="display:flex;gap:8px;flex-wrap:wrap">
            <input class="input role-edit-name" placeholder="Role name" style="flex:1;min-width:140px" value="${esc(r.name)}" />
            <input class="input role-edit-desc" placeholder="Description" style="flex:2;min-width:200px" value="${esc(r.description || '')}" />
          </div>` : ''}
          <div class="role-edit-perms" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            ${allPerms.map(p => `
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;user-select:none">
                <input type="checkbox" value="${p.id}" ${r.permissions.find(rp => rp.id === p.id) ? 'checked' : ''} style="accent-color:var(--accent)" />
                <span title="${esc(p.description || '')}">${esc(p.name)}</span>
              </label>`).join('')}
          </div>
          <div class="role-edit-err" style="display:none;color:var(--red);font-size:12px"></div>
          <div style="display:flex;gap:6px;justify-content:flex-end">
            <button class="btn btn-secondary btn-sm role-edit-cancel" style="font-size:11px">Cancel</button>
            <button class="btn btn-primary btn-sm role-edit-save" style="font-size:11px">Save</button>
          </div>
        </div>`;

      return `
        <div class="gh-token-row" data-role-id="${r.id}" data-role-name="${esc(r.name)}" data-builtin="${isBuiltIn}" style="flex-direction:column;align-items:stretch;gap:6px">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="color:var(--text-muted);flex-shrink:0"><circle cx="12" cy="8" r="6"/><path d="M15.477 12.89L17 22l-5-3-5 3 1.523-9.11"/></svg>
            <span class="gh-token-row-label">${esc(r.name)}</span>
            ${isBuiltIn ? `<span style="padding:1px 7px;border-radius:3px;font-size:10px;background:rgba(210,153,34,.15);color:#e3b341">built-in</span>` : ''}
            ${r.description ? `<span style="font-size:12px;color:var(--text-muted)">${esc(r.description)}</span>` : ''}
            <div style="margin-left:auto;display:flex;gap:6px">
              ${r.name !== 'admin' ? `<button class="btn btn-secondary btn-sm role-edit-btn" style="padding:3px 8px;font-size:11px">Edit</button>` : ''}
              ${!isBuiltIn ? `<button class="btn btn-danger btn-sm role-delete-btn" style="padding:3px 8px;font-size:11px">Delete</button>` : ''}
            </div>
          </div>
          ${r.permissions.length ? `<div style="display:flex;flex-wrap:wrap;gap:4px;padding-left:20px">${permTags}</div>` : ''}
          ${editFormHtml}
        </div>`;
    }).join('');

    list.querySelectorAll('[data-role-id]').forEach(row => {
      const roleId   = parseInt(row.dataset.roleId);
      const roleName = row.dataset.roleName;
      const isBuiltIn = row.dataset.builtin === 'true';
      const form     = row.querySelector('.role-edit-form');

      row.querySelector('.role-edit-btn')?.addEventListener('click', () => {
        const isOpen = form.style.display === 'flex';
        form.style.display = isOpen ? 'none' : 'flex';
        if (!isOpen) row.querySelector('.role-edit-err').style.display = 'none';
      });

      row.querySelector('.role-edit-cancel')?.addEventListener('click', () => { form.style.display = 'none'; });

      row.querySelector('.role-edit-save')?.addEventListener('click', async () => {
        const nameEl = row.querySelector('.role-edit-name');
        const descEl = row.querySelector('.role-edit-desc');
        const permIds = [...row.querySelectorAll('.role-edit-perms input:checked')].map(c => parseInt(c.value));
        const errEl  = row.querySelector('.role-edit-err');
        const saveBtn = row.querySelector('.role-edit-save');
        errEl.style.display = 'none';

        const payload = { permission_ids: permIds };
        if (nameEl) {
          const newName = nameEl.value.trim();
          if (newName.length < 2) { errEl.textContent = 'Name must be at least 2 characters'; errEl.style.display = 'block'; return; }
          payload.name = newName;
        }
        if (descEl) payload.description = descEl.value.trim() || null;

        saveBtn.disabled = true;
        try {
          await api.updateRole(roleId, payload);
          _cachedRoles = null;
          await renderRolesList(modal);
          await populateRoleSelect(modal);
          toast(`Role updated`);
        } catch (e) {
          errEl.textContent = e.message;
          errEl.style.display = 'block';
          saveBtn.disabled = false;
        }
      });

      row.querySelector('.role-delete-btn')?.addEventListener('click', async () => {
        if (!await confirm(`Delete role "${roleName}"?`, 'Users assigned to this role will lose it.')) return;
        try {
          await api.deleteRole(roleId);
          _cachedRoles = null;
          await renderRolesList(modal);
          await populateRoleSelect(modal);
          toast(`Role "${roleName}" deleted`);
        } catch (e) {
          toast(e.message, 'error');
        }
      });
    });
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);font-size:12px">${e.message}</div>`;
  }
}

// ── System Settings (ports, limits from config.yaml) ─────────────────────────
function wireSystemSettingsButton() {
  const btn = document.getElementById('btn-system-settings');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    let modal = document.getElementById('system-settings-modal-global');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'system-settings-modal-global';
      modal.className = 'dialog-backdrop';
      modal.innerHTML = `
        <div class="dialog dialog-modern" style="max-width:500px;width:90%">
          <div class="dialog-title">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="margin-right:8px;vertical-align:-2px"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            System Settings
          </div>
          <div class="dialog-body" style="display:flex;flex-direction:column;gap:14px">
            <p style="margin:0;font-size:13px;color:var(--text-secondary)">
              These settings update <code style="background:var(--bg-muted);padding:1px 4px;border-radius:3px">~/.cloudbase/config.yaml</code> on disk.
              A restart of Cloudbase is required for port range changes to take effect.
            </p>

            <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)">Authentication</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
              <div class="field" style="margin:0">
                <label class="field-label">Token Expiry (seconds)</label>
                <input class="input" id="sys-token-expire" type="number" min="60" max="2592000" />
              </div>
            </div>

            <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)">Port Ranges</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
              <div class="field" style="margin:0">
                <label class="field-label">Instance Port Min</label>
                <input class="input" id="sys-inst-min" type="number" min="1024" max="65000" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Instance Port Max</label>
                <input class="input" id="sys-inst-max" type="number" min="1024" max="65000" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Tunnel Port Min</label>
                <input class="input" id="sys-tun-min" type="number" min="1024" max="65000" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Tunnel Port Max</label>
                <input class="input" id="sys-tun-max" type="number" min="1024" max="65000" />
              </div>
            </div>

            <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)">Limits</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
              <div class="field" style="margin:0">
                <label class="field-label">Max Apps</label>
                <input class="input" id="sys-max-apps" type="number" min="1" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Max Instances</label>
                <input class="input" id="sys-max-inst" type="number" min="1" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Max Nodes</label>
                <input class="input" id="sys-max-nodes" type="number" min="1" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Max Restarts / Window</label>
                <input class="input" id="sys-max-restarts" type="number" min="1" />
              </div>
              <div class="field" style="margin:0">
                <label class="field-label">Restart Window (sec)</label>
                <input class="input" id="sys-restart-window" type="number" min="10" />
              </div>
            </div>

            <div id="sys-settings-msg" style="display:none;padding:8px 10px;border-radius:6px;font-size:12px"></div>
          </div>
          <div class="dialog-actions">
            <button class="btn btn-secondary" id="sys-settings-cancel">Cancel</button>
            <button class="btn btn-primary" id="sys-settings-save">Save</button>
          </div>
        </div>`;
      document.body.appendChild(modal);

      modal.querySelector('#sys-settings-cancel').onclick = () => { modal.style.display = 'none'; };
      modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });

      modal.querySelector('#sys-settings-save').addEventListener('click', async () => {
        const saveBtn = modal.querySelector('#sys-settings-save');
        const msg     = modal.querySelector('#sys-settings-msg');
        msg.style.display = 'none';
        saveBtn.disabled  = true;
        saveBtn.textContent = 'Saving…';
        try {
          const v = id => parseInt(modal.querySelector(id).value, 10);
          await api.saveSystemSettings({
            auth: {
              token_expire_seconds: v('#sys-token-expire'),
            },
            ports: {
              instance_min: v('#sys-inst-min'),
              instance_max: v('#sys-inst-max'),
              tunnel_min:   v('#sys-tun-min'),
              tunnel_max:   v('#sys-tun-max'),
            },
            limits: {
              max_apps:                v('#sys-max-apps'),
              max_instances:           v('#sys-max-inst'),
              max_nodes:               v('#sys-max-nodes'),
              max_restarts_per_window: v('#sys-max-restarts'),
              restart_window_seconds:  v('#sys-restart-window'),
            },
          });
          showMsg(msg, 'Saved. Restart Cloudbase for port range changes to take effect.', true);
          setTimeout(() => { modal.style.display = 'none'; }, 2200);
        } catch (e) {
          showMsg(msg, e.message || 'Failed to save', false);
        } finally {
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
        }
      });
    }

    // Pre-fill current values each time modal opens
    const msg = modal.querySelector('#sys-settings-msg');
    msg.style.display = 'none';
    modal.style.display = 'flex';
    try {
      const data = await api.getSystemSettings();
      modal.querySelector('#sys-token-expire').value = data.auth?.token_expire_seconds ?? '';
      modal.querySelector('#sys-inst-min').value    = data.ports?.instance_min ?? '';
      modal.querySelector('#sys-inst-max').value    = data.ports?.instance_max ?? '';
      modal.querySelector('#sys-tun-min').value     = data.ports?.tunnel_min   ?? '';
      modal.querySelector('#sys-tun-max').value     = data.ports?.tunnel_max   ?? '';
      modal.querySelector('#sys-max-apps').value    = data.limits?.max_apps    ?? '';
      modal.querySelector('#sys-max-inst').value    = data.limits?.max_instances ?? '';
      modal.querySelector('#sys-max-nodes').value   = data.limits?.max_nodes   ?? '';
      modal.querySelector('#sys-max-restarts').value = data.limits?.max_restarts_per_window ?? '';
      modal.querySelector('#sys-restart-window').value = data.limits?.restart_window_seconds ?? '';
    } catch {}
  });
}