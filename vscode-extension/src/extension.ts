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

        // We embed the localhost server via iframe
        // The project path could be passed via query string if the backend supported it,
        // but for now the user can select it in the UI or it remembers the last one.
        panel.webview.html = getWebviewContent();
    });

    context.subscriptions.push(disposable);
}

function getWebviewContent() {
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
        }
        iframe {
            border: none;
            width: 100%;
            height: 100%;
        }
        .error-message {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: var(--vscode-editor-foreground);
            font-family: var(--vscode-font-family);
            text-align: center;
            padding: 20px;
        }
        .code {
            background: var(--vscode-textCodeBlock-background);
            padding: 4px 8px;
            border-radius: 4px;
            font-family: monospace;
        }
    </style>
</head>
<body>
    <iframe id="df-frame" src="http://127.0.0.1:8010" onload="hideError()" onerror="showError()"></iframe>
    
    <div id="error-overlay" class="error-message" style="display: none; position: absolute; top: 0; left: 0; width: 100%; background: var(--vscode-editor-background);">
        <h2>Cannot connect to DesignFlow backend</h2>
        <p>Make sure the local server is running on port 8010.</p>
        <p>Run <span class="code">python run.py</span> in your terminal, then reload this tab.</p>
    </div>

    <script>
        const frame = document.getElementById('df-frame');
        const overlay = document.getElementById('error-overlay');
        
        function hideError() {
            // Simple check: if we can't access frame contentDocument, it means it loaded a different origin successfully (CORS).
            // If it failed to load completely, some browsers fire onload but it's tricky to detect perfectly cross-origin.
            // For now, assume onload means success.
            overlay.style.display = 'none';
        }

        // Periodically ping to ensure server is alive, otherwise show overlay
        setInterval(() => {
            fetch('http://127.0.0.1:8010/')
                .then(() => overlay.style.display = 'none')
                .catch(() => overlay.style.display = 'flex');
        }, 3000);
    </script>
</body>
</html>`;
}

export function deactivate() {}
