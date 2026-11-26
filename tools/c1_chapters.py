# -*- coding: utf-8 -*-
"""C1 类（章节变体）章节重建：中国民居 / 外国古建筑二十讲 / 图说_14651002 / 造房子

这四本书都是扫描件，标题是竖排大字或写在页眉/目录里，没有「第N章」。
策略：
- 古建筑二十讲：目录页有 20 个「第N讲 标题」，正文每页页眉带「第N讲」→ 按讲切章
- 造房子：目录页（竖排大字）列了篇目 → 在正文里找到同名竖排大字的位置切章
- 图说_14651002：上/中/下篇
- 中国民居：以「××族民居」「××民居」这类小节标题切章

用法：python tools/c1_chapters.py <book_dir> [--dry]
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CN_NUM = "一二三四五六七八九十"
VOLNUM = r"[0-9一二三四五六七八九十百]{1,3}"


def vol_int(s):
    """中文/阿拉伯卷号 → 整数（一~九十九够用）。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    d = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + d.get(s[1:], 0)
    if s.endswith("十"):
        return d.get(s[0], 0) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return (d.get(a, 1) * 10) + d.get(b, 0)
    return d.get(s, 0)



def norm(s):
    return re.sub(r"[\s·・.,，。、;；:：\-—－()（）\[\]【】◎①…]", "", s or "")


def load_pages(book_dir):
    out = {}
    # 用 os.listdir 而非 glob：书稿目录名常含 []（如「（[宋]某某 撰…）」），glob 会把 [] 当通配符 → 0 页
    pdir = os.path.join(book_dir, "pages")
    try:
        names = os.listdir(pdir)
    except OSError:
        return out
    for name in names:
        m = re.fullmatch(r"page_(\d+)_ocr2\.json", name)
        if not m:
            continue
        n = int(m.group(1))
        try:
            out[n] = json.load(open(os.path.join(pdir, name), encoding="utf-8"))
        except Exception:
            out[n] = []
    return out


def line_h(ln):
    return (ln.get("y1") or 0) - (ln.get("y0") or 0)


def line_w(ln):
    return (ln.get("x1") or 0) - (ln.get("x0") or 0)


def vertical_titles(pages, min_h=90, ratio=2.0):
    """竖排大字（标题常见形态）。返回 {page: [text]}"""
    res = {}
    for n, lines in pages.items():
        for ln in lines:
            t = (ln.get("text") or "").strip()
            if not (2 <= len(t) <= 24):
                continue
            h, w = line_h(ln), line_w(ln)
            if h >= min_h and h > ratio * max(w, 1):
                res.setdefault(n, []).append(t)
    return res


# ---------------------------------------------------------------- 各书策略
def chapters_lectures(book_dir, pages):
    """外国古建筑二十讲：目录页的第N讲标题 + 正文页眉"""
    titles = {}
    for pn in (9, 10):
        for ln in pages.get(pn, []):
            t = (ln.get("text") or "").strip()
            m = re.match(r"^第\s*([一二三四五六七八九十]+)\s*讲\s*(.*)$", t)
            if m:
                num = m.group(1)
                name = re.sub(r"^[.·。]+", "", m.group(2)).strip()
                if num not in titles or (name and not titles[num]):
                    titles[num] = name
    if not titles:
        return []
    starts = {}
    for n, lines in pages.items():
        for ln in lines:
            t = (ln.get("text") or "").strip()
            m = re.match(r"^第\s*([一二三四五六七八九十]+)\s*讲", t)
            if m and n > 11:
                num = m.group(1)
                if num not in starts or n < starts[num]:
                    starts[num] = n
    out = []
    for num in sorted(titles, key=lambda x: (CN_NUM.index(x[0]) if x[0] in CN_NUM else 99)):
        if num in starts:
            out.append(("第%s讲 %s" % (num, titles[num]), starts[num]))
    out.sort(key=lambda x: x[1])
    return out


def chapters_essays(book_dir, pages):
    """造房子：目录页竖排篇目 + 正文竖排大字定位（标题本身就是竖排大字）"""
    vt = vertical_titles(pages, min_h=110, ratio=1.8)
    toc = set()
    for pn in (4, 5):
        for t in vt.get(pn, []):
            clean = norm(t)
            if len(clean) >= 2 and not clean.isdigit():
                toc.add(clean)
    if not toc:
        return []
    out = []
    for n in sorted(pages):
        if n <= 6:
            continue
        for t in vt.get(n, []):
            clean = norm(t)
            if len(clean) < 2:
                continue
            hit = None
            for key in toc:
                if key and (key in clean or clean in key) and len(clean) >= 2:
                    hit = key
                    break
            if hit:
                out.append((t.strip(), n))
                break
    res = []
    seen = set()
    for t, p in out:
        k = norm(t)
        if k in seen:
            continue
        if res and p - res[-1][1] < 3:
            continue
        seen.add(k)
        res.append((t, p))
    return res


