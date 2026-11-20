# -*- coding: utf-8 -*-
'''把 ch-N.new.json 汇总成 build 需要的 ch-N.json（不覆盖原始草稿）。'''
import io, json, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ROOT
DIR = os.path.join(ROOT, "题库", "_parts", "某书")
dry = '--apply' not in sys.argv
plan = json.load(io.open(os.path.join(DIR, '_plan.json'), encoding='utf-8'))
tot, miss, per = 0, [], {}
for ch in plan['chapters']:
    cid = ch['id']
    src = os.path.join(DIR, cid + '.new.json')
    if not os.path.exists(src):
        miss.append(cid); continue
    arr = json.load(io.open(src, encoding='utf-8'))
    per[cid] = len(arr); tot += len(arr)
    if not dry:
        shutil.copy2(src, os.path.join(DIR, cid + '.json'))
print('汇总: %d 题 / %d 章 | 缺: %s' % (tot, len(per), miss or '无'))
for cid in sorted(per, key=lambda x: int(x.split('-')[1])):
    ch = next(c for c in plan['chapters'] if c['id'] == cid)
    flag = '' if per[cid] == ch['quota'] else '  <-- 配额 %d' % ch['quota']
    print('   %-7s %3d 题%s' % (cid, per[cid], flag))
if dry: print('\n（干跑，未写文件；加 --apply 才复制）')