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

  // Render Global List (Only if no project is open)
  if (!projectOpen) {
    html += `<h3 style="margin-top:10px;margin-bottom:12px;font-size:12.5px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;border-bottom:1px solid var(--border);padding-bottom:6px">Global Team Templates</h3>`;
    if (!globalAgentConfigs.length) {
      html += `<div style="color:var(--muted);font-size:12.5px;font-style:italic;margin-bottom:20px;padding:8px 0">No global agents configured.</div>`;
    } else {
      globalAgentConfigs.forEach((cfg, idx) => {
        html += renderSingleCard(cfg, idx, true);
      });
    }
  }

  // Render Project List (Only if project is open)
  if (projectOpen) {
    html += `<h3 style="margin-top:10px;margin-bottom:12px;font-size:12.5px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;border-bottom:1px solid var(--border);padding-bottom:6px">Project Team</h3>`;
    if (!projectAgentConfigs.length) {
      html += `<div style="color:var(--muted);font-size:12.5px;font-style:italic;margin-bottom:10px;padding:8px 0">No project agents configured. Inheriting global team:</div>`;
      if (!globalAgentConfigs.length) {
        html += `<div style="color:var(--muted);font-size:12.5px;font-style:italic;margin-bottom:20px;padding:8px 0">No global agents configured either.</div>`;
      } else {
        globalAgentConfigs.forEach((cfg, idx) => {
          html += renderSingleCard(cfg, idx, true); // true to render them as global/read-only or editable if we want
        });
      }
    } else {
      projectAgentConfigs.forEach((cfg, idx) => {
        html += renderSingleCard(cfg, idx, false);
      });
    }
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

    ')" style="padding:4px 10px;font-size:11px">Customize for Project</button>
          <button class="btn btn-secondary" onclick="startEditAgent('${uid}', true, ${idx})" style="padding:4px 10px;font-size:11px">Edit Global</button>
        `;
      } else {
        actionButtons += `
          <button class="btn btn-secondary" onclick="startEditAgent('${uid}', true, ${idx})" style="padding:4px 10px;font-size:11px">Edit</button>
        `;
      }
      actionButtons += `
        <button class="btn ${isPaused ? 'btn-primary' : 'btn-secondary'}" onclick="togglePauseAgent('${cfg.id}', true, ${idx})" style="padding:4px 10px;font-size:11px">${isPaused ? 'Resume' : 'Pause'}</button>
        <button class="btn btn-danger" onclick="deleteAgent('${cfg.id}', true)" style="padding:4px 10px;font-size:11px">Delete</button>
      `;
    } else {
      actionButtons += `
        <button class="btn btn-secondary" onclick="startEditAgent('${uid}', false, ${idx})" style="padding:4px 10px;font-size:11px">Edit</button>
        <button class="btn ${isPaused ? 'btn-primary' : 'btn-secondary'}" onclick="togglePauseAgent('${cfg.id}', false, ${idx})" style="padding:4px 10px;font-size:11px">${isPaused ? 'Resume' : 'Pause'}</button>
        <button class="btn btn-danger" onclick="deleteAgent('${cfg.id}', false)" style="padding:4px 10px;font-size:11px">Delete</button>
      `;
    }

    
    const badgeHTML = pauseBadge + badge;

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
    const capacityLabel = isLocal ? 'Local' : quotaBlocked ? 'Quota exhausted' : runtimeBlocked || statusInfo.status === 'failed' ? 'Blocked' : capacity.retry_at ? 'Limited' : statusInfo.status === 'success' ? 'Healthy' : 'Checking';
    const capacityClass = quotaBlocked || runtimeBlocked ? 'blocked' : capacityLabel.toLowerCase();
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
            </div>
            <div class="agent-card-subtitle">
              <span><strong>Kind:</strong> ${cfg.kind}</span>
              <span>·</span>
              <span><strong>Model:</strong> ${escHtml(cfg.model || 'default')}</span>
              <span>·</span>
              <span><strong>Role:</strong> ${escHtml(cfg.role || 'none')}</span>
            </div>
            <button type="button" class="agent-capacity-pill ${capacityClass}" onclick="toggleAgentCapacity('${uid}')">Capacity: ${capacityLabel}</button>
            <div class="agent-capacity-details" id="capacity-${uid}" hidden>
              <div><strong>${isLocal ? 'Local runtime' : 'Provider capacity'}</strong><span>${isLocal ? 'No cloud credits required' : runtimeBlocked ? escHtml(friendlyProviderError(capacity.error)) : statusInfo.status === 'failed' ? escHtml(friendlyProviderError(statusInfo.error)) : 'Inference endpoint is reachable'}</span></div>
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
    <div class="agent-editor-header">
      <div><span class="agent-editor-eyebrow">Project team</span><h3>${isNew ? 'Add agent' : 'Edit agent'}</h3></div>
      <button class="agent-editor-close" onclick="cancelEditAgent()" aria-label="Close editor">×</button>
    </div>
    <div class="agent-editor-form" data-form-type="other">
      <div class="form-group"><label>Agent name *</label><input name="agent_display_name" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.name || '')}" placeholder="e.g. Security reviewer" oninput="editingAgentData.name=this.value"></div>
      <div class="form-group"><label>Specialty</label><input name="agent_specialty" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.role || '')}" placeholder="e.g. security, UX, backend" oninput="editingAgentData.role=this.value"></div>
      <div class="agent-source-switch"><button type="button" class="${!isCli && !isOllama ? 'active' : ''}" onclick="changeEditingAgentKind('openai')">API key</button><button type="button" class="${isOllama ? 'active' : ''}" onclick="changeEditingAgentKind('ollama')">Ollama</button><button type="button" class="${isCli ? 'active' : ''}" onclick="changeEditingAgentKind('cli')">CLI</button></div>
      ${isCli ? `
        <div class="form-group"><label>Command *</label><input name="agent_cli_command" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.cli_command || '')}" placeholder="e.g. my-agent --stdio" oninput="editingAgentData.cli_command=this.value"></div>
        <p class="agent-field-note">CLI agents run this local command and do not need an API key or URL.</p>
      ` : isOllama ? `
        <div class="form-group"><label>Ollama URL <span class="label-optional">Optional</span></label><input name="agent_ollama_url" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.base_url || '')}" placeholder="http://localhost:11434" oninput="editingAgentData.base_url=this.value"></div>
        <button type="button" class="btn btn-secondary agent-verify-btn" onclick="discoverAgentModels('${editingAgentId}')">Test connection and find models</button>
      ` : `
        <div class="form-group"><label>API key *</label><input type="text" class="masked-input" name="agent_provider_credential" value="${escAttr(data.api_key || '')}" placeholder="Paste your provider key" oninput="editingAgentData.api_key=this.value" onchange="detectAndVerifyProvider('${editingAgentId}')" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-form-type="other" data-lpignore="true" data-1p-ignore="true" data-bwignore="true"></div>
        <div class="agent-detection-status ${connectionVerified ? 'success' : connectionError ? 'failed' : ''}">${connectionVerified ? `✓ ${escHtml(providerLabel)} verified · ${models.length} models found` : connectionError ? escHtml(connectionError) : 'Provider and models will be detected when you enter the key.'}</div>
      `}
      ${!isCli ? `<details class="agent-advanced" ${(data.extra?.advanced_open || data.extra?.show_available_models) ? 'open' : ''} ontoggle="editingAgentData.extra.advanced_open=this.open"><summary>Advanced settings</summary><div class="agent-advanced-fields">
        ${!isOllama ? `<div class="form-group"><label>Provider</label><select onchange="changeEditingAgentKind(this.value)">${KINDS.filter(k => !['cli','ollama'].includes(k)).map(k=>`<option value="${k}" ${k===data.kind?'selected':''}>${k.charAt(0).toUpperCase()+k.slice(1)}</option>`).join('')}</select></div><div class="form-group"><label>Base URL <span class="label-optional">Optional</span></label><input name="agent_provider_url" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" value="${escAttr(data.base_url || '')}" placeholder="Use detected provider default" oninput="editingAgentData.base_url=this.value"></div>` : ''}
        <div class="form-group"><label>Preferred model</label><input name="agent_model" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" list="model-options-editor" value="${escAttr(data.model || '')}" placeholder="${modelPlaceholder(data.kind)}" oninput="editingAgentData.model=this.value"><datalist id="model-options-editor">${models.map(model => `<option value="${escAttr(model)}"></option>`).join('')}</datalist></div>
        <button type="button" class="btn btn-secondary" onclick="toggleAvailableModels()">${data.extra?.show_available_models ? 'Hide' : 'Show'} available models (${models.length})</button>
        ${data.extra?.show_available_models ? `<div class="available-model-list">${models.length ? models.map(model => `<button type="button" onclick="selectAgentModel('${escAttr(model)}')" class="available-model-chip ${model === data.model ? 'selected' : ''}">${escHtml(model)}</button>`).join('') : '<span>No models loaded yet. Test the connection first.</span>'}</div>` : ''}
      </div></details>` : ''}
      <label class="agent-coordinator-option"><input type="checkbox" ${data.extra?.is_coordinator ? 'checked' : ''} onchange="editingAgentData.extra.is_coordinator=this.checked"><span><strong>Team coordinator</strong><small>Manages the debate and execution loop.</small></span></label>
    </div>
    <div class="agent-form-actions"><button class="btn btn-secondary" onclick="cancelEditAgent()">Cancel</button><button class="btn btn-primary" onclick="saveAgent('${agentId}')">${isNew ? 'Add agent' : 'Save changes'}</button></div>`;
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
  delete editingAgentData.extra.available_models;
  delete editingAgentData.extra.connection_verified;
  delete editingAgentData.extra.connection_error;
  renderAgentEditor();
  await discoverAgentModels(uid);
};

