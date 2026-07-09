// ── Agent config ──────────────────────────────────────────────────────────────
const KINDS = ['claude','openai','gemini','cli','ollama'];
let globalAgentConfigs = [];
let projectAgentConfigs = [];
let editingAgentId = null; // String format: 'global-<id>' or 'project-<id>'
let editingAgentData = {};

async function loadAgentConfig() {
  const res = await fetch('/agents').then(r=>r.json());
  globalAgentConfigs = res.global || [];
  projectAgentConfigs = res.project || [];
  renderAgentCards();
}

function renderAgentCards() {
  const container = document.getElementById('agentCardsContainer');
  if (!container) return;

  const scopeLabel = document.getElementById('scopeLabel');
  if (projectOpen) {
    scopeLabel.textContent = `Project Scope: ${escHtml(currentProjectPath.split('/').pop())}`;
  } else {
    scopeLabel.textContent = `Global Scope (No Project Open)`;
  }

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
}

function renderSingleCard(cfg, idx, isGlobal) {
  const uid = (isGlobal ? 'global-' : 'project-') + cfg.id;
  const isEditing = editingAgentId === uid;
  
  if (!isEditing) {
    const isCoordinator = cfg.extra?.is_coordinator;
    const initial = (cfg.name || '?').charAt(0).toUpperCase();
    let kindClass = 'color-default';
    if (['claude','openai','gemini','cli','ollama'].includes(cfg.kind)) {
      kindClass = `color-${cfg.kind}`;
    }
    
    let actionButtons = '';
    if (isGlobal) {
      if (projectOpen) {
        actionButtons += `
          <button class="btn btn-secondary" onclick="overrideForProject('${cfg.id}')" style="padding:4px 10px;font-size:11px">Customize for Project</button>
          <button class="btn btn-secondary" onclick="startEditAgent('${uid}', true, ${idx})" style="padding:4px 10px;font-size:11px">Edit Global</button>
        `;
      } else {
        actionButtons += `
          <button class="btn btn-secondary" onclick="startEditAgent('${uid}', true, ${idx})" style="padding:4px 10px;font-size:11px">Edit</button>
        `;
      }
      actionButtons += `
        <button class="btn btn-danger" onclick="deleteAgent('${cfg.id}', true)" style="padding:4px 10px;font-size:11px">Delete</button>
      `;
    } else {
      actionButtons += `
        <button class="btn btn-secondary" onclick="startEditAgent('${uid}', false, ${idx})" style="padding:4px 10px;font-size:11px">Edit</button>
        <button class="btn btn-secondary" onclick="promoteToGlobal('${cfg.id}')" style="padding:4px 10px;font-size:11px">Make Global</button>
        <button class="btn btn-danger" onclick="deleteAgent('${cfg.id}', false)" style="padding:4px 10px;font-size:11px">Delete</button>
      `;
    }

    const badge = isGlobal 
      ? '<span class="agent-scope-badge global">Global Team</span>'
      : '<span class="agent-scope-badge project">Project Override</span>';

    const statusInfo = agentHealthStatus[uid] || { status: 'testing', error: '' };
    if (!agentHealthStatus[uid]) {
      agentHealthStatus[uid] = { status: 'testing', error: '' };
      setTimeout(() => checkAgentHealth(cfg, uid), 50);
    }
    const hoverTitle = statusInfo.status === 'failed' 
      ? statusInfo.error 
      : (statusInfo.status === 'success' ? 'Agent working correctly' : 'Testing connection...');

    return `
      <div class="agent-card" id="card-${uid}">
        <div class="agent-card-row">
          <div class="agent-card-avatar ${kindClass}">
            ${initial}
          </div>
          <div class="agent-card-details">
            <div class="agent-card-title-row">
              <span class="agent-card-title">${escHtml(cfg.name || '(New Agent)')}</span>
              <span class="health-dot ${statusInfo.status}" title="${escAttr(hoverTitle)}" style="width:7px;height:7px;border-radius:50%;display:inline-block;vertical-align:middle;margin-left:4px"></span>
              ${badge}
              ${isCoordinator ? '<span style="font-size:11px;color:var(--yellow);font-weight:600">👑 Coordinator</span>' : ''}
            </div>
            <div class="agent-card-subtitle">
              <span><strong>Kind:</strong> ${cfg.kind}</span>
              <span>·</span>
              <span><strong>Model:</strong> ${escHtml(cfg.model || 'default')}</span>
              <span>·</span>
              <span><strong>Role:</strong> ${escHtml(cfg.role || 'none')}</span>
            </div>
          </div>
          <div class="agent-card-actions">
            ${actionButtons}
          </div>
        </div>
      </div>
    `;
  } else {
    // EDIT MODE FORM
    const data = editingAgentData;
    return `
      <div class="agent-card editing" id="card-${uid}">
        <div style="font-family:var(--heading-font);font-size:13.5px;font-weight:700;margin-bottom:12px;color:var(--accent2);border-bottom:1px solid var(--border);padding-bottom:6px">
          Editing ${isGlobal ? 'Global' : 'Project'} Agent: ${escHtml(cfg.name || 'New Agent')}
        </div>
        <div class="form-grid">
          <div class="form-group">
            <label>Agent Name</label>
            <input value="${escAttr(data.name)}" placeholder="Builder, Ada, critic..." oninput="editingAgentData.name=this.value">
          </div>
          <div class="form-group">
            <label>Kind</label>
            <select onchange="editingAgentData.kind=this.value;renderAgentCards()">
              ${KINDS.map(k=>`<option value="${k}" ${k===data.kind?'selected':''}>${k}</option>`).join('')}
            </select>
          </div>
          <div class="form-group">
            <label>Role</label>
            <input value="${escAttr(data.role||'')}" placeholder="developer, reviewer, tester..." oninput="editingAgentData.role=this.value">
          </div>
          <div class="form-group">
            <label>Model</label>
            <input value="${escAttr(data.model||'')}" placeholder="${modelPlaceholder(data.kind)}" oninput="editingAgentData.model=this.value">
          </div>
          <div class="form-group full" style="display:flex;align-items:center;gap:6px;margin:4px 0">
            <input type="checkbox" id="edit-coordinator-${uid}" ${data.extra?.is_coordinator ? 'checked' : ''} onchange="editingAgentData.extra.is_coordinator=this.checked" style="width:auto;margin:0">
            <label for="edit-coordinator-${uid}" style="cursor:pointer;font-weight:600;font-size:11px;user-select:none;margin:0;text-transform:none">Team Coordinator (acts as manager of debate & execution loop)</label>
          </div>
          
          <div class="form-group full">
            <label>API Key</label>
            <input type="password" value="${escAttr(data.api_key||'')}" placeholder="fallback to env var" oninput="editingAgentData.api_key=this.value" autocomplete="new-password" data-lpignore="true">
          </div>
          <div class="form-group full">
            <label>CLI command (for local models)</label>
            <input value="${escAttr(data.cli_command||'')}" placeholder="e.g. ollama run llama3" oninput="editingAgentData.cli_command=this.value">
          </div>
          <details style="grid-column: span 2; margin-top: 6px;">
            <summary style="font-size:11px;font-weight:600;color:var(--accent2);cursor:pointer;user-select:none;outline:none">Enterprise Endpoints</summary>
            <div class="form-grid" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border)">
              <div class="form-group full">
                <label>Base URL (Foundry / DeepInfra)</label>
                <input value="${escAttr(data.base_url||'')}" placeholder="e.g. https://api.endpoints.anyscale.com/v1" oninput="editingAgentData.base_url=this.value">
              </div>
              <div class="form-group full">
                <label>Cloud Platform (Anthropic)</label>
                <select onchange="setEditingExtra('platform', this.value)">
                  <option value="" ${!data.extra?.platform ? 'selected' : ''}>Standard Public API</option>
                  <option value="bedrock" ${data.extra?.platform === 'bedrock' ? 'selected' : ''}>AWS Bedrock</option>
                  <option value="vertex" ${data.extra?.platform === 'vertex' ? 'selected' : ''}>GCP Vertex AI</option>
                </select>
              </div>
            </div>
          </details>
          <details style="grid-column: span 2; margin-top: 6px;">
            <summary style="font-size:11px;font-weight:600;color:var(--accent2);cursor:pointer;user-select:none;outline:none">Override System Prompt</summary>
            <div class="form-grid" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border)">
              <div class="form-group full">
                <textarea style="height:60px" oninput="editingAgentData.system_prompt=this.value">${escHtml(data.system_prompt||'')}</textarea>
              </div>
            </div>
          </details>
        </div>
        <div class="agent-form-actions">
          <button class="btn btn-secondary" onclick="cancelEditAgent()">Cancel</button>
          <button class="btn btn-primary" onclick="saveAgent('${cfg.id}', ${isGlobal})">Save</button>
        </div>
      </div>
    `;
  }
}

