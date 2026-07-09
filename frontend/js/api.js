
// ── Auth & 401 Interceptor ──────────────────────────────────────────────────
const originalFetch = window.fetch;
window.fetch = async function(...args) {
    if (args.length === 1 && typeof args[0] === 'string') {
        args.push({ credentials: 'same-origin' });
    } else if (args.length === 2 && typeof args[1] === 'object') {
        args[1].credentials = args[1].credentials || 'same-origin';
    }
    const response = await originalFetch(...args);
    if (response.status === 401 || response.status === 403) {
        document.getElementById('loginModal').style.display = 'flex';
        // We do not set loginError here because a 401 on initial load is expected and isn't an "error" the user made.
    }
    return response;
};

async function submitLogin() {
    const u = document.getElementById('loginUsername').value;
    const p = document.getElementById('loginPassword').value;
    
    // Bypass the wrapper so we can handle 401 manually for the login route itself
    const res = await originalFetch('/auth/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: u, password: p})
    });
    
    if (res.ok) {
        document.getElementById('loginModal').style.display = 'none';
        document.getElementById('loginError').textContent = '';
        document.getElementById('logoutBtn').style.display = 'inline-block';
        // Reload page to re-fetch projects and events
        window.location.reload();
    } else {
        const data = await res.json().catch(()=>({}));
        document.getElementById('loginError').textContent = data.detail || "Login failed";
    }
}

async function submitLogout() {
    await fetch('/auth/logout', { method: 'POST' });
    window.location.reload();
}

// ── SSE connection ───────────────────────────────────────────────────────────

const WORKFLOW_TEMPLATES = {
  new_design: `Design a fresh solution for this project. Create or refresh DESIGN.md, PLAN.md, and DECISIONS.md with a practical MVP-first approach.`,
  refine_plan: `Review the current DESIGN.md, PLAN.md, and DECISIONS.md. Refine the existing plan without restarting from scratch. Tighten sequencing, reduce ambiguity, and call out exactly what changed.`,
  resolve_issue: `We hit an issue while following the current plan. Keep the existing design unless it must change. Identify the root cause, update only the affected parts of DESIGN.md / PLAN.md / DECISIONS.md, and add a short "what changed and why" summary.`,
  redebate_decision: `Re-open one important architectural or product decision from DECISIONS.md. Challenge the current choice, compare alternatives, recommend the best option now, and update DESIGN.md / PLAN.md / DECISIONS.md consistently.`,
  simplify_scope: `Simplify the current solution to the smallest credible version. Remove non-essential scope, preserve the core user outcome, and update DESIGN.md / PLAN.md / DECISIONS.md to match the leaner direction.`
};

window.applyWorkflowTemplate = function(key) {
  const template = WORKFLOW_TEMPLATES[key];
  const input = document.getElementById('steerInput');
  if (!template || !input) return;
  input.value = template;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  notify('Prompt template loaded. Add any project-specific detail, then send.');
};

function isCoordinatorEvent(ev) {
  return !!(ev?.data?.is_coordinator || ev?.data?.actor_role === 'coordinator' || ev?.kind === 'phase' && ev?.data?.phase === 'coordinator');
}


const COLLAB_STEER_TEMPLATES = {
  approve: 'The current direction looks good. Keep the overall design and continue refining the best version of this approach.',
  alternatives: 'Before locking this in, show me the strongest 2 alternatives with trade-offs and a recommendation.',
  challenge: 'Challenge the current assumption set. Identify what may be weak or premature and suggest a safer alternative if needed.',
  simplify: 'Simplify this plan further. Preserve the main user outcome but reduce scope, moving parts, and implementation complexity.',
  userflow: 'Evaluate the current direction through the user journey first. Tighten onboarding, reduce friction, and call out UX risks clearly.'
};

window.applyCollabSteer = function(key) {
  const template = COLLAB_STEER_TEMPLATES[key];
  const input = document.getElementById('steerInput');
  if (!template || !input) return;
  input.value = template;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  notify('User involvement prompt loaded. Add any detail you want, then send.');
};

function parseProtocolSection(text, header) {
  if (!text) return '';
  const marker = `## ${header}`;
  const start = text.indexOf(marker);
  if (start === -1) return '';

  const afterMarker = text.slice(start + marker.length).replace(/^\s*/, '');
  const nextHeaderIdx = afterMarker.indexOf('\n## ');
  const body = nextHeaderIdx >= 0 ? afterMarker.slice(0, nextHeaderIdx) : afterMarker;
  return body.trim();
}

function summarizeInstructions(text) {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return '';
  return clean.length > 220 ? clean.slice(0, 217) + '...' : clean;
}

