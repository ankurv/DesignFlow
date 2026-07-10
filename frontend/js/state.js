// ── State ───────────────────────────────────────────────────────────────────
let appStatus = null;
let totalTokens = 0;
let totalCost = 0;
let eventCount = 0;
let agentColors = {};
const COLORS = ['#818cf8','#22c55e','#14b8a6','#f97316','#eab308','#ec4899','#06b6d4'];
let colorIdx = 0;
let agentConfigs = []; // local copy for config panel
let currentWsKey = 'cockpit';
let agentHealthStatus = {};
let paused = false;
let projectOpen = false;
let currentProjectPath = '';
let currentProjectBrief = '';
let toastTimer = null;
let lastMermaidCode = '';
let promptHistory = [];
let promptHistoryIndex = -1;
const RECENT_PROJECTS_KEY = 'designflow.recentProjects';
const MAX_RECENT_PROJECTS = 8;

function getRecentProjects() {
  try {
    const raw = localStorage.getItem(RECENT_PROJECTS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter(p => typeof p === 'string' && p.trim()) : [];
  } catch (_) {
    return [];
  }
}

function saveRecentProjects(paths) {
  try {
    localStorage.setItem(RECENT_PROJECTS_KEY, JSON.stringify(paths.slice(0, MAX_RECENT_PROJECTS)));
  } catch (_) {}
}

function projectDisplayName(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  return parts[parts.length - 1] || path;
}

function renderRecentProjects() {
  const paths = getRecentProjects();
  const targets = [
    { shell: document.getElementById('recentProjectsModal'), list: document.getElementById('recentProjectsModalList') },
    { shell: document.getElementById('recentProjectsEmptyState'), list: document.getElementById('recentProjectsEmptyList') },
  ];

  targets.forEach(({ shell, list }) => {
    if (!shell || !list) return;
    if (!paths.length) {
      shell.style.display = 'none';
      list.innerHTML = '';
      return;
    }
    shell.style.display = 'flex';
    list.innerHTML = paths.map(path => `
      <button class="recent-project-btn" type="button" onclick="openRecentProject(${JSON.stringify(path).replace(/"/g, '&quot;')})">
        <span class="recent-project-name">${escHtml(projectDisplayName(path))}</span>
        <span class="recent-project-path">${escHtml(path)}</span>
      </button>
    `).join('');
  });
}

function rememberRecentProject(path) {
  const clean = String(path || '').trim();
  if (!clean) return;
  const updated = [clean, ...getRecentProjects().filter(existing => existing !== clean)].slice(0, MAX_RECENT_PROJECTS);
  saveRecentProjects(updated);
  renderRecentProjects();
}

window.clearRecentProjects = function() {
  try {
    localStorage.removeItem(RECENT_PROJECTS_KEY);
  } catch (_) {}
  renderRecentProjects();
};

window.openRecentProject = async function(path) {
  const ok = await openProject(path);
  if (ok) closeProjectModal();
};

function applyProjectState(data = {}) {
  const isOpen = !!data.open;
  projectOpen = isOpen;
  currentProjectPath = isOpen ? (data.path || '') : '';
  currentProjectBrief = isOpen ? (data.brief || '') : '';
  document.body.classList.toggle('project-open', isOpen);

  if (isOpen && data.settings && data.settings.max_tokens) {
    const el = document.getElementById('maxTokensInput');
    if (el) el.value = data.settings.max_tokens;
    maxTokens = data.settings.max_tokens;
  }

  const stateEl = document.getElementById('projectState');
  if (stateEl) {
    if (isOpen && currentProjectPath) {
      const parts = currentProjectPath.split('/');
      const projName = parts[parts.length - 1] || currentProjectPath;
      stateEl.textContent = projName;
      stateEl.className = 'project-state ready';
    } else {
      stateEl.textContent = 'No project open';
      stateEl.className = 'project-state';
    }
  }

  if (typeof updateDesignCockpit === 'function') updateDesignCockpit();
}

function notify(message, isError=false) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className = `toast show ${isError ? 'error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.className = 'toast', 2600);
}
window.onerror = function(message, source, lineno, colno, error) {
  notify(`UI Error: ${message}`, true);
};
window.onunhandledrejection = function(event) {
  notify(`Promise Rejected: ${event.reason}`, true);
};

function copyMermaidSVG() {
  const svg = document.querySelector('#mermaidTarget svg');
  if (!svg) { notify('No diagram visual rendered to copy', true); return; }
  navigator.clipboard.writeText(svg.outerHTML);
  notify('Copied diagram SVG XML to clipboard!');
}

function copyMermaidCode() {
  if (!lastMermaidCode) { notify('No diagram source code found to copy', true); return; }
  navigator.clipboard.writeText(lastMermaidCode);
  notify('Copied Mermaid flowchart source code to clipboard!');
}

async function loadPresetTeam() {
  const select = document.getElementById('presetSelect');
  const val = select.value;
  if (!val) return;
  if (!confirm(`Are you sure you want to load the ${val === 'cloud' ? 'Standard Cloud' : val === 'local' ? 'Fully Local' : 'Dual Agent'} preset team? This will add preset agents to your active scope.`)) {
    select.value = '';
    return;
  }

  const isGlobal = !projectOpen;
  const url = isGlobal ? '/agents/global' : '/agents';

  let agents = [];
  if (val === 'cloud') {
    agents = [
      {
        name: 'CloudCoord',
        kind: 'gemini',
        role: 'Coordinator',
        model: 'gemini-1.5-pro',
        api_key: '',
        cli_command: '',
        system_prompt: 'You are the coordinator managing the team of agents.',
        max_history_turns: 20,
        extra: { is_coordinator: true }
      },
      {
        name: 'CloudArch',
        kind: 'gemini',
        role: 'Architect',
        model: 'gemini-1.5-flash',
        api_key: '',
        cli_command: '',
        system_prompt: 'You are the architect designing components.',
        max_history_turns: 20,
        extra: {}
      },
      {
        name: 'CloudCritic',
        kind: 'openai',
        role: 'Reviewer',
        model: 'gpt-4o-mini',
        api_key: '',
        cli_command: '',
        system_prompt: 'You are the critic/reviewer verifying specifications.',
        max_history_turns: 20,
        extra: {}
      }
    ];
  } else if (val === 'local') {
    agents = [
      {
        name: 'LocalCoord',
        kind: 'ollama',
        role: 'Coordinator',
        model: 'llama3',
        api_key: '',
        cli_command: '',
        system_prompt: 'You are the coordinator managing local agents.',
        max_history_turns: 15,
        extra: { is_coordinator: true }
      },
      {
        name: 'LocalArch',
        kind: 'ollama',
        role: 'Architect',
        model: 'mistral',
        api_key: '',
        cli_command: '',
        system_prompt: 'You are the architect designing local software.',
        max_history_turns: 15,
        extra: {}
      },
      {
        name: 'LocalCritic',
        kind: 'ollama',
        role: 'Reviewer',
        model: 'gemma',
        api_key: '',
        cli_command: '',
        system_prompt: 'You are the critic reviewing code.',
        max_history_turns: 15,
        extra: {}
      }
    ];
  } else if (val === 'dual') {
    agents = [
      {
        name: 'DualCoord',
        kind: 'gemini',
        role: 'Coordinator',
        model: 'gemini-1.5-pro',
        api_key: '',
        cli_command: '',
        system_prompt: 'You coordinate the architect debates.',
        max_history_turns: 20,
        extra: { is_coordinator: true }
      },
      {
        name: 'DualArch',
        kind: 'openai',
        role: 'Architect',
        model: 'gpt-4o',
        api_key: '',
        cli_command: '',
        system_prompt: 'You design clean system components.',
        max_history_turns: 20,
        extra: {}
      }
    ];
  }

  try {
    for (const agent of agents) {
      await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(agent)
      });
    }
    notify(`Loaded preset team successfully!`);
    await loadAgentConfig();
    fetchAgentStatus();
  } catch (err) {
    console.error("Failed to load preset team", err);
    notify("Error loading preset team", true);
  } finally {
    select.value = '';
  }
}

function closeProjectModal() {
  const modal = document.getElementById('projectOpenModal');
  if (modal) modal.style.display = 'none';
}

async function submitProjectPath() {
  const input = document.getElementById('projectPathInput');
  const path = input ? input.value.trim() : '';
  if (!path) return false;
  const ok = await openProject(path);
  if (ok) closeProjectModal();
  return ok;
}

async function openProject(pathOverride = '') {
  const path = (pathOverride || '').trim();
  if (!path) {
    const modal = document.getElementById('projectOpenModal');
    const input = document.getElementById('projectPathInput');
    if (input) {
      input.value = currentProjectPath || '';
      input.focus();
      input.select();
    }
    renderRecentProjects();
    if (modal) modal.style.display = 'flex';
    return false;
  }

  const response = await fetch('/project/open', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({path})
  });
  const data = await response.json();
  if (!response.ok) { notify(data.detail || 'Could not open project', true); return false; }
  rememberRecentProject(path);
  applyProjectState(data);
  currentWsKey = 'cockpit';
  await loadAgentConfig();
  renderHistory(data.recent_runs || []);
  await fetchAgentStatus();
  notify(data.brief ? 'Project opened · design brief loaded' : 'Project opened');
  
  // Refresh workspace to load files
  if (typeof refreshWorkspace === 'function') refreshWorkspace();
  
  return true;
}

async function loadCurrentProject() {
  renderRecentProjects();
  const data = await fetch('/project').then(r=>r.json());
  applyProjectState(data);
  if (data.open && data.path) rememberRecentProject(data.path);
  if (!data.open) return;
  renderHistory(data.recent_runs || []);
  await loadAgentConfig();
}
