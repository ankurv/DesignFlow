import re

with open('backend/server.py', 'r') as f:
    content = f.read()

# 1. Update merged_configs
# Old:
#     def merged_configs(self) -> list[dict]:
#         # If project has its own agents, use ONLY those. Otherwise, fallback to global.
#         if self.configs:
#             return list(self.configs)
#         return load_global_agents()
# New:
#     def merged_configs(self) -> list[dict]:
#         return list(self.configs)

content = re.sub(
    r'def merged_configs\(self\) -> list\[dict\]:[\s\n]*# If project has its own agents, use ONLY those. Otherwise, fallback to global.[\s\n]*if self\.configs:[\s\n]*return list\(self\.configs\)[\s\n]*return load_global_agents\(\)',
    r'def merged_configs(self) -> list[dict]:\n        return list(self.configs)',
    content
)

# 2. Remove load_global_agents, save_global_agents, GLOBAL_AGENTS_PATH
content = re.sub(
    r'GLOBAL_AGENTS_PATH = Path\.home\(\) / "\.designflow" / "global_agents\.json"[\s\S]*?def save_global_agents\(configs: list\[dict\]\):[\s\S]*?GLOBAL_AGENTS_PATH\.write_text\(json\.dumps\(configs_copy, indent=2\)\)',
    '',
    content
)

# 3. Update list_agents
content = re.sub(
    r'@app\.get\("/agents"\)\ndef list_agents\(state: AppState = Depends\(get_state\)\):\n    return {\n        "global": load_global_agents\(\),\n        "project": state\.configs,\n        "merged": state\.merged_configs,\n        "kinds": list\(AGENT_KINDS\.keys\(\)\)\n    }',
    r'@app.get("/agents")\ndef list_agents(state: AppState = Depends(get_state)):\n    return {\n        "agents": state.configs,\n        "kinds": list(AGENT_KINDS.keys())\n    }',
    content
)

# 4. Remove /agents/global routes
content = re.sub(
    r'@app\.get\("/agents/global"\)[\s\S]*?raise HTTPException\(404, "Global agent not found"\)',
    '',
    content
)

with open('backend/server.py', 'w') as f:
    f.write(content)

print("Server updated successfully")
