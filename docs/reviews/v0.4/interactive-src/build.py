from pathlib import Path
import html
import re
import os

ROOT = Path(__file__).resolve().parents[3]
WORK = Path(__file__).resolve().parent

SOURCE_DIR=ROOT

def inline(text):
    codes=[]
    def code(m):
        codes.append('<code>'+html.escape(m[1])+'</code>')
        return 'INLINECODETOKEN'+str(len(codes)-1)+'ENDTOKEN'
    text=re.sub(r'`([^`]+)`',code,text)
    text=html.escape(text)
    def link(m):
        url=html.unescape(m[2])
        if not re.match(r'^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|/|#)',url):
            url=os.path.relpath(SOURCE_DIR/url,ROOT)
        return '<a href="'+html.escape(url,quote=True)+'">'+m[1]+'</a>'
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',text)
    for i,c in enumerate(codes):text=text.replace('INLINECODETOKEN'+str(i)+'ENDTOKEN',c)
    return text

def md(text,source_dir=ROOT):
    global SOURCE_DIR
    SOURCE_DIR=source_dir
    lines=text.splitlines();out=[];i=0
    while i<len(lines):
        line=lines[i]
        if not line.strip():i+=1;continue
        if line.startswith('```'):
            lang=line[3:].strip();i+=1;block=[]
            while i<len(lines) and not lines[i].startswith('```'):block.append(lines[i]);i+=1
            out.append('<pre><code data-language="'+html.escape(lang)+'">'+html.escape('\n'.join(block))+'</code></pre>');i+=1;continue
        m=re.match(r'^(#{1,6}) (.+)',line)
        if m:
            n=len(m[1]);out.append(f'<h{n}>'+inline(m[2])+f'</h{n}>');i+=1;continue
        if line.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].startswith('|'):
                row=[x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?',x) for x in row):rows.append(row)
                i+=1
            if rows:
                out.append('<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+inline(x)+'</th>' for x in rows[0])+'</tr></thead><tbody>')
                for row in rows[1:]:out.append('<tr>'+''.join('<td>'+inline(x)+'</td>' for x in row)+'</tr>')
                out.append('</tbody></table></div>')
            continue
        if re.match(r'^\s*[-*] ',line) or re.match(r'^\d+\. ',line):
            ordered=bool(re.match(r'^\d+\. ',line));tag='ol' if ordered else 'ul';out.append('<'+tag+'>')
            while i<len(lines) and (re.match(r'^\s*[-*] ',lines[i]) or re.match(r'^\d+\. ',lines[i])):
                out.append('<li>'+inline(re.sub(r'^\s*(?:[-*]|\d+\.) ','',lines[i]))+'</li>');i+=1
            out.append('</'+tag+'>');continue
        if re.fullmatch(r'---+',line.strip()):out.append('<hr>');i+=1;continue
        para=[line];i+=1
        while i<len(lines) and lines[i].strip() and not re.match(r'^(?:#|\||```|[-*] |\d+\. )',lines[i]):para.append(lines[i]);i+=1
        out.append('<p>'+inline(' '.join(para))+'</p>')
    return '\n'.join(out)

prd=(ROOT/'PRD-创作平台与画布Agent-v0.4.md').read_text()
techpath=ROOT/'技术方案-创作平台与画布Agent-v0.4.md'
discussionpath=ROOT/'五角色评审纪要-v0.4.md'
tech=techpath.read_text() if techpath.exists() else '# 技术方案\n完整技术方案正在汇总中。'
discussion=discussionpath.read_text() if discussionpath.exists() else '# 五角色讨论\n正在进行第二轮质询。'
roles=[]
for p in sorted((ROOT/'reviews/v0.4').glob('0[1-5]-*.md')):
    roles.append('<details><summary>'+html.escape(p.stem)+'</summary><article class="prose">'+md(p.read_text(),p.parent)+'</article></details>')
checks=[]
for line in prd.splitlines():
    if re.match(r'^\| A\d\d ',line):
        cols=[c.strip() for c in line.strip('|').split('|')]
        code=cols[0].split(' ')[0]
        checks.append('<label class="check-row"><input type="checkbox" data-check="'+code+'"><div><strong>'+inline(cols[0])+'</strong><span>'+inline(cols[1])+'</span></div></label>')
output=(WORK/'wrapper.html').read_text()
for key,value in {'PROTOTYPE':html.escape((WORK/'prototype.html').read_text(),quote=True),'PRD':md(prd),'TECH':md(tech),'DISCUSSION':md(discussion),'ROLE_REVIEWS':'\n'.join(roles),'CHECKS':'\n'.join(checks)}.items():
    output=output.replace('{{'+key+'}}',value)
path=ROOT/'交互PRD-创作平台-v0.4.html'
path.write_text(output)
print(str(path),path.stat().st_size,'bytes',len(checks),'acceptance rows',len(roles),'role reports')

# 独立产品效果页与文档内演练复用同一份状态机。
prototype=(WORK/'prototype.html').read_text().replace("aivideo-prd-v04-demo-1", "aivideo-product-v04-demo-1")
prototype=prototype.replace('</style>', '\n'+(WORK/'product-extension.css').read_text()+'\n</style>', 1)
marker="if(doc()){selected=doc().mask?.source||null;"
assert marker in prototype, 'Prototype startup marker changed'
prototype=prototype.replace(marker, (WORK/'product-extension.js').read_text()+'\n'+marker, 1)
product=(WORK/'product-shell.html').read_text().replace('{{PRODUCT_PROTOTYPE}}', html.escape(prototype,quote=True))
product_path=ROOT/'产品效果-创作平台-v0.4.html'
product_path.write_text(product)
print(str(product_path),product_path.stat().st_size,'bytes')
