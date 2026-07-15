import re

with open('frontend/index.html', 'r') as f:
    html = f.read()

# Login button
html = html.replace('class="btn btn-primary" onclick="submitLogin()" style="width:100%; padding:10px 12px; font-size:12.5px;"', 'class="btn btn-primary btn-lg" onclick="submitLogin()" style="width:100%;"')

# Open Project Modal Buttons
html = html.replace('class="btn btn-secondary" onclick="closeProjectModal()" style="padding:6px 10px;"', 'class="btn btn-secondary" onclick="closeProjectModal()"')
html = html.replace('class="btn btn-primary" onclick="submitProjectPath()"', 'class="btn btn-primary" onclick="submitProjectPath()"')

# Dashboard openProjectBtn
html = html.replace('class="btn btn-secondary" id="openProjectBtn" onclick="openProject()" style="padding: 6px 12px; font-size: 12px; border-radius: 20px;"', 'class="btn btn-secondary btn-sm" id="openProjectBtn" onclick="openProject()" style="border-radius: 20px;"')

# Settings / Save
html = html.replace('class="btn btn-secondary" onclick="updateTokens()" style="padding:8px 16px;"', 'class="btn btn-secondary" onclick="updateTokens()"')
html = html.replace('class="btn btn-secondary" onclick="changeMyPassword()" style="padding:10px 20px;"', 'class="btn btn-secondary btn-lg" onclick="changeMyPassword()"')
html = html.replace('class="btn btn-primary" onclick="addUser()" style="padding:8px 14px;"', 'class="btn btn-primary" onclick="addUser()"')

# New File button
html = html.replace('class="btn btn-secondary" onclick="createNewFile()" style="padding:5px 9px; font-size:10.5px"', 'class="btn btn-secondary btn-sm" onclick="createNewFile()"')

# Workflow Templates
html = html.replace('class="btn btn-secondary" onclick="applyWorkflowTemplate(\'resolve_issue\')" style="padding:6px 12px; font-size:12px"', 'class="btn btn-secondary btn-sm" onclick="applyWorkflowTemplate(\'resolve_issue\')"')
html = html.replace('class="btn btn-secondary" onclick="applyWorkflowTemplate(\'redebate_decision\')" style="padding:6px 12px; font-size:12px"', 'class="btn btn-secondary btn-sm" onclick="applyWorkflowTemplate(\'redebate_decision\')"')
html = html.replace('class="btn btn-secondary" onclick="applyWorkflowTemplate(\'refine_plan\')" style="padding:6px 12px; font-size:12px"', 'class="btn btn-secondary btn-sm" onclick="applyWorkflowTemplate(\'refine_plan\')"')

# Add Agent
html = html.replace('class="btn btn-primary" onclick="addNewAgentCard()" style="font-size:12px; padding:6px 12px"', 'class="btn btn-primary btn-sm" onclick="addNewAgentCard()"')

# Add MCP
html = html.replace('class="btn btn-primary" onclick="document.getElementById(\'mcpAddForm\').style.display=\'flex\'" style="font-size:12px; padding:6px 12px"', 'class="btn btn-primary btn-sm" onclick="document.getElementById(\'mcpAddForm\').style.display=\'flex\'"')
html = html.replace('class="btn btn-secondary" style="padding:4px 8px; font-size:11px" onclick="document.getElementById(\'mcpAddForm\').style.display=\'none\'"', 'class="btn btn-secondary btn-sm" onclick="document.getElementById(\'mcpAddForm\').style.display=\'none\'"')

# Edit agent card actions - wait, those use button directly without inline style. Let's see if there are any others.

with open('frontend/index.html', 'w') as f:
    f.write(html)

print("Buttons HTML updated successfully")