function updateCurrentWorkSummary(fields = {}) {
  const agentName = document.getElementById('contextAgentName');
  const focus = document.getElementById('contextWorkSummary');
  const whyNow = document.getElementById('contextWhyNow');
  const expected = document.getElementById('contextExpectedOutput');
  const needs = document.getElementById('contextNeedsInput');
  if (agentName) agentName.textContent = fields.agentName || 'System';
  if (focus) focus.textContent = fields.summary || 'The team will summarize the current focus here.';
  if (whyNow) whyNow.textContent = fields.whyNow || 'The reason for this step will appear here.';
  if (expected) expected.textContent = fields.expectedOutput || 'The expected output for this step will appear here.';
  if (needs) needs.textContent = fields.needsInput || 'No user action needed right now.';
}


const CHECKPOINT_STEER_TEMPLATES = {
  framing: 'Pause and confirm the current problem framing, assumptions, and constraints with me before going deeper.',
  architecture: 'Before proceeding, summarize the chosen architecture direction, its strongest alternative, and let me confirm the direction.',
  plan: 'Before finalizing, summarize the implementation priorities and sequencing so I can confirm the plan order.'
};

window.applyCheckpointSteer = function(key) {
  const template = CHECKPOINT_STEER_TEMPLATES[key];
  const input = document.getElementById('steerInput');
  if (!template || !input) return;
  input.value = template;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  notify('Checkpoint prompt loaded. Add detail if you want, then send.');
};

function extractDecisionOptions(text) {
  const options = [];
  const bulletRegex = /^\s*-\s*\[([A-Z])\]\s+(.+)$/gm;
  for (const match of text.matchAll(bulletRegex)) {
    options.push({ label: match[1], text: match[2].trim() });
  }
  if (!options.length) {
    const altRegex = /^\s*([A-Z])[\).:-]\s+(.+)$/gm;
    for (const match of text.matchAll(altRegex)) {
      options.push({ label: match[1], text: match[2].trim() });
    }
  }
  const rec = text.match(/recommendation\s*[:\-]\s*([A-Z])/i);
  return { options, recommendation: rec ? rec[1].toUpperCase() : '' };
}

window.useDecisionOption = function(label, text) {
  const input = document.getElementById('contextAnswerInput');
  if (!input) return;
  input.value = `${label}${text ? ' — ' + text : ''}`;
  input.focus();
};

function renderQuestionBody(content) {
  const parsed = extractDecisionOptions(content || '');
  const optionsEl = document.getElementById('contextDecisionOptions');
  if (optionsEl) {
    if (parsed.options.length) {
      optionsEl.style.display = 'flex';
      optionsEl.innerHTML = parsed.options.map(opt => {
        const recommended = parsed.recommendation === opt.label;
        return `<button class="btn ${recommended ? 'btn-primary' : 'btn-secondary'}" onclick="useDecisionOption('${escAttr(opt.label)}','${escAttr(opt.text)}')" style="padding:8px 12px; font-size:12px">${recommended ? 'Recommended · ' : ''}${escHtml(opt.label)} — ${escHtml(opt.text)}</button>`;
      }).join('');
    } else {
      optionsEl.style.display = 'none';
      optionsEl.innerHTML = '';
    }
  }
  return parseMarkdown(content);
}

function connectSSE() {
  const es = new EventSource('/events');
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    handleEvent(ev);
  };
  // EventSource reconnects itself. Creating another instance here multiplies
  // streams and replays history once per connection.
  es.onerror = () => {};
}

function handleEvent(ev) {
  eventCount++;
  document.getElementById('eventCount').textContent = eventCount;

  // Update status
  if (ev.kind === 'phase') {
    if (ev.data.status === 'waiting_for_continuation' || ev.data.status === 'waiting_for_approval' || ev.data.status === 'budget_exhausted') {
      updateStatus('paused');
      if (ev.data.status === 'waiting_for_approval') {
        showInteractiveQuestions();
      }
    } else {
      updateStatus('running');
      const pendingActions = document.getElementById('contextPendingActions');
      if (pendingActions) pendingActions.style.display = 'none';
    }
  }
  if (ev.kind === 'done')  { updateStatus('done'); loadRunHistory(); }
  if (ev.kind === 'error') updateStatus(ev.data.recoverable ? 'needs_attention' : 'error');

  // Parse coordinator turn into user-facing summary cards
  if (ev.kind === 'turn_end' && isCoordinatorEvent(ev)) {
     const text = ev.data.response || '';
     const nextAgent = parseProtocolSection(text, 'NEXT_AGENT');
     const summary = parseProtocolSection(text, 'USER_SUMMARY');
     const whyNow = parseProtocolSection(text, 'WHY_THIS_NOW');
     const expectedOutput = parseProtocolSection(text, 'EXPECTED_OUTPUT');
     const needsUserInput = parseProtocolSection(text, 'NEEDS_USER_INPUT');
     const instructions = parseProtocolSection(text, 'INSTRUCTIONS');

     updateCurrentWorkSummary({
        agentName: nextAgent && nextAgent !== 'NONE' ? nextAgent : 'System',
        summary: summary || summarizeInstructions(instructions) || 'The coordinator did not provide a summary for this step yet.',
        whyNow: whyNow || 'This step is part of the current design refinement sequence.',
        expectedOutput: expectedOutput || 'A clearer design, plan update, or decision recommendation.',
        needsInput: needsUserInput && needsUserInput.toUpperCase() !== 'NONE' ? needsUserInput : 'No user action needed right now.',
     });
  }

  // Render feed item
  appendFeed(ev);

  // Update agent sidebar on turn events
  if (ev.kind === 'turn_start' || ev.kind === 'turn_end' || ev.kind === 'retry' || ev.kind === 'error') {
    fetchAgentStatus();
  }
  
  if (ev.kind === 'turn_end') {
    if (typeof refreshWorkspace === 'function') refreshWorkspace();
    extractLiveInsights(ev);
  }
}