window.toggleAvailableModels = function() {
  editingAgentData.extra = editingAgentData.extra || {};
  editingAgentData.extra.show_available_models = !editingAgentData.extra.show_available_models;
  renderAgentEditor();
};

window.selectAgentModel = function(model) {
  editingAgentData.model = model;
  renderAgentEditor();
};

function modelPlaceholder(kind) {
  return {claude:'claude-sonnet-4-6',openai:'gpt-4o',groq:'llama-3.3-70b-versatile',gemini:'gemini-2.5-flash',ollama:'llama3',cli:''}[kind]||'';
}

window.discoverAgentModels = async function(uid) {
  if (!editingAgentData || !editingAgentData.kind) return;
  if (editingAgentData.kind === 'cli') {
    notify('CLI agents do not expose a model catalog.', true);
    return;
  }
  try {
    const response = await fetch('/agents/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: editingAgentData.name || 'model-discovery',
        kind: editingAgentData.kind,
        role: editingAgentData.role || '',
        model: editingAgentData.model || '',
        api_key: editingAgentData.api_key || '',
        base_url: editingAgentData.base_url || '',
        cli_command: editingAgentData.cli_command || '',
        system_prompt: editingAgentData.system_prompt || '',
        max_history_turns: Number(editingAgentData.max_history_turns || 20),
        extra: editingAgentData.extra || {},
      })
    });
    const data = await response.json();
    if (!data.ok) {
      notify(friendlyProviderError(data.error), true);
      return;
    }
    editingAgentData.extra = editingAgentData.extra || {};
    editingAgentData.extra.available_models = data.models || [];
    if (!editingAgentData.model && data.models?.length) {
      editingAgentData.model = data.models[0];
    }
    const testResponse = await fetch('/agents/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ...editingAgentData,
        max_history_turns: Number(editingAgentData.max_history_turns || 20),
      })
    });
    const testResult = await testResponse.json();
    editingAgentData.extra.connection_verified = !!testResult.ok;
    if (testResult.ok) {
      delete editingAgentData.extra.connection_error;
    } else {
      const rawError = String(testResult.error || 'Inference test failed');
      editingAgentData.extra.connection_error = editingAgentData.extra.detected_provider === 'NVIDIA NIM' && /403|forbidden|authorization failed/i.test(rawError)
        ? 'NVIDIA catalog access works, but inference is not enabled for this key. Check NVIDIA API credits or model access.'
        : friendlyProviderError(rawError);
    }
    renderAgentEditor();
    if (testResult.ok) {
      notify(`Verified inference and found ${data.models.length} compatible models.`);
    } else {
      notify(editingAgentData.extra.connection_error, true);
    }
  } catch (err) {
    notify('Could not query provider models.', true);
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
  if (editingAgentId) {
    const parts = editingAgentId.split('-');
    const scope = parts[0];
    const idVal = parts.slice(1).join('-');
    if (idVal.startsWith('new-')) {
      if (scope === 'global') {
        globalAgentConfigs = globalAgentConfigs.filter(a => a.id !== idVal);
      } else {
        projectAgentConfigs = projectAgentConfigs.filter(a => a.id !== idVal);
      }
    }
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
  delete payload.extra.show_available_models;
  delete payload.extra.advanced_open;
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

  const url = isGlobal 
    ? (isNew ? '/agents/global' : `/agents/global/${agentId}`)
    : (isNew ? '/agents' : `/agents/${agentId}`);
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
  if (!confirm("Are you sure you want to delete this agent?")) return;
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

,
      body: JSON.stringify(clone)
    });
    if (!response.ok) {
      const data = await response.json();
      notify(data.detail || 'Failed to copy to global team.', true);
      return;
    }
    notify(`Copied "${source.name}" to the Global Team list.`);
    await loadAgentConfig();
  } catch (err) {
    console.error("Failed to promote agent", err);
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
    extra: { is_coordinator: false }
  };

  projectAgentConfigs.push(newAgent);
  editingAgentId = tempId;
  editingAgentData = newAgent;
  renderAgentCards();
}

