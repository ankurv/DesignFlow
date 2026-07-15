import re

with open('frontend/index.html', 'r') as f:
    html = f.read()

# Remove agent scope banner
html = re.sub(
    r'<div class="agent-scope-banner">[\s\S]*?<div style="display:flex; flex-direction:column; gap:2px;">[\s\S]*?<span id="scopeLabel"[\s\S]*?</div>[\s\S]*?<button class="btn btn-primary btn-sm" onclick="addNewAgentCard\(\)">＋ Add Agent</button>[\s\S]*?</div>',
    r'<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">\n            <h2 style="margin:0; font-family:var(--heading-font); font-size:18px;">Project Agents</h2>\n            <button class="btn btn-primary btn-sm" onclick="addNewAgentCard()">＋ Add Agent</button>\n          </div>',
    html
)

with open('frontend/index.html', 'w') as f:
    f.write(html)

print("Index HTML updated successfully")