function appendFeed(ev) {
  const feed = document.getElementById('feed');
  const div = document.createElement('div');
  div.className = `feed-item ${ev.kind}`;

  const agentColor = agentColors[ev.agent] || '#64748b';
  const ts = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';

  let summary = '';
  let detail = '';
  let metricsHtml = '';

  switch(ev.kind) {
    case 'phase':
      summary = `Phase: ${ev.data.phase?.toUpperCase() || ''} ${ev.data.status ? '— ' + ev.data.status : ''} ${ev.data.iteration ? 'iter ' + ev.data.iteration : ''} ${ev.data.round ? 'round ' + ev.data.round : ''}`;
      if (ev.data.roles) summary += ' | ' + Object.entries(ev.data.roles).map(([r,a])=>`${a}=${r}`).join(' ');
      break;
    case 'turn_start': {
      const verb = getAgentVerb(ev.agent);
      summary = `${verb}... (${ev.data.turn_id || 'turn'} · attempt ${ev.data.attempt || 1})`;
      break;
    }
    case 'turn_end': {
      const u = ev.data.usage || {};
      const verb = getAgentVerb(ev.agent);
      summary = `${verb} completed (${ev.data.turn_id || 'turn'} · attempt ${ev.data.attempt || 1}) · ${(u.input_tokens||0).toLocaleString()} in, ${(u.output_tokens||0).toLocaleString()} out`;
      detail = ev.data.response || '';
      const totalTok = (u.input_tokens || 0) + (u.output_tokens || 0);
      const turnCost = ev.data.pricing_known ? formatCost(ev.data.cost_usd || 0) : 'unpriced';
      metricsHtml = `<span class="feed-metrics-badge">${totalTok.toLocaleString()} tok · ${turnCost}</span>`;
      break;
    }
    case 'vote':
      summary = `Vote: ${ev.data.vote} (round ${ev.data.round})`;
      break;
    case 'verdict':
      summary = `${ev.data.role?.toUpperCase()} verdict: ${ev.data.verdict}`;
      break;
    case 'consensus':
      summary = ev.data.forced ? '⚠ Forced consensus (max rounds)' : `✓ Consensus reached in round ${ev.data.round}`;
      break;
    case 'file_write':
      summary = `Wrote ${ev.data.file}`;
      detail = ev.data.preview || '';
      if (ev.data.file === 'DESIGN.md') {
        const dashboardBtn = document.getElementById('wsbtn-dashboard');
        if (dashboardBtn && dashboardBtn.classList.contains('active')) {
          loadWsFile('dashboard');
        }
      }
      break;
    case 'steer':
      summary = `Steering injected: "${ev.data.message}"`;
      break;
    case 'retry':
      summary = `${ev.data.turn_id || 'Turn'} waiting · attempt ${ev.data.attempt} · retry in ${formatDuration(ev.data.retry_in_seconds)}`;
      detail = ev.data.reason || '';
      break;
    case 'done':
      summary = '✅ Run complete';
      updateStatus('done');
      break;
    case 'error':
      summary = ev.data.recoverable
        ? `${ev.data.turn_id} failed on attempt ${ev.data.attempt} · fix ${ev.agent} and retry this turn`
        : `Error: ${ev.data.error}`;
      detail = ev.data.error || ev.data.message || '';
      break;
    default:
      summary = JSON.stringify(ev.data).slice(0, 100);
  }

  const avatarChar = (ev.agent || 'SYS').slice(0, 1).toUpperCase();

  div.innerHTML = `
    <div class="feed-row">
      <div class="feed-avatar" style="background:${agentColor}; text-shadow: 0 1px 4px rgba(0,0,0,0.3)">
        ${avatarChar}
      </div>
      <div class="feed-meta">
        <div class="feed-header-line">
          <div class="feed-agent-details">
            <span class="feed-agent">${ev.agent || 'System'}</span>
            <span class="feed-kind">${ev.kind}</span>
            ${metricsHtml}
          </div>
          <span class="feed-ts">${ts}</span>
        </div>
        <div class="feed-text" style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
          <span style="flex: 1">${escHtml(summary)}</span>
          ${detail ? `<button class="btn btn-secondary" style="padding: 2px 8px; font-size: 12px; font-weight: bold; line-height: 1; border-radius: 4px;" onclick="const d = this.parentElement.nextElementSibling; if(d.style.display === 'none'){d.style.display='block';this.innerText='-';}else{d.style.display='none';this.innerText='+';}">${ev.data.verdict === 'PAUSE_FOR_INPUT' ? '-' : '+'}</button>` : ''}
        </div>
        ${detail ? `<div class="feed-detail markdown-body" style="display: ${ev.data.verdict === 'PAUSE_FOR_INPUT' ? 'block' : 'none'}; margin-top: 8px;">${parseMarkdown(detail)}</div>` : ''}
      </div>
    </div>
  `;

  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  if (window.mermaid) { try { mermaid.run({ querySelector: '.mermaid' }); } catch(e) {} }
}


