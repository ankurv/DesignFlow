// ── Agent config ──────────────────────────────────────────────────────────────
const KINDS = ['claude','openai','groq','gemini','cli','ollama'];
let projectAgentConfigs = [];
let editingAgentId = null;
let editingAgentData = {};

async function loadAgentConfig() {
  const res = await fetch('/agents').then(r=>r.json());
  projectAgentConfigs = res.agents || [];
  renderAgentCards();
}

function renderAgentCards() {
  const container = document.getElementById('agentCardsContainer');
  if (!container) return;
  const expandedCapacityIds = [...container.querySelectorAll('.agent-capacity-details:not([hidden])')].map(el => el.id);



  let html = '';
  if (!projectOpen) {
    html = '<div class="panel-empty-message"><h3>Open a project to configure agents</h3><p>Each project has its own team, models, credentials, and specialties.</p><button class="btn btn-primary" onclick="openProject()">Open project</button></div>';
  } else if (!projectAgentConfigs.length) {
    html = '<div class="panel-empty-message"><h3>No agents configured</h3><p>Use “Add Agent” above to create the first agent for this project.</p></div>';
  } else {
    html = projectAgentConfigs.map((cfg, idx) => renderSingleCard(cfg, idx)).join('');
  }

  container.innerHTML = html;
  expandedCapacityIds.forEach(id => {
    const details = document.getElementById(id);
    if (details) details.hidden = false;
  });
  renderAgentEditor();
}

