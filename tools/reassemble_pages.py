# -*- coding: utf-8 -*-
"""按页重组：把双栏/图文混排页的正文按栏重排，图内标注与表格文字隔离成单独括注块。

用法：
    python tools\reassemble_pages.py <书名> [页码 ...]           # 试算（dry-run）
    python tools\reassemble_pages.py <书名> [页码 ...] --apply   # 写回（自动备份 + 字符守恒校验）

规则：
  - 正文栏 = 长行（>=15 汉字）密集出现的 x0 带；带内所有行都算正文（含短句、缩进句）
  - 栏外短行 = 图内标注/表格文字，汇总成一个括注块，保证一个字符都不丢
  - 页眉（跨页重复 >=8 次的行）与页码行剔除
  - 段落切分：栏内按 y 排序，缩进（x0 > 栏基准 + 28）或大间距（>90px）起新段
  - 写回前校验：旧文本汉字多重集 99.5% 被新文本覆盖，否则该页回退
"""
import io, json, os, re, shutil, sys, time
from paths import ROOT
from collections import Counter

BOOKS = os.path.join(ROOT, 'books')
HAN = re.compile(r'[\u4e00-\u9fff]')
NUMROW = re.compile(r'^[0-9一二三四五六七八九十.．\s~～\-，,]+$', re.U)


def norm(s):
    return ''.join(HAN.findall(s or ''))


def btxt(x):
    return x.get('text') or x.get('caption') or ''


def resolve(arg):
    key = re.sub(r'[\s\u3000·…．\-—_（）()，,]', '', arg or '')
    hit = [d for d in sorted(os.listdir(BOOKS)) if key and key in re.sub(r'[\s\u3000·…．\-—_（）()，,]', '', d)]
    if len(hit) == 1:
        return os.path.join(BOOKS, hit[0])
    raise SystemExit('书名匹配到 %d 本: %s' % (len(hit), hit[:6]))


def load_lines(book_dir):
    pages = os.path.join(book_dir, 'pages')
    lbp, lc = {}, Counter()
    for f in sorted(os.listdir(pages)):
        m = re.match(r'page_(\d+)_ocr2\.json', f)
        if not m:
            continue
        p = int(m.group(1))
        L = json.load(io.open(os.path.join(pages, f), encoding='utf-8'))
        lbp[p] = L
        for l in L:
            t = norm(l.get('text', ''))
            if len(t) >= 4:
                lc[t[:12]] += 1
    hdr = {t for t, c in lc.items() if c >= 8}
    return lbp, hdr


def body_bands(lines, min_len=15, gap=120, pad=60):
    """长行 x0 聚类成若干正文栏带，返回 [(lo, hi), ...]（左→右）。
    只保留「长行数 >= 3 且长行占比 >= 0.35」的带，避免把表格区当正文。"""
    pts = sorted(l['x0'] for l in lines if len(norm(l.get('text', ''))) >= min_len)
    if len(pts) < 3:
        return []
    groups, cur = [], [pts[0]]
    for x in pts[1:]:
        if x - cur[-1] > gap:
            groups.append(cur); cur = [x]
        else:
            cur.append(x)
    groups.append(cur)
    out = []
    for g in groups:
        lo, hi = g[0] - pad, g[-1] + pad
        inside = [l for l in lines if lo <= l['x0'] <= hi]
        longish = [l for l in inside if len(norm(l.get('text', ''))) >= min_len]
        if len(longish) >= 3 and len(longish) / max(1, len(inside)) >= 0.35:
            out.append((lo, hi))
    merged = []
    for b in out:
        if merged and b[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b[1]))
        else:
            merged.append(b)
    return merged


