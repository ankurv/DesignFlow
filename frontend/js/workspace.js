// ── Workspace ─────────────────────────────────────────────────────────────────
let currentFileContent = '';

function getFileIcon(f) {
  const ext = f.split('.').pop().toLowerCase();
  if (['py'].includes(ext)) return '<span class="file-icon" style="color:#38bdf8">🐍</span>';
  if (['js','ts'].includes(ext)) return '<span class="file-icon" style="color:#f59e0b">⚡</span>';
  if (['html'].includes(ext)) return '<span class="file-icon" style="color:#f97316">🌐</span>';
  if (['css'].includes(ext)) return '<span class="file-icon" style="color:#6366f1">🎨</span>';
  if (['json'].includes(ext)) return '<span class="file-icon" style="color:#14b8a6">📦</span>';
  if (['md'].includes(ext)) return '<span class="file-icon" style="color:#818cf8">📝</span>';
  if (['db','sqlite'].includes(ext)) return '<span class="file-icon" style="color:#94a3b8">💾</span>';
  return '<span class="file-icon" style="color:var(--muted)">📄</span>';
}

let monacoEditorInstance = null;

function getMonacoLanguage(filename) {
  const ext = filename.split('.').pop().toLowerCase();
  if (['py'].includes(ext)) return 'python';
  if (['js', 'ts'].includes(ext)) return 'javascript';
  if (['html'].includes(ext)) return 'html';
  if (['css'].includes(ext)) return 'css';
  if (['json'].includes(ext)) return 'json';
  if (['md'].includes(ext)) return 'markdown';
  return 'plaintext';
}

function renderFileContent(filename, content) {
  currentFileContent = content;
  const container = document.getElementById('fileViewContainer');
  if (!container) return;

  if (content === undefined || content === null) {
    container.innerHTML = '<div style="color:var(--muted);font-size:12.5px;font-style:italic">Select a file to view its contents.</div>';
    return;
  }

  container.innerHTML = `
    <div class="ws-content-header">
      <span class="ws-content-path">${escHtml(filename)}</span>
      <div style="display:flex; gap:8px;">
        <button class="btn btn-primary" id="wsEditBtn" onclick="startFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Edit</button>
        <button class="btn btn-secondary" onclick="navigator.clipboard.writeText(monacoEditorInstance ? monacoEditorInstance.getValue() : currentFileContent);notify('Copied file contents to clipboard!')" style="padding: 4px 10px; font-size: 11px">Copy</button>
      </div>
    </div>
    <div id="monacoContainer" style="width:100%; height:calc(100% - 40px);"></div>
  `;

  if (window.monacoReady && window.monaco) {
    monacoEditorInstance = monaco.editor.create(document.getElementById('monacoContainer'), {
      value: content,
      language: getMonacoLanguage(filename),
      theme: 'vs-dark',
      readOnly: true,
      minimap: { enabled: false },
      automaticLayout: true,
      scrollBeyondLastLine: false,
      fontSize: 13
    });
  } else {
    document.getElementById('monacoContainer').innerHTML = '<div style="padding:20px; color:var(--muted)">Loading editor...</div>';
    window.onMonacoReady = () => {
      renderFileContent(filename, currentFileContent);
    };
  }
}

function startFileEdit(filename) {
  const header = document.querySelector('.ws-content-header');
  if (header) {
    header.innerHTML = `
      <span class="ws-content-path">Editing: ${escHtml(filename)}</span>
      <div style="display:flex; gap:8px">
        <button class="btn btn-primary" onclick="saveFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Save</button>
        <button class="btn btn-secondary" onclick="cancelFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Cancel</button>
      </div>
    `;
  }
  if (monacoEditorInstance) {
    monacoEditorInstance.updateOptions({ readOnly: false });
  }
}

function cancelFileEdit(filename) {
  renderFileContent(filename, currentFileContent);
}

async function saveFileEdit(filename) {
  const newContent = monacoEditorInstance ? monacoEditorInstance.getValue() : currentFileContent;
  const isRootFile = ['design', 'plan', 'decisions', 'consensus', 'tests', 'questions', 'logbook'].includes(filename);
  
  const encodedName = filename.split('/').map(encodeURIComponent).join('/');
  const url = isRootFile ? `/workspace/file/${filename}` : `/workspace/src/${encodedName}`;
  
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: newContent })
    });
    
    if (res.ok) {
      notify('File saved successfully!');
      currentFileContent = newContent;
      renderFileContent(filename, currentFileContent);
    } else {
      const err = await res.json();
      alert('Failed to save file: ' + (err.detail || JSON.stringify(err)));
    }
  } catch (err) {
    console.error(err);
    alert('Failed to save file: ' + err.message);
  }
}