function renderSingleCard(cfg, idx) {
  const uid = cfg.id;
  const isSelected = editingAgentId === uid;
    const isCoordinator = cfg.extra?.is_coordinator;
    const initial = (cfg.name || '?').charAt(0).toUpperCase();
    let kindClass = 'color-default';
    if (['claude','openai','groq','gemini','cli','ollama'].includes(cfg.kind)) {
      kindClass = `color-${cfg.kind}`;
    }
    
    let actionButtons = '';
    actionButtons += `<button class="agent-health-refresh" onclick="refreshAgentHealth('${uid}', ${idx})" title="Check health" aria-label="Check ${escAttr(cfg.name || 'agent')} health">↻</button>`;
    const isPaused = cfg.is_paused;
    const pauseBadge = isPaused ? '<span class="agent-scope-badge danger" style="background:var(--red);color:white;border-color:var(--red);">Paused</span>' : '';

    actionButtons += `
      <button class="btn btn-secondary" onclick="startEditAgent('${uid}', ${idx})" style="padding:4px 10px;font-size:11px">Edit</button>
      <button class="btn ${isPaused ? 'btn-primary' : 'btn-secondary'}" onclick="togglePauseAgent('${cfg.id}', ${idx})" style="padding:4px 10px;font-size:11px">${isPaused ? 'Resume' : 'Pause'}</button>
      <button class="btn btn-danger" onclick="deleteAgent('${cfg.id}')" style="padding:4px 10px;font-size:11px">Delete</button>
    `;

    const badgeHTML = pauseBadge;

    if (isPaused) {
      delete agentHealthStatus[uid];
      delete agentCapacityStatus[uid];
    }
    const statusInfo = agentHealthStatus[uid] || { status: isPaused ? 'paused' : 'testing', error: '' };
    if (!agentHealthStatus[uid] && !isPaused) {
      agentHealthStatus[uid] = { status: 'testing', error: '' };
      setTimeout(() => checkAgentHealth(cfg, uid), 50);
    }
    const hoverTitle = statusInfo.status === 'failed' 
      ? friendlyProviderError(statusInfo.error)
      : (statusInfo.status === 'success' ? 'Agent working correctly' : 'Testing connection...');
    const capacity = agentCapacityStatus[uid] || {};
    const isLocal = ['cli', 'ollama'].includes(cfg.kind);
    const runtimeBlocked = capacity.runtime_status === 'error';
    const quotaBlocked = capacity.error_code === 'quota_exhausted' || /quota|credit|billing|usage limit|resource exhausted/i.test(capacity.error || '');
    const capacityLabel = isPaused ? 'Paused' : isLocal ? 'Local' : quotaBlocked ? 'Quota exhausted' : runtimeBlocked || statusInfo.status === 'failed' ? 'Blocked' : capacity.retry_at ? 'Limited' : statusInfo.status === 'success' ? 'Healthy' : 'Checking';
    const capacityClass = isPaused ? 'paused' : quotaBlocked || runtimeBlocked ? 'blocked' : capacityLabel.toLowerCase();
    const costText = capacity.pricing_known === false ? 'Cost unavailable for this model' : `DesignFlow usage: $${Number(capacity.cost_usd || 0).toFixed(4)}`;
    const checkedText = statusInfo.checked_at ? new Date(statusInfo.checked_at).toLocaleTimeString() : 'Not checked yet';

    return `
      <div class="agent-card ${isSelected ? 'selected' : ''}" id="card-${uid}">
        <div class="agent-card-row">
          <div class="agent-card-avatar ${kindClass}">
            ${initial}
          </div>
          <div class="agent-card-details">
            <div class="agent-card-title-row">
              <span class="agent-card-title">${escHtml(cfg.name || '(New Agent)')}</span>
              <span class="health-dot ${statusInfo.status}" title="${escAttr(hoverTitle)}" style="width:7px;height:7px;border-radius:50%;display:inline-block;vertical-align:middle;margin-left:4px"></span>
              ${badgeHTML}
              ${isCoordinator ? '<span style="font-size:11px;color:var(--yellow);font-weight:600">👑 Coordinator</span>' : ''}
              <button type="button" class="agent-capacity-pill ${capacityClass}" style="margin-top:0; margin-left:6px;" onclick="toggleAgentCapacity('${uid}')">Capacity: ${capacityLabel}</button>
            </div>
            <div class="agent-card-subtitle" style="display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:2px;">
              <span><strong>Kind:</strong> ${cfg.kind}</span>
            </div>

            <div class="agent-capacity-details" id="capacity-${uid}" hidden>
              <div><strong>${isLocal ? 'Local runtime' : 'Provider capacity'}</strong><span>${isPaused ? 'Disabled and excluded from new agent assignments' : isLocal ? 'No cloud credits required' : runtimeBlocked ? escHtml(friendlyProviderError(capacity.error)) : statusInfo.status === 'failed' ? escHtml(friendlyProviderError(statusInfo.error)) : 'Inference endpoint is reachable'}</span></div>
              <div><strong>Local usage</strong><span>${Number(capacity.total_tokens || 0).toLocaleString()} tokens · ${costText}</span></div>
              ${capacity.retry_at ? `<div><strong>Retry</strong><span>${new Date(capacity.retry_at).toLocaleString()}</span></div>` : ''}
              <div><strong>Provider balance</strong><span>${isLocal ? 'Not applicable' : 'Not exposed by this provider credential'}</span></div>
              <div><strong>Last checked</strong><span>${checkedText}</span></div>
            </div>
          </div>
          <div class="agent-card-actions">
            ${actionButtons}
          </div>
        </div>
      </div>
    `;
}

window.toggleAgentCapacity = function(uid) {
  const details = document.getElementById(`capacity-${uid}`);
  if (details) details.hidden = !details.hidden;
};

