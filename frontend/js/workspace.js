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

window.initializeArchitectDashboard = function() {
  const cockpitPanel = document.getElementById('panel-chat');
  const cockpitHost = document.getElementById('designCockpitView');
  if (!cockpitPanel || !cockpitHost || cockpitHost.dataset.initialized === '1') return;

  const feedArea = cockpitPanel.querySelector('.feed-area');
  if (!feedArea) return;

  cockpitHost.appendChild(feedArea);
  cockpitHost.dataset.initialized = '1';
  cockpitPanel.style.display = 'none';
};

function getMermaidDiagramTitle(rawGraph, idx) {
  const firstLine = (rawGraph || '').split('\n')[0].trim().toLowerCase();
  if (firstLine.startsWith('statediagram')) return `Diagram ${idx + 1} · Lifecycle state flow`;
  if (firstLine.startsWith('flowchart')) return `Diagram ${idx + 1} · Product, UI, and service map`;
  if (firstLine.startsWith('sequencediagram')) return `Diagram ${idx + 1} · Interaction sequence`;
  return `Diagram ${idx + 1}`;
}

function setupMermaidViewport(viewport, canvas, target, controls) {
  const svg = target.querySelector('svg');
  if (!svg) return;

  svg.style.maxWidth = 'none';
  svg.style.display = 'block';
  svg.style.pointerEvents = 'none';

  const viewBox = svg.viewBox && svg.viewBox.baseVal;
  const naturalWidth = (viewBox && viewBox.width) || svg.getBBox().width || svg.getBoundingClientRect().width || 1200;
  const naturalHeight = (viewBox && viewBox.height) || svg.getBBox().height || svg.getBoundingClientRect().height || 720;
  const viewportMaxHeight = Math.min(Math.round(window.innerHeight * 0.68), 720);
  const viewportMinHeight = 220;
  const viewportPadding = 56;
  const desiredHeight = Math.max(viewportMinHeight, Math.min(Math.round(naturalHeight + viewportPadding), viewportMaxHeight));
  viewport.style.height = `${desiredHeight}px`;

  const state = { scale: 1, minScale: 0.2, maxScale: 2.4, isPanning: false, startX: 0, startY: 0, startLeft: 0, startTop: 0 };

  function updateZoomLabel() {
    if (controls?.zoomLabel) controls.zoomLabel.textContent = `${Math.round(state.scale * 100)}%`;
  }

  function applyScale() {
    svg.style.width = `${naturalWidth * state.scale}px`;
    svg.style.height = `${naturalHeight * state.scale}px`;
    updateZoomLabel();
  }

  function centerViewport() {
    const vw = viewport.clientWidth || 1;
    const vh = viewport.clientHeight || 1;
    const contentW = naturalWidth * state.scale;
    const contentH = naturalHeight * state.scale;
    viewport.scrollLeft = Math.max(0, (contentW - vw) / 2);
    viewport.scrollTop = Math.max(0, (contentH - vh) / 2);
  }

  function fitToView() {
    const vw = viewport.clientWidth || 1;
    const vh = viewport.clientHeight || 1;
    const scale = Math.min((vw - 48) / naturalWidth, (vh - 48) / naturalHeight, 1);
    state.scale = Math.max(state.minScale, Math.min(scale, state.maxScale));
    applyScale();
    centerViewport();
    return state.scale;
  }

  function setScale(nextScale, originX = null, originY = null) {
    const bounded = Math.max(state.minScale, Math.min(nextScale, state.maxScale));
    if (Math.abs(bounded - state.scale) < 0.001) return;

    const previousScale = state.scale;
    if (originX == null || originY == null) {
      state.scale = bounded;
      applyScale();
      centerViewport();
      return;
    }

    const contentX = (viewport.scrollLeft + originX) / previousScale;
    const contentY = (viewport.scrollTop + originY) / previousScale;
    state.scale = bounded;
    applyScale();
    viewport.scrollLeft = Math.max(0, contentX * state.scale - originX);
    viewport.scrollTop = Math.max(0, contentY * state.scale - originY);
  }

  viewport.addEventListener('wheel', (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const originX = event.clientX - rect.left;
    const originY = event.clientY - rect.top;
    const factor = Math.exp(-event.deltaY * 0.01);
    setScale(state.scale * factor, originX, originY);
  }, { passive: false });

  viewport.addEventListener('dblclick', (event) => {
    const rect = viewport.getBoundingClientRect();
    const originX = event.clientX - rect.left;
    const originY = event.clientY - rect.top;
    const nextScale = state.scale < 1.35 ? Math.max(1.4, state.scale * 1.35) : fitToView();
    if (nextScale != null) setScale(nextScale, originX, originY);
  });

  viewport.addEventListener('mousedown', (event) => {
    state.isPanning = true;
    state.startX = event.clientX;
    state.startY = event.clientY;
    state.startLeft = viewport.scrollLeft;
    state.startTop = viewport.scrollTop;
    viewport.classList.add('is-panning');
    event.preventDefault();
  });

  window.addEventListener('mousemove', (event) => {
    if (!state.isPanning) return;
    viewport.scrollLeft = state.startLeft - (event.clientX - state.startX);
    viewport.scrollTop = state.startTop - (event.clientY - state.startY);
  });

  window.addEventListener('mouseup', () => {
    if (!state.isPanning) return;
    state.isPanning = false;
    viewport.classList.remove('is-panning');
  });

  controls?.zoomIn?.addEventListener('click', () => setScale(state.scale * 1.15, viewport.clientWidth / 2, viewport.clientHeight / 2));
  controls?.zoomOut?.addEventListener('click', () => setScale(state.scale / 1.15, viewport.clientWidth / 2, viewport.clientHeight / 2));
  controls?.fit?.addEventListener('click', fitToView);
  controls?.reset?.addEventListener('click', () => setScale(1));

  fitToView();
  window.addEventListener('resize', fitToView);
}


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
  const isRootFile = ['context', 'design', 'plan', 'decisions', 'questions', 'logbook'].includes(filename);
  
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
  const designCockpitView = document.getElementById('designCockpitView');

  if (key === 'cockpit') {
    if (designCockpitView) designCockpitView.style.display = 'flex';
    if (dashboardView) dashboardView.style.display = 'none';
    if (fileViewContainer) fileViewContainer.style.display = 'none';
    if (typeof fetchAgentStatus === 'function') fetchAgentStatus();
    return;
  }

  if (key === 'dashboard') {
    if (designCockpitView) designCockpitView.style.display = 'none';
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
            const title = getMermaidDiagramTitle(rawGraph, idx);

            const wrapper = document.createElement('section');
            wrapper.className = 'mermaid-diagram-card';

            const header = document.createElement('div');
            header.className = 'mermaid-diagram-header';
            header.innerHTML = `
              <div>
                <div class="mermaid-diagram-title">${escHtml(title)}</div>
                <div class="mermaid-diagram-hint">Drag to pan · Pinch or ⌘/Ctrl+scroll to zoom · Double-click to zoom</div>
              </div>
              <div class="mermaid-diagram-actions">
                <button class="btn btn-secondary mermaid-control-btn" type="button" data-action="zoom-out">−</button>
                <span class="mermaid-zoom-label">100%</span>
                <button class="btn btn-secondary mermaid-control-btn" type="button" data-action="zoom-in">+</button>
                <button class="btn btn-secondary mermaid-control-btn" type="button" data-action="fit">Fit</button>
                <button class="btn btn-secondary mermaid-control-btn" type="button" data-action="reset">100%</button>
                <button class="btn btn-secondary mermaid-control-btn" type="button" data-action="copy">Copy Code</button>
              </div>
            `;

            const viewport = document.createElement('div');
            viewport.className = 'mermaid-viewport';

            const canvas = document.createElement('div');
            canvas.className = 'mermaid-canvas';

            const target = document.createElement('div');
            target.className = 'mermaid mermaid-figure';
            target.textContent = rawGraph;

            canvas.appendChild(target);
            viewport.appendChild(canvas);
            wrapper.appendChild(header);
            wrapper.appendChild(viewport);
            diagramsContainer.appendChild(wrapper);

            const controls = {
              zoomIn: header.querySelector('[data-action="zoom-in"]'),
              zoomOut: header.querySelector('[data-action="zoom-out"]'),
              fit: header.querySelector('[data-action="fit"]'),
              reset: header.querySelector('[data-action="reset"]'),
              copy: header.querySelector('[data-action="copy"]'),
              zoomLabel: header.querySelector('.mermaid-zoom-label'),
            };

            controls.copy?.addEventListener('click', () => {
              navigator.clipboard.writeText(rawGraph);
              notify('Copied diagram source code!');
            });

            if (window.mermaid) {
              try {
                mermaid.run({ nodes: [target] });
                requestAnimationFrame(() => requestAnimationFrame(() => setupMermaidViewport(viewport, canvas, target, controls)));
              } catch (mErr) {
                console.error('Failed to render Mermaid graph', mErr);
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
    if (designCockpitView) designCockpitView.style.display = 'none';
    if (dashboardView) dashboardView.style.display = 'none';
    if (fileViewContainer) fileViewContainer.style.display = 'flex';

    let content = '';
    try {
      if (['context','design','plan','decisions','questions','logbook'].includes(key)) {
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

  const briefButton = document.getElementById('wsbtn-brief');
  if (briefButton) {
    briefButton.style.display = (ws.src_files || []).includes('DESIGNFLOW.md') ? 'flex' : 'none';
  }
  
  // Update sidebar buttons based on whether core files exist
  const coreFiles = ['context', 'design', 'plan', 'decisions', 'questions', 'logbook'];
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
  srcList.innerHTML = (ws.src_files||[]).filter(f => f !== 'DESIGNFLOW.md').map(f =>
    `<button class="ws-file-btn" onclick="loadWsFile(decodeURIComponent('${encodeURIComponent(f)}'))">${getFileIcon(f)} ${escHtml(f)}</button>`
  ).join('');
  
  if (currentWsKey !== 'cockpit' && currentWsKey !== 'dashboard' && coreFiles.includes(currentWsKey)) {
    if (ws[currentWsKey] === '(empty)' || !ws[currentWsKey]) {
      currentWsKey = 'cockpit';
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

window.generateVisualDesign = async function() {
  const prompt = "Update DESIGN.md directly: preserve its existing content and add a clear Mermaid architecture diagram based on the current project. This is a bounded document edit; use one agent and do not start a debate.";
  const button = document.querySelector('[onclick="generateVisualDesign()"]');
  if (button) {
    button.disabled = true;
    button.textContent = 'Starting…';
  }
  try {
    const statusRes = await fetch('/run/status');
    if (!statusRes.ok) throw new Error('Could not read run status');
    const status = (await statusRes.json()).status || 'idle';
    if (!['idle', 'done', 'error'].includes(status)) {
      notify('Finish or stop the active run before generating a visual design.', true);
      return;
    }
    const started = await startRun(prompt, {hiddenPrompt: true});
    if (!started) return;
    notify('Visual design generation started. DESIGN.md will update automatically.');
    const architectTab = Array.from(document.querySelectorAll('.tab')).find(t => t.textContent.includes('Architect Dashboard'));
    if (architectTab) architectTab.click();
    if (typeof loadWsFile === 'function') loadWsFile('cockpit');
  } catch (err) {
    notify(err.message || 'Could not start visual design generation.', true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Generate Visual Design';
    }
  }
};