function parseMarkdown(text) {
  if (!text) return '';
  if (!window.marked) return escHtml(text);
  
  const renderer = new marked.Renderer();
  renderer.code = function(code, language, isEscaped) {
    if (language === 'mermaid') {
      return `<div class="mermaid">${escHtml(code)}</div>`;
    }
    return `<pre><code class="language-${escHtml(language || 'plaintext')}">${escHtml(code)}</code></pre>`;
  };
  
  marked.setOptions({ renderer: renderer, gfm: true, breaks: true });
  return marked.parse(text);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function getAgentVerb(agent) {
  const map = {
    'architect': 'Designing',
    'developer': 'Coding',
    'reviewer': 'Reviewing',
    'tester': 'Testing',
    'coordinator': 'Coordinating'
  };
  return map[(agent||'').toLowerCase()] || 'Thinking';
}

function escAttr(s) {
  return escHtml(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Run controls ─────────────────────────────────────────────────────────────
// ── Run controls ─────────────────────────────────────────────────────────────
async function startRun(prompt) {
  let idea = prompt;
  if (idea === undefined) {
    idea = document.getElementById('steerInput').value.trim();
    document.getElementById('steerInput').value = '';
  }

  if (!idea) {
    notify('Please type a prompt/task in the bottom chat input to start the run.', true);
    return;
  }

  if (!projectOpen) {
    const opened = await openProject();
    if (!opened) return;
  }

  const agents = await fetch('/agents').then(r=>r.json());
  const mergedAgents = agents.merged || [];
  if (!mergedAgents.length) { notify('Add at least one agent in the Agents tab', true); return; }

  // Assign colors
  mergedAgents.forEach(a => {
    if (!agentColors[a.name]) agentColors[a.name] = COLORS[colorIdx++ % COLORS.length];
  });
  // Default to debate, but allow the user to override via prompt if they explicitly ask to build
  let mode = "debate";
  totalTokens = 0;
  totalCost = 0;
  eventCount = 0;
  document.getElementById('totalTokens').textContent = '0';
  document.getElementById('totalCachedTokens').textContent = '0';
  document.getElementById('totalCost').textContent = '$0.000000';
  document.getElementById('eventCount').textContent = '0';
  document.getElementById('feed').innerHTML = '';

  const res = await fetch('/run/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      idea,
      project_path: currentProjectPath,
      max_debate_rounds: parseInt(document.getElementById('debateRoundsSlider').value, 10),
      max_tokens: parseInt(document.getElementById('maxTokensInput').value, 10) || 100000,
      max_build_iterations: 10,
      mode: mode,
    })
  });
  const data = await res.json();
  if (res.ok && data.ok) {
    document.getElementById('runId').textContent = data.run_id;
    updateStatus('running');
  } else {
    notify(data.detail || 'Failed to start', true);
  }
}

function formatDuration(seconds) {
  seconds = Number(seconds || 0);
  if (seconds >= 3600) return `${Math.ceil(seconds/3600)}h`;
  if (seconds >= 60) return `${Math.ceil(seconds/60)}m`;
  return `${Math.ceil(seconds)}s`;
}

async function pauseResume() {
  if (paused) {
    await fetch('/run/resume', {method:'POST'});
    paused = false;
    document.getElementById('pauseBtn').textContent = 'Pause';
    updateStatus('running');
  } else {
    await fetch('/run/pause', {method:'POST'});
    paused = true;
    document.getElementById('pauseBtn').textContent = 'Resume';
    updateStatus('paused');
  }
}

async function retryFailedTurn() {
  const response = await fetch('/run/retry', {method:'POST'});
  const data = await response.json();
  if (!response.ok) { notify(data.detail || 'Could not retry the failed turn', true); return; }
  paused = false;
  updateStatus('running');
  notify('Retrying the same failed turn');
}