function renderAgentEditor() {
  const panel = document.getElementById('agentEditorPanel');
  if (!panel) return;
  const previousScrollTop = panel.querySelector('.agent-editor-form')?.scrollTop || 0;
  const layout = panel.closest('.agent-config-layout');
  if (!editingAgentId) {
    panel.innerHTML = '';
    panel.hidden = true;
    if (layout) layout.classList.remove('editor-open');
    return;
  }
  panel.hidden = false;
  if (layout) layout.classList.add('editor-open');

  const data = editingAgentData;
  const agentId = editingAgentId;
  const isNew = agentId.startsWith('new-');
  const isCli = data.kind === 'cli';
  const isOllama = data.kind === 'ollama';
  const models = data.extra?.available_models || [];
  const providerLabel = data.extra?.detected_provider || (data.kind ? data.kind.charAt(0).toUpperCase() + data.kind.slice(1) : 'Provider');
  const connectionVerified = data.extra?.connection_verified === true;
  const connectionError = data.extra?.connection_error || '';

  panel.innerHTML = `

    <div class="agent-editor-form" data-form-type="other">
      <div class="form-group"><label>Name *</label><input name="agent_display_name" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.name || '')}" oninput="editingAgentData.name=this.value"></div>
      <div class="agent-source-switch"><button type="button" class="${!isCli && !isOllama ? 'active' : ''}" onclick="changeEditingAgentKind('openai')">API key</button><button type="button" class="${isOllama ? 'active' : ''}" onclick="changeEditingAgentKind('ollama')">Ollama</button><button type="button" class="${isCli ? 'active' : ''}" onclick="changeEditingAgentKind('cli')">CLI</button></div>
      ${isCli ? `
        <div class="form-group"><label>Command *</label><input name="agent_cli_command" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.cli_command || '')}" placeholder="e.g. my-agent --stdio" oninput="editingAgentData.cli_command=this.value"></div>
        <p class="agent-field-note">CLI agents run this local command and do not need an API key or URL.</p>
      ` : isOllama ? `
        <p class="agent-field-note">Ollama agents connect to localhost by default. Add base_url to config to override.</p>
      ` : `
        <div class="form-group"><label>API key *</label><input type="text" class="masked-input" name="agent_provider_credential" value="${escAttr(data.api_key || '')}" placeholder="Paste your provider key" oninput="editingAgentData.api_key=this.value" onchange="detectAndVerifyProvider('${editingAgentId}')" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-form-type="other" data-lpignore="true" data-1p-ignore="true" data-bwignore="true"></div>
        <div class="form-group"><label>Provider</label><select onchange="changeEditingAgentKind(this.value)">${KINDS.filter(k => !['cli','ollama'].includes(k)).map(k=>`<option value="${k}" ${k===data.kind?'selected':''}>${k.charAt(0).toUpperCase()+k.slice(1)}</option>`).join('')}</select></div>
      `}
      ${!isCli ? `<details class="agent-advanced" ${(data.extra?.advanced_open) ? 'open' : ''} ontoggle="editingAgentData.extra.advanced_open=this.open"><summary>Advanced settings</summary><div class="agent-advanced-fields">
        <div class="form-group"><textarea name="agent_extra" autocomplete="off" spellcheck="false" rows="6" style="font-family: monospace; font-size: 11.5px; resize: vertical; line-height: 1.4;" onchange="try { const parsed = JSON.parse(this.value || '{}'); const extra = editingAgentData.extra || {}; const internalKeys = ['advanced_open', 'detected_provider', 'connection_verified', 'connection_error']; const internalData = {}; internalKeys.forEach(k => { if (k in extra) internalData[k] = extra[k]; }); editingAgentData.extra = { ...parsed, ...internalData }; this.style.borderColor=''; } catch(e) { this.style.borderColor='var(--danger)'; }">${escAttr((()=>{
          const extraFiltered = Object.fromEntries(Object.entries(data.extra || {}).filter(([k]) => !['advanced_open', 'detected_provider', 'connection_verified', 'connection_error'].includes(k)));
          if (!('base_url' in extraFiltered)) extraFiltered.base_url = data.base_url || "";
          return JSON.stringify(extraFiltered, null, 2);
        })())}</textarea></div>
      </div></details>` : ''}
    </div>
    <div class="agent-form-actions"><button type="button" class="btn btn-secondary btn-sm" style="margin-right:auto;" onclick="testAgentConnection('${agentId}', event)">Test</button><button type="button" class="btn btn-secondary" onclick="cancelEditAgent()">Cancel</button><button type="button" class="btn btn-primary" onclick="saveAgent('${agentId}')">${isNew ? 'Add agent' : 'Save changes'}</button></div>`;
  const form = panel.querySelector('.agent-editor-form');
  if (form) form.scrollTop = previousScrollTop;
}

function changeEditingAgentKind(kind) {
  editingAgentData.kind = kind;
  editingAgentData.extra = editingAgentData.extra || {};
  delete editingAgentData.extra.available_models;
  delete editingAgentData.extra.detected_provider;
  delete editingAgentData.extra.connection_verified;
  delete editingAgentData.extra.connection_error;
  renderAgentEditor();
}

