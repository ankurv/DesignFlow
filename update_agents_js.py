import re

with open('frontend/js/agents.js', 'r') as f:
    js = f.read()

# 1. Remove globalAgentConfigs
js = re.sub(r'let globalAgentConfigs = \[\];\n', '', js)

# 2. Fix editingAgentId comment
js = js.replace("let editingAgentId = null; // String format: 'global-<id>' or 'project-<id>'", "let editingAgentId = null;")

# 3. Update loadAgentConfig
js = re.sub(
    r"globalAgentConfigs = res\.global \|\| \[\];\s*projectAgentConfigs = res\.project \|\| \[\];",
    r"projectAgentConfigs = res.agents || [];",
    js
)

# 4. Remove scopeLabel and Global Scope logic from renderAgentCards
js = re.sub(
    r"  const scopeLabel = document\.getElementById\('scopeLabel'\);\s*if \(projectOpen\) \{\s*scopeLabel\.textContent = `Project Scope: \$\{escHtml\(currentProjectPath\.split\('/'\)\.pop\(\)\)\}`;\s*\} else \{\s*scopeLabel\.textContent = `Global Scope \(No Project Open\)`;\s*\}",
    "",
    js
)

# 5. Completely rewrite renderAgentCards core logic
new_render_core = """
  let html = '';
  if (!projectOpen) {
    html += `<div style="color:var(--muted);font-size:12.5px;font-style:italic;margin-bottom:20px;padding:8px 0">Please open a project to configure agents.</div>`;
  } else {
    if (!projectAgentConfigs.length) {
      html += `<div style="color:var(--muted);font-size:12.5px;font-style:italic;margin-bottom:10px;padding:8px 0">No agents configured. Click Add Agent to start.</div>`;
    }
    projectAgentConfigs.forEach((cfg, idx) => {
      html += renderSingleCard(cfg, idx, false);
    });
  }
"""
js = re.sub(r"  // Render Global List.*?\} else \{\s*// Render Project List.*?\}\s*\}", new_render_core, js, flags=re.DOTALL)

# 6. Fix renderSingleCard signature and usage
js = js.replace("function renderSingleCard(cfg, idx, isGlobal)", "function renderSingleCard(cfg, idx)")
js = js.replace("const uid = (isGlobal ? 'global-' : 'project-') + cfg.id;", "const uid = cfg.id;")
js = re.sub(r"\s*const scopeBadge = isGlobal[\s\S]*?;\s*", "", js)
js = js.replace("${scopeBadge}", "")

# 7. Remove overrideForProject and promoteToGlobal entirely
js = re.sub(r"function overrideForProject\(agentId\) \{[\s\S]*?\}[\s\n]*async function promoteToGlobal\(agentId\) \{[\s\S]*?\}", "", js)

# 8. Fix editAgent
js = js.replace("function editAgent(agentId, isGlobal) {", "function editAgent(agentId) {")
js = js.replace("const isGlobal = editingAgentId.startsWith('global-');\n  const agentId = editingAgentId.replace(/^(global|project)-/, '');", "const agentId = editingAgentId;")
js = js.replace("const arr = isGlobal ? globalAgentConfigs : projectAgentConfigs;", "const arr = projectAgentConfigs;")
js = js.replace("editingAgentId = (isGlobal ? 'global-' : 'project-') + agentId;", "editingAgentId = agentId;")

# 9. Fix saveAgent
js = js.replace("const url = (projectOpen && !isGlobal)\n    ? (isNew ? '/agents' : `/agents/${agentId}`)\n    : (isNew ? '/agents/global' : `/agents/global/${agentId}`);", "const url = isNew ? '/agents' : `/agents/${agentId}`;")
js = js.replace("const uid = (isGlobal ? 'global-' : 'project-') + (data.agent?.id || agentId);", "const uid = data.agent?.id || agentId;")

# 10. Fix cancelEditAgent
js = js.replace("function cancelEditAgent(agentId, isGlobal) {", "function cancelEditAgent(agentId) {")

# 11. Fix checkAgentHealth
js = js.replace("async function checkAgentHealth(cfg, uid) {", "async function checkAgentHealth(cfg, uid) {") # keep

# 12. Fix refreshAgentHealth
js = js.replace("window.refreshAgentHealth = async function(uid, isGlobal, idx) {", "window.refreshAgentHealth = async function(uid, idx) {")
js = js.replace("const configs = isGlobal ? globalAgentConfigs : projectAgentConfigs;", "const configs = projectAgentConfigs;")

# 13. Fix deleteAgent
js = js.replace("async function deleteAgent(agentId, isGlobal) {", "async function deleteAgent(agentId) {")
js = js.replace("const url = isGlobal ? `/agents/global/${agentId}` : `/agents/${agentId}`;", "const url = `/agents/${agentId}`;")

# 14. Fix addNewAgentCard
new_add_agent = """function addNewAgentCard() {
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
}"""
js = re.sub(r"function addNewAgentCard\(\) \{[\s\S]*?renderAgentCards\(\);\n\}", new_add_agent, js)

# 15. Fix togglePauseAgent
js = js.replace("async function togglePauseAgent(id, isGlobal, idx) {", "async function togglePauseAgent(id, idx) {")
js = js.replace("const configs = isGlobal ? globalAgentConfigs : projectAgentConfigs;", "const configs = projectAgentConfigs;")
js = js.replace("const url = isGlobal ? `/agents/global/${id}` : `/agents/${id}`;", "const url = `/agents/${id}`;")

# Remove remaining isGlobal references in onclick handlers in renderSingleCard
js = js.replace("onclick=\"editAgent('${cfg.id}', ${isGlobal})\"", "onclick=\"editAgent('${cfg.id}')\"")
js = js.replace("onclick=\"deleteAgent('${cfg.id}', ${isGlobal})\"", "onclick=\"deleteAgent('${cfg.id}')\"")
js = js.replace("onclick=\"cancelEditAgent('${cfg.id}', ${isGlobal})\"", "onclick=\"cancelEditAgent('${cfg.id}')\"")
js = js.replace("refreshAgentHealth('${uid}', ${isGlobal}, ${idx})", "refreshAgentHealth('${uid}', ${idx})")
js = js.replace("togglePauseAgent('${cfg.id}', ${isGlobal}, ${idx})", "togglePauseAgent('${cfg.id}', ${idx})")
js = re.sub(r'isGlobal\s*\?\s*`<button class="btn btn-secondary btn-sm" onclick="overrideForProject\(\'\$\{cfg\.id\}\'\)" title="Customize this agent for this project"\>Customize locally</button>`\s*:\s*`<button class="btn btn-secondary btn-sm" onclick="promoteToGlobal\(\'\$\{cfg\.id\}\'\)" title="Make this agent available globally"\>Copy to Global</button>`,', '', js)

with open('frontend/js/agents.js', 'w') as f:
    f.write(js)

print("Agents JS updated successfully")
