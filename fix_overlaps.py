import re

with open('frontend/css/style.css', 'r') as f:
    css = f.read()

# Fix feed item padding
css = re.sub(r'\.feed-item \{ border-radius: var\(--radius\); padding: 14px 16px;', r'.feed-item { border-radius: var(--radius); padding: 16px 20px;', css)

# Prevent text overlap in agent cards
css = re.sub(r'\.agent-card-name \{ font-family: var\(--heading-font\); font-weight: 700; font-size: var\(--fs-xl\); flex: 1; color: var\(--text\); \}', r'.agent-card-name { font-family: var(--heading-font); font-weight: 700; font-size: var(--fs-xl); flex: 1; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }', css)

# Fix feed agent details overflow
if 'white-space: nowrap' not in css.split('.feed-agent-details')[1][:150]:
    css = re.sub(r'\.feed-agent-details \{ display: flex; align-items: center; gap: 8px; \}', r'.feed-agent-details { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }', css)

# Improve feed text readability with better line height and margin
css = re.sub(r'\.feed-text \{ margin-top: 6px; font-size: var\(--fs-lg\); \}', r'.feed-text { margin-top: 10px; font-size: var(--fs-lg); line-height: 1.6; color: var(--text-muted); }', css)

# Improve markdown body padding
css = re.sub(r'\.markdown-body \{\n  font-family: inherit;\n  line-height: 1.6;\n\}', r'.markdown-body {\n  font-family: inherit;\n  line-height: 1.6;\n  padding: 0 4px;\n}', css)

with open('frontend/css/style.css', 'w') as f:
    f.write(css)

print("Overlap fixes applied")
