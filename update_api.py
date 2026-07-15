import re

with open('frontend/js/api.js', 'r') as f:
    js = f.read()

# Fix agentCapacityStatus updating
js = re.sub(
    r"\['global-', 'project-'\]\.forEach\(prefix => \{\s*const uid = prefix \+ baseId;\s*const current = agentCapacityStatus\[uid\] \|\| \{total_tokens:0, cost_usd:0, pricing_known:true\};\s*current\.total_tokens \+= Number\(agent\.total_tokens \|\| 0\);\s*current\.cost_usd \+= Number\(agent\.cost_usd \|\| 0\);\s*current\.pricing_known = current\.pricing_known && agent\.pricing_known !== false;\s*if \(agent\.retry_at\) current\.retry_at = agent\.retry_at;\s*if \(agent\.status === 'error'\) \{\s*current\.runtime_status = 'error';\s*current\.error = agent\.error_message \|\| 'Agent execution failed';\s*\}\s*agentCapacityStatus\[uid\] = current;\s*\}\);",
    r"const current = agentCapacityStatus[baseId] || {total_tokens:0, cost_usd:0, pricing_known:true};\n    current.total_tokens += Number(agent.total_tokens || 0);\n    current.cost_usd += Number(agent.cost_usd || 0);\n    current.pricing_known = current.pricing_known && agent.pricing_known !== false;\n    if (agent.retry_at) current.retry_at = agent.retry_at;\n    if (agent.status === 'error') {\n      current.runtime_status = 'error';\n      current.error = agent.error_message || 'Agent execution failed';\n    }\n    agentCapacityStatus[baseId] = current;",
    js
)

# Fix failedTurn agentCapacityStatus updating
js = re.sub(
    r"\['global-', 'project-'\]\.forEach\(prefix => \{\s*const uid = prefix \+ failedBaseId;\s*const current = agentCapacityStatus\[uid\] \|\| \{\};\s*current\.runtime_status = 'error';\s*current\.error_code = failedTurn\.error_code \|\| '';\s*current\.error = failedTurn\.public_error \|\| failedTurn\.error \|\| 'Agent execution failed';\s*agentCapacityStatus\[uid\] = current;\s*\}\);",
    r"const current = agentCapacityStatus[failedBaseId] || {};\n    current.runtime_status = 'error';\n    current.error_code = failedTurn.error_code || '';\n    current.error = failedTurn.public_error || failedTurn.error || 'Agent execution failed';\n    agentCapacityStatus[failedBaseId] = current;",
    js
)

# Fix visibleAgents fallback to use .agents instead of .merged
js = re.sub(r'visibleAgents = \(configured\.merged \|\| \[\]\)\.map\(a =>', r'visibleAgents = (configured.agents || []).map(a =>', js)

with open('frontend/js/api.js', 'w') as f:
    f.write(js)

print("API JS updated successfully")