function detectProviderFromKey(key) {
  const value = String(key || '').trim();
  if (value.startsWith('nvapi-')) return {kind:'openai', label:'NVIDIA NIM', baseUrl:'https://integrate.api.nvidia.com/v1'};
  if (value.startsWith('gsk_')) return {kind:'groq', label:'Groq', baseUrl:''};
  if (value.startsWith('AIza')) return {kind:'gemini', label:'Google Gemini', baseUrl:''};
  if (value.startsWith('sk-ant-')) return {kind:'claude', label:'Anthropic', baseUrl:''};
  if (value.startsWith('sk-or-v1-')) return {kind:'openai', label:'OpenRouter', baseUrl:'https://openrouter.ai/api/v1'};
  if (value.startsWith('sk-')) return {kind:'openai', label:'OpenAI', baseUrl:''};
  return null;
}

window.detectAndVerifyProvider = async function(uid) {
  const detected = detectProviderFromKey(editingAgentData.api_key);
  if (!detected) {
    notify('Provider could not be detected. Choose it under Advanced settings.', true);
    renderAgentEditor();
    return;
  }
  editingAgentData.kind = detected.kind;
  editingAgentData.base_url = detected.baseUrl;
  editingAgentData.extra = editingAgentData.extra || {};
  editingAgentData.extra.detected_provider = detected.label;
  delete editingAgentData.extra.connection_verified;
  delete editingAgentData.extra.connection_error;
  renderAgentEditor();
};

window.testAgentConnection = async function(uid, event) {
  if (!editingAgentData || !editingAgentData.kind) return;
  const modal = document.getElementById('agentTestModal');
  const modalContent = document.getElementById('agentTestModalContent');
  const modalTitle = document.getElementById('agentTestModalTitle');
  const modalSubtitle = document.getElementById('agentTestModalSubtitle');

  if (editingAgentData.kind === 'cli') {
    modalTitle.textContent = 'CLI Agent';
    modalTitle.style.color = 'var(--text)';
    modalSubtitle.textContent = '';
    modalContent.innerHTML = 'CLI agents do not expose a model catalog.';
    modal.style.display = 'flex';
    return;
  }
  const oldBtn = event?.currentTarget;
  if (oldBtn) { oldBtn.textContent = 'Testing...'; oldBtn.disabled = true; }
  try {
    const response = await fetch('/agents/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ...editingAgentData,
        base_url: editingAgentData.extra?.base_url || editingAgentData.base_url || '',
        max_history_turns: 20
      })
    });
    const data = await response.json();
    if (!data.ok) {
      modalTitle.textContent = 'Connection Failed';
      modalTitle.style.color = '#ef4444';
      modalSubtitle.textContent = 'Please check your configuration or API key.';
      modalContent.innerHTML = escHtml(friendlyProviderError(data.error));
      modal.style.display = 'flex';
    } else {
      const models = data.models || [];
      modalTitle.textContent = 'Connection Successful';
      modalTitle.style.color = '#22c55e';
      modalSubtitle.textContent = `Found ${models.length} available models.`;
      modalContent.innerHTML = escHtml(models.join('\n'));
      modal.style.display = 'flex';
    }
  } catch (err) {
      modalTitle.textContent = 'Network Error';
      modalTitle.style.color = '#ef4444';
      modalSubtitle.textContent = 'Failed to reach the provider.';
      modalContent.innerHTML = escHtml(err.message || 'Unknown network error occurred.');
      modal.style.display = 'flex';
  } finally {
    if (oldBtn) { oldBtn.textContent = 'Test'; oldBtn.disabled = false; }
  }
};



function setEditingExtra(key, value) {
  if (!editingAgentData.extra) editingAgentData.extra = {};
  if (value === '') {
    delete editingAgentData.extra[key];
  } else {
    editingAgentData.extra[key] = Number(value);
  }
}

function startEditAgent(uid, idx) {
  editingAgentId = uid;
  const arr = projectAgentConfigs;
  editingAgentData = JSON.parse(JSON.stringify(arr[idx]));
  if (!editingAgentData.extra) editingAgentData.extra = {};
  renderAgentCards();
}

function cancelEditAgent() {
  if (editingAgentId?.startsWith('new-')) {
    projectAgentConfigs = projectAgentConfigs.filter(agent => agent.id !== editingAgentId);
  }
  editingAgentId = null;
  editingAgentData = {};
  renderAgentCards();
}

