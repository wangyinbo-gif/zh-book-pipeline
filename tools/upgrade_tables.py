# -*- coding: utf-8 -*-
"""upgrade_tables.py —— 用表格线检测结果，择优替换/新增 table 块。

分级：
  T1 替换：该页已有 table 块，且网格版得分更高
  T2 新增：网格版带「表X-Y」表题，但该页没有对应 table 块
  T3 跳过：无表题且无现有块 → 只登记到存疑，不凭空造结构
得分 = 实填行数×2 - 空列数×3 - 碎列惩罚
"""
import io, json, os, re, shutil, sys, time
from collections import Counter

d = os.path.join(BOOKS, "某书")
fp = os.path.join(d, "book.json")
raw = lambda s: re.sub(r"\s", "", s or "")
HAN = re.compile(r"[\u4e00-\u9fff]")
norm = lambda s: "".join(HAN.findall(s or ""))


NUMRUN = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,3})?")


def score(rows):
    """实填行加分；空列、碎列、以及「一格塞进 >=3 个数字」都扣分——
    后者是关键：旧版把 8 列表压成 3 列时行数反而更多，只按行数打分就会选错。"""
    if not rows:
        return -999, 0, 0
    ncol = max(len(r) for r in rows)
    filled_rows = sum(1 for r in rows if sum(1 for c in r if c) >= 2)
    empty_cols = 0
    for k in range(ncol):
        vals = [r[k] for r in rows if len(r) > k]
        if not vals or sum(1 for v in vals if v) <= 0.25 * len(vals):
            empty_cols += 1
    merged = sum(1 for r in rows for c in r if c and len(NUMRUN.findall(c)) >= 3)
    s = filled_rows * 2 - empty_cols * 3 - max(0, ncol - 8) * 2 - merged * 4
    return s, filled_rows, empty_cols


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
    grid = json.load(io.open(os.path.join(d, "_grid_scan.json"), encoding="utf-8"))
    b = json.load(io.open(fp, encoding="utf-8"))
    bl = b["blocks"]
    before = Counter()
    for x in bl:
        t = x.get("text") or x.get("caption") or ""
        if x.get("type") == "table":
            t = (x.get("caption") or "") + "".join("".join(r) for r in x.get("rows", []))
        before.update(raw(t))

    PROTECT = []
    pf = os.path.join(d, "_table_protect.json")
    if os.path.exists(pf):
        PROTECT = json.load(io.open(pf, encoding="utf-8"))
    def protected(x):
        cap = (x.get("caption") or "").strip()
        return any(cap.startswith(p[:10]) for p in PROTECT)

    cur_tabs = {}
    for i, x in enumerate(bl):
        if x.get("type") == "table":
            cur_tabs.setdefault(x.get("page"), []).append((i, x))

    repl, ins, skip = [], [], []
    used = set()

    TITRE = re.compile(u"^[（(]?[上下左右]?[：:]?\\s*表\\s*[0-9０-９]+([.．\\-—~～][0-9０-９]+)*")

    def recover_title(P, bbox):
        """表框上方 160px 内的 OCR 行里找「表X-Y …」作表题"""
        f = os.path.join(d, "pages", "page_%03d_ocr2.json" % P)
        if not os.path.exists(f):
            return ""
        L = json.load(io.open(f, encoding="utf-8"))
        cands = [l for l in L if bbox[1] - 170 <= l["y0"] <= bbox[1] + 8 and TITRE.match((l.get("text") or "").strip())]
        if not cands:
            return ""
        l = min(cands, key=lambda z: abs(z["y0"] - bbox[1]))
        t = (l.get("text") or "").strip()
        m = TITRE.match(t)
        num = re.match(u"表\\s*[0-9０-９]+(?:[.．\\-—~～][0-9０-９]+)*", t)
        rest = re.sub(u"[（(]?[上下左右]?[：:]?\\s*表\\s*[0-9０-９]+(?:[.．\\-—~～][0-9０-９]+)*[）)]?", "", t).strip()
        return ((num.group(0).replace(" ", "") if num else (m.group(0) if m else "")) + " " + rest).strip()
    for p in sorted(grid, key=int):
        P = int(p)
        cands = []
        for t in grid[p]:
            s, fr, ec = score(t["rows"])
            if fr < 3 or s <= 0:
                continue
            cands.append((s, fr, t))
        if not cands:
            continue
        cands.sort(key=lambda z: (-z[0], -z[1]))
        have = [(i, x) for i, x in cur_tabs.get(P, []) if i not in used]
        for s, fr, t in cands:
            title = (t.get("title") or "").strip()
            best = None; bestscore = -999
            for i, x in have:
                if i in used:
                    continue
                sx, fx, _ = score(x.get("rows") or [])
                if fx == 0:
                    sx = -999
                if sx > bestscore:
                    bestscore, best = sx, (i, x)
            if best is not None and protected(bl[best[0]]):
                continue
            if best is not None:
                if s > bestscore:
                    repl.append((P, best[0], s, bestscore, t, title or (bl[best[0]].get("caption") or "")))
                    used.add(best[0])
                continue
            # 该页没有任何 table 块才考虑新增
            if not title:
                title = recover_title(P, t["bbox"])
            if not title:
                # 续表：本页 OCR 有「续表」时沿用上一张表的表题
                f2 = os.path.join(d, "pages", "page_%03d_ocr2.json" % P)
                txt2 = ""
                if os.path.exists(f2):
                    txt2 = "".join((l.get("text") or "") for l in json.load(io.open(f2, encoding="utf-8")))
                if u"续表" in txt2:
                    prev = None
                    for q in range(P - 1, max(0, P - 4), -1):
                        for i2, x2 in cur_tabs.get(q, []):
                            prev = x2.get("caption") or prev
                        if prev:
                            break
                    title = (u"续表 " + (prev or "")).strip()
            if title and s >= 6:
                ins.append((P, s, t, title))
            else:
                skip.append((P, s, fr, len(t["rows"]), len(t["cols"])))

    print("T1 替换 %d 张 | T2 新增 %d 张 | T3 跳过（无表题且无现有块）%d 处" % (len(repl), len(ins), len(skip)))
    print()
    print("--- T1 替换样例（前 12）---")
    for P, i, s, s0, t, title in repl[:12]:
        print("   p%-4s [%d] 分 %d→%d  行%d→%d 列%d→%d  表题=%s" % (
            P, i, s0, s, len(bl[i].get("rows") or []), len(t["rows"]),
            len((bl[i].get("rows") or [[]])[0]), len(t["cols"]), (title or bl[i].get("caption") or "")[:22]))
    print()
    print("--- T2 新增样例（前 16）---")
    for P, s, t, title in ins[:16]:
        print("   p%-4s 分%3d %d行×%d列  %s" % (P, s, len(t["rows"]), len(t["cols"]), title[:34]))
    print()
    print("--- T3 跳过（前 16）---")
    for x in skip[:16]:
        print("   p%-4s 分%3d 实填%d 行%d 列%d" % x)

    if mode != "apply":
        print("\n（dry：未写盘）")
        return 0

    # 从后往前替换/插入，避免下标漂移
    for P, i, s, s0, t, title in sorted(repl, key=lambda r: -r[1]):
        x = bl[i]
        x["rows"] = t["rows"]
        if title:
            x["caption"] = title
    for P, s, t, title in sorted(ins, key=lambda r: -r[0]):
        idxs = [k for k, y in enumerate(bl) if y.get("page") == P]
        pos = (idxs[-1] + 1) if idxs else len(bl)
        bl.insert(pos, {"type": "table", "page": P, "y": t["bbox"][1], "caption": title, "rows": t["rows"]})

    # 去重：表格里已有的长片段从标注块删掉
    removed = 0
    tabs = [x for x in bl if x.get("type") == "table"]
    labs = [x for x in bl if x.get("type") == "paragraph" and (x.get("text") or "").startswith(u"（图内标注/表格文字")]
    bypage = {}
    for x in labs:
        bypage[x.get("page")] = x
    for tb in tabs:
        lab = bypage.get(tb.get("page"))
        if not lab:
            continue
        cells = sorted({c for r in tb.get("rows", []) for c in r if raw(c) and len(raw(c)) >= 6},
                       key=lambda v: -len(raw(v)))
        for c in cells:
            s = lab.get("text") or ""
            if c in s:
                lab["text"] = s.replace(c, "", 1)
                removed += 1

    after = Counter()
    for x in bl:
        t = x.get("text") or x.get("caption") or ""
        if x.get("type") == "table":
            t = (x.get("caption") or "") + "".join("".join(r) for r in x.get("rows", []))
        after.update(raw(t))
    lost = before - after
    tabbag = Counter()
    for x in bl:
        if x.get("type") == "table":
            tabbag.update(raw((x.get("caption") or "") + "".join("".join(r) for r in x.get("rows", []))))
    unexplained = {k: v for k, v in lost.items() if v > tabbag.get(k, 0)}
    print("\n标注块去重片段:", removed, "| 净缺失:", sum(lost.values()), "| 无法用表格解释:", sum(unexplained.values()), dict(list(unexplained.items())[:8]))
    if sum(unexplained.values()) > 40:
        print("!! 超出表格可解释范围，拒绝写盘")
        return 1

    b["blocks"] = bl
    b["stats"]["chars"] = sum(len(x.get("text") or x.get("caption") or "") for x in bl
                              if x.get("type") in ("paragraph", "heading", "table"))
    b["stats"]["tables"] = sum(1 for x in bl if x.get("type") == "table")
    chs = b["chapters"]
    for c in chs:
        tgt = c["title"][:8]
        hit = next((k for k, y in enumerate(bl) if y.get("type") == "heading" and (y.get("text") or "").strip().startswith(tgt)), None)
        if hit is not None:
            c["startBlock"] = hit
    chs.sort(key=lambda c: c["startBlock"])
    for n, c in enumerate(chs):
        hi = chs[n + 1]["startBlock"] if n + 1 < len(chs) else len(bl)
        c["blocks"] = bl[c["startBlock"]:hi]
        c["chars"] = sum(len(y.get("text") or y.get("caption") or "") for y in c["blocks"]
                         if y.get("type") in ("paragraph", "heading", "table"))
        c["figures"] = sum(1 for y in c["blocks"] if y.get("type") == "figure")
    tot = sum(len(c["blocks"]) for c in chs)
    assert tot == len(bl), (tot, len(bl))
    b["chapters"] = chs
    bak = os.path.join(d, "book.json.bak_%s_表格升级前" % time.strftime("%Y%m%d_%H%M"))
    shutil.copy2(fp, bak)
    tmp = fp + ".tmp"
    json.dump(b, io.open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, fp)
    print("已写回 | tables=%d | blocks=%d | 备份 %s" % (b["stats"]["tables"], len(bl), os.path.basename(bak)))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