async function resetRun() {
  if (runStatus === 'running') return;
  if (!confirm("Are you sure you want to reset? This will clear the conversational history (but keep your project files intact).")) return;
  try {
    await fetch('/run/reset', { method: 'POST' });
    document.getElementById('feed').innerHTML = '';
    notify('Run state reset.');
  } catch (err) {
    console.error(err);
    alert('Failed to reset run.');
  }
}

async function stopRun() {
  await fetch('/run/stop', {method:'POST'});
  paused = false;
  updateStatus('idle');
}

function handleSteerInput(event) {
  if (event.key === 'Enter') {
    steer();
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    if (promptHistoryIndex > 0) {
      promptHistoryIndex--;
      document.getElementById('steerInput').value = promptHistory[promptHistoryIndex];
    }
  } else if (event.key === 'ArrowDown') {
    event.preventDefault();
    if (promptHistoryIndex < promptHistory.length - 1) {
      promptHistoryIndex++;
      document.getElementById('steerInput').value = promptHistory[promptHistoryIndex];
    } else {
      promptHistoryIndex = promptHistory.length;
      document.getElementById('steerInput').value = '';
    }
  }
}

async function steer() {
  const msg = document.getElementById('steerInput').value.trim();

  if (msg) {
    if (promptHistory.length === 0 || promptHistory[promptHistory.length - 1] !== msg) {
      promptHistory.push(msg);
    }
    promptHistoryIndex = promptHistory.length;
  }

  if (appStatus === 'idle' || appStatus === 'done' || appStatus === 'error') {
    document.getElementById('steerInput').value = '';
    await startRun(msg);
  } else {
    if (msg) {
      await fetch('/run/steer', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({message: msg})
      });
    }
    document.getElementById('steerInput').value = '';
    if (appStatus === 'paused' || appStatus === 'waiting_for_continuation') {
      await fetch('/run/resume', {method:'POST'});
      paused = false;
      updateStatus('running');
    }
  }
}

function updateStatus(s) {
  appStatus = s;
  paused = (s === 'paused');
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  const pauseBtn = document.getElementById('pauseBtn');
  const stopBtn = document.getElementById('stopBtn');
  const retryBtn = document.getElementById('retryBtn');
  dot.className = `status-dot ${s}`;
  txt.textContent = s;

  // Sync to progress pane status banner
  const pDot = document.getElementById('progressStatusDot');
  const pTxt = document.getElementById('progressStatusText');
  if (pDot && pTxt) {
    const color = {
      idle: 'var(--muted, #8e9cae)',
      running: 'var(--yellow, #eab308)',
      paused: 'var(--yellow, #eab308)',
      needs_attention: 'var(--red, #ef4444)',
      done: 'var(--green, #22c55e)',
      error: 'var(--red, #ef4444)'
    }[s] || 'var(--muted, #8e9cae)';
    pDot.style.background = color;
    pTxt.textContent = s.charAt(0).toUpperCase() + s.slice(1).replace('_', ' ');
  }

  const steerInput = document.getElementById('steerInput');
  const sendBtn = document.getElementById('sendBtn');
  const nameEl = document.getElementById('contextAgentName');
  const stEl = document.getElementById('contextAgentStatus');

  if (s === 'idle' || s === 'done' || s === 'error') {
    if (nameEl) nameEl.textContent = 'Ready to start';
    if (instEl) instEl.textContent = 'Type a high-level feature request below to start the design process.';
    if (stEl) stEl.className = 'status-indicator idle';
    
    if (steerInput) steerInput.placeholder = 'Type a prompt/task here and press Enter to start the run…';
    if (sendBtn) sendBtn.textContent = 'Start Run';
  } else {
    if (stEl) stEl.className = 'status-indicator running';
    
    if (steerInput) {
      if (s === 'paused' || s === 'waiting_for_continuation') {
        steerInput.placeholder = 'Optional steering message... or just press Enter to Resume';
        if (sendBtn) sendBtn.textContent = 'Resume Run';
      } else {
        steerInput.placeholder = 'Steer agents — inject a message into the active run…';
        if (sendBtn) sendBtn.textContent = 'Steer';
      }
    }
  }

  const chips = document.getElementById('debateActionChips');
  if (chips) {
    chips.style.display = (s === 'idle' || s === 'done' || s === 'error') ? 'flex' : 'none';
  }

  const running = s === 'running' || s === 'paused' || s === 'needs_attention';
  if (retryBtn) retryBtn.style.display = s === 'needs_attention' ? '' : 'none';
  if (pauseBtn) {
    pauseBtn.style.display = (s === 'running' || s === 'paused') ? '' : 'none';
    pauseBtn.textContent = s === 'paused' ? 'Resume' : 'Pause';
  }
  if (stopBtn) {
    stopBtn.style.display = running ? '' : 'none';
  }
  const resetBtn = document.getElementById('resetBtn');
  if (resetBtn) {
    resetBtn.style.display = (s === 'done' || s === 'error') ? '' : 'none';
  }
}

