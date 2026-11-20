# -*- coding: utf-8 -*-
"""build_tables.py —— 从 pages/*_ocr2.json 的坐标里恢复表格，生成 table 块。

判据：
  - 行：同一 y（容差 14px）的若干片段
  - 列：x0 聚类（容差 40px）
  - 表区：连续若干行、其中 >=3 行有 >=3 个片段，且片段普遍短（表格单元）
  - 表题：表区上方 260px 内最近的「表X-Y」行；没有则用「（表题未识别）」
  - 续表：同一页/下一页表区上方出现「续表」时并入同一 caption 前缀

用法：
    python tools\build_tables.py <书名> dry      # 只报告
    python tools\build_tables.py <书名> apply    # 写回（备份 + 守恒校验）
"""
from paths import BOOKS
import io
import json
import os
import re
import shutil
import sys
import time
from collections import Counter

BOOKS = BOOKS
HAN = re.compile(r"[\u4e00-\u9fff]")
norm = lambda s: "".join(HAN.findall(s or ""))
btxt = lambda x: x.get("text") or x.get("caption") or ""
TABTITLE = re.compile(r"^\s*(续表|表)\s*([0-9０-９]+[.．]?[0-9０-９]*)\s*[-－—.．]\s*([0-9０-９]+)")


def resolve(arg):
    key = re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", arg or "")
    hit = [x for x in sorted(os.listdir(BOOKS)) if key and key in re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", x)]
    if len(hit) == 1:
        return os.path.join(BOOKS, hit[0])
    raise SystemExit("匹配到 %d 本" % len(hit))


def load_page_lines(book_dir):
    pages = os.path.join(book_dir, "pages")
    out = {}
    for f in sorted(os.listdir(pages)):
        m = re.match(r"page_(\d+)_ocr2\.json", f)
        if not m:
            continue
        p = int(m.group(1))
        L = []
        for l in json.load(io.open(os.path.join(pages, f), encoding="utf-8")):
            t = (l.get("text") or "").strip()
            if not t:
                continue
            L.append({"text": t, "x0": l["x0"], "y0": l["y0"], "x1": l["x1"], "y1": l["y1"]})
        out[p] = L
    return out


def group_rows(cells, tol=14):
    cells = sorted(cells, key=lambda c: (c["y0"], c["x0"]))
    rows = []
    for c in cells:
        if rows and abs(c["y0"] - rows[-1][0]["y0"]) <= tol:
            rows[-1].append(c)
        else:
            rows.append([c])
    return rows


def cluster_cols(rows, tol=40):
    xs = sorted({round(c["x0"]) for r in rows for c in r})
    cols = []
    for x in xs:
        if cols and x - cols[-1][-1] <= tol:
            cols[-1].append(x)
        else:
            cols.append([x])
    return [(min(g), max(g)) for g in cols]


def find_regions(lines, min_rows=3, min_cols=3):
    """返回表区列表：[(rows, cols, y0, y1), ...]"""
    rows = group_rows(lines)
    regions = []
    cur = []
    for r in rows:
        wide = len(r) >= 3
        if wide:
            cur.append(r)
        elif cur and len(r) >= 2:
            cur.append(r)
        else:
            if len(cur) >= min_rows:
                regions.append(cur)
            cur = []
    if len(cur) >= min_rows:
        regions.append(cur)
    out = []
    for rs in regions:
        cols = cluster_cols(rs)
        if len(cols) < min_cols:
            continue
        # 表区里必须有多行确实是多列（防止把缩进正文当表）
        multi = sum(1 for r in rs if len(r) >= 3)
        if multi < min_rows:
            continue
        y0 = min(c["y0"] for r in rs for c in r)
        y1 = max(c["y1"] for r in rs for c in r)
        out.append((rs, cols, y0, y1))
    return out


def region_to_rows(rs, cols):
    grid = []
    for r in rs:
        line = [""] * len(cols)
        for c in sorted(r, key=lambda x: x["x0"]):
            ci = next((i for i, (lo, hi) in enumerate(cols) if lo - 45 <= c["x0"] <= hi + 45), len(cols) - 1)
            line[ci] = (line[ci] + " " + c["text"]).strip() if line[ci] else c["text"].strip()
        grid.append(line)
    return grid


def find_title(lines, y0, span=300):
    cand = [l for l in lines if y0 - span <= l["y0"] <= y0 + 30 and TABTITLE.match(l["text"])]
    if not cand:
        return None
    best = min(cand, key=lambda l: abs(l["y0"] - y0))
    return best["text"].strip()


def scan(book_dir):
    LBP = load_page_lines(book_dir)
    found = []
    for p in sorted(LBP):
        if p <= 21:
            continue
        lines = LBP[p]
        for rs, cols, y0, y1 in find_regions(lines):
            grid = region_to_rows(rs, cols)
            cells = [c for r in grid for c in r if c]
            filled = sum(1 for r in grid if sum(1 for c in r if c) >= 2)
            if filled < 3:
                continue
            title = find_title(lines, y0)
            found.append({"page": p, "y0": int(y0), "y1": int(y1), "rows": len(grid),
                          "cols": len(cols), "filled": filled, "title": title,
                          "grid": grid, "cell_texts": cells})
    return found


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    d = resolve(sys.argv[1])
    mode = sys.argv[2]
    found = scan(d)
    print("识别到表区 %d 个，涉及页 %d" % (len(found), len({f["page"] for f in found})))
    with_title = sum(1 for f in found if f["title"])
    print("其中带「表X-Y」表题的: %d" % with_title)
    for f in found[:18]:
        print("  p%-4d y%-5d %d行×%d列 实填%d %s" % (f["page"], f["y0"], f["rows"], f["cols"], f["filled"],
              (f["title"] or "（无表题）")[:34]))
    # 只转「有表题 + 结构够密」的表区，避免把图的坐标网格臆造成表格
    pick = [f for f in found
            if f["title"] and f["rows"] >= 3 and f["cols"] >= 3 and f["filled"] >= 3]
    print("其中可安全转 table（有表题且行列齐备）: %d" % len(pick))
    if mode == "dry":
        for f in pick[:10]:
            print("  p%-4d %-16s %d行×%d列" % (f["page"], f["title"][:16], f["rows"], f["cols"]))
        return 0
    return apply_tables(d, pick)


def apply_tables(book_dir, found):
    fp = os.path.join(book_dir, "book.json")
    book = json.load(io.open(fp, encoding="utf-8"))
    bl = book["blocks"]
    raw = lambda s: re.sub(r"\s", "", s or "")
    before = Counter(raw("".join(btxt(b) for b in bl)))
    done = []
    for f in sorted(found, key=lambda x: (-x["page"], -x["y0"])):
        # 找该页最后一个 paragraph（表插在正文之后、标注块之前）
        idxs = [i for i, b in enumerate(bl) if b.get("page") == f["page"] and b.get("type") == "paragraph"]
        if not idxs:
            continue
        lab = next((i for i in idxs if btxt(bl[i]).startswith(u"（图内标注/表格文字")), None)
        pos = lab if lab is not None else idxs[-1] + 1
        tb = {"type": "table", "page": f["page"], "y": f["y0"],
              "caption": f["title"] or u"（表题未识别 p%d）" % f["page"],
              "rows": f["grid"]}
        bl.insert(pos, tb)
        # 从标注块里删掉已进入表格的单元文本，避免重复
        if lab is not None:
            for c in f["cell_texts"]:
                t = raw(c)
                if not t:
                    continue
                cur = bl[pos]["text"] if False else None
                lb = bl[pos]
                if lb.get("type") != "paragraph":
                    continue
                s = lb["text"]
                if c in s:
                    lb["text"] = s.replace(c, "", 1)
        done.append((f["page"], f["y0"], f["rows"], f["cols"]))
    after = Counter(raw("".join(btxt(b) if b.get("type") != "table"
                          else (b.get("caption") or "") + "".join("".join(r) for r in b.get("rows", []))
                          for b in bl)))
    lost = sum(max(0, before[k] - after.get(k, 0)) for k in before)
    print("表格块插入 %d 个 | 守恒：缺 %d 字符" % (len(done), lost))
    if lost > 60:
        print("!! 缺失过多，回滚不写")
        return 1
    book["blocks"] = bl
    book["stats"]["chars"] = sum(len(btxt(b)) for b in bl if b.get("type") in ("paragraph", "heading", "table"))
    book["stats"]["tables"] = sum(1 for b in bl if b.get("type") == "table")
    bak = os.path.join(book_dir, "book.json.bak_%s_表格转换前" % time.strftime("%Y%m%d_%H%M"))
    shutil.copy2(fp, bak)
    tmp = fp + ".tmp"
    json.dump(book, io.open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, fp)
    print("已写回 | 备份", os.path.basename(bak))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