def chapters_pian(book_dir, pages):
    """图说_14651002：上/中/下篇"""
    out = []
    for tag, fallback in (("上篇", 1), ("中篇", 147), ("下篇", 230)):
        start = fallback
        for n in sorted(pages):
            if n <= 9:
                continue
            for ln in pages[n]:
                t = (ln.get("text") or "").strip()
                if t.startswith(tag) and len(t) <= 30:
                    start = n
                    break
            else:
                continue
            break
        out.append((tag, start))
    # 去重、保证递增
    seen = set()
    res = []
    for t, p in out:
        if p in seen:
            continue
        seen.add(p)
        res.append((t, p))
    return res


def chapters_ethnic(book_dir, pages):
    """中国民居建筑：以「××族民居」「××民居」小节标题切章（去重、控制密度）"""
    pat = re.compile(r"^[\u4e00-\u9fff·]{2,10}(族)?民居$")
    raw = []
    for n in sorted(pages):
        if n < 57:
            continue
        for ln in pages[n]:
            t = (ln.get("text") or "").strip()
            if pat.match(t):
                raw.append((t, n))
                break
    def key(t):
        return norm(t).replace("族民居", "民居")
    res = []
    seen = set()
    for t, p in raw:
        k = key(t)
        if k in seen:
            continue
        if res and p - res[-1][1] < 5:
            continue
        seen.add(k)
        res.append((t, p))
    while len(res) > 88:
        res = res[::2]
    return res



def chapters_generic(book_dir, pages):
    """通用章节识别（无「第N章」的书）：
    1) 页眉/正文里重复出现的「第N讲/第N篇/第N卷」→ 按编号取首次出现页
    1b) 点校体卷系池（A1 改1 + 坑1 2026-11-25）三种形态合并：
        「卷第一 / 卷二 城东」A1 原形；「第三卷」纯题；「乃粒第一卷/铸第八卷」篇名前置式
        （含 1 字残断卷端，天工开物实证）。同编号按页聚类（≤3 页并一档，对翻页重印目录），
        取第 2 档 = 正文卷端；再按卷号升序、页码单调过滤（目录残留页倒序自动弃）。
        编号断档不再整体弃用（缺卷是 OCR 丢卷端，不是假阳性；断档由 gate「卷序连续」项兜底）。
    2) 上/中/下篇
    3) 页面上部的竖排大字标题（A1 改3：对 book.json 图注池做负过滤，防图版说明行做假章）
    """
    # 1b) 卷系点校体（先于「第N讲」尝试：伽蓝记/梦华录类只有 卷第X 形态）
    VOL_FORMS = [
        re.compile(r"^卷\s*第?\s*(" + VOLNUM + r")\s*(.*)$"),                   # 卷第一 / 卷二 城东
        re.compile(r"^第\s*(" + VOLNUM + r")\s*卷$"),                           # 第三卷（正文卷端）
        re.compile(r"^[\u4e00-\u9fff]{1,6}\s*第\s*(" + VOLNUM + r")\s*卷$"),     # 乃粒第一卷 / 铸第八卷（残断 1 字实证）
    ]
    SENT_END = "。；，！？“”\"'"
    vol_hits = {}
    for n in sorted(pages):
        for ln in pages[n]:
            t = (ln.get("text") or "").strip()
            for vp in VOL_FORMS:
                m = vp.match(t)
                if not m:
                    continue
                rest = (m.group(2) or "").strip() if m.re.groups >= 2 else ""
                # 纯标题资格：带 rest 的形态须短且无句读（防「卷一“城内”之愿会寺…」引用行）
                if len(rest) > 12 or any(ch in SENT_END for ch in rest):
                    break
                hp = vol_hits.setdefault(m.group(1), [])
                if not hp or hp[-1][1] != n:   # 同页多行同卷号算一次
                    hp.append((t[:30], n))
                break
    if len(vol_hits) >= 2:
        cands = []
        for num, hits in vol_hits.items():
            clusters = []
            for tt, p in hits:   # hits 已按页升序（扫描序）
                if clusters and p - clusters[-1][-1][1] <= 3:
                    clusters[-1].append((tt, p))
                else:
                    clusters.append([(tt, p)])
            pick = clusters[1][0] if len(clusters) >= 2 else clusters[0][0]
            cands.append((vol_int(num), pick[0], pick[1]))
        cands.sort()
        res = []
        for _v, t, p in cands:
            if res and p <= res[-1][1] + 1:   # 卷号升序下页码必须前进，否则是目录残留
                continue
            res.append((t, p))
        if len(res) >= 2:
            return res
    pat = re.compile(r"^第\s*([一二三四五六七八九十]+)\s*([讲篇卷])")
    first = {}
    for n in sorted(pages):
        for ln in pages[n]:
            t = (ln.get("text") or "").strip()
            m = pat.match(t)
            if not m:
                continue
            key = m.group(1) + m.group(2)
            if key not in first:
                first[key] = (n, t[:30])
    if len(first) >= 2:
        out = [(v[1], v[0]) for v in first.values()]
        out.sort(key=lambda x: x[1])
        # 起始页至少隔 4 页，避免页眉误判
        res = []
        for t, p in out:
            if res and p - res[-1][1] < 4:
                continue
            res.append((t, p))
        if len(res) >= 2:
            return res
    # 上/中/下篇
    res = []
    for tag in ("上篇", "中篇", "下篇"):
        for n in sorted(pages):
            hit = False
            for ln in pages[n]:
                t = (ln.get("text") or "").strip()
                if t.startswith(tag) and len(t) <= 30:
                    res.append((t[:30], n))
                    hit = True
                    break
            if hit:
                break
    if len(res) >= 2:
        return res
    # 竖排大字（A1 改3：图注池负过滤——图版说明行的竖排字列不是章题）
    cap_pool = set()
    bj = os.path.join(book_dir, "book.json")
    if os.path.isfile(bj):
        try:
            bk = json.load(open(bj, encoding="utf-8-sig"))
            for b in bk.get("blocks") or []:
                if b.get("type") == "heading":
                    continue   # 真章题本会成 heading，不算吞并行
                s = norm(b.get("text") or b.get("caption") or "")
                if b.get("type") != "table" and len(s) >= 4:
                    cap_pool.add(s)
        except Exception:
            pass

    def swallowed(s):
        # 候选竖排大字与某正文块文本相同/被其吞并 → 熔掉的说明行，非独立章题
        k = norm(s)
        if not k or len(k) < 4:
            return False
        return any(k == bt or (len(bt) > len(k) and k in bt) for bt in cap_pool)

    vt = vertical_titles(pages, min_h=120, ratio=2.2)
    out = []
    for n in sorted(vt):
        for t in vt[n]:
            if len(t) >= 4 and not swallowed(t):
                out.append((t, n))
                break
    res = []
    for t, p in out:
        if res and p - res[-1][1] < 5:
            continue
        res.append((t, p))
    return res if len(res) >= 2 else []