async function saveAgent(agentId) {
  if (!editingAgentData.name || !editingAgentData.name.trim()) {
    notify('Agent name is required.', true);
    return;
  }
  if (editingAgentData.kind === 'cli' && !editingAgentData.cli_command?.trim()) {
    notify('Command is required for a CLI agent.', true);
    return;
  }
  if (!['cli', 'ollama'].includes(editingAgentData.kind) && !editingAgentData.api_key?.trim()) {
    notify('API key is required for this provider.', true);
    return;
  }
  if (!['cli', 'ollama'].includes(editingAgentData.kind) && editingAgentData.extra?.connection_verified === false) {
    notify(editingAgentData.extra.connection_error || 'Verify provider inference before saving.', true);
    return;
  }

  const payload = {...editingAgentData};
  payload.extra = {...(payload.extra || {})};
  delete payload.extra.advanced_open;
  
  if (payload.extra.base_url) {
    payload.base_url = payload.extra.base_url;
    delete payload.extra.base_url;
  }
  const isNew = agentId.startsWith('new-');

  // If this agent is coordinator, clear coordinator status on others locally
  if (payload.extra?.is_coordinator) {
    const arr = projectAgentConfigs;
    arr.forEach(a => {
      if (a.id !== agentId) {
        if (!a.extra) a.extra = {};
        a.extra.is_coordinator = false;
      }
    });
  }

  const url = isNew ? '/agents' : `/agents/${agentId}`;
  const method = isNew ? 'POST' : 'PUT';

  try {
    const response = await fetch(url, {
      method: method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
      notify(data.detail || 'Failed to save agent.', true);
      return;
    }

    notify(`Agent "${payload.name}" saved successfully.`);
    editingAgentId = null;
    editingAgentData = {};
    const uid = data.agent?.id || agentId;
    delete agentHealthStatus[uid];
    await loadAgentConfig();
    fetchAgentStatus();
  } catch (err) {
    console.error("Failed to save agent config", err);
    notify("Network error saving agent details", true);
  }
}

async function checkAgentHealth(cfg, uid) {
  if (!cfg.name || !cfg.name.trim()) {
    agentHealthStatus[uid] = { status: 'testing', error: '' };
    return;
  }

  try {
    const res = await fetch('/agents/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: cfg.name,
        kind: cfg.kind,
        role: cfg.role || '',
        model: cfg.model || '',
        api_key: cfg.api_key || '',
        base_url: cfg.base_url || '',
        cli_command: cfg.cli_command || '',
        system_prompt: cfg.system_prompt || '',
        max_history_turns: Number(cfg.max_history_turns || 20),
        extra: cfg.extra || {}
      })
    }).then(r => r.json());

    if (res.ok) {
      agentHealthStatus[uid] = { status: 'success', error: '', checked_at: new Date().toISOString() };
    } else {
      agentHealthStatus[uid] = { status: 'failed', error: res.error || 'Configuration check failed', checked_at: new Date().toISOString() };
    }
  } catch (err) {
    agentHealthStatus[uid] = { status: 'failed', error: err.message || 'Connection timeout', checked_at: new Date().toISOString() };
  }

  const card = document.getElementById('card-' + uid);
  if (card) {
    const dot = card.querySelector('.health-dot');
    if (dot) {
      dot.className = `health-dot ${agentHealthStatus[uid].status}`;
      dot.title = agentHealthStatus[uid].status === 'failed' 
        ? friendlyProviderError(agentHealthStatus[uid].error)
        : (agentHealthStatus[uid].status === 'success' ? 'Agent working correctly' : 'Testing connection...');
    }
  }
}

window.refreshAgentHealth = async function(uid, idx) {
  const configs = projectAgentConfigs;
  const cfg = configs[idx];
  if (!cfg) return;
  agentHealthStatus[uid] = {status: 'testing', error: '', checked_at: new Date().toISOString()};
  renderAgentCards();
  await checkAgentHealth(cfg, uid);
  renderAgentCards();
  const result = agentHealthStatus[uid];
  notify(result.status === 'success' ? `${cfg.name} is healthy.` : friendlyProviderError(result.error), result.status !== 'success');
};

async function deleteAgent(agentId) {
  if (!(await window.appConfirm("Are you sure you want to delete this agent?", "Delete Agent", "Delete", true))) return;
  const url = `/agents/${agentId}`;
  try {
    const response = await fetch(url, { method: 'DELETE' });
    if (!response.ok) {
      const data = await response.json();
      notify(data.detail || 'Failed to delete agent', true);
      return;
    }
    notify("Agent deleted successfully.");
    await loadAgentConfig();
    fetchAgentStatus();
  } catch (err) {
    console.error("Failed to delete agent", err);
    notify("Failed to delete agent", true);
  }
}