// ── MCP Config ──────────────────────────────────────────────────────────────

let mcpServers = [];

async function loadMCPServers() {
  const configSection = document.getElementById('mcpConfigSection');
  const projectRequired = document.getElementById('mcpProjectRequired');
  if (!projectOpen) {
    if (configSection) configSection.style.display = 'none';
    if (projectRequired) projectRequired.style.display = 'flex';
    return;
  }
  if (projectRequired) projectRequired.style.display = 'none';
  if (configSection) configSection.style.display = 'block';
  const res = await fetch('/mcp').then(r => r.json());
  mcpServers = res.servers || [];
  renderMCPServers();
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
        <div style="font-weight:600; font-size:14px">${escHtml(s.name)}</div>
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
  
  const res = await fetch('/mcp', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, command, args, env })
  });
  
  if (res.ok) {
    document.getElementById('mcpName').value = '';
    document.getElementById('mcpCommand').value = '';
    document.getElementById('mcpArgs').value = '';
    document.getElementById('mcpEnv').value = '';
    const form = document.getElementById('mcpAddForm');
    if (form) form.style.display = 'none';
    notify('MCP server added');
    loadMCPServers();
  } else {
    notify('Failed to add MCP server', true);
  }
}

async function deleteMCPServer(id) {
  if (!confirm('Delete this MCP server?')) return;
  const res = await fetch('/mcp/' + id, { method: 'DELETE' });
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
