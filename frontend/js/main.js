// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(id, tabElement) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.remove('active');
    t.setAttribute('aria-selected', 'false');
  });
  document.getElementById('panel-'+id).classList.add('active');
  if (tabElement) {
    tabElement.classList.add('active');
    tabElement.setAttribute('aria-selected', 'true');
  }
  if (id === 'workspace') {
    refreshWorkspace();
    fetchAgentStatus();
  }
  if (id === 'config') loadAgentConfig();
  if (id === 'mcp') loadMCPServers();
  if (id === 'history') loadRunHistory();
}

// ── Init ──────────────────────────────────────────────────────────────────────
if (window.initializeArchitectDashboard) {
  window.initializeArchitectDashboard();
}

window.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const u = params.get('auto_user');
    const p = params.get('auto_pass');
    
    if (u && p && !sessionStorage.getItem('designflow_session_id')) {
        const uInput = document.getElementById('loginUsername');
        const pInput = document.getElementById('loginPassword');
        if (uInput && pInput) {
            uInput.value = u;
            pInput.value = p;
            submitLogin();
        }
    }

    const isVsCode = params.get('vscode_mode');
    if (isVsCode === 'true') {
        document.body.classList.add('vscode-mode');
        showTab('chat');
        
        const vscodeProject = params.get('vscode_project');
        if (vscodeProject) {
            const input = document.getElementById('projectPathInput');
            if (input) {
                input.value = vscodeProject;
                // Wait briefly for main.js functions to fully load if necessary, 
                // though submitProjectPath is in api.js which is loaded before main.js
                setTimeout(() => submitProjectPath(), 100);
            }
        }
    }
});
if (window.mermaid) {
  mermaid.initialize({
    startOnLoad: false,
    theme: 'base',
    securityLevel: 'loose',
    themeVariables: {
      background: '#0a0f1a',
      primaryColor: 'rgba(118, 131, 255, 0.08)',
      primaryTextColor: '#e8eef7',
      primaryBorderColor: 'rgba(148, 163, 184, 0.22)',
      secondaryColor: 'rgba(255, 255, 255, 0.03)',
      secondaryTextColor: '#e8eef7',
      secondaryBorderColor: 'rgba(148, 163, 184, 0.18)',
      tertiaryColor: 'rgba(118, 131, 255, 0.05)',
      tertiaryTextColor: '#dbe5f3',
      tertiaryBorderColor: 'rgba(148, 163, 184, 0.16)',
      lineColor: '#93a4bc',
      textColor: '#e8eef7',
      mainBkg: 'rgba(10, 15, 26, 0.92)',
      secondBkg: 'rgba(15, 21, 35, 0.94)',
      tertiaryBkg: 'rgba(118, 131, 255, 0.06)',
      clusterBkg: 'rgba(118, 131, 255, 0.05)',
      clusterBorder: 'rgba(148, 163, 184, 0.18)',
      edgeLabelBackground: 'rgba(10, 15, 26, 0.92)',
      labelBoxBkgColor: 'rgba(10, 15, 26, 0.92)',
      labelBoxBorderColor: 'rgba(148, 163, 184, 0.16)',
      fontFamily: 'Plus Jakarta Sans, system-ui, sans-serif'
    }
  });
}
// Bind the browser session to its project runtime before subscribing. Opening
// the stream first leaves it attached to the temporary, unbound session state.
loadCurrentProject().finally(() => connectSSE(true));
loadAgentConfig();
loadMCPServers();
fetchAgentStatus();
setInterval(() => {
  // This reads local run state only. Provider health checks are manual or run
  // once when agents are loaded, because they can consume provider quota.
  if (['running', 'paused', 'needs_attention'].includes(appStatus)) fetchAgentStatus();
}, 4000);

// Keep this tab's project lease alive. If the tab disappears, the server
// expires the binding and stops the project after the collaboration grace period.
setInterval(() => {
  if (sessionStorage.getItem('designflow_session_id')) {
    fetch('/session/heartbeat', {method: 'POST'}).catch(() => {});
  }
}, 20000);
