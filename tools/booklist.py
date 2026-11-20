# -*- coding: utf-8 -*-
"""重新生成 书单.md（43 本当前数字 + 题库题数）。

用法：python tools\\booklist.py
数据来源：books\\*\\book.json 的 stats/shelfCategory + 题库\\**\\*.json（同书取题数最多的一份）
"""

from paths import ROOT
import io, os, json, glob, re, time
root = ROOT
B = os.path.join(root,'books')
# 题库：同 bookId 取题数最多的一份（正式版）
banks = {}
for fp in glob.glob(os.path.join(root,'题库','**','*.json'), recursive=True):
    base = os.path.basename(fp)
    if base.startswith('_') or '.bak' in base: continue
    try: d = json.load(open(fp, encoding='utf-8'))
    except Exception: continue
    if not isinstance(d, dict) or not d.get('bookId'): continue
    n = len(d.get('questions') or [])
    if n > banks.get(d['bookId'], (0,''))[0]: banks[d['bookId']] = (n, fp)
rows = []
for dn in sorted(os.listdir(B)):
    fp = os.path.join(B, dn, 'book.json')
    if not os.path.exists(fp): continue
    b = json.load(open(fp, encoding='utf-8'))
    s = b.get('stats',{})
    qb = banks.get(dn, (0,''))[0]
    rows.append(dict(title=b.get('bookTitle') or dn, author=b.get('author') or '—',
                     pages=s.get('pages',0), chars=s.get('chars',0), chapters=s.get('chapters',0),
                     figs=s.get('figures',0), paired=s.get('paired',0), qbank=qb, cat=b.get('shelfCategory') or '扩展'))
tp = sum(r['pages'] for r in rows); tc = sum(r['chars'] for r in rows)
tch = sum(r['chapters'] for r in rows); tf = sum(r['figs'] for r in rows); tp2 = sum(r['paired'] for r in rows)
tq = sum(r['qbank'] for r in rows)
def table(items, start=1):
    out = ['| # | 书名 | 作者 | 页 | 万字 | 章 | 图 | 配注率 | 题库 |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for i, r in enumerate(sorted(items, key=lambda x: x['title']), start):
        rate = '—' if not r['figs'] else ('100%' if r['paired']==r['figs'] else '%.1f%%' % (100.0*r['paired']/r['figs']))
        out.append('| %d | %s | %s | %d | %.1f | %d | %d | %s | %s |' % (
            i, r['title'], r['author'], r['pages'], r['chars']/10000.0, r['chapters'], r['figs'], rate,
            ('✅ %d 题' % r['qbank']) if r['qbank'] else ''))
    return out
jiaoc = [r for r in rows if r['cat']=='教材']; ext = [r for r in rows if r['cat']!='教材']
L = []
L.append('# 筑学书库 · 书单（%d 本）' % len(rows))
L.append('')
L.append('> 生成时间：%s ｜ 数据来源：各书 book.json 的 stats / shelfCategory + 题库 JSON' % time.strftime('%Y-%m-%d %H:%M'))
L.append('>')
L.append('> 本页由 tools 现场汇总生成；逐本质检数字见 交接文档\\质检-复查汇总.md。')
L.append('')
L.append('**合计：%d 本 · %s 页 · %.1f 万字 · %d 章 · %s 张插图（配注 %s）**' % (len(rows), format(tp,','), tc/10000.0, tch, format(tf,','), format(tp2,',')))
L.append('')
L.append('分类：**教材 %d 本**（考研主教材）+ **扩展 %d 本**（参考/理论/图录）；**题库 %d 套 / %s 题**。书架顶部可按分类筛选、下拉快速跳转。' % (len(jiaoc), len(ext), sum(1 for r in rows if r['qbank']), format(tq,',')))
L.append('')
L.append('## 教材（%d 本） · %.1f 万字 · %d 页' % (len(jiaoc), sum(r['chars'] for r in jiaoc)/10000.0, sum(r['pages'] for r in jiaoc)))
L.append('')
L += table(jiaoc)
L.append('')
L.append('## 扩展书籍（%d 本） · %.1f 万字 · %s 页' % (len(ext), sum(r['chars'] for r in ext)/10000.0, format(sum(r['pages'] for r in ext),',')))
L.append('')
L += table(ext, 1)
L.append('')
p = os.path.join(root, '交接文档', '书单.md')
io.open(p+'.tmp','w',encoding='utf-8',newline='\n').write('\n'.join(L))
os.replace(p+'.tmp', p)
print('交接文档/书单.md 重新生成：', len(L), '行 |', len(rows), '本 | 页', tp, '| 字', tc, '| 章', tch, '| 图', tf, '| 配注', tp2, '| 题库', tq)