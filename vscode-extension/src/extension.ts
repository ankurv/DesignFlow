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
        const serverUrl = config.get<string>('serverUrl') || 'http://127.0.0.1:8010';
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
    
    <div id="composer">
        <input type="text" id="steerInput" placeholder="Ask agents to design something..." disabled onkeydown="if(event.key === 'Enter') sendSteer()">
        <button id="sendBtn" onclick="sendSteer()" disabled>Send</button>
    </div>

    <script>
        const serverUrl = '${serverUrl}';
        const user = '${safeUser}';
        const pass = '${safePass}';
        const projectPath = '${safeProject}';
        let sessionId = '';
        let eventSource = null;

        const feed = document.getElementById('feed');
        const statusMsg = document.getElementById('statusMsg');
        const steerInput = document.getElementById('steerInput');
        const sendBtn = document.getElementById('sendBtn');

        function appendMessage(agent, text, isError = false) {
            const div = document.createElement('div');
            div.className = 'feed-item' + (isError ? ' error' : '');
            
            // Very simple markdown-like rendering for bold and code
            const formatted = String(text)
                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                .replace(/\`(.*?)\`/g, '<code style="background:rgba(0,0,0,0.3);padding:2px 4px;border-radius:4px;">$1</code>')
                .replace(/\n/g, '<br>');

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
                
                const res = await fetch(serverUrl + '/steer', {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify({ message: val })
                });
                
                if (!res.ok) {
                    const data = await res.json().catch(()=>({}));
                    appendMessage('System Error', data.detail || 'Failed to send prompt', true);
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
