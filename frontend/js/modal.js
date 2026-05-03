import { api } from './api.js';
import { icon, spinner, toast } from './utils.js';
import { pickGitHubToken } from './sidebar.js';

// ── Cert picker helper ────────────────────────────────────────────────────────
let _certCache = null;

async function loadCerts() {
  if (_certCache) return _certCache;
  try {
    _certCache = await api.discoverCerts();
  } catch {
    _certCache = { certs: [], keys: [] };
  }
  return _certCache;
}

function showPicker(inputEl, items, label) {
  // Remove any existing picker
  document.querySelectorAll('.cert-picker').forEach(p => p.remove());

  if (!items.length) {
    toast(`No ${label} found on this machine`, 'warn');
    return;
  }

  const picker = document.createElement('div');
  picker.className = 'cert-picker';
  picker.style.cssText = `
    position:absolute; z-index:9999; background:#161b22; border:1px solid #30363d;
    border-radius:6px; max-height:200px; overflow-y:auto; min-width:320px;
    box-shadow:0 8px 24px rgba(0,0,0,.5); font-size:12px;`;

  items.forEach(path => {
    const row = document.createElement('div');
    row.className = 'cert-picker-row';
    row.textContent = path;
    row.style.cssText = 'padding:8px 12px; cursor:pointer; color:#e6edf3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
    row.addEventListener('mouseenter', () => row.style.background = '#21262d');
    row.addEventListener('mouseleave', () => row.style.background = '');
    row.addEventListener('click', () => {
      inputEl.value = path;
      picker.remove();
      inputEl.dispatchEvent(new Event('input'));
    });
    picker.appendChild(row);
  });

  const rect = inputEl.getBoundingClientRect();
  picker.style.top  = `${rect.bottom + window.scrollY + 4}px`;
  picker.style.left = `${rect.left  + window.scrollX}px`;
  picker.style.width = `${rect.width}px`;
  document.body.appendChild(picker);

  const close = e => { if (!picker.contains(e.target) && e.target !== inputEl) { picker.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}

export function openDeployModal(onSuccess) {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = modalHTML();
  document.body.appendChild(backdrop);

  const modal = backdrop.querySelector('.modal');
  const form  = modal.querySelector('#deploy-form');
  let envCount = 0;



  // Close
  const close = () => backdrop.remove();
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  modal.querySelector('#modal-close').addEventListener('click', close);
  modal.querySelector('#modal-cancel').addEventListener('click', close);

  // Add env var row
  modal.querySelector('#add-env').addEventListener('click', () => addEnvRow(modal, ++envCount));

  // Pick saved GitHub token
  modal.querySelector('#f-token-pick').addEventListener('click', () => {
    pickGitHubToken(modal.querySelector('#f-token'), modal.querySelector('#f-token-id'));
  });

  // Submit
  form.addEventListener('submit', async e => {
    e.preventDefault();
    await handleDeploy(modal, form, onSuccess, close);
  });
}

function modalHTML() {
  return `
    <div class="modal deploy-modal">
      <div class="modal-header">
        <div>
          <div class="modal-title">Deploy Application</div>
          <div class="modal-sub">Configure and deploy a new application from GitHub</div>
        </div>
        <button class="modal-close" id="modal-close">${icon.x}</button>
      </div>

      <form id="deploy-form" novalidate>
        <div class="modal-body">

          <!-- Basic -->
          <div class="modal-section modal-section--compact">
            <div class="section-title">${icon.terminal} Basic Configuration</div>
            <div class="deploy-grid deploy-grid--basic">
              <div class="field deploy-field-span-2">
                <label class="field-label">Application Name <span class="req">*</span></label>
                <input class="input" id="f-name" placeholder="my-app" required autocomplete="off" />
              </div>

              <div class="field deploy-field-span-2">
                <label class="field-label">GitHub Repository URL <span class="req">*</span></label>
                <div class="input-icon-wrap">
                  <span class="icon">${icon.github}</span>
                  <input class="input" id="f-repo" placeholder="https://github.com/user/repo" required />
                </div>
              </div>

              <div class="field deploy-field-span-2 deploy-token-field">
                <label class="field-label">GitHub Token <span class="hint">(optional, for private repos)</span></label>
                <div class="deploy-token-row">
                  <div class="input-icon-wrap deploy-input-grow">
                    <span class="icon">${icon.lock}</span>
                    <input class="input input-mono" id="f-token" type="password" placeholder="ghp_..." />
                  </div>
                  <button type="button" class="btn btn-secondary btn-sm deploy-token-btn" id="f-token-pick">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
                    Saved
                  </button>
                </div>
                <input type="hidden" id="f-token-id" />
              </div>
            </div>
          </div>

          <!-- Process -->
          <div class="modal-section modal-section--compact">
            <div class="section-title">${icon.settings} Process</div>
            <div class="deploy-grid">
              <div class="field">
                <label class="field-label">Start Command <span class="hint">(auto-detected if empty)</span></label>
                <input class="input input-mono" id="f-cmd" placeholder="npm start" />
              </div>
              <div class="field">
                <label class="field-label">Internal Port</label>
                <input class="input" id="f-port" type="number" placeholder="3000" />
                <div class="field-hint">For Docker apps this is the port inside the container.</div>
              </div>
            </div>
          </div>

          <div class="modal-section modal-section--compact">
            <div class="section-title">${icon.server} Docker Runtime</div>
            <div class="deploy-grid">
              <div class="field">
                <label class="field-label">CPU Limit</label>
                <input class="input" id="f-docker-cpu" type="number" min="0.1" step="0.1" placeholder="1.0" />
              </div>
              <div class="field">
                <label class="field-label">Memory Limit (MB)</label>
                <input class="input" id="f-docker-memory" type="number" min="1" step="1" placeholder="512" />
              </div>
              <div class="field deploy-field-span-2 deploy-toggle-stack" style="margin-bottom:0">
                <div class="deploy-toggle-item">
                  <div class="toggle-row">
                    <span class="field-label" style="margin-bottom:0">Read-only root filesystem</span>
                    <label class="toggle" for="f-docker-readonly">
                      <input type="checkbox" id="f-docker-readonly" />
                      <span class="toggle-slider"></span>
                    </label>
                  </div>
                </div>
                <div class="deploy-toggle-item">
                  <div class="toggle-row">
                    <span class="field-label" style="margin-bottom:0">Tmpfs at /tmp</span>
                    <label class="toggle" for="f-docker-tmpfs-enabled">
                      <input type="checkbox" id="f-docker-tmpfs-enabled" />
                      <span class="toggle-slider"></span>
                    </label>
                  </div>
                  <div class="deploy-inline-hint-row">
                    <input class="input deploy-tmpfs-input" id="f-docker-tmpfs-size" type="number" min="1" step="1" placeholder="64" />
                    <span class="deploy-inline-hint">MB for /tmp tmpfs</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Env vars -->
          <div class="modal-section modal-section--compact modal-section--full">
            <div class="section-title">${icon.lock} Environment Variables</div>
            <div id="env-rows"></div>
            <button type="button" class="add-env-btn" id="add-env">${icon.plus} Add variable</button>
          </div>

          <div id="modal-error" class="modal-error" style="display:none"></div>
        </div>

        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" id="modal-cancel">Cancel</button>
          <button type="submit" class="btn btn-primary" id="modal-submit">${icon.play} Deploy Application</button>
        </div>
      </form>
    </div>`;
}

function addEnvRow(modal, idx) {
  const row = document.createElement('div');
  row.className = 'env-row';
  row.id = `env-row-${idx}`;
  row.innerHTML = `
    <input class="input input-mono" placeholder="KEY" data-env-key />
    <input class="input input-mono" placeholder="value" data-env-val />
    <button type="button" class="btn-remove" onclick="this.closest('.env-row').remove()">${icon.trash}</button>`;
  modal.querySelector('#env-rows').appendChild(row);
}

async function handleDeploy(modal, form, onSuccess, close) {
  const errEl  = modal.querySelector('#modal-error');
  const submit = modal.querySelector('#modal-submit');

  errEl.style.display = 'none';
  submit.disabled = true;
  submit.innerHTML = `${spinner} Deploying…`;

  const env_vars = {};
  modal.querySelectorAll('.env-row').forEach(row => {
    const k = row.querySelector('[data-env-key]').value.trim();
    const v = row.querySelector('[data-env-val]').value;
    if (k) env_vars[k] = v;
  });

  const tokenId = modal.querySelector('#f-token-id').value.trim();
  const dockerCpu = parseFloat(modal.querySelector('#f-docker-cpu').value);
  const dockerMemory = parseInt(modal.querySelector('#f-docker-memory').value, 10);
  const dockerTmpfsSize = parseInt(modal.querySelector('#f-docker-tmpfs-size').value, 10);
  const payload = {
    name:            modal.querySelector('#f-name').value.trim(),
    repo_url:        modal.querySelector('#f-repo').value.trim(),
    ...(tokenId
      ? { github_token_id: tokenId }
      : { github_token: modal.querySelector('#f-token').value.trim() || null }),
    start_command: modal.querySelector('#f-cmd').value.trim() || null,
    port:          parseInt(modal.querySelector('#f-port').value) || null,
    docker_cpu_limit: Number.isFinite(dockerCpu) ? dockerCpu : null,
    docker_memory_limit_mb: Number.isInteger(dockerMemory) ? dockerMemory : null,
    docker_read_only_root: modal.querySelector('#f-docker-readonly').checked,
    docker_tmpfs_enabled: modal.querySelector('#f-docker-tmpfs-enabled').checked,
    docker_tmpfs_size_mb: Number.isInteger(dockerTmpfsSize) ? dockerTmpfsSize : null,
    env_vars,
  };

  if (!payload.name || !payload.repo_url) {
    errEl.textContent = 'Name and repository URL are required.';
    errEl.style.display = 'block';
    submit.disabled = false;
    submit.innerHTML = `${icon.play} Deploy Application`;
    return;
  }

  try {
    const app = await api.deploy(payload);
    close();
    onSuccess(app);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
    submit.disabled = false;
    submit.innerHTML = `${icon.play} Deploy Application`;
  }
}

