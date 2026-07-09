// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(id, tabElement) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  if (tabElement) tabElement.classList.add('active');
  if (id === 'workspace') refreshWorkspace();
  if (id === 'config') loadAgentConfig();
  if (id === 'mcp') loadMCPServers();
  if (id === 'history') loadRunHistory();
  if (id === 'chat') fetchAgentStatus();
}

// ── Init ──────────────────────────────────────────────────────────────────────
if (window.mermaid) {
  mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
}
connectSSE();
loadCurrentProject();
loadAgentConfig();
loadMCPServers();
fetchAgentStatus();
setInterval(fetchAgentStatus, 4000);