async function createNewFile() {
  if (!projectOpen) {
    notify('Open a project first', true);
    return;
  }
  const filename = prompt('Enter new file path (e.g. src/utils.py):');
  if (!filename || !filename.trim()) return;
  
  const encodedName = filename.trim().split('/').map(encodeURIComponent).join('/');
  try {
    const res = await fetch(`/workspace/src/${encodedName}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: '' })
    });
    
    if (res.ok) {
      notify('File created successfully!');
      await refreshWorkspace();
      await loadWsFile(filename.trim());
      startFileEdit(filename.trim());
    } else {
      const err = await res.json();
      alert('Failed to create file: ' + (err.detail || JSON.stringify(err)));
    }
  } catch (err) {
    console.error(err);
    alert('Failed to create file: ' + err.message);
  }
}

async function loadWsFile(key) {
  currentWsKey = key;
  document.querySelectorAll('.ws-file-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('wsbtn-'+key);
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.ws-file-btn').forEach(b => {
    if (b.textContent.includes(key)) b.classList.add('active');
  });

  const dashboardView = document.getElementById('dashboardView');
  const fileViewContainer = document.getElementById('fileViewContainer');

  if (key === 'dashboard') {
    if (dashboardView) dashboardView.style.display = 'flex';
    if (fileViewContainer) fileViewContainer.style.display = 'none';

    try {
      const designRes = await fetch('/workspace/file/design').then(r=>r.json());
      const planRes = await fetch('/workspace/file/plan').then(r=>r.json());
      const decisionsRes = await fetch('/workspace/file/decisions').then(r=>r.json()).catch(() => ({ content: '' }));

      // Parse and Render Mermaid Diagram(s) if present in DESIGN.md
      const mapContainer = document.getElementById('mermaidMapContainer');
      const diagramsContainer = document.getElementById('mermaidDiagramsContainer');
      const designContent = designRes.content || '';
      const decisionsContent = decisionsRes.content || '';
      const decisionMemoryBody = document.getElementById('decisionMemoryBody');
      if (decisionMemoryBody) {
        const trimmed = decisionsContent.trim();
        decisionMemoryBody.innerHTML = trimmed && trimmed !== '(empty)'
          ? parseMarkdown(trimmed)
          : '<div style="color:var(--muted);font-style:italic;">No key decisions captured yet. Ask the team to maintain DECISIONS.md as it debates trade-offs.</div>';
      }
      
      const mermaidMatches = [...designContent.matchAll(/```mermaid\n([\s\S]*?)```/g)];
      
      if (diagramsContainer && mapContainer) {
        mapContainer.style.display = 'flex';
        diagramsContainer.innerHTML = '';
        
        if (mermaidMatches.length > 0) {
          mermaidMatches.forEach((match, idx) => {
          const rawGraph = match[1].trim();
          
          const wrapper = document.createElement('div');
          wrapper.style.cssText = 'display:flex; flex-direction:column; background:#090c12; padding:16px; border-radius:6px; overflow:auto; position:relative';
          
          const btnGroup = document.createElement('div');
          btnGroup.style.cssText = 'position:absolute; top:8px; right:8px; display:flex; gap:8px; z-index:10';
          btnGroup.innerHTML = `<button class="btn btn-secondary" onclick="navigator.clipboard.writeText(\`${rawGraph.replace(/`/g, '\\`').replace(/\$/g, '$$$$')}\`); notify('Copied diagram source code!')" style="padding: 4px 10px; font-size: 11px">Copy Code</button>`;
          wrapper.appendChild(btnGroup);
          
          const target = document.createElement('div');
          target.className = 'mermaid';
          target.style.cssText = 'display:flex; justify-content:center; align-items:center; width:100%';
          target.textContent = rawGraph;
          
          wrapper.appendChild(target);
          diagramsContainer.appendChild(wrapper);
          
          if (window.mermaid) {
            try {
              mermaid.run({ nodes: [target] });
            } catch (mErr) {
              console.error("Failed to render Mermaid graph", mErr);
              target.innerHTML = `<div style="color:var(--red);font-size:12px;font-family:var(--font)">Diagram parse error: ${escHtml(mErr.message)}</div>`;
            }
          }
        });
        } else {
          // Empty State
          diagramsContainer.innerHTML = `
            <div style="padding: 24px; text-align:center; color:var(--muted); font-size:13px; border: 1px dashed var(--border); border-radius: 8px;">
              <div style="margin-bottom:12px">No visual architecture diagrams found in DESIGN.md</div>
              <button class="btn btn-secondary" onclick="generateVisualDesign()" style="padding:6px 12px">Generate Visual Design</button>
            </div>
          `;
        }
      }
    } catch (err) {
      console.error("Failed to load dashboard files", err);
    }
  } else {
    if (dashboardView) dashboardView.style.display = 'none';
    if (fileViewContainer) fileViewContainer.style.display = 'flex';

    let content = '';
    try {
      if (['design','plan','decisions','consensus','tests','questions','logbook'].includes(key)) {
        const res = await fetch(`/workspace/file/${key}`).then(r=>r.json());
        content = res.content;
      } else {
        const encoded = key.split('/').map(encodeURIComponent).join('/');
        const res = await fetch(`/workspace/src/${encoded}`).then(r=>r.json());
        content = res.content;
      }
      renderFileContent(key, content);
    } catch (err) {
      console.error("Failed to load file contents", err);
      renderFileContent(key, null);
    }
  }
}

async function refreshWorkspace() {
  const ws = await fetch('/workspace').then(r=>r.json());
  
  // Update sidebar buttons based on whether core files exist
  const coreFiles = ['design', 'plan', 'decisions', 'consensus', 'questions', 'tests', 'logbook'];
  coreFiles.forEach(key => {
    const btn = document.getElementById(`wsbtn-${key}`);
    if (btn) {
      if (ws[key] === '(empty)' || !ws[key]) {
        btn.style.display = 'none';
      } else {
        btn.style.display = 'flex';
      }
    }
  });
  
  const srcList = document.getElementById('srcFileList');
  srcList.innerHTML = (ws.src_files||[]).map(f =>
    `<button class="ws-file-btn" onclick="loadWsFile(decodeURIComponent('${encodeURIComponent(f)}'))">${getFileIcon(f)} ${escHtml(f)}</button>`
  ).join('');
  
  // Only load if the current key exists or is dashboard
  if (currentWsKey !== 'dashboard' && coreFiles.includes(currentWsKey)) {
    if (ws[currentWsKey] === '(empty)' || !ws[currentWsKey]) {
      currentWsKey = 'dashboard';
    }
  }
  await loadWsFile(currentWsKey);
}

async function loadRunHistory() {
  const data = await fetch('/runs').then(r=>r.json());
  renderHistory(data.runs || []);
}

function renderHistory(runs) {
  const grid = document.getElementById('historyGrid');
  if (!runs.length) {
    grid.innerHTML = '<div class="empty-state">No saved runs for this project yet.</div>';
    return;
  }
  grid.innerHTML = runs.map(run => `<div class="run-card">
    <div class="run-card-title">${escHtml(run.idea)}</div>
    <div class="run-card-meta">
      <div>${escHtml(run.status)} · ${new Date(run.started_at).toLocaleString()}</div>
      <div>${Number(run.total_tokens||0).toLocaleString()} tokens · ${formatCost(run.estimated_cost_usd||0)}</div>
      <div>run ${escHtml(run.run_id)}</div>
    </div>
  </div>`).join('');
}

window.generateVisualDesign = function() {
  const steerInput = document.getElementById('steerInput');
  const chatTabBtn = document.getElementById('tab-chat'); // Assuming id or querySelector
  if (steerInput) {
    steerInput.value = "Review the current project state and update DESIGN.md to include a comprehensive visual architecture diagram using Mermaid.js.";
    if (window.sendSteer) window.sendSteer();
    
    // Switch to Chat tab
    const chatTab = Array.from(document.querySelectorAll('.tab')).find(t => t.textContent.includes('Live Feed'));
    if (chatTab) chatTab.click();
  }
};