async function runChipPrompt(prompt) {
  document.getElementById('steerInput').value = prompt;
  await steer();
}

async function fetchAgentStatus() {
  const res = await fetch('/run/status').then(r=>r.json());
  if (res.status && res.status !== appStatus) {
    updateStatus(res.status);
  }
  let visibleAgents = res.agents || [];
  if (!visibleAgents.length) {
    const configured = await fetch('/agents').then(r=>r.json());
    visibleAgents = (configured.merged || []).map(a => ({...a, status:'idle', total_tokens:0,
      input_tokens:0, cached_input_tokens:0, output_tokens:0, cost_usd:0, pricing_known:false}));
  }
  const list = document.getElementById('agentList');
  const maxTokens = Math.max(1, ...visibleAgents.map(a => a.total_tokens || 0));
  list.innerHTML = visibleAgents.map(a => {
    const color = agentColors[a.name] || '#64748b';
    const statusColor = {thinking:'var(--yellow)',waiting:'var(--yellow)',done:'var(--green)',error:'var(--red)',idle:'var(--muted)'}[a.status] || 'var(--muted)';
    const cost = a.pricing_known ? formatCost(a.cost_usd || 0) : 'cost n/a';
    const cacheUsage = a.cache_reporting === 'unavailable'
      ? `${a.context_reused ? 'session resumed' : 'session new'} · cache usage unreported`
      : `cached ${(a.cached_input_tokens||0).toLocaleString()}`;
    const pct = Math.max(0, Math.min(100, ((a.total_tokens||0) / maxTokens) * 100));
    const retry = a.status === 'waiting' && a.retry_at
      ? `<div class="usage-detail" style="color:var(--yellow)">retry scheduled ${new Date(a.retry_at).toLocaleTimeString()}</div>`
      : a.status === 'error'
        ? `<div class="usage-detail" style="color:var(--red)">${escHtml(a.error_message || 'Turn failed')}</div>`
        : '';
    return `<div class="agent-chip" style="display:block">
      <div style="display:flex;align-items:center;gap:8px">
      <div class="agent-dot" style="background:${color}"></div>
      <div style="flex:1;min-width:0">
        <div class="aname">${escHtml(a.name)}</div>
        <div class="akind">${escHtml(a.role || 'Generalist')} · ${escHtml(a.kind)} ${a.model ? '· '+escHtml(a.model) : ''}</div>
      </div>
      <div style="text-align:right">
        <div class="astatus" style="color:${statusColor}">${a.status}</div>
        <div class="token-count">${(a.total_tokens||0).toLocaleString()} tok · ${cost}</div>
      </div>
      </div>
      <div class="usage-detail">in ${(a.input_tokens||0).toLocaleString()} · ${cacheUsage} · out ${(a.output_tokens||0).toLocaleString()}</div>
      ${retry}
      <div class="usage-track"><div class="usage-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`;
  }).join('');

  totalTokens = (res.agents || []).reduce((sum, a) => sum + (a.total_tokens || 0), 0);
  const cached = (res.agents || []).reduce((sum, a) => sum + (a.cached_input_tokens || 0), 0);
  const hasUnreportedCache = (res.agents || []).some(a => a.cache_reporting === 'unavailable');
  totalCost = (res.agents || []).reduce((sum, a) => sum + (a.cost_usd || 0), 0);
  const allKnown = (res.agents || []).every(a => a.pricing_known);
  const costText = allKnown ? formatCost(totalCost) : `${formatCost(totalCost)} + unpriced`;
  document.getElementById('totalTokens').textContent = totalTokens.toLocaleString();
  document.getElementById('totalCachedTokens').textContent = cached.toLocaleString() + (hasUnreportedCache ? ' + unreported' : '');
  document.getElementById('totalCost').textContent = costText;
}

function formatCost(value) {
  return '$' + Number(value || 0).toFixed(value >= 0.01 ? 4 : 6);
}

function clearFeed() {
  document.getElementById('feed').innerHTML = '';
  totalTokens = 0; eventCount = 0;
  document.getElementById('totalTokens').textContent = '0';
  document.getElementById('totalCachedTokens').textContent = '0';
  document.getElementById('totalCost').textContent = '$0.000000';
  document.getElementById('progressTaskList').innerHTML = '<div style="color:var(--muted);font-size:12px">No tasks defined in PLAN.md yet.</div>';
  document.getElementById('eventCount').textContent = '0';
  
  const liveInsights = document.getElementById('liveInsightsContainer');
  if (liveInsights) {
    liveInsights.innerHTML = '<div style="color:var(--muted); font-style:italic;" id="liveInsightsEmpty">No insights gathered yet. Start a run to see live team decisions!</div>';
  }

  updateCurrentWorkSummary({
    agentName: 'Ready to start',
    summary: 'Start a run to see a compact summary of what the team is exploring.',
    whyNow: 'The team will explain why the current step matters as the run progresses.',
    expectedOutput: 'You will see the next artifact or decision outcome here.',
    needsInput: 'No user action needed right now.',
  });
}

