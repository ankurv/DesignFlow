import re

with open('frontend/js/api.js', 'r') as f:
    js = f.read()

# Fix agent card buttons
js = js.replace('<button class="btn btn-secondary" onclick="editAgent(\'${agent.name}\')">Config</button>', '<button class="btn btn-secondary btn-sm" onclick="editAgent(\'${agent.name}\')">Config</button>')
js = js.replace('<button class="btn btn-danger" onclick="deleteAgent(\'${agent.name}\')">Remove</button>', '<button class="btn btn-danger btn-sm" onclick="deleteAgent(\'${agent.name}\')">Remove</button>')

# Fix feed details expand button
js = js.replace('<button class="btn btn-secondary" style="padding: 2px 7px; font-size: 11px; font-weight: bold; line-height: 1; border-radius: 4px;"', '<button class="btn btn-secondary btn-sm" style="padding: 2px 7px; font-weight: bold; line-height: 1;"')

# Fix delete user button
js = js.replace('<button class="btn" style="background:#dc3545; color:white;"', '<button class="btn btn-danger btn-sm"')
js = js.replace('<button class="btn btn-secondary" onclick="resetUserPassword', '<button class="btn btn-secondary btn-sm" onclick="resetUserPassword')

# mcp table buttons
js = js.replace('<button class="btn btn-secondary" onclick="editMCP(', '<button class="btn btn-secondary btn-sm" onclick="editMCP(')
js = js.replace('<button class="btn btn-danger" onclick="deleteMCP(', '<button class="btn btn-danger btn-sm" onclick="deleteMCP(')

with open('frontend/js/api.js', 'w') as f:
    f.write(js)

print("Buttons API updated successfully")