function addNewAgentCard() {
  if (!projectOpen) {
    notify('Please open a project first.', true);
    return;
  }
  if (editingAgentId && editingAgentId.includes('new-')) {
    notify('Please save or cancel the current new agent form first.', true);
    return;
  }

  const tempId = 'new-' + Date.now();
  const newAgent = {
    id: tempId,
    name: '',
    role: '',
    kind: 'claude',
    model: '',
    api_key: '',
    system_prompt: '',
    max_history_turns: 20,
    extra: { is_coordinator: false, prefer_free_models: true }
  };

  projectAgentConfigs.push(newAgent);
  editingAgentId = tempId;
  editingAgentData = newAgent;
  renderAgentCards();
}

// ── MCP Config ──────────────────────────────────────────────────────────────

let mcpServers = [];

async function loadMCPServers() {
  await loadMCPAccessToken();
  const configSection = document.getElementById('mcpConfigSection');
  const projectRequired = document.getElementById('mcpProjectRequired');
  if (!projectOpen) {
    if (configSection) configSection.style.display = 'none';
    if (projectRequired) projectRequired.style.display = 'flex';
    return;
  }
  if (projectRequired) projectRequired.style.display = 'none';
  if (configSection) configSection.style.display = 'block';
  const res = await fetch('/mcp/servers').then(r => r.json());
  mcpServers = res.servers || [];
  renderMCPServers();
}

async function loadMCPAccessToken() {
  const statusNode = document.getElementById('mcpAccessStatus');
  const generateButton = document.getElementById('mcpGenerateTokenButton');
  const revokeButton = document.getElementById('mcpRevokeTokenButton');
  const generated = document.getElementById('mcpGeneratedToken');
  if (!statusNode) return;
  if (generated) generated.style.display = 'none';
  try {
    const response = await fetch('/mcp/access-token');
    if (response.status === 403) {
      statusNode.textContent = 'Only an administrator can generate or revoke the server MCP token.';
      if (generateButton) generateButton.style.display = 'none';
      if (revokeButton) revokeButton.style.display = 'none';
      return;
    }
    if (!response.ok) throw new Error('Unable to load MCP token status');
    const status = await response.json();
    if (generateButton) {
      generateButton.style.display = '';
      generateButton.textContent = status.configured ? 'Regenerate token' : 'Generate token';
      generateButton.dataset.configured = status.configured ? 'true' : 'false';
    }
    if (revokeButton) revokeButton.style.display = status.configured ? '' : 'none';
    const details = [];
    if (status.configured) details.push(`UI-generated token active${status.created_at ? ` since ${new Date(status.created_at).toLocaleString()}` : ''}.`);
    else details.push('No UI-generated token is active. Local MCP access requires no token.');
    if (status.environment_token_configured) details.push('An environment token is also active and must be managed where the server is launched.');
    statusNode.textContent = details.join(' ');
  } catch (error) {
    statusNode.textContent = error.message || 'Unable to load MCP token status.';
  }
}

