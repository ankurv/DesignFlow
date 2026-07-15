import * as vscode from 'vscode';

export function activate(context: vscode.ExtensionContext) {
    let disposable = vscode.commands.registerCommand('designflow.start', () => {
        const panel = vscode.window.createWebviewPanel(
            'designflow',
            'DesignFlow Dashboard',
            vscode.ViewColumn.One,
            {
                enableScripts: true,
                retainContextWhenHidden: true
            }
        );

        // Get the current workspace folder if one is open
        const workspaceFolders = vscode.workspace.workspaceFolders;
        let projectPath = '';
        if (workspaceFolders && workspaceFolders.length > 0) {
            projectPath = workspaceFolders[0].uri.fsPath;
        }

        // Read settings
        const config = vscode.workspace.getConfiguration('designflow');
        let serverUrl = config.get<string>('serverUrl') || 'http://127.0.0.1:8010';
        serverUrl = serverUrl.replace(new RegExp('/+$'), '');
        const username = config.get<string>('username') || '';
        const password = config.get<string>('password') || '';

        panel.webview.html = getWebviewContent(serverUrl, username, password, projectPath);
    });

    context.subscriptions.push(disposable);
}

function getWebviewContent(serverUrl: string, username?: string, password?: string, projectPath?: string) {
    // Escape strings just in case
    const safeUser = (username || '').replace(/'/g, "\\'");
    const safePass = (password || '').replace(/'/g, "\\'");
    const safeProject = (projectPath || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");

    return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DesignFlow</title>
    <style>
        body, html {
            margin: 0;
            padding: 0;
            height: 100vh;
            width: 100vw;
            overflow: hidden;
            background-color: var(--vscode-editor-background);
            color: var(--vscode-editor-foreground);
            font-family: var(--vscode-font-family);
            font-size: var(--vscode-font-size);
            display: flex;
            flex-direction: column;
        }
        #feed {
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .feed-item {
            background-color: var(--vscode-editorWidget-background);
            border: 1px solid var(--vscode-widget-border, transparent);
            padding: 12px;
            border-radius: 6px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            font-size: 13px;
            line-height: 1.4;
        }
        .feed-item.error {
            border-color: var(--vscode-errorForeground);
        }
        .feed-item .agent {
            font-weight: 600;
            color: var(--vscode-symbolIcon-classForeground);
            margin-bottom: 4px;
            display: block;
            text-transform: capitalize;
        }
        .feed-item pre {
            background: rgba(0,0,0,0.2);
            padding: 8px;
            border-radius: 4px;
            overflow-x: auto;
            white-space: pre-wrap;
        }
        #checkpoint {
            display: none;
            margin: 0 16px 12px;
            padding: 14px;
            border: 2px solid var(--vscode-focusBorder, #8b97ff);
            border-radius: 8px;
            background: var(--vscode-editorWidget-background, #111827);
            color: var(--vscode-editorWidget-foreground, var(--vscode-editor-foreground));
        }
        #checkpoint.visible { display: block; }
        #checkpointLabel { color: var(--vscode-editorWidget-foreground, var(--vscode-editor-foreground)); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
        #checkpointQuestion { margin: 8px 0 14px; line-height: 1.6; font-size: 14px; font-weight: 650; }
        #checkpointOptions { display: flex; flex-direction: column; gap: 7px; }
        #checkpointOptions button { min-height: 42px; text-align: left; padding: 10px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; line-height: 1.45; }
        #checkpointOptions button.recommended { background: var(--vscode-button-background, #4f5bd5); color: var(--vscode-button-foreground, #fff); border: 2px solid var(--vscode-focusBorder, #aeb7ff); }
        #checkpointOptions button.secondary { background: var(--vscode-button-secondaryBackground, #1e293b); color: var(--vscode-button-secondaryForeground, #f1f5f9); border: 1px solid var(--vscode-contrastBorder, #64748b); }
        #checkpointOptions button:focus-visible, #composer button:focus-visible, #composer input:focus-visible { outline: 3px solid var(--vscode-focusBorder, #c7d2fe); outline-offset: 2px; }
        #checkpointOptions button:disabled { opacity: .72; cursor: wait; }
        #checkpointHint { margin-top: 10px; color: var(--vscode-editorWidget-foreground, var(--vscode-editor-foreground)); font-size: 12px; line-height: 1.45; }
        #composer {
            display: flex;
            gap: 8px;
            padding: 16px;
            background-color: var(--vscode-editorWidget-background);
            border-top: 1px solid var(--vscode-widget-border, transparent);
        }
        #composer input {
            flex: 1;
            background-color: var(--vscode-input-background);
            color: var(--vscode-input-foreground);
            border: 1px solid var(--vscode-input-border, transparent);
            padding: 8px 12px;
            border-radius: 4px;
            outline: none;
        }
        #composer input:focus {
            border-color: var(--vscode-focusBorder);
        }
        #composer button {
            background-color: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 600;
        }
        #composer button:hover {
            background-color: var(--vscode-button-hoverBackground);
        }
        #composer button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
    </style>