async function showInteractiveQuestions() {
  try {
    const res = await fetch('/workspace/file/questions').then(r=>r.json());
    if (res && res.content && res.content.trim().length > 0 && res.content.trim() !== '(empty)') {
      const pendingPane = document.getElementById('contextPendingActions');
      const bodyEl = document.getElementById('contextQuestionsBody');
      if (pendingPane && bodyEl) {
        pendingPane.style.display = 'flex';
        bodyEl.innerHTML = renderQuestionBody(res.content);
        const plain = res.content.replace(/^#.*$/gm, '').replace(/\s+/g, ' ').trim();
        updateCurrentWorkSummary({
          agentName: document.getElementById('contextAgentName')?.textContent || 'User input needed',
          summary: document.getElementById('contextWorkSummary')?.textContent || 'The team needs your input before moving forward.',
          whyNow: document.getElementById('contextWhyNow')?.textContent || 'A decision or clarification is blocking the next design step.',
          expectedOutput: document.getElementById('contextExpectedOutput')?.textContent || 'A resolved decision so the team can continue.',
          needsInput: plain || 'Review the open decision and reply with your preference.',
        });
        if (typeof loadWsFile === 'function') {
          loadWsFile('dashboard');
        }
      }
    }
  } catch (err) {
    console.error("Failed to load interactive questions", err);
  }
}

function extractLiveInsights(ev) {
  if (ev.kind !== 'turn_end' || !ev.data.response) return;
  const text = ev.data.response;
  
  const consensusMatch = text.match(/## CONSENSUS_APPEND\s*\n([\s\S]*?)(?=##|$)/);
  const decisionMatch = text.match(/## DECISION_CHECKPOINT\s*\n([\s\S]*?)(?=##|$)/);
  
  let insightText = '';
  if (consensusMatch) insightText = consensusMatch[1].trim();
  else if (decisionMatch) insightText = decisionMatch[1].trim();
  
  if (insightText && !insightText.includes('VOTE: DISAGREE')) {
    const container = document.getElementById('liveInsightsContainer');
    const emptyMsg = document.getElementById('liveInsightsEmpty');
    if (container) {
      if (emptyMsg) emptyMsg.style.display = 'none';
      
      const insightDiv = document.createElement('div');
      insightDiv.style.background = 'rgba(255,255,255,0.03)';
      insightDiv.style.padding = '12px';
      insightDiv.style.borderRadius = '8px';
      insightDiv.style.borderLeft = '3px solid var(--accent)';
      
      const agentLabel = document.createElement('div');
      agentLabel.style.fontSize = '11px';
      agentLabel.style.fontWeight = 'bold';
      agentLabel.style.color = 'var(--muted)';
      agentLabel.style.marginBottom = '6px';
      agentLabel.style.textTransform = 'uppercase';
      agentLabel.textContent = ev.agent || 'Agent';
      
      const content = document.createElement('div');
      content.className = 'md-content';
      content.innerHTML = parseMarkdown(insightText);
      
      insightDiv.appendChild(agentLabel);
      insightDiv.appendChild(content);
      
      container.insertBefore(insightDiv, container.firstChild);
      
      // Keep only the last 10 insights to avoid clutter
      while (container.children.length > 11) {
        container.removeChild(container.lastChild);
      }
    }
  }
}

window.submitContextAnswer = function() {
  const answer = document.getElementById('contextAnswerInput').value;
  if (!answer.trim()) return;
  
  const steerInput = document.getElementById('steerInput');
  if (steerInput) {
     steerInput.value = answer;
     steer();
  }
  
  document.getElementById('contextPendingActions').style.display = 'none';
  document.getElementById('contextAnswerInput').value = '';
};

async function exportContext() {
  try {
    const planRes = await fetch('/workspace/file/plan');
    const planData = planRes.ok ? await planRes.json() : {content: 'No PLAN.md found.'};
    const designRes = await fetch('/workspace/file/design');
    const designData = designRes.ok ? await designRes.json() : {content: 'No DESIGN.md found.'};
    const wsRes = await fetch('/workspace');
    const wsData = wsRes.ok ? await wsRes.json() : {root: 'project'};
    
    let projName = "project";
    const pathStr = wsData.project_path || wsData.root;
    if (pathStr) {
        const parts = pathStr.split(/[/\\]/);
        projName = parts[parts.length - 1] || "project";
    }
    
    const bundled = `# Architecture Design\n\n${designData.content}\n\n# Implementation Plan\n\n${planData.content}`;
    
    const blob = new Blob([bundled], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${projName}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    alert(`DESIGN.md and PLAN.md bundled and downloaded as ${projName}.md!`);
  } catch (e) {
    alert('Failed to export context: ' + e.message);
  }
}

// ── Settings & User Management ──────────────────────────────────────────────
let currentUser = null;

async function checkAuth() {
    const res = await originalFetch('/users/me');
    if (res.ok) {
        currentUser = await res.json();
        const label = document.getElementById('loggedInUserLabel');
        if (label) {
            label.textContent = "👤 " + currentUser.username + " (" + currentUser.role + ")";
            label.style.display = 'inline-block';
        }
        const btn = document.getElementById('logoutBtn');
        if (btn) btn.style.display = 'inline-block';
        
        if (currentUser.role === 'admin') {
            document.getElementById('adminSettingsBlock').style.display = 'block';
            document.getElementById('shutdownBtn').style.display = 'inline-block';
            loadUsers();
        }
    }
}

async function loadUsers() {
    const res = await fetch('/users');
    if (res.ok) {
        const data = await res.json();
        const tbody = document.getElementById('usersTableBody');
        tbody.innerHTML = '';
        data.users.forEach(u => {
            tbody.innerHTML += `
              <tr style="border-bottom:1px solid var(--border);">
                <td style="padding:10px;">${u.username}</td>
                <td style="padding:10px;">${u.role}</td>
                <td style="padding:10px;">
                  <button class="btn btn-secondary" onclick="resetUserPassword('${u.username}')">Reset Password</button>
                  ${u.username === 'admin' ? '' : `<button class="btn" style="background:#dc3545; color:white;" onclick="deleteUser('${u.username}')">Delete</button>`}
                </td>
              </tr>
            `;
        });
    }
}

async function changeMyPassword() {
    const np = document.getElementById('newPassword').value;
    if (!np) return alert("Enter a new password");
    
    const res = await fetch('/users/password', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: currentUser.username, new_password: np})
    });
    if (res.ok) {
        alert("Password updated!");
        document.getElementById('newPassword').value = '';
    } else {
        alert("Failed to update password");
    }
}

async function resetUserPassword(username) {
    const np = prompt(`Enter new password for ${username}:`);
    if (!np) return;
    const res = await fetch('/users/password', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: username, new_password: np})
    });
    if (res.ok) alert(`Password for ${username} updated!`);
    else alert("Failed to reset password");
}