function modelPlaceholder(kind) {
  return {claude:'claude-sonnet-4-6',openai:'gpt-4o',gemini:'gemini-2.5-flash',ollama:'llama3',cli:''}[kind]||'';
}

function setEditingExtra(key, value) {
  if (!editingAgentData.extra) editingAgentData.extra = {};
  if (value === '') {
    delete editingAgentData.extra[key];
  } else {
    editingAgentData.extra[key] = Number(value);
  }
}

function startEditAgent(uid, isGlobal, idx) {
  editingAgentId = uid;
  const arr = isGlobal ? globalAgentConfigs : projectAgentConfigs;
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

async function saveAgent(agentId, isGlobal) {
  if (!editingAgentData.name || !editingAgentData.name.trim()) {
    notify('Agent name is required.', true);
    return;
  }

  const payload = {...editingAgentData};
  const isNew = agentId.startsWith('new-');

  // If this agent is coordinator, clear coordinator status on others locally
  if (payload.extra?.is_coordinator) {
    const arr = isGlobal ? globalAgentConfigs : projectAgentConfigs;
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
    const uid = (isGlobal ? 'global-' : 'project-') + (data.agent?.id || agentId);
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
        cli_command: cfg.cli_command || '',
        system_prompt: cfg.system_prompt || '',
        max_history_turns: Number(cfg.max_history_turns || 20),
        extra: cfg.extra || {}
      })
    }).then(r => r.json());

    if (res.ok) {
      agentHealthStatus[uid] = { status: 'success', error: '' };
    } else {
      agentHealthStatus[uid] = { status: 'failed', error: res.error || 'Configuration check failed' };
    }
  } catch (err) {
    agentHealthStatus[uid] = { status: 'failed', error: err.message || 'Connection timeout' };
  }

  const card = document.getElementById('card-' + uid);
  if (card) {
    const dot = card.querySelector('.health-dot');
    if (dot) {
      dot.className = `health-dot ${agentHealthStatus[uid].status}`;
      dot.title = agentHealthStatus[uid].status === 'failed' 
        ? agentHealthStatus[uid].error 
        : (agentHealthStatus[uid].status === 'success' ? 'Agent working correctly' : 'Testing connection...');
    }
  }
}