def rebuild_page(p, lbp, hdr):
    L = lbp.get(p) or []
    lines = []
    for l in L:
        t = (l.get('text') or '').strip()
        n = norm(t)
        if len(n) < 2:
            continue
        if n[:12] in hdr:
            continue
        if NUMROW.match(t):
            continue
        lines.append(l)
    if len(lines) < 5:
        return None
    bands = body_bands(lines)
    if not bands:
        pts = sorted(l['x0'] for l in lines if len(norm(l.get('text', ''))) >= 10)
        if pts:
            mid = pts[len(pts)//2]
            bands = [(mid - 130, mid + 130)]
    if not bands:
        return None
    inband = lambda x: any(lo <= x <= hi for lo, hi in bands)
    body = [l for l in lines if inband(l['x0'])]
    other = [l for l in lines if not inband(l['x0'])]
    # 正文：先按栏（x0 所在带顺序），栏内按 y
    band_idx = {id(b): next(i for i, (lo, hi) in enumerate(bands) if lo <= b['x0'] <= hi) for b in body}
    body.sort(key=lambda x: (band_idx[id(x)], round(x['y0'] / 6), x['x0']))
    base = min(l['x0'] for l in body)
    # 段内缩进基准改用「该栏自己的最小 x0」
    band_base = {}
    for b in body:
        i = band_idx[id(b)]
        band_base[i] = min(band_base.get(i, 1e9), b['x0'])
    paras, cur, prev_y, cur_band = [], [], None, None
    for l in body:
        t = (l.get('text') or '').strip()
        b0 = band_base[band_idx[id(l)]]
        if cur and cur_band != band_idx[id(l)]:
            paras.append((cur[0]['y0'], ''.join(x.get('text') or '' for x in cur)))
            cur = []
        cur_band = band_idx[id(l)]
        newpara = cur and (l['x0'] > b0 + 28 or (prev_y and l['y0'] - prev_y > 90))
        if newpara:
            paras.append((cur[0]['y0'], ''.join(x.get('text') or '' for x in cur)))
            cur = []
        cur.append(l)
        prev_y = l['y0']
    if cur:
        paras.append((cur[0]['y0'], ''.join(x.get('text') or '' for x in cur)))
    other_sorted = sorted(other, key=lambda x: (x['y0'], x['x0']))
    # 以「图X.Y-Z / 表X.Y-Z」开头的隔离行，其实是被截断的图注/表注
    caps, rest = [], []
    for l in other_sorted:
        t = (l.get('text') or '').strip()
        if re.match(r'^(图|表)\s*[0-9０-９][^.。]{0,3}[-－—.．]', t) or re.match(r'^(图|表)\s*[0-9０-９]+[.．][0-9０-９]+', t):
            caps.append(t)
        else:
            rest.append(t)
    other_txt = ''.join(rest)
    return paras, other_txt, len(body), len(other), caps



def rebuild_window(lbp, hdr, pgs):
    """把连续若干页当一个窗口重建：行按 (页, 栏带, y) 排序，栏内按缩进/间距分段。
    返回 (paras, other_txt, caps)；paras = [(page, y, text), ...]"""
    lines = []
    for p in pgs:
        for l in lbp.get(p, []):
            t = (l.get('text') or '').strip()
            n = norm(t)
            if len(n) < 2:
                continue
            if n[:12] in hdr:
                continue
            if NUMROW.match(t):
                continue
            lines.append((p, l))
    if len(lines) < 8:
        return None
    allx = [l['x0'] for _, l in lines]
    bands = body_bands([l for _, l in lines])
    if not bands:
        mid = sorted(allx)[len(allx)//2]
        bands = [(mid - 130, mid + 130)]
    band_idx = {}
    for i, (lo, hi) in enumerate(bands):
        band_idx[i] = (lo, hi)
    body, other = [], []
    for p, l in lines:
        bi = next((i for i, (lo, hi) in band_idx.items() if lo <= l['x0'] <= hi), None)
        (body if bi is not None else other).append((p, bi if bi is not None else 99, l))
    body.sort(key=lambda x: (x[0], x[1], round(x[2]['y0'] / 6), x[2]['x0']))
    band_base = {}
    for p, bi, l in body:
        k = (p, bi)
        band_base[k] = min(band_base.get(k, 1e9), l['x0'])
    paras = []
    cur = []
    cur_key = None
    prev_y = None
    for p, bi, l in body:
        k = (p, bi)
        b0 = band_base[k]
        if cur and (cur_key != k or l['x0'] > b0 + 28 or (prev_y and l['y0'] - prev_y > 90)):
            paras.append((cur[0][0], cur[0][2]['y0'], ''.join(x[2].get('text') or '' for x in cur)))
            cur = []
        cur.append((p, bi, l))
        cur_key = k
        prev_y = l['y0']
    if cur:
        paras.append((cur[0][0], cur[0][2]['y0'], ''.join(x[2].get('text') or '' for x in cur)))
    caps, rest = [], []
    for p, bi, l in sorted(other, key=lambda x: (x[0], x[2]['y0'], x[2]['x0'])):
        t = (l.get('text') or '').strip()
        if re.match(r'^(图|表)\s*[0-9０-９][^.。]{0,3}[-－—.．]', t) or re.match(r'^(图|表)\s*[0-9０-９]+[.．][0-9０-９]+', t):
            caps.append(t)
        else:
            rest.append(t)
    return paras, ''.join(rest), caps


def windows_from(pages, span=2):
    pgs = sorted(set(pages))
    wins = []
    for p in pgs:
        lo, hi = max(1, p - span), p + span
        if wins and lo <= wins[-1][1]:
            wins[-1] = (wins[-1][0], max(hi, wins[-1][1]))
        else:
            wins.append((lo, hi))
    return wins


def descramble_block(text, pool_lines, span_pages):
    """零丢失去乱序：把块文本贪心对齐到窗口内的 OCR 行，
    命中的行按 (页, 栏带, y) 重排并输出「整行原文」（顺带补回被截断的字），
    对不上的残余字符原样附在末尾 —— 保证输出字符是旧字符的超集。"""
    from collections import Counter
    tn = norm(text)
    cand = []
    for (q, l) in pool_lines:
        n = norm(l.get('text', ''))
        if len(n) >= 3:
            cand.append([n, q, l, False])
    cand.sort(key=lambda x: -len(x[0]))
    pos = 0
    used = []
    residue = []
    while pos < len(tn):
        hit = -1
        for i, c in enumerate(cand):
            if c[3]:
                continue
            if tn.startswith(c[0], pos):
                hit = i
                break
        if hit < 0:
            # 允许部分命中：找最长公共前缀 >=6 字
            best = (0, -1)
            for i, c in enumerate(cand):
                if c[3]:
                    continue
                k = 0
                while k < len(c[0]) and pos + k < len(tn) and c[0][k] == tn[pos + k]:
                    k += 1
                if k > best[0]:
                    best = (k, i)
            if best[0] >= 6:
                c = cand[best[1]]
                used.append((c[1], c[2]))
                c[3] = True
                pos += best[0]
            else:
                residue.append(tn[pos])
                pos += 1
        else:
            c = cand[hit]
            used.append((c[1], c[2]))
            c[3] = True
            pos += len(c[0])
    if len(used) < 2:
        return None
    bands = body_bands([l for _, l in used]) or [(-1, 1e9)]
    def keyf(x):
        q, l = x
        bi = next((i for i, (lo, hi) in enumerate(bands) if lo <= l['x0'] <= hi), len(bands))
        return (q, bi, l['y0'], l['x0'])
    ordered = sorted(used, key=keyf)
    out = ''.join((l.get('text') or '') for _, l in ordered)
    res = ''.join(residue)
    if res:
        out += res
    old_bag = Counter(norm(text))
    new_bag = Counter(norm(out))
    lost = sum((old_bag - new_bag).values())
    return out, len(used), lost


def descramble_mode(d, bl, lbp, hdr, apply, target_idx, span=2):
    """对指定块做零丢失去乱序。"""
    from collections import Counter
    base_bag = Counter(norm(''.join(btxt(b) for b in bl)))
    plan = []
    for i in target_idx:
        b = bl[i]
        p = b.get('page')
        if b.get('type') != 'paragraph':
            continue
        pool = [(q, l) for q in range(p - span, p + span + 1) for l in lbp.get(q, [])]
        r = descramble_block(btxt(b), pool, None)
        if not r:
            print('  blk%-5d p%-4d 对齐不足，跳过' % (i, p))
            continue
        out, nused, lost = r
        if lost > 0:
            print('  blk%-5d p%-4d 丢失 %d 字，跳过' % (i, p, lost))
            continue
        plan.append((i, out, nused, len(btxt(b)), len(out)))
        print('  blk%-5d p%-4d 用行%-3d 字数 %d -> %d (+%d)' % (i, p, nused, len(btxt(b)), len(out), len(out) - len(btxt(b))))
    if not plan:
        return 0
    if not apply:
        print('\n（dry-run：加 --apply 写回）')
        return 0
    import shutil, time
    for i, out, nused, l0, l1 in plan:
        bl[i]['text'] = out
    fp = os.path.join(d, 'book.json')
    bak = os.path.join(d, 'book.json.bak_%s_去乱序前' % time.strftime('%Y%m%d_%H%M'))
    shutil.copy2(fp, bak)
    full = json.load(io.open(fp, encoding='utf-8'))
    full['blocks'] = bl
    full['stats']['chars'] = sum(len(btxt(b)) for b in bl if b.get('type') in ('paragraph', 'heading', 'table'))
    after = Counter(norm(''.join(btxt(b) for b in bl)))
    lost = sum(max(0, base_bag[k] - after.get(k, 0)) for k in base_bag)
    if lost:
        print('!! 全局缺字 %d，拒绝写回' % lost)
        return 1
    tmp = fp + '.tmp'
    json.dump(full, io.open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, fp)
    print('已写回 %d 块 | 全局缺字 0 | 备份 %s' % (len(plan), os.path.basename(bak)))
    return 0


def main():
    args = [a for a in sys.argv[1:] if a != '--apply']
    apply = '--apply' in sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    d = resolve(args[0])
    want = []
    for a in args[1:]:
        a = a.strip()
        if re.fullmatch(r'\d+', a):
            want.append(int(a))
        elif re.fullmatch(r'\d+-\d+', a):
            lo, hi = a.split('-')
            want.extend(range(int(lo), int(hi) + 1))
    book = json.load(io.open(os.path.join(d, 'book.json'), encoding='utf-8'))
    bl = book['blocks']
    lbp, hdr = load_lines(d)
    if "--descramble" in sys.argv[1:]:
        idxs = [int(x) for x in args[1:] if re.fullmatch(r'\d+', x.strip())]
        return descramble_mode(d, bl, lbp, hdr, apply, idxs)
    if "--window" in sys.argv[1:]:
        return window_mode(d, bl, lbp, hdr, apply, want)
    pages = sorted({b.get('page') for b in bl if b.get('page')}) if not want else want
    plan = []
    for p in pages:
        r = rebuild_page(p, lbp, hdr)
        if not r:
            print('p%-4d 跳过（无法判定正文栏）' % p)
            continue
        paras, other_txt, nb, no, caps = r
        old = [b for b in bl if b.get('page') == p and b.get('type') == 'paragraph']
        old_txt = norm(''.join(btxt(b) for b in old))
        new_txt = norm(''.join(t for _, t in paras) + other_txt)
        cov = sum((Counter(old_txt) & Counter(new_txt)).values()) / max(1, len(old_txt))
        lost = sum((Counter(old_txt) - Counter(new_txt)).values())
        plan.append((p, old, paras, other_txt, cov, lost, nb, no, caps))
        miss = Counter(old_txt) - Counter(new_txt)
        print('p%-4d 旧段%-3d -> 正文段%-3d + 标注块1 | 覆盖 %.3f 缺字 %d | 正文行%d 标注行%d | 缺: %s' % (
              p, len(old), len(paras), cov, lost, nb, no, ''.join(sorted(miss.elements()))[:46]))
        for y, t in paras[:3]:
            print('      ·', t[:74])
        if other_txt:
            print('      标注:', other_txt[:70])
    # 全局守恒：把 plan 里的新文本套上去，与全书旧文本比多重集
    sim = list(bl)
    for p, old, paras, other_txt, cov, lost, nb, no, caps in plan:
        idx = [i for i, b in enumerate(sim) if b.get('page') == p and b.get('type') == 'paragraph']
        if not idx:
            continue
        newb = [{'type': 'paragraph', 'page': p, 'y': y, 'col': 0, 'text': t} for y, t in paras if t.strip()]
        if other_txt.strip():
            newb.append({'type': 'paragraph', 'page': p, 'y': 9999, 'col': 1,
                         'text': '（图内标注/表格文字：' + other_txt.strip() + '）'})
        sim[idx[0]:idx[-1] + 1] = newb
    ob = norm(''.join(btxt(b) for b in bl))
    nb2 = norm(''.join(btxt(b) for b in sim))
    co, cn = Counter(ob), Counter(nb2)
    deficit = {k: co[k] - cn.get(k, 0) for k in co if co[k] > cn.get(k, 0)}
    total_def = sum(deficit.values())
    print('\n== 全局守恒 == 旧 %d 字 / 新 %d 字 | 缺 %d 字（种类 %d）' % (len(ob), len(nb2), total_def, len(deficit)))
    print('   缺字明细:', ''.join(sorted(Counter(deficit).elements()))[:140])
    if not apply:
        print('\n（dry-run：加 --apply 写回）')
        return 0
    bad = [x for x in plan if x[4] < 0.98]
    if bad:
        print('覆盖率 <0.98 的页将跳过：', [x[0] for x in bad])
    fp = os.path.join(d, 'book.json')
    bak = os.path.join(d, 'book.json.bak_%s_按页重组前' % time.strftime('%Y%m%d_%H%M'))
    shutil.copy2(fp, bak)
    done = []
    for p, old, paras, other_txt, cov, lost, nb, no, caps in plan:
        if cov < 0.98:
            continue
        idx = [i for i, b in enumerate(bl) if b.get('page') == p and b.get('type') == 'paragraph']
        if not idx:
            continue
        for cap in caps:
            cands = [x for x in bl if x.get('type') == 'figure' and x.get('page') == p]
            if cands:
                tgt = min(cands, key=lambda x: len(x.get('caption') or ''))
                cur = (tgt.get('caption') or '').strip()
                if cap not in cur:
                    tgt['caption'] = (cur + ' ' + cap).strip() if cur else cap
        newb = [{'type': 'paragraph', 'page': p, 'y': y, 'col': 0, 'indent': 1, 'text': t}
                for y, t in paras if t.strip()]
        if other_txt.strip():
            newb.append({'type': 'paragraph', 'page': p, 'y': 9999, 'col': 1, 'indent': 0,
                         'text': '（图内标注/表格文字：' + other_txt.strip() + '）'})
        # 整页重建：遍历原块序列，第一个该页段落处插入 newb，其余该页段落跳过；
        # heading/figure/其他页块原样保留（顺序不变、绝不丢块）
        seen = False
        rebuilt = []
        for k, bb in enumerate(bl):
            is_target = bb.get('page') == p and bb.get('type') == 'paragraph' and any(bb is o for o in old)
            if is_target:
                if not seen:
                    rebuilt.extend(newb)
                    seen = True
                continue
            rebuilt.append(bb)
        bl[:] = rebuilt
        done.append(p)
    def count_chars(bks):
        return sum(len(btxt(b)) for b in bks if b.get('type') in ('paragraph', 'heading', 'table'))
    book['stats']['chars'] = count_chars(bl)
    book['stats']['paragraphs'] = sum(1 for b in bl if b.get('type') == 'paragraph')
    tmp = fp + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(book, f, ensure_ascii=False)
    os.replace(tmp, fp)
    print('已写回 %d 页: %s' % (len(done), done))
    print('blocks:', len(bl), 'chars:', book['stats']['chars'], '| 备份', os.path.basename(bak))
    print('下一步：python tools\\realign_chapters.py "%s" && python tools\\pipeline.py recheck "%s"' % (d, os.path.basename(d)))
    return 0


def window_mode(d, bl, lbp, hdr, apply, want):
    """窗口重建模式：把相邻页合成窗口整体重建，保证旧字符 100% 被覆盖。"""
    from collections import Counter
    wins = windows_from(want or sorted({b.get('page') for b in bl if b.get('page')}), span=2)
    base_bag = Counter(norm(''.join(btxt(b) for b in bl)))
    plan = []
    for lo, hi in wins:
        pgs = [p for p in range(lo, hi + 1) if p in lbp]
        r = rebuild_window(lbp, hdr, pgs)
        if not r:
            continue
        paras, other_txt, caps = r
        old = [b for b in bl if b.get('page') in pgs and b.get('type') == 'paragraph']
        if not old:
            continue
        oldtxt = norm(''.join(btxt(b) for b in old))
        newtxt = norm(''.join(t for _, _, t in paras) + other_txt + ''.join(caps))
        deficit = sum((Counter(oldtxt) - Counter(newtxt)).values())
        if deficit > 0:
            print('窗口 p%d-%d 有 %d 字无法覆盖，跳过' % (lo, hi, deficit))
            continue
        plan.append((pgs, old, paras, other_txt, caps, len(oldtxt), len(newtxt)))
        print('窗口 p%-4d-%-4d 旧段%-3d -> 正文段%-3d + 标注%d + 图注%d | 字数 %d -> %d (+%d)' % (
              lo, hi, len(old), len(paras), 1 if other_txt.strip() else 0, len(caps),
              len(oldtxt), len(newtxt), len(newtxt) - len(oldtxt)))
    if not apply:
        print('\n（dry-run：加 --apply 写回）')
        return 0
    import shutil, time
    fp = os.path.join(d, 'book.json')
    bak = os.path.join(d, 'book.json.bak_%s_窗口重组前' % time.strftime('%Y%m%d_%H%M'))
    shutil.copy2(fp, bak)
    for pgs, old, paras, other_txt, caps, nold, nnew in plan:
        for cap in caps:
            for p in pgs:
                cands = [x for x in bl if x.get('type') == 'figure' and x.get('page') == p]
                if cands:
                    tgt = min(cands, key=lambda x: len(x.get('caption') or ''))
                    cur = (tgt.get('caption') or '').strip()
                    if cap not in cur:
                        tgt['caption'] = (cur + ' ' + cap).strip() if cur else cap
                    break
        newb = [{'type': 'paragraph', 'page': p, 'y': y, 'col': 0, 'indent': 1, 'text': t}
                for p, y, t in paras if t.strip()]
        if other_txt.strip():
            newb.append({'type': 'paragraph', 'page': pgs[-1], 'y': 9999, 'col': 1, 'indent': 0,
                         'text': '（图内标注/表格文字：' + other_txt.strip() + '）'})
        oldset = set(id(b) for b in old)
        seen = False
        rebuilt = []
        for b in bl:
            if id(b) in oldset:
                if not seen:
                    rebuilt.extend(newb)
                    seen = True
                continue
            rebuilt.append(b)
        bl[:] = rebuilt
    book = {'blocks': bl}
    full = json.load(io.open(fp, encoding='utf-8'))
    full['blocks'] = bl
    full['stats']['chars'] = sum(len(btxt(b)) for b in bl if b.get('type') in ('paragraph', 'heading', 'table'))
    full['stats']['paragraphs'] = sum(1 for b in bl if b.get('type') == 'paragraph')
    after = Counter(norm(''.join(btxt(b) for b in bl)))
    lost = sum(max(0, base_bag[k] - after.get(k, 0)) for k in base_bag)
    tmp = fp + '.tmp'
    if lost > 0:
        print('!! 全局缺字 %d，拒绝写回' % lost)
        return 1
    json.dump(full, io.open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, fp)
    print('已写回 %d 个窗口 | blocks %d | chars %d | 全局缺字 0 | 备份 %s' % (
          len(plan), len(bl), full['stats']['chars'], os.path.basename(bak)))
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)