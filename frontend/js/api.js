
// ── Auth & 401 Interceptor ──────────────────────────────────────────────────
const originalFetch = window.fetch;
window.fetch = async function(...args) {
    const tabSession = sessionStorage.getItem('designflow_session_id');
    if (args.length === 1 && typeof args[0] === 'string') {
        args.push({ credentials: 'same-origin' });
    } else if (args.length === 2 && typeof args[1] === 'object') {
        args[1].credentials = args[1].credentials || 'same-origin';
    }
    if (tabSession) {
        if (args.length === 1) args.push({ headers: {'X-DesignFlow-Session': tabSession}, credentials: 'same-origin' });
        else args[1].headers = {...(args[1].headers || {}), 'X-DesignFlow-Session': tabSession};
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
        const loginData = await res.json();
        sessionStorage.setItem('designflow_session_id', loginData.session_id);
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
    sessionStorage.removeItem('designflow_session_id');
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

window.showCockpitInfoTab = function(name) {
  const tabs = ['summary', 'details', 'actions'];
  for (const key of tabs) {
    const btn = document.getElementById(`cockpitSubtab-${key}`);
    const panel = document.getElementById(`cockpitPanel-${key}`);
    if (btn) btn.classList.toggle('active', key === name);
    if (panel) panel.style.display = key === name ? 'block' : 'none';
    if (panel) panel.classList.toggle('active', key === name);
  }
};


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
  const lines = String(text || '').split('\n');
  let optionGroupStarted = false;
  for (const line of lines) {
    const match = line.match(/^\s*-\s*\[([A-Z])\]\s+(.+)$/);
    if (match) {
      optionGroupStarted = true;
      options.push({ label: match[1], text: match[2].trim() });
      if (options.length === 3) break;
    } else if (optionGroupStarted && line.trim()) {
      break;
    }
  }
  if (!options.length) {
    const altRegex = /^\s*([A-Z])[\).:-]\s+(.+)$/gm;
    for (const match of text.matchAll(altRegex)) {
      options.push({ label: match[1], text: match[2].trim() });
      if (options.length === 3) break;
    }
  }
  const rec = text.match(/recommendation\s*[:\-]\s*([A-Z])/i);
  const allOptionCount = [...String(text || '').matchAll(/^\s*-\s*\[[A-Z]\]\s+.+$/gm)].length;
  return {
    options,
    recommendation: rec ? rec[1].toUpperCase() : '',
    hasMoreDecisions: allOptionCount > options.length,
  };
}

window.useDecisionOption = function(label, text) {
  const input = document.getElementById('steerInput');
  if (!input) return;
  input.value = `${label}${text ? ' — ' + text : ''}`;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
};

window.submitDecisionOption = async function(label, text) {
  if (!awaitingDecisionInput) return;
  const buttons = document.querySelectorAll('.decision-option, .decision-other');
  buttons.forEach(button => { button.disabled = true; });
  const status = document.getElementById('decisionSubmitStatus');
  if (status) status.textContent = 'Submitting your decision…';
  const input = document.getElementById('steerInput');
  const answer = `${label}${text ? ' — ' + text : ''}`;
  if (input) input.value = answer;
  try {
    const steerRes = await fetch('/run/steer', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: answer})
    });
    if (!steerRes.ok) throw new Error('decision submission failed');
    const resumeRes = await fetch('/run/resume', {method: 'POST'});
    if (!resumeRes.ok) throw new Error('workflow resume failed');
    if (input) input.value = '';
    awaitingDecisionInput = false;
    paused = false;
    updateStatus('running');
    const pendingPane = document.getElementById('contextPendingActions');
    if (pendingPane) pendingPane.style.display = 'none';
  } catch (error) {
    buttons.forEach(button => { button.disabled = false; });
    if (status) status.textContent = 'Could not submit. Choose again or type an answer below.';
  }
};

window.useCustomDecision = function() {
  const input = document.getElementById('steerInput');
  if (!input) return;
  input.value = '';
  input.placeholder = 'Type your own answer, then press Submit decision…';
  input.focus();
};

function renderQuestionBody(content) {
  const parsed = extractDecisionOptions(content || '');
  const optionsEl = document.getElementById('contextDecisionOptions');
  if (optionsEl) {
    optionsEl.style.display = 'none';
    optionsEl.innerHTML = '';
  }
  if (!parsed.options.length) return parseMarkdown(content);

  const firstOptionIndex = content.search(/^\s*-\s*\[[A-Z]\]\s+/m);
  const intro = firstOptionIndex > 0 ? content.slice(0, firstOptionIndex).trim() : '# Decision checkpoint';
  const introLines = intro.replace(/^#.*$/gm, '').split('\n').map(line => line.trim()).filter(Boolean);
  let questionStart = introLines.findLastIndex(line => /^(?:decision|question)(?:\s+\d+)?\s*[:\-—–]/i.test(line));
  if (questionStart < 0) questionStart = Math.max(0, introLines.length - 1);
  const rationale = introLines.slice(0, questionStart).join(' ');
  const question = introLines.slice(questionStart).join(' ') || 'Choose the direction you want the team to take.';
  const recommendationMatch = content.match(/recommendation\s*[:\-][^\n]*(?:\n(?!\s*-\s*\[[A-Z]\])[^\n]+)?/i);
  const recommendation = recommendationMatch ? recommendationMatch[0].trim() : '';
  const buttons = parsed.options.map(opt => {
    const recommended = parsed.recommendation === opt.label;
    const encodedText = encodeURIComponent(opt.text);
    return `<button class="decision-option ${recommended ? 'is-recommended' : ''}" onclick="submitDecisionOption('${escAttr(opt.label)}',decodeURIComponent('${encodedText}'))"><span class="decision-option-key">${escHtml(opt.label)}</span><span class="decision-option-copy">${escHtml(opt.text)}</span>${recommended ? '<span class="decision-recommended-badge">Recommended</span>' : ''}</button>`;
  }).join('');
  const more = parsed.hasMoreDecisions
    ? '<div class="decision-more-note">Additional decisions will be asked after you answer this one.</div>'
    : '';
  const context = rationale ? `<details class="decision-context"><summary>Why this decision is needed</summary><div>${escHtml(rationale)}</div></details>` : '';
  return `<div class="decision-group"><div class="decision-question-copy"><div class="decision-step-label">Decision required</div>${context}<div class="decision-question">${escHtml(question)}</div>${recommendation ? `<div class="decision-recommendation">${parseMarkdown(recommendation)}</div>` : ''}${more}</div><div class="decision-inline-options" role="group" aria-label="Decision options">${buttons}<button class="decision-option decision-other" onclick="useCustomDecision()"><span class="decision-option-key">O</span><span class="decision-option-copy">Other — write my own answer</span></button><div id="decisionSubmitStatus" class="decision-submit-status" aria-live="polite">Choose one option to continue.</div></div></div>`;
}

function renderInteractiveQuestionPanel(content) {
  const pendingPane = document.getElementById('contextPendingActions');
  const bodyEl = document.getElementById('contextQuestionsBody');
  const hasQuestion = awaitingDecisionInput && !!(content && content.trim() && content.trim() !== '(empty)');
  if (!pendingPane || !bodyEl) return false;

  if (!hasQuestion) {
    pendingPane.style.display = 'none';
    return false;
  }

  pendingPane.style.display = 'flex';
  bodyEl.innerHTML = renderQuestionBody(content);
  const steerInput = document.getElementById('steerInput');
  const sendBtn = document.getElementById('sendBtn');
  if (steerInput && !steerInput.value) {
    steerInput.placeholder = 'Choose an option above or type your answer here…';
  }
  if (sendBtn) sendBtn.textContent = 'Submit decision';
  return true;
}


let recentFileWrites = [];

function extractMarkdownSection(doc, heading) {
  if (!doc) return '';
  const lines = doc.split('\n');
  const target = heading.trim().toLowerCase();
  const startIdx = lines.findIndex(line => line.trim().toLowerCase() === `## ${target}`);
  if (startIdx === -1) return '';
  const body = [];
  for (let i = startIdx + 1; i < lines.length; i++) {
    if (lines[i].trim().startsWith('## ')) break;
    body.push(lines[i]);
  }
  return body.join('\n').trim();
}

function markdownBullets(text, limit = 4) {
  return (text || '')
    .split('\n')
    .map(line => line.trim())
    .filter(line => /^[-*]\s+/.test(line))
    .map(line => line.replace(/^[-*]\s+/, '').trim())
    .slice(0, limit);
}

function checklistSummary(plan) {
  const matches = [...(plan || '').matchAll(/- \[( |x)\] (.+)/gi)];
  const done = matches.filter(m => m[1].toLowerCase() === 'x').length;
  return { total: matches.length, done, pending: Math.max(0, matches.length - done) };
}

function inferStage({ questions, decisions, plan, design }) {
  if ((questions || '').trim() && questions.trim() !== '(empty)') return 'Waiting on input';
  if ((decisions || '').trim() === '(empty)' || !(decisions || '').trim()) return 'Framing';
  if ((plan || '').includes('## Implementation Phases')) return 'Planning';
  if ((design || '').toLowerCase().includes('api') || (design || '').toLowerCase().includes('architecture')) return 'Architecture';
  return 'Exploration';
}

function inferConfidence({ questions, decisions, plan }) {
  if ((questions || '').trim() && questions.trim() !== '(empty)') return 'Needs input';
  const summary = checklistSummary(plan || '');
  if ((decisions || '').trim() && summary.total > 0) return 'High';
  if ((decisions || '').trim()) return 'Medium';
  return 'Early';
}

function recommendationFromContent(questions, decisions) {
  const rec = (questions || '').match(/recommendation\s*[:\-]\s*(.+)/i);
  if (rec) return rec[1].trim();
  const bullets = markdownBullets(decisions || '', 2);
  return bullets[0] || 'No strong recommendation captured yet.';
}

function latestDecisionTimeline(decisions) {
  const lines = (decisions || '').split('\n').map(line => line.trim()).filter(Boolean);
  const items = [];
  for (const line of lines) {
    if (/^###?\s+/.test(line)) {
      items.push({ title: line.replace(/^###?\s+/, ''), meta: 'Decision section' });
    } else if (/^[-*]\s+/.test(line)) {
      items.push({ title: line.replace(/^[-*]\s+/, ''), meta: 'Recorded decision' });
    }
    if (items.length >= 5) break;
  }
  return items;
}

function openLoopsFromArtifacts({ questions, plan, design }) {
  const loops = [];
  if ((questions || '').trim() && questions.trim() !== '(empty)') {
    loops.push({ title: 'User input required', meta: summarizeInstructions((questions || '').replace(/^#.*$/gm, '').trim()) });
  }
  const risks = markdownBullets(extractMarkdownSection(plan || '', 'Risks'), 3);
  risks.forEach(risk => loops.push({ title: risk, meta: 'Risk from PLAN.md' }));
  const assumptions = markdownBullets(extractMarkdownSection(plan || '', 'Assumptions'), 2);
  assumptions.forEach(item => loops.push({ title: item, meta: 'Validate assumption' }));
  if (!loops.length && (design || '').trim() && !(design || '').includes('```mermaid')) {
    loops.push({ title: 'Visual architecture still weak', meta: 'Consider adding or refining the main diagram.' });
  }
  return loops.slice(0, 5);
}

function renderCockpitList(id, items, emptyText) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!items || !items.length) {
    el.innerHTML = `<div class="cockpit-empty">${escHtml(emptyText)}</div>`;
    return;
  }
  el.innerHTML = items.map(item => `
    <div class="cockpit-list-item">
      <div class="cockpit-list-item-title">${escHtml(item.title || item)}</div>
      ${item.meta ? `<div class="cockpit-list-item-meta">${escHtml(item.meta)}</div>` : ''}
    </div>
  `).join('');
}

async function updateDesignCockpit() {
  const goalEl = document.getElementById('cockpitProjectGoal');
  if (goalEl) {
    if (projectOpen && currentProjectPath) {
      const parts = currentProjectPath.split('/').filter(Boolean);
      goalEl.textContent = parts[parts.length - 1] || currentProjectPath;
    } else {
      goalEl.textContent = 'Open a project to begin.';
    }
  }
  const emptyState = document.getElementById('cockpitEmptyState');
  const loadedState = document.getElementById('cockpitLoadedState');
  if (emptyState) emptyState.style.display = projectOpen ? 'none' : 'flex';
  if (loadedState) loadedState.style.display = projectOpen ? 'flex' : 'none';
  if (!projectOpen) return;
  try {
    const [designRes, planRes, decisionsRes, questionsRes] = await Promise.all([
      fetch('/workspace/file/design').then(r => r.json()).catch(() => ({ content: '' })),
      fetch('/workspace/file/plan').then(r => r.json()).catch(() => ({ content: '' })),
      fetch('/workspace/file/decisions').then(r => r.json()).catch(() => ({ content: '' })),
      fetch('/workspace/file/questions').then(r => r.json()).catch(() => ({ content: '' })),
    ]);
    const design = designRes.content || '';
    const plan = planRes.content || '';
    const decisions = decisionsRes.content || '';
    const questions = questionsRes.content || '';
    const stage = inferStage({ questions, decisions, plan, design });
    const decisionText = (questions && questions !== '(empty)')
      ? questions.replace(/^#.*$/gm, '').trim()
      : (latestDecisionTimeline(decisions)[0]?.title || 'No active decision is surfaced right now.');
    const recommendation = recommendationFromContent(questions, decisions);
    const loops = openLoopsFromArtifacts({ questions, plan, design });
    renderInteractiveQuestionPanel(questions);

    const stageEl = document.getElementById('cockpitStage');
    if (stageEl) stageEl.textContent = stage;
    const decisionEl = document.getElementById('cockpitCurrentDecision');
    if (decisionEl) decisionEl.textContent = decisionText || 'No active decision is surfaced right now.';
    const recommendationEl = document.getElementById('cockpitRecommendation');
    if (recommendationEl) recommendationEl.textContent = recommendation || 'Recommendations from the debate will appear here.';
    const statusEl = document.getElementById('cockpitStatusChip');
    if (statusEl) statusEl.textContent = (appStatus || 'idle').replace(/_/g, ' ');
    const logMetaEl = document.getElementById('reasoningLogMeta');
    if (logMetaEl) logMetaEl.textContent = `${eventCount} events captured`;

    renderCockpitList('cockpitDecisionTimeline', latestDecisionTimeline(decisions).slice(0, 3), 'No decisions captured yet.');
    renderCockpitList('cockpitOpenLoops', loops.slice(0, 3), 'No open loops detected.');
    renderCockpitList('cockpitRecentChanges', recentFileWrites.slice(0, 2).map(item => ({ title: item.file, meta: item.meta })), 'No meaningful changes yet.');
  } catch (err) {
    console.error('Failed to update design cockpit', err);
  }
}

let activeEventSource = null;
let seenEventIds = new Set();

function connectSSE(resetHistory = false) {
  if (resetHistory) seenEventIds = new Set();
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }
  const tabSession = sessionStorage.getItem('designflow_session_id');
  const eventsUrl = tabSession ? `/events?session_id=${encodeURIComponent(tabSession)}` : '/events';
  const es = new EventSource(eventsUrl);
  activeEventSource = es;
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    const eventId = String(ev.event_id || e.lastEventId || '');
    if (eventId && seenEventIds.has(eventId)) return;
    if (eventId) seenEventIds.add(eventId);
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
    if (ev.data.status === 'stopped') {
      awaitingDecisionInput = false;
      updateStatus('idle');
    } else if (ev.data.status === 'waiting_for_continuation' || ev.data.status === 'waiting_for_approval' || ev.data.status === 'budget_exhausted') {
      awaitingDecisionInput = ev.data.status === 'waiting_for_approval';
      updateStatus('paused');
      if (ev.data.status === 'waiting_for_approval') {
        showInteractiveQuestions();
      }
    } else {
      awaitingDecisionInput = false;
      updateStatus('running');
      const pendingActions = document.getElementById('contextPendingActions');
      if (pendingActions) pendingActions.style.display = 'none';
    }
  }
  if (ev.kind === 'done')  { awaitingDecisionInput = false; updateStatus('done'); loadRunHistory(); }
  if (ev.kind === 'file_write') {
    recentFileWrites.unshift({ file: ev.data.file || 'Unknown file', meta: ev.data.preview || 'Artifact updated' });
    recentFileWrites = recentFileWrites.slice(0, 8);
  }
  if (ev.kind === 'error') {
    awaitingDecisionInput = false;
    const retryBtn = document.getElementById('retryBtn');
    if (retryBtn && ev.data.recoverable) {
      retryBtn.textContent = ev.data.error_code === 'context_too_large' ? 'Compact & Retry' : 'Retry failed turn';
      retryBtn.title = ev.data.error_code === 'context_too_large'
        ? 'Re-run preflight with bounded history and compact project context'
        : 'Retry the failed model turn';
      retryBtn.disabled = false;
    }
    updateStatus(ev.data.recoverable ? 'needs_attention' : 'error');
  }

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
    updateDesignCockpit();
  }
  if (ev.kind === 'phase' || ev.kind === 'file_write' || ev.kind === 'done') {
    updateDesignCockpit();
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
  let kindLabel = (ev.kind || 'update').replace(/_/g, ' ');

  switch(ev.kind) {
    case 'phase':
      if (ev.data.status === 'stopped') {
        document.querySelectorAll('.feed-item.retry').forEach(item => item.classList.add('cancelled'));
        document.querySelectorAll('.feed-item.turn_start:not(.completed)').forEach(item => {
          item.classList.add('completed', 'cancelled');
          const pendingSummary = item.querySelector('.feed-summary');
          if (pendingSummary) pendingSummary.textContent = 'Agent turn cancelled when the run was stopped.';
        });
        summary = ev.data.message || 'Run stopped. Scheduled retries were cancelled.';
        kindLabel = 'stopped';
        break;
      }
      if (ev.data.status === 'budget_exhausted') {
        summary = ev.data.projected_turn_tokens
          ? `Paused before the next model call. It may need about ${Number(ev.data.projected_turn_tokens || 0).toLocaleString()} tokens, with ${Math.max(0, Number(ev.data.run_max_tokens || 0) - Number(ev.data.run_total_tokens || 0)).toLocaleString()} remaining. Increase the project limit or stop the run.`
          : `Run token budget reached (${Number(ev.data.run_total_tokens || 0).toLocaleString()} of ${Number(ev.data.run_max_tokens || 0).toLocaleString()}). Increase the limit or stop the run.`;
        kindLabel = 'token limit';
        break;
      }
      summary = `Phase: ${ev.data.phase?.toUpperCase() || ''} ${ev.data.status ? '— ' + ev.data.status : ''} ${ev.data.iteration ? 'iter ' + ev.data.iteration : ''} ${ev.data.round ? 'round ' + ev.data.round : ''}`;
      if (ev.data.roles) summary += ' | ' + Object.entries(ev.data.roles).map(([r,a])=>`${a}=${r}`).join(' ');
      break;
    case 'turn_start': {
      const verb = getAgentVerb(ev.agent);
      summary = `${verb} through the request…`;
      kindLabel = 'thinking';
      break;
    }
    case 'turn_end': {
      const pendingTurn = ev.data.turn_id
        ? feed.querySelector(`.feed-item.turn_start[data-turn-id="${CSS.escape(String(ev.data.turn_id))}"]`)
        : null;
      if (pendingTurn) {
        pendingTurn.classList.add('completed');
        const startedAt = Number(pendingTurn.dataset.startedAt || 0);
        const elapsed = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : 0;
        const pendingSummary = pendingTurn.querySelector('.feed-summary');
        if (pendingSummary) pendingSummary.textContent = elapsed ? `Model responded in ${formatDuration(elapsed)}.` : 'Model responded.';
      }
      const u = ev.data.usage || {};
      detail = ev.data.response || '';
      summary = conversationalAgentSummary(detail, ev.agent);
      kindLabel = 'summary';
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
      summary = `${friendlyProviderError(ev.data.reason)} Retrying in ${formatDuration(ev.data.retry_in_seconds)}.`;
      kindLabel = 'retrying';
      break;
    case 'done':
      summary = 'Planning baseline ready for implementation and further discovery.';
      updateStatus('done');
      break;
    case 'error':
      summary = ev.data.error_code ? String(ev.data.error || 'Agent request failed.') : friendlyProviderError(ev.data.error || ev.data.message);
      if (ev.data.recoverable) summary += ' You can retry after resolving the provider issue.';
      kindLabel = 'error';
      break;
    default:
      summary = JSON.stringify(ev.data).slice(0, 100);
  }

  const avatarChar = (ev.agent || 'SYS').slice(0, 1).toUpperCase();
  const providerAgent = ev.data.provider_agent || '';
  const providerKind = ev.data.provider_kind || '';
  const providerModel = ev.data.provider_model || '';
  const providerLabel = providerAgent
    ? `${providerAgent}${providerKind ? ' · ' + providerKind : ''}${providerModel ? ' · ' + providerModel : ''}`
    : '';

  div.innerHTML = `
    <div class="feed-row">
      <div class="feed-avatar" style="background:${agentColor}; text-shadow: 0 1px 4px rgba(0,0,0,0.3)">
        ${avatarChar}
      </div>
      <div class="feed-meta">
        <div class="feed-header-line">
          <div class="feed-agent-details">
            <span class="feed-agent">${ev.agent || 'System'}</span>
            ${providerLabel ? `<span class="feed-provider" title="Underlying configured agent and model">via ${escHtml(providerLabel)}</span>` : ''}
            <span class="feed-kind">${escHtml(kindLabel)}</span>
            ${metricsHtml}
          </div>
          <span class="feed-ts">${ts}</span>
        </div>
        <div class="feed-text" style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
          <span class="feed-summary" style="flex: 1">${escHtml(summary)}</span>
          ${detail ? `<button class="btn btn-secondary" style="padding: 2px 7px; font-size: 11px; font-weight: bold; line-height: 1; border-radius: 4px;" onclick="const d = this.parentElement.nextElementSibling; if(d.style.display === 'none'){d.style.display='block';this.innerText='-';}else{d.style.display='none';this.innerText='+';}">${ev.data.verdict === 'PAUSE_FOR_INPUT' ? '-' : '+'}</button>` : ''}
        </div>
        ${detail ? `<div class="feed-detail markdown-body" style="display: ${ev.data.verdict === 'PAUSE_FOR_INPUT' ? 'block' : 'none'}; margin-top: 8px;">${parseMarkdown(detail)}</div>` : ''}
      </div>
    </div>
  `;

  if (ev.kind === 'turn_start') {
    div.dataset.turnId = String(ev.data.turn_id || '');
    div.dataset.startedAt = String(ev.timestamp ? new Date(ev.timestamp).getTime() : Date.now());
  }

  const wasNearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  feed.appendChild(div);
  if (wasNearBottom) feed.scrollTop = feed.scrollHeight;
  if (window.mermaid) { try { mermaid.run({ querySelector: '.mermaid' }); } catch(e) {} }
}

setInterval(() => {
  document.querySelectorAll('.feed-item.turn_start:not(.completed)').forEach(item => {
    const startedAt = Number(item.dataset.startedAt || 0);
    if (!startedAt) return;
    const elapsed = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    if (elapsed < 15) return;
    const summary = item.querySelector('.feed-summary');
    if (summary) summary.textContent = `Still waiting for the model · ${formatDuration(elapsed)} elapsed. You can stop the run if it appears stuck.`;
  });
}, 1000);

function friendlyProviderError(rawError) {
  const error = String(rawError || '').toLowerCase();
  if (/quota|insufficient_quota|resource.?exhausted|billing|credit balance|usage limit/.test(error)) {
    return 'Model quota exhausted.';
  }
  if (/rate.?limit|too many requests|status.?429|\b429\b/.test(error)) {
    return 'Provider rate limit reached.';
  }
  if (/api.?key|auth|credential|unauthorized|forbidden|status.?401|status.?403|\b401\b|\b403\b/.test(error)) {
    return 'Provider authentication failed.';
  }
  if (/model.*(not found|unavailable|unsupported|deprecated)|unknown model|invalid model/.test(error)) {
    return 'Configured model is unavailable.';
  }
  if (/context.?length|context window|maximum context|token limit|too many tokens/.test(error)) {
    return 'Request exceeds the model context limit.';
  }
  if (/timeout|timed out|deadline exceeded/.test(error)) {
    return 'Model provider timed out.';
  }
  if (/network|connection|connect failed|dns|name resolution|fetch failed|service unavailable|status.?5\d\d/.test(error)) {
    return 'Model provider is temporarily unavailable.';
  }
  if (/content filter|safety|policy violation|blocked/.test(error)) {
    return 'Request was blocked by the provider safety policy.';
  }
  return 'Agent request failed.';
}

function conversationalAgentSummary(response, agent) {
  const preferredSections = ['USER_SUMMARY', 'DECISION_CHECKPOINT'];
  for (const section of preferredSections) {
    const value = parseProtocolSection(response || '', section);
    if (value) return summarizeConversationText(value);
  }

  const cleaned = String(response || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/^#{1,6}\s+.*$/gm, ' ')
    .replace(/^[A-Z][A-Z_ ]+:\s*.*$/gm, ' ')
    .replace(/^[-*]\s+/gm, '')
    .replace(/[*_`>]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  return cleaned
    ? summarizeConversationText(cleaned)
    : `${agent || 'The agent'} finished reviewing the request.`;
}

function summarizeConversationText(text) {
  const clean = String(text || '')
    .replace(/^[-*]\s+/gm, '')
    .replace(/[*_`>]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (clean.length <= 280) return clean;
  const shortened = clean.slice(0, 277);
  const sentenceEnd = Math.max(shortened.lastIndexOf('. '), shortened.lastIndexOf('? '), shortened.lastIndexOf('! '));
  return `${sentenceEnd > 140 ? shortened.slice(0, sentenceEnd + 1) : shortened}…`;
}

function appendUserPrompt(message) {
  const feed = document.getElementById('feed');
  if (!feed || !message) return;
  const item = document.createElement('div');
  item.className = 'feed-item user-prompt';
  const row = document.createElement('div');
  row.className = 'feed-row';
  const avatar = document.createElement('div');
  avatar.className = 'feed-avatar';
  avatar.style.background = 'var(--accent)';
  avatar.textContent = 'Y';
  const meta = document.createElement('div');
  meta.className = 'feed-meta';
  const header = document.createElement('div');
  header.className = 'feed-header-line';
  header.innerHTML = '<span class="feed-agent">You</span><span class="feed-ts">Just now</span>';
  const text = document.createElement('div');
  text.className = 'feed-text';
  text.textContent = message;
  meta.append(header, text);
  row.append(avatar, meta);
  item.appendChild(row);
  feed.appendChild(item);
  feed.scrollTop = feed.scrollHeight;
}

function appendProgressResponse(message) {
  appendFeed({
    kind: 'turn_end',
    agent: 'DesignFlow',
    timestamp: new Date().toISOString(),
    data: {response: message, usage: {}, pricing_known: true, cost_usd: 0}
  });
}

async function showRunProgress() {
  const statusButton = document.getElementById('statusBtn');
  if (statusButton) statusButton.disabled = true;
  try {
    const response = await fetch('/run/progress');
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      notify(data.detail || 'Could not read design progress.', true);
      return;
    }
    appendProgressResponse(data.message);
  } catch (err) {
    notify('Could not read design progress.', true);
  } finally {
    if (statusButton) statusButton.disabled = false;
  }
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
  // Auto routes basic questions to one model and substantive design work to the team.
  let mode = "auto";
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
    if (idea) appendUserPrompt(idea);
    if (data.resumed) notify('Continuing the previous design run.');
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
  const retryBtn = document.getElementById('retryBtn');
  if (retryBtn) {
    retryBtn.disabled = true;
    retryBtn.textContent = 'Compacting…';
  }
  const response = await fetch('/run/retry', {method:'POST'});
  const data = await response.json();
  if (!response.ok) {
    if (retryBtn) retryBtn.disabled = false;
    notify(data.detail || 'Could not retry the failed turn', true);
    return;
  }
  paused = false;
  updateStatus('running');
  notify('Context compacted. Retrying the same turn.');
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
  const response = await fetch('/run/stop', {method:'POST'});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    notify(error.detail || 'Could not stop the run.', true);
    return;
  }
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

  // Project runtimes can be recreated after the last collaborator leaves. Do
  // not route a prompt using stale browser state from the previous runtime.
  try {
    const statusRes = await fetch('/run/status');
    if (statusRes.ok) {
      const statusData = await statusRes.json();
      if (statusData.status) updateStatus(statusData.status);
    }
  } catch (err) {
    notify('Could not read the current run state. Please try again.', true);
    return;
  }

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
      const steerRes = await fetch('/run/steer', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({message: msg})
      });
      if (!steerRes.ok) {
        const error = await steerRes.json().catch(() => ({}));
        notify(error.detail || 'Could not send the prompt to the active run.', true);
        return;
      }
    }
    document.getElementById('steerInput').value = '';
    if (appStatus === 'paused' || appStatus === 'waiting_for_continuation') {
      const resumeRes = await fetch('/run/resume', {method:'POST'});
      if (!resumeRes.ok) {
        const error = await resumeRes.json().catch(() => ({}));
        notify(error.detail || 'Could not resume the run.', true);
        return;
      }
      paused = false;
      updateStatus('running');
    }
  }
}

function updateStatus(s) {
  appStatus = s;
  paused = (s === 'paused');
  if (!awaitingDecisionInput) {
    const pendingActions = document.getElementById('contextPendingActions');
    if (pendingActions) pendingActions.style.display = 'none';
  }
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
    awaitingDecisionInput = false;
    if (nameEl) nameEl.textContent = 'Ready to start';
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
  const [res, project] = await Promise.all([
    fetch('/run/status').then(r=>r.json()),
    fetch('/project').then(r=>r.json()).catch(() => ({ open: false })),
  ]);
  if (typeof applyProjectState === 'function') {
    applyProjectState(project);
  }
  if (res.status) {
    awaitingDecisionInput = res.awaiting_input === true;
    updateStatus(res.status);
  }
  let visibleAgents = res.agents || [];
  agentCapacityStatus = {};
  (res.agents || []).forEach(agent => {
    const baseId = String(agent.base_id || agent.id || '');
    if (!baseId) return;
    ['global-', 'project-'].forEach(prefix => {
      const uid = prefix + baseId;
      const current = agentCapacityStatus[uid] || {total_tokens:0, cost_usd:0, pricing_known:true};
      current.total_tokens += Number(agent.total_tokens || 0);
      current.cost_usd += Number(agent.cost_usd || 0);
      current.pricing_known = current.pricing_known && agent.pricing_known !== false;
      if (agent.retry_at) current.retry_at = agent.retry_at;
      if (agent.status === 'error') {
        current.runtime_status = 'error';
        current.error = agent.error_message || 'Agent execution failed';
      }
      agentCapacityStatus[uid] = current;
    });
  });
  const failedTurn = res.failed_turn || {};
  const retryBtn = document.getElementById('retryBtn');
  if (retryBtn && failedTurn.error_code === 'context_too_large') {
    retryBtn.textContent = 'Compact & Retry';
    retryBtn.title = 'Re-run preflight with bounded history and compact project context';
    retryBtn.disabled = false;
  }
  if (failedTurn.agent_id) {
    const failedBaseId = String(failedTurn.agent_id);
    ['global-', 'project-'].forEach(prefix => {
      const uid = prefix + failedBaseId;
      const current = agentCapacityStatus[uid] || {};
      current.runtime_status = 'error';
      current.error_code = failedTurn.error_code || '';
      current.error = failedTurn.public_error || failedTurn.error || 'Agent execution failed';
      agentCapacityStatus[uid] = current;
    });
  }
  if (!visibleAgents.length) {
    const configured = await fetch('/agents').then(r=>r.json());
    visibleAgents = (configured.merged || []).map(a => ({...a, status:'idle', total_tokens:0,
      input_tokens:0, cached_input_tokens:0, output_tokens:0, cost_usd:0, pricing_known:false}));
  }
  const list = document.getElementById('agentList');
  const maxTokens = Math.max(1, ...visibleAgents.map(a => a.total_tokens || 0));
  if (list) list.innerHTML = visibleAgents.map(a => {
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

  const projectUsage = res.project_usage || {};
  totalTokens = Number(projectUsage.total_tokens || 0);
  const cached = Number(projectUsage.cached_input_tokens || 0);
  totalCost = Number(projectUsage.estimated_cost_usd || 0);
  const costText = projectUsage.pricing_complete === false ? `${formatCost(totalCost)} + unpriced` : formatCost(totalCost);
  document.getElementById('totalTokens').textContent = totalTokens.toLocaleString();
  document.getElementById('totalCachedTokens').textContent = cached.toLocaleString();
  document.getElementById('totalCost').textContent = costText;
  updateDesignCockpit();
  const configPanel = document.getElementById('panel-config');
  if (configPanel?.classList.contains('active') && typeof renderAgentCards === 'function') {
    renderAgentCards();
  }
}

function formatCost(value) {
  return '$' + Number(value || 0).toFixed(value >= 0.01 ? 4 : 6);
}

function clearFeed() {
  document.getElementById('feed').innerHTML = '';
  eventCount = 0;
  const progressTaskList = document.getElementById('progressTaskList');
  if (progressTaskList) {
    progressTaskList.innerHTML = '<div style="color:var(--muted);font-size:12px">No tasks defined in PLAN.md yet.</div>';
  }
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
  recentFileWrites = [];
  updateDesignCockpit();
}

async function showInteractiveQuestions() {
  try {
    const res = await fetch('/workspace/file/questions').then(r=>r.json());
    renderInteractiveQuestionPanel(res?.content || '');
  } catch (err) {
    console.error("Failed to load interactive questions", err);
  }
}

function extractLiveInsights(ev) {
  if (ev.kind !== 'turn_end' || !ev.data.response) return;
  const text = ev.data.response;
  
  const decisionMatch = text.match(/## DECISION_CHECKPOINT\s*\n([\s\S]*?)(?=##|$)/);
  
  let insightText = '';
  if (decisionMatch) insightText = decisionMatch[1].trim();
  
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

async function exportContext() {
  try {
    const planRes = await fetch('/workspace/file/plan');
    const planData = planRes.ok ? await planRes.json() : {content: 'No PLAN.md found.'};
    const designRes = await fetch('/workspace/file/design');
    const designData = designRes.ok ? await designRes.json() : {content: 'No DESIGN.md found.'};
    const decisionsRes = await fetch('/workspace/file/decisions');
    const decisionsData = decisionsRes.ok ? await decisionsRes.json() : {content: 'No DECISIONS.md found.'};
    const questionsRes = await fetch('/workspace/file/questions');
    const questionsData = questionsRes.ok ? await questionsRes.json() : {content: '(empty)'};
    const wsRes = await fetch('/workspace');
    const wsData = wsRes.ok ? await wsRes.json() : {root: 'project'};
    
    let projName = "project";
    const pathStr = wsData.project_path || wsData.root;
    if (pathStr) {
        const parts = pathStr.split(/[/\\]/);
        projName = parts[parts.length - 1] || "project";
    }
    
    const unresolved = questionsData.content && questionsData.content.trim() !== '(empty)'
      ? `\n\n# Unresolved Questions\n\n${questionsData.content}`
      : '';
    const bundled = `# DesignFlow Planning Baseline

This package converts a high-level goal into a stronger technical starting point. It is not a final implementation specification. Validate the documented assumptions, follow the discovery checkpoints, and update the architecture when real code, data, provider behavior, or user feedback contradicts the plan.

# Architecture Design

${designData.content}

# Implementation Plan

${planData.content}

# Decision Ledger

${decisionsData.content}${unresolved}`;
    
    const blob = new Blob([bundled], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${projName}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    alert(`Planning baseline bundled and downloaded as ${projName}.md!`);
  } catch (e) {
    alert('Failed to export context: ' + e.message);
  }
}

// ── Settings & User Management ──────────────────────────────────────────────
let currentUser = null;

async function checkAuth() {
    const res = await fetch('/users/me');
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