async function deleteAgent(agentId, isGlobal) {
  if (!confirm("Are you sure you want to delete this agent?")) return;
  const url = isGlobal ? `/agents/global/${agentId}` : `/agents/${agentId}`;
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

function overrideForProject(agentId) {
  const source = globalAgentConfigs.find(a => a.id === agentId);
  if (!source) return;
  
  const tempId = 'new-' + Date.now();
  const clone = JSON.parse(JSON.stringify(source));
  clone.id = tempId;
  if (!clone.extra) clone.extra = {};
  
  projectAgentConfigs.push(clone);
  editingAgentId = `project-${tempId}`;
  editingAgentData = clone;
  renderAgentCards();
  notify(`Customizing "${source.name}" locally for this project.`);
}

async function promoteToGlobal(agentId) {
  const source = projectAgentConfigs.find(a => a.id === agentId);
  if (!source) return;

  const clone = JSON.parse(JSON.stringify(source));
  clone.id = 'new-' + Date.now();

  try {
    const response = await fetch('/agents/global', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
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

  if (projectOpen) {
    projectAgentConfigs.push(newAgent);
    editingAgentId = `project-${tempId}`;
  } else {
    globalAgentConfigs.push(newAgent);
    editingAgentId = `global-${tempId}`;
  }

  editingAgentData = newAgent;
  renderAgentCards();
}

// ── MCP Config ──────────────────────────────────────────────────────────────

let mcpServers = [];

async function loadMCPServers() {
  if (!projectOpen) {
    document.getElementById('mcpConfigSection').style.display = 'none';
    return;
  }
  document.getElementById('mcpConfigSection').style.display = 'block';
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