STRATEGIES = [
    ("外国古建筑二十讲", chapters_lectures),
    ("造房子", chapters_essays),
    ("图说_14651002", chapters_pian),
    ("中国民居建筑", chapters_ethnic),
]


def rebuild(book, chapters):
    blocks = book.get("blocks") or []
    if not blocks:
        return 0
    # 章节起始页 → 起始 block 下标
    starts = []
    for title, page in chapters:
        idx = None
        for i, blk in enumerate(blocks):
            if (blk.get("page") or 0) >= page:
                idx = i
                break
        if idx is None:
            continue
        if starts and idx <= starts[-1][1]:
            continue
        starts.append((title, idx))
    if len(starts) < 2:
        return 0
    out = []
    for k, (title, idx) in enumerate(starts):
        end = starts[k + 1][1] if k + 1 < len(starts) else len(blocks)
        chunk = blocks[idx:end]
        nchars = sum(len(b.get("text") or "") for b in chunk if b.get("type") in ("paragraph", "heading"))
        out.append({"id": "ch-%d" % k, "title": title, "startBlock": idx, "chars": nchars, "blocks": chunk})
    # 第一个章节之前的块并到第一章
    if starts[0][1] > 0:
        out[0]["blocks"] = blocks[:starts[0][1]] + out[0]["blocks"]
        out[0]["startBlock"] = 0
        out[0]["chars"] = sum(len(b.get("text") or "") for b in out[0]["blocks"] if b.get("type") in ("paragraph", "heading"))
    book["chapters"] = out
    book["stats"]["chapters"] = len(out)
    return len(out)


def main():
    dirs = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    for d in dirs:
        bj = os.path.join(d, "book.json")
        if not os.path.exists(bj):
            print("跳过:", d)
            continue
        book = json.load(open(bj, encoding="utf-8"))
        pages = load_pages(d)
        name = os.path.basename(d)
        strat = None
        for key, fn in STRATEGIES:
            if key in name:
                strat = fn
                break
        chapters = strat(d, pages) if strat else chapters_generic(d, pages)
        if not chapters:
            print("识别不出章节:", name[:40])
            continue
        print("== %s" % name[:44])
        print("   识别章节: %d" % len(chapters))
        for t, p in chapters[:8]:
            print("     p%-4s %s" % (p, t[:36]))
        if dry:
            continue
        n = rebuild(book, chapters)
        if n < 2:
            print("   章节仍不足，未写回")
            continue
        json.dump(book, open(bj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("   写回: %d 章" % n)


if __name__ == "__main__":
    main()
