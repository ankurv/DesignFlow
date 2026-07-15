import re

with open('frontend/css/style.css', 'r') as f:
    css = f.read()

# Fix .btn-secondary colors
css = re.sub(r'\.btn-secondary \{[\s\S]*?\}', 
             r'.btn-secondary { \n    background: var(--bg3);\n    color: var(--text); \n    border-color: var(--border2);\n  }', css)

css = re.sub(r'\.btn-secondary:hover \{[\s\S]*?\}', 
             r'.btn-secondary:hover { \n    border-color: var(--muted);\n    background: var(--border);\n    color: var(--text);\n    transform: translateY(-1px); \n    box-shadow: none; \n  }', css)

# Add btn size variants after .btn
btn_classes = """  .btn-sm { padding: 5px 10px; font-size: var(--fs-xs); border-radius: 8px; }
  .btn-lg { padding: 10px 18px; font-size: var(--fs-md); border-radius: 12px; }
"""
css = css.replace('.btn-primary {', btn_classes + '\n  .btn-primary {')

# Remove aggressive overrides
css = re.sub(r'#openProjectBtn,\s*#dashboardView \.btn,\s*#fileViewContainer \.btn,\s*#panel-settings \.btn,\s*#panel-config \.btn,\s*#panel-mcp \.btn,\s*#panel-history \.btn\s*\{[\s\S]*?\}', '', css)
css = re.sub(r'#loginModal \.btn\s*\{[\s\S]*?\}', '', css)
css = re.sub(r'#projectOpenModal \.btn\s*\{[\s\S]*?\}', '', css)

# agent card actions button
css = re.sub(r'\.agent-card-actions \.btn\s*\{[\s\S]*?\}', r'.agent-card-actions .btn { min-height: 26px; }', css)

with open('frontend/css/style.css', 'w') as f:
    f.write(css)

print("Buttons CSS updated successfully")
