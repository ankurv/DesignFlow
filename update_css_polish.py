import re

with open('frontend/css/style.css', 'r') as f:
    css = f.read()

# 1. Custom Scrollbars
scrollbar_css = """
/* Custom Scrollbar */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: var(--border2);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
  background: var(--muted);
}
"""
if "::-webkit-scrollbar" not in css:
    css = css + "\n" + scrollbar_css

# 2. Fix steer-bar input flex
css = re.sub(r'#steerInput\s*\{[\s\S]*?\}', r'#steerInput { flex: 1; min-width: 0; padding: 12px 16px; border-radius: 12px; border: 1px solid var(--border); background: var(--bg2); color: var(--text); outline: none; transition: border-color 0.2s; }', css)

if '#steerInput {' not in css:
    # Maybe it uses .steer-bar input
    css = re.sub(r'\.steer-bar input\s*\{[\s\S]*?\}', r'.steer-bar input { flex: 1; min-width: 0; padding: 12px 16px; border-radius: 12px; border: 1px solid var(--border); background: var(--bg2); color: var(--text); outline: none; transition: border-color 0.2s; font-size: var(--fs-md); }', css)

# 3. Enhance padding in agent-card and feed-item
css = re.sub(r'\.agent-card\s*\{([\s\S]*?padding:\s*)16px;([\s\S]*?)\}', r'.agent-card {\1 20px;\2}', css)
css = re.sub(r'\.feed-item\s*\{([\s\S]*?padding:\s*)16px;([\s\S]*?)\}', r'.feed-item {\1 20px;\2}', css)

with open('frontend/css/style.css', 'w') as f:
    f.write(css)

print("CSS Polish applied successfully")