async function generateMCPAccessToken() {
  const button = document.getElementById('mcpGenerateTokenButton');
  if (button?.dataset.configured === 'true' && !(await window.appConfirm('Regenerate the token? Existing clients using the UI-generated token will immediately lose access.', 'Regenerate Token', 'Regenerate', true))) return;
  const response = await fetch('/mcp/access-token', { method: 'POST' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    notify(data.detail || 'Unable to generate MCP token', true);
    return;
  }
  const generated = document.getElementById('mcpGeneratedToken');
  const value = document.getElementById('mcpGeneratedTokenValue');
  if (value) value.value = data.token || '';
  if (generated) generated.style.display = 'block';
  await loadMCPAccessToken();
  if (generated) generated.style.display = 'block';
  notify('MCP access token generated. Copy it now.');
}

async function copyMCPAccessToken() {
  const value = document.getElementById('mcpGeneratedTokenValue');
  if (!value?.value) return;
  await navigator.clipboard.writeText(value.value);
  notify('MCP access token copied');
}

async function revokeMCPAccessToken() {
  if (!(await window.appConfirm('Revoke the UI-generated MCP token? Connected clients using it will immediately lose access.', 'Revoke Token', 'Revoke', true))) return;
  const response = await fetch('/mcp/access-token', { method: 'DELETE' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    notify(data.detail || 'Unable to revoke MCP token', true);
    return;
  }
  document.getElementById('mcpGeneratedTokenValue').value = '';
  document.getElementById('mcpGeneratedToken').style.display = 'none';
  await loadMCPAccessToken();
  notify('MCP access token revoked');
}

function renderMCPServers() {
  const container = document.getElementById('mcpList');
  if (!container) return;
  if (!mcpServers.length) {
    container.innerHTML = '<div style="color:var(--muted);font-size:12.5px;font-style:italic">No MCP servers configured for this project.</div>';
    return;
  }
  container.innerHTML = mcpServers.map(s => `
    <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:6px; padding:12px; display:flex; justify-content:space-between; align-items:center;">
      <div>
        <div style="font-weight:600; font-size:14px">
          ${escHtml(s.name)}
          ${s.username || s.password ? '<span style="font-size:10px; padding:2px 4px; background:var(--bg3); border-radius:4px; margin-left:6px;" title="Authenticated">🔒 Auth</span>' : ''}
        </div>
        <div style="font-family:var(--mono); font-size:11px; color:var(--muted); margin-top:4px;">
          ${escHtml(s.command)} ${escHtml((s.args||[]).join(' '))}
        </div>
      </div>
      <button class="btn btn-danger" onclick="deleteMCPServer('${s.id}')" style="padding:6px 12px; font-size:12px">Delete</button>
    </div>
  `).join('');
}

async function addMCPServer() {
  const name = document.getElementById('mcpName').value.trim();
  const command = document.getElementById('mcpCommand').value.trim();
  const argsRaw = document.getElementById('mcpArgs').value.trim();
  const envRaw = document.getElementById('mcpEnv').value.trim();
  const username = document.getElementById('mcpUsername').value.trim();
  const password = document.getElementById('mcpPassword').value.trim();
  
  if (!name || !command) {
    notify('Name and command are required.', true);
    return;
  }
  
  const args = argsRaw ? argsRaw.split(',').map(a => a.trim()) : [];
  const env = {};
  if (envRaw) {
    envRaw.split(',').forEach(pair => {
      const [k, v] = pair.split('=');
      if (k && v) env[k.trim()] = v.trim();
    });
  }
  
  const res = await fetch('/mcp/servers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, command, args, env, username, password })
  });
  
  if (res.ok) {
    document.getElementById('mcpName').value = '';
    document.getElementById('mcpCommand').value = '';
    document.getElementById('mcpArgs').value = '';
    document.getElementById('mcpEnv').value = '';
    document.getElementById('mcpUsername').value = '';
    document.getElementById('mcpPassword').value = '';
    const form = document.getElementById('mcpAddForm');
    if (form) form.style.display = 'none';
    notify('MCP server added');
    loadMCPServers();
  } else {
    notify('Failed to add MCP server', true);
  }
}

async function deleteMCPServer(id) {
  if (!(await window.appConfirm('Delete this MCP server?', 'Delete Server', 'Delete', true))) return;
  const res = await fetch('/mcp/servers/' + id, { method: 'DELETE' });
  if (res.ok) {
    notify('MCP server deleted');
    loadMCPServers();
  } else {
    notify('Failed to delete MCP server', true);
  }
}

async function togglePauseAgent(id, idx) {
  const configs = projectAgentConfigs;
  const cfg = configs[idx];
  if (!cfg || cfg.id !== id) return;

  const originalPause = cfg.is_paused || false;
  cfg.is_paused = !originalPause;

  try {
    const url = `/agents/${id}`;
    const res = await fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg)
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      cfg.is_paused = originalPause; // revert
      notify(data.detail || "Failed to toggle agent pause state", true);
    } else {
      notify(cfg.is_paused ? 'Agent paused; its active specialists will use available fallback models' : 'Agent resumed');
    }
  } catch (err) {
    cfg.is_paused = originalPause;
    notify("Error: " + err.message, true);
  }

  loadAgentConfig();
}