</head>
<body>
    <div id="feed">
        <div class="feed-item" id="statusMsg">Connecting to DesignFlow backend...</div>
    </div>
    <section id="checkpoint" aria-live="polite">
        <div id="checkpointLabel">Decision checkpoint · one question at a time</div>
        <div id="checkpointQuestion"></div>
        <div id="checkpointOptions"></div>
        <div id="checkpointHint">Choose an option, or select Other to type your own answer.</div>
    </section>
    
    <div id="composer">
        <input type="text" id="steerInput" placeholder="Ask agents to design something..." disabled onkeydown="if(event.key === 'Enter') sendSteer()">
        <button id="sendBtn" onclick="sendSteer()" disabled>Send</button>
    </div>

    <script>
        const statusMsg = document.getElementById('statusMsg');
        window.onerror = function(message, source, lineno, colno, error) {
            if (statusMsg) {
                statusMsg.innerText = 'Script Error: ' + message + ' at ' + lineno + ':' + colno;
                statusMsg.className = 'feed-item error';
            }
        };
        const serverUrl = '${serverUrl}';
        const user = '${safeUser}';
        const pass = '${safePass}';
        const projectPath = '${safeProject}';
        let sessionId = '';
        let eventSource = null;

        const feed = document.getElementById('feed');
        const steerInput = document.getElementById('steerInput');
        const sendBtn = document.getElementById('sendBtn');
        const checkpoint = document.getElementById('checkpoint');
        const checkpointQuestion = document.getElementById('checkpointQuestion');
        const checkpointOptions = document.getElementById('checkpointOptions');
        const checkpointHint = document.getElementById('checkpointHint');
        let awaitingDecision = false;
        let currentPhaseStatus = '';

        function parseCheckpoint(content) {
            const lines = String(content || '').split('\\n');
            const options = [];
            let firstOption = -1;
            let recommendation = '';
            for (let i = 0; i < lines.length; i++) {
                const match = lines[i].match(/^\\s*-\\s*\\[([A-Z])\\]\\s+(.+)$/);
                if (match && options.length < 5) {
                    if (firstOption < 0) firstOption = i;
                    options.push({ label: match[1], text: match[2].trim() });
                } else if (firstOption >= 0 && options.length) {
                    const rec = lines[i].match(/recommendation\\s*[:\\-]\\s*([A-Z])/i);
                    if (rec) recommendation = rec[1].toUpperCase();
                }
            }
            const question = lines.slice(0, firstOption < 0 ? lines.length : firstOption)
                .filter(line => !/^#/.test(line.trim()))
                .join(' ').trim();
            return { question, options, recommendation };
        }

        async function showCheckpoint() {
            const headers = {};
            if (sessionId) headers['X-DesignFlow-Session'] = sessionId;
            const res = await fetch(serverUrl + '/workspace/file/questions', { headers });
            if (!res.ok) return;
            // Prevent race conditions during event replay
            if (currentPhaseStatus !== 'waiting_for_approval') return;
            
            const data = await res.json();
            const parsed = parseCheckpoint(data.content);
            if (!parsed.question || !parsed.options.length) return;
            awaitingDecision = true;
            checkpointQuestion.textContent = parsed.question;
            checkpointOptions.innerHTML = '';
            parsed.options.forEach(option => {
                const button = document.createElement('button');
                const recommended = parsed.recommendation === option.label;
                button.className = recommended ? 'recommended' : 'secondary';
                button.textContent = option.label + ' — ' + (recommended ? 'Recommended · ' : '') + option.text;
                button.onclick = () => submitDecision(option.label + ' — ' + option.text);
                checkpointOptions.appendChild(button);
            });
            const other = document.createElement('button');
            other.className = 'secondary';
            other.textContent = 'O — Other…';
            other.onclick = () => {
                steerInput.value = '';
                steerInput.placeholder = 'Type your own answer…';
                steerInput.focus();
            };
            checkpointOptions.appendChild(other);
            checkpoint.classList.add('visible');
            steerInput.placeholder = 'Or type your own answer…';
            sendBtn.textContent = 'Submit decision';
        }

        async function submitDecision(answer) {
            Array.from(checkpointOptions.querySelectorAll('button')).forEach(button => button.disabled = true);
            checkpointHint.textContent = 'Submitting your decision…';
            steerInput.value = answer;
            await sendSteer();
        }

        function hideCheckpoint() {
            awaitingDecision = false;
            checkpoint.classList.remove('visible');
            checkpointHint.textContent = 'Choose an option, or select Other to type your own answer.';
            sendBtn.textContent = 'Send';
            steerInput.placeholder = 'Ask agents to design something...';
        }

        function appendMessage(agent, text, isError = false) {
            const div = document.createElement('div');
            div.className = 'feed-item' + (isError ? ' error' : '');
            
            const escapedText = String(text)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;");

            const formatted = escapedText
                .replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>')
                .replace(new RegExp(String.fromCharCode(96) + '(.*?)' + String.fromCharCode(96), 'g'), '<code style="background:rgba(0,0,0,0.3);padding:2px 4px;border-radius:4px;">$1</code>')
                .replace(/\\n/g, '<br>');

            div.innerHTML = \`<span class="agent">\${agent}</span><div>\${formatted}</div>\`;
            feed.appendChild(div);
            feed.scrollTop = feed.scrollHeight;
        }

        async function init() {
            try {
                // 1. Login
                if (user && pass) {
                    statusMsg.innerText = 'Logging in...';
                    const res = await fetch(serverUrl + '/auth/login', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ username: user, password: pass })
                    });
                    if (!res.ok) throw new Error('Login failed. Check your credentials in settings.');
                    const data = await res.json();
                    sessionId = data.session_id;
                }

                // 2. Open Project
                if (projectPath) {
                    statusMsg.innerText = 'Opening project...';
                    const headers = { 'Content-Type': 'application/json' };
                    if (sessionId) headers['X-DesignFlow-Session'] = sessionId;
                    
                    const res2 = await fetch(serverUrl + '/project/open', {
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify({ path: projectPath })
                    });
                    if (!res2.ok) throw new Error('Failed to open project.');
                }

                statusMsg.innerText = 'Connected. Project ready.';
                steerInput.disabled = false;
                sendBtn.disabled = false;
                steerInput.focus();

                // 3. Connect SSE
                const eventsUrl = sessionId ? \`\${serverUrl}/events?session_id=\${encodeURIComponent(sessionId)}\` : \`\${serverUrl}/events\`;
                eventSource = new EventSource(eventsUrl);
                
                eventSource.onmessage = (e) => {
                    const ev = JSON.parse(e.data);
                    if (ev.kind === 'turn_end') {
                        appendMessage(ev.agent || 'Agent', ev.data.response || 'Turn completed.');
                    } else if (ev.kind === 'error') {
                        appendMessage('System Error', ev.data.error || ev.data.message || 'Unknown error', true);
                    } else if (ev.kind === 'file_write') {
                        appendMessage('System', \`Updated file: \${ev.data.file}\`);
                    } else if (ev.kind === 'steer') {
                        // User prompt
                        appendMessage('You', ev.data.message);
                    } else if (ev.kind === 'phase') {
                        currentPhaseStatus = ev.data.status;
                        if (ev.data.status === 'waiting_for_approval') {
                            showCheckpoint();
                        } else if (awaitingDecision) {
                            hideCheckpoint();
                        }
                    }
                };
                
                eventSource.onerror = () => {
                    // Reconnection is automatic by EventSource
                };

            } catch (err) {
                statusMsg.innerText = err.message;
                statusMsg.className = 'feed-item error';
            }
        }

        async function sendSteer() {
            const val = steerInput.value.trim();
            if (!val) return;
            
            steerInput.value = '';
            steerInput.disabled = true;
            sendBtn.disabled = true;
            
            try {
                const headers = { 'Content-Type': 'application/json' };
                if (sessionId) headers['X-DesignFlow-Session'] = sessionId;
                
                let res = await fetch(serverUrl + '/run/steer', {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify({ message: val })
                });
                
                if (res.status === 409) {
                    // Try to start a new run
                    res = await fetch(serverUrl + '/run/start', {
                        method: 'POST',
                        headers: headers,
                    body: JSON.stringify({ idea: val, mode: 'auto' })
                    });
                }
                
                if (!res.ok) {
                    const data = await res.json().catch(()=>({}));
                    appendMessage('System Error', data.detail || 'Failed to send prompt', true);
                } else if (awaitingDecision) {
                    const resume = await fetch(serverUrl + '/run/resume', { method: 'POST', headers: headers });
                    if (!resume.ok) throw new Error('Decision was saved, but the workflow could not resume.');
                    hideCheckpoint();
                }
            } catch (err) {
                appendMessage('System Error', err.message, true);
            } finally {
                steerInput.disabled = false;
                sendBtn.disabled = false;
                steerInput.focus();
            }
        }

        init();
    </script>
</body>
</html>`;
}

export function deactivate() {}
