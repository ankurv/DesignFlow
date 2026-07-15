import re

with open('frontend/js/agents.js', 'r') as f:
    js = f.read()

js = re.sub(r'if \(isGlobal\) \{[\s\S]*?\}', '', js)
js = re.sub(r'const badge = isGlobal[\s\S]*?;', '', js)

# Fix eyebrow
js = re.sub(r'\<span class="agent-editor-eyebrow"\>.*?\</span\>', '<span class="agent-editor-eyebrow">Project team</span>', js)

# Fix saveAgent signature
js = js.replace('saveAgent(\'${agentId}\', ${isGlobal})', 'saveAgent(\'${agentId}\')')
js = js.replace('async function saveAgent(agentId, isGlobal)', 'async function saveAgent(agentId)')

# Fix startEditAgent
js = js.replace('function startEditAgent(uid, isGlobal, idx)', 'function startEditAgent(uid, idx)')

with open('frontend/js/agents.js', 'w') as f:
    f.write(js)

with open('frontend/js/state.js', 'r') as f:
    state_js = f.read()

state_js = state_js.replace('const isGlobal = !projectOpen;', '')
state_js = state_js.replace('const url = isGlobal ? \'/agents/global\' : \'/agents\';', 'const url = \'/agents\';')

with open('frontend/js/state.js', 'w') as f:
    f.write(state_js)

print("Remaining isGlobal references fixed")
