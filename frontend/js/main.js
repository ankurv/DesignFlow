// ── Tabs ──────────────────────────────────────────────────────────────────────
loadAppVersion();

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
loadCurrentProject().finally(() => {
  connectSSE(true);
  if (!projectOpen) {
    loadAgentConfig();
    loadMCPServers();
    fetchAgentStatus(false);
  }
});
setInterval(() => {
  // This reads local run state only. Provider health checks are manual or run
  // once when agents are loaded, because they can consume provider quota.
  if (['running', 'paused', 'needs_attention'].includes(appStatus)) fetchAgentStatus();
}, 4000);

// Keep this tab's project lease alive. If the tab disappears, the server
// expires the binding and stops the project after the collaboration grace period.
setInterval(() => {
  if (sessionStorage.getItem('designflow_session_id')) {
    fetch('/session/heartbeat', {method: 'POST'})
      .then(res => {
        if (res.status === 401) {
          sessionStorage.removeItem('designflow_session_id');
        }
      })
      .catch(() => {});
  }
}, 20000);

// ── Custom Dialogs ─────────────────────────────────────────────────────────────
window.appDialog = function(options) {
  return new Promise((resolve) => {
    const overlay = document.getElementById('customDialogOverlay');
    const titleEl = document.getElementById('customDialogTitle');
    const msgEl = document.getElementById('customDialogMessage');
    const inputWrapper = document.getElementById('customDialogInputWrapper');
    const inputEl = document.getElementById('customDialogInput');
    const cancelBtn = document.getElementById('customDialogCancelBtn');
    const confirmBtn = document.getElementById('customDialogConfirmBtn');

    titleEl.textContent = options.title || 'Notification';
    msgEl.textContent = options.message || '';
    
    // Configure buttons
    cancelBtn.style.display = options.showCancel ? 'inline-flex' : 'none';
    confirmBtn.textContent = options.confirmText || 'OK';
    if (options.isDanger) {
      confirmBtn.className = 'btn btn-danger';
    } else {
      confirmBtn.className = 'btn btn-primary';
    }

    // Configure Input
    if (options.isPrompt) {
      inputWrapper.style.display = 'block';
      inputEl.value = options.defaultValue || '';
    } else {
      inputWrapper.style.display = 'none';
      inputEl.value = '';
    }

    overlay.style.display = 'flex';
    if (options.isPrompt) inputEl.focus();

    const cleanup = () => {
      overlay.style.display = 'none';
      cancelBtn.onclick = null;
      confirmBtn.onclick = null;
      inputEl.onkeydown = null;
    };

    cancelBtn.onclick = () => {
      cleanup();
      resolve(options.isPrompt ? null : false);
    };

    const submit = () => {
      cleanup();
      resolve(options.isPrompt ? inputEl.value : true);
    };

    confirmBtn.onclick = submit;
    
    if (options.isPrompt) {
      inputEl.onkeydown = (e) => {
        if (e.key === 'Enter') submit();
        if (e.key === 'Escape') cancelBtn.onclick();
      };
    }
  });
};

window.appAlert = function(message, title = 'Notification') {
  return window.appDialog({ title, message, showCancel: false });
};

window.appConfirm = function(message, title = 'Confirm', confirmText = 'Confirm', isDanger = false) {
  return window.appDialog({ title, message, showCancel: true, confirmText, isDanger });
};

window.appPrompt = function(message, title = 'Input Required', defaultValue = '') {
  return window.appDialog({ title, message, showCancel: true, isPrompt: true, defaultValue, confirmText: 'Submit' });
};
