import re

with open('frontend/css/style.css', 'r') as f:
    css = f.read()

# Update variables
css = re.sub(r'--fs-2xs:\s*10px;', r'--fs-2xs:   10px;', css)
css = re.sub(r'--fs-xs:\s*11px;', r'--fs-xs:    11px;', css)
css = re.sub(r'--fs-sm:\s*12px;', r'--fs-sm:    12px;', css)
css = re.sub(r'--fs-md:\s*13px;', r'--fs-md:    13px;', css)
css = re.sub(r'--fs-lg:\s*14px;', r'--fs-lg:    14px;', css)
css = re.sub(r'--fs-xl:\s*15px;', r'--fs-xl:    16px;', css)
css = re.sub(r'--fs-2xl:\s*18px;', r'--fs-2xl:   20px;', css)

# Increase base body font size and line height
css = re.sub(r'body \{([\s\S]*?)font-size: var\(--fs-md\);([\s\S]*?)\}', r'body {\1font-size: var(--fs-lg); line-height: 1.6;\2}', css)
# Improve markdown line height
css = re.sub(r'\.markdown-body \{\n  font-family: inherit;\n  line-height: 1.5;', r'.markdown-body {\n  font-family: inherit;\n  line-height: 1.6;', css)


# Map hardcoded sizes to variables
replacements = [
    (r'font-size:\s*9px', 'font-size: var(--fs-2xs)'),
    (r'font-size:\s*10px', 'font-size: var(--fs-2xs)'),
    (r'font-size:\s*10\.5px', 'font-size: var(--fs-xs)'),
    (r'font-size:\s*11px', 'font-size: var(--fs-xs)'),
    (r'font-size:\s*11\.5px', 'font-size: var(--fs-sm)'),
    (r'font-size:\s*12px', 'font-size: var(--fs-sm)'),
    (r'font-size:\s*12\.5px', 'font-size: var(--fs-md)'),
    (r'font-size:\s*13px', 'font-size: var(--fs-md)'),
    (r'font-size:\s*13\.5px', 'font-size: var(--fs-lg)'),
    (r'font-size:\s*14px', 'font-size: var(--fs-lg)'),
    (r'font-size:\s*15px', 'font-size: var(--fs-xl)'),
    (r'font-size:\s*16px', 'font-size: var(--fs-xl)'),
    (r'font-size:\s*18px', 'font-size: var(--fs-2xl)'),
    (r'font-size:\s*19px', 'font-size: var(--fs-2xl)'),
    (r'font-size:\s*20px', 'font-size: var(--fs-2xl)')
]

for old, new in replacements:
    # Important rules
    css = re.sub(old + r'\s*!important', new + ' !important', css)
    # Normal rules
    css = re.sub(old + r'([^!])', new + r'\1', css)

with open('frontend/css/style.css', 'w') as f:
    f.write(css)

print("CSS updated successfully")
