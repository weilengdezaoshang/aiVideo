"""Build a portable visual prototype from the established v0.4 state machine."""
from pathlib import Path
import base64
import html
import json
import re

HERE = Path(__file__).resolve().parent
DOCS = HERE.parents[2]
BASE = DOCS / 'reviews/v0.4/interactive-src'
REPO = DOCS.parent

def data_url(path, mime):
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode()

assets = {key: data_url(HERE / f'assets/{key}.jpg', 'image/jpeg') for key in ('architecture', 'flower', 'ocean', 'tea')}
names = {'arrow':'arrow-up-right', 'plus':'plus-lg', 'grid':'grid', 'image':'image', 'video':'camera-video', 'home':'house', 'folder':'folder', 'sparkles':'stars', 'moon':'moon', 'sun':'sun'}
icons = {key: (REPO / f'web/vendor/shoelace/cdn/assets/icons/{name}.svg').read_text().replace('<svg ', '<svg aria-hidden="true" ') for key, name in names.items()}
font = data_url(HERE / 'assets/display.ttf', 'font/ttf')
prototype = (BASE / 'prototype.html').read_text().replace('aivideo-prd-v04-demo-1', 'aivideo-product-v05-demo-1')
theme = (HERE / 'theme.css').read_text()
styles = (BASE / 'product-extension.css').read_text() + '\n' + (HERE / 'app.css').read_text() + '\n' + theme
styles = styles.replace('{{DISPLAY_FONT}}', font)
prototype = prototype.replace('</style>', '\n' + styles + '\n</style>', 1)
marker = 'if(doc()){selected=doc().mask?.source||null;'
assert marker in prototype
script = 'const VISUAL_ASSETS=' + json.dumps(assets) + ';const ICONS=' + json.dumps(icons) + ';\n'
extension = (BASE / 'product-extension.js').read_text()
extension = extension.replace('咖啡店视觉探索 · 示例', '视觉灵感集 · 示例').replace('午后的咖啡店，安静、有自然光。', '打开这组展示素材，比较构图并体验画布编辑。').replace('咖啡店的四种视觉方向', '四种视觉灵感 · 展示素材').replace("['正面构图','逆光侧影','空间全景','局部细节']", "['空间光影','自然静物','山海之间','日常灵感']")
script += extension + '\n' + (HERE / 'app.js').read_text() + '\n'
prototype = prototype.replace(marker, script + marker, 1)
output = (HERE / 'product-shell.html').read_text().replace('</style>', '\n' + theme + '\n' + (HERE / 'landing.css').read_text() + '\n</style>', 1)
output = output.replace('{{LANDING}}', (HERE / 'landing.html').read_text())
output = output.replace('{{LANDING_SCRIPT}}', (HERE / 'landing.js').read_text())
replacements = {'PRODUCT_PROTOTYPE': html.escape(prototype, quote=True), 'ASSETS_JSON': json.dumps(assets), 'DISPLAY_FONT': font}
replacements['MEDIUM_FONT'] = data_url(HERE / 'assets/display-medium.ttf', 'font/ttf')
replacements['EDITOR_PREVIEW'] = data_url(HERE / 'assets/editor-preview.jpg', 'image/jpeg')
replacements.update({'ASSET_' + k.upper(): v for k, v in assets.items()})
replacements.update({'ICON_' + k.upper(): v for k, v in icons.items()})
for key, value in replacements.items():
    output = output.replace('{{' + key + '}}', value)
assert not re.search(r'\{\{[A-Z_]+\}\}', output), 'Unresolved template token'
path = DOCS / '产品效果-创作平台-v0.5.html'
path.write_text(output)
print(path, path.stat().st_size, 'bytes')