async function addUser() {
    const u = document.getElementById('newUsername').value;
    const p = document.getElementById('newUserPassword').value;
    
    if (!u || !p) return alert("Fill in all fields");
    
    const res = await fetch('/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: u, password: p, role: "user"})
    });
    
    if (res.ok) {
        document.getElementById('newUsername').value = '';
        document.getElementById('newUserPassword').value = '';
        loadUsers();
    } else {
        const errData = await res.json().catch(()=>({}));
        alert("Failed to add user: " + (errData.detail || "Unknown error"));
    }
}

// Check auth on boot
window.addEventListener('load', checkAuth);


async function deleteUser(username) {
    const res = await fetch(`/users/${username}`, { method: 'DELETE' });
    if (res.ok) {
        loadUsers();
    } else {
        const err = await res.json().catch(()=>({}));
        alert("Failed to delete user: " + (err.detail || "Unknown error"));
    }
}

async function updateTokens() {
  const maxTokens = parseInt(document.getElementById('maxTokensInput').value, 10);
  if (isNaN(maxTokens)) {
    notify('Please enter a valid number for max tokens', true);
    return;
  }
  
  if (projectOpen && currentProjectPath) {
    try {
      await fetch('/project/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ max_tokens: maxTokens })
      });
    } catch (err) {
      console.error('Failed to save project settings', err);
    }
  }
  
  if (paused || appStatus === 'needs_attention') {
    // If paused (or budget exhausted), resume with new tokens
    await fetch('/run/resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ max_tokens: maxTokens })
    });
    paused = false;
    document.getElementById('pauseBtn').textContent = 'Pause';
    updateStatus('running');
    notify(`Tokens updated to ${maxTokens.toLocaleString()} and run resumed.`);
  } else if (appStatus === 'running') {
    // Just update the tokens on backend if it's already running
    await fetch('/run/resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ max_tokens: maxTokens })
    });
    notify(`Tokens updated to ${maxTokens.toLocaleString()}.`);
  } else {
    notify(`Next run will use ${maxTokens.toLocaleString()} tokens limit. (Saved to settings)`);
  }
}

async function shutdownServer() {
  if (confirm("Are you sure you want to shut down the server? You will need to restart it manually from the terminal.")) {
    try {
      const res = await fetch('/admin/shutdown', { method: 'POST' });
      if (res.ok) {
        alert("Server is shutting down.");
      } else {
        const text = await res.text();
        alert("Failed to shut down: " + text);
      }
    } catch (e) {
      alert("Error shutting down: " + e.message);
    }
  }
}
