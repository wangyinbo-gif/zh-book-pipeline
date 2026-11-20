# -*- coding: utf-8 -*-
"""从 OCR 缓存把「丢掉的正文行」放回书稿（只增不删，字数守恒校验）。

背景：转换时若把每页顶部几行误当页眉，book.json 会少行；textcheck 能查出来
（lost_lines），本工具把丢失行按「同段中间插回 / 续到下一段 / 接上段尾 / 独立成段」
放回原位置。

用法：
    python tools/repair_lost_lines.py "书名或目录"            # 只出计划（dry-run）
    python tools/repair_lost_lines.py "书名或目录" --apply    # 写回 book.json（自动备份）
    python tools/repair_lost_lines.py "书名或目录" --min-hanzi 12

护栏（避免把无关文字粘到一起）：
    * 不跨标题合并；
    * 不往「目录/地图标签/图库索引」这类段落里粘（label_like 判定）；
    * 片段前后行若不在同一段，不硬塞进段中，改为独立成段。

安全：改动前自动备份 book.json；应用后校验「新增字数 == 各片段字数之和」，
不等则抛错不写盘；块数变化只影响该书，若该书有题库，需另跑 qbank 重定位锚点。
"""
from paths import BOOKS
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402  用它的 textcheck / block_text

HAN = re.compile(r"[\u4e00-\u9fff]")
TERM = '。！？…”』」）)】'
BOOKS = pipeline.BOOKS


def hz(t):
    return len(HAN.findall(t or ""))


def read_json(path, default=None):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_ocr(book_dir):
    pd = os.path.join(book_dir, "pages")
    out = {}
    if not os.path.isdir(pd):
        return out
    for fn in sorted(os.listdir(pd)):
        m = re.match(r"page_(\d+)_ocr2?\.json$", fn)
        if not m:
            continue
        pg = int(m.group(1))
        if pg in out:
            continue
        lines = read_json(os.path.join(pd, fn), []) or []
        out[pg] = [l for l in lines if (l.get("text") or "").strip()]
    return out


def split_x(lines):
    xs = [l.get("x0") or 0 for l in lines]
    if not xs:
        return 500.0
    lo, hi = min(xs), max(xs)
    return (lo + hi) / 2 if hi - lo > 300 else 1e9


def col_of(line, sx):
    return 0 if (line.get("x0") or 0) < sx else 1


def body_like(t, min_hanzi=10):
    h = hz(t)
    if h < min_hanzi or re.match(r"^(图|表)\s*\d", t or ""):
        return False
    return bool(re.search(r"[，。；：、？！]", t)) or h >= max(18, min_hanzi + 8)


def margin_of(ocr, pg, col):
    from collections import Counter
    lines = ocr.get(pg) or []
    sx = split_x(lines)
    xs = [int(round(l.get("x0") or 0)) for l in lines if col_of(l, sx) == col]
    return Counter(xs).most_common(1)[0][0] if xs else None


def label_like(t):
    t = (t or "").strip()
    if not t:
        return True
    if not re.search(r"[。！？]", t) and len(t) > 30:
        return True
    if t.count("·") >= 5:
        return True
    return False


def orderv(b):
    return ((b.get("page") or 0), (b.get("col") if b.get("col") is not None else 0), (b.get("y") or 0))


def build_plan(book_dir, min_hanzi):
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    blocks = book.get("blocks") or []
    tc = pipeline.textcheck(book_dir)
    lost = tc.get("lost_lines") or []
    ocr = load_ocr(book_dir)
    rows, dropped = [], []
    for pg, frag in lost:
        lines = ocr.get(pg) or []
        sx = split_x(lines)
        hit = next((l for l in lines if (l.get("text") or "").startswith(frag)), None)
        if hit is None:
            dropped.append(frag)
            continue
        t = (hit.get("text") or "").strip()
        rec = {"page": pg, "col": col_of(hit, sx), "y": hit.get("y0") or 0,
               "x": hit.get("x0") or 0, "text": t}
        (rows if body_like(t, min_hanzi) else dropped).append(rec)
    rows.sort(key=lambda r: (r["page"], r["col"], r["y"]))
    groups = []
    for r in rows:
        g = groups[-1] if groups else None
        if g and g["page"] == r["page"] and g["col"] == r["col"] and r["y"] - g["last"] < 80:
            g["lines"].append(r["text"])
            g["last"] = r["y"]
        else:
            groups.append({"page": r["page"], "col": r["col"], "first": r["y"], "last": r["y"],
                           "x": r["x"], "lines": [r["text"]]})

    order_lines = []
    for pg in sorted(ocr):
        lines = ocr[pg]
        sx = split_x(lines)
        for c in (0, 1):
            ls = [l for l in lines if col_of(l, sx) == c]
            if not ls:
                continue
            ls.sort(key=lambda l: (round((l.get("y0") or 0) / 6), l.get("x0") or 0))
            for l in ls:
                order_lines.append({"page": pg, "col": c, "y": l.get("y0") or 0,
                                    "text": (l.get("text") or "").strip()})

    heads = [orderv(b) for b in blocks if b.get("type") == "heading"]

    def heading_between(a, b):
        return any(a < h < b for h in heads)

    def find_block(text, mode):
        for i, b in enumerate(blocks):
            if b.get("type") not in ("paragraph", "heading"):
                continue
            t = b.get("text") or ""
            if mode == "start" and t.startswith(text):
                return i, t
            if mode == "any" and text in t:
                return i, t
        return (None, None)

    def nearest_para_before(pos):
        for i in range(len(blocks) - 1, -1, -1):
            b = blocks[i]
            if b.get("type") != "paragraph":
                continue
            if orderv(b) < pos:
                return i, b
        return (None, None)

    def nearest_para_after(pos):
        for i, b in enumerate(blocks):
            if b.get("type") != "paragraph":
                continue
            if orderv(b) > pos:
                return i, b
        return (None, None)

    plans = []
    for g in groups:
        frag = "".join(g["lines"])
        pos = (g["page"], g["col"], g["first"])
        lastpos = (g["page"], g["col"], g["last"])
        gi = next((i for i, r in enumerate(order_lines)
                   if (r["page"], r["col"], r["y"]) == pos), None)
        pv = order_lines[gi - 1]["text"] if gi is not None and gi > 0 else ""
        nx = order_lines[gi + len(g["lines"])]["text"] if gi is not None and gi + len(g["lines"]) < len(order_lines) else ""
        op = None
        # 1) 前后邻居行在同一段 → 插回段中
        if pv and nx:
            pi, bt = find_block(pv[-16:], "any")
            ni, _ = find_block(nx[:16], "any")
            if pi is not None and pi == ni:
                a = bt.find(pv[-16:]) + len(pv[-16:])
                b = bt.find(nx[:16])
                if 0 <= a <= b:
                    op = {"op": "splice", "idx": pi, "a": a, "text": frag}
        mg = margin_of(ocr, g["page"], g["col"])
        indent = mg is not None and (g["x"] - mg) > 40
        # 2) 接上段尾（上一段被截断）
        if op is None and not indent:
            pi, pb = nearest_para_before(pos)
            if pi is not None:
                t = (pb.get("text") or "").rstrip()
                if (t and t[-1] not in TERM
                        and not heading_between(orderv(pb), pos)
                        and not label_like(t)):
                    op = {"op": "append", "idx": pi, "text": frag}
        # 3) 续到下一段首（本片段结尾没有句末标点）
        if op is None and frag.rstrip()[-1:] not in TERM:
            ni, nb = nearest_para_after(lastpos)
            if ni is not None and not heading_between(lastpos, orderv(nb)) and not label_like(nb.get("text")):
                op = {"op": "prepend", "idx": ni, "text": frag}
        # 4) 独立成段
        if op is None:
            ni, nb = nearest_para_after(lastpos)
            op = {"op": "insert", "idx": ni, "page": g["page"], "col": g["col"],
                  "y": g["first"], "text": frag}
        op.update({"page": g["page"], "col": g["col"], "y": g["first"]})
        plans.append(op)
    return book, blocks, plans, dropped, len(lost)


def apply_plan(book_dir, book, blocks, plans, backup_suffix="丢文修复前"):
    old_len = len(blocks)
    old_chars = sum(len(b.get("text") or "") for b in blocks if b.get("type") == "paragraph")
    add = sum(len(p["text"]) for p in plans)
    new_blocks = [dict(b) for b in blocks]
    # splice：纯插入
    grouped = {}
    for p in plans:
        if p["op"] == "splice":
            grouped.setdefault(p["idx"], []).append(p)
    for idx, ops in grouped.items():
        t = new_blocks[idx].get("text") or ""
        for p in sorted(ops, key=lambda x: -x["a"]):
            t = t[:p["a"]] + p["text"] + t[p["a"]:]
        new_blocks[idx]["text"] = t
    # append
    for p in [x for x in plans if x["op"] == "append"]:
        new_blocks[p["idx"]]["text"] = (new_blocks[p["idx"]].get("text") or "") + p["text"]
    # prepend（按 y 倒序，保证同段多片段的先后顺序）
    for p in sorted([x for x in plans if x["op"] == "prepend"],
                    key=lambda x: (x["page"], x["col"], x["y"]), reverse=True):
        new_blocks[p["idx"]]["text"] = p["text"] + (new_blocks[p["idx"]].get("text") or "")
    # insert
    ins = sorted([x for x in plans if x["op"] == "insert"],
                 key=lambda x: (-(x["idx"] if x["idx"] is not None else old_len), x["page"], x["col"], x["y"]))
    for p in ins:
        t = p["idx"] if p["idx"] is not None else old_len
        new_blocks.insert(t, {"type": "paragraph", "text": p["text"], "page": p["page"],
                              "col": p["col"], "y": p["y"], "indent": False, "_repaired": True})
    ins_pos = sorted([(p["idx"] if p["idx"] is not None else old_len) for p in ins])
    for c in book.get("chapters") or []:
        sb = c.get("startBlock")
        if isinstance(sb, int):
            c["startBlock"] = sb + sum(1 for t in ins_pos if t <= sb)
    book["blocks"] = new_blocks
    st = book.setdefault("stats", {})
    new_chars = sum(len(b.get("text") or "") for b in new_blocks if b.get("type") == "paragraph")
    if new_chars != old_chars + add:
        raise SystemExit("字数不守恒：%d != %d + %d（拒绝写盘）" % (new_chars, old_chars, add))
    st["chars"] = new_chars
    st["paragraphs"] = sum(1 for b in new_blocks if b.get("type") == "paragraph")
    bak = os.path.join(book_dir, "book.json.bak_%s_%s" % (time.strftime("%Y%m%d_%H%M"), backup_suffix))
    shutil.copy2(os.path.join(book_dir, "book.json"), bak)
    with io.open(os.path.join(book_dir, "book.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(book, ensure_ascii=False, indent=2))
    return {"blocks": (old_len, len(new_blocks)), "chars": (old_chars, new_chars),
            "backup": os.path.basename(bak)}


def main():
    ap = argparse.ArgumentParser(description="把 OCR 里丢掉、书稿里没有的正文行放回去（只增不删）")
    ap.add_argument("book", help="书名片段或 books/<目录>")
    ap.add_argument("--apply", action="store_true", help="真正写回 book.json（默认只出计划）")
    ap.add_argument("--min-hanzi", type=int, default=10, help="片段至少多少汉字才算正文（默认 10）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    d = pipeline.find_book_dir(args.book)
    book, blocks, plans, dropped, total_lost = build_plan(d, args.min_hanzi)
    from collections import Counter
    print("# %s" % os.path.basename(d))
    print("  textcheck 丢失行 %d → 正文片段组 %d（排除标签/图注 %d 行）"
          % (total_lost, len(plans), total_lost - sum(len(p["text"]) and 1 for p in plans) + len(dropped) - len(dropped)))
    print("  操作：%s | 预计新增汉字 %d"
          % (dict(Counter(p["op"] for p in plans)), sum(hz(p["text"]) for p in plans)))
    for p in plans[:12]:
        print("   [%s] p%-4s c%s y=%-5s %s" % (p["op"], p["page"], p["col"], round(p["y"]), p["text"][:48]))
    if not args.apply:
        print("\n（dry-run：加 --apply 写回，会自动备份 book.json）")
        return 0
    r = apply_plan(d, book, blocks, plans)
    print("\n已写回：blocks %d → %d | chars %d → %d（+%d）| 备份 %s"
          % (r["blocks"][0], r["blocks"][1], r["chars"][0], r["chars"][1],
             r["chars"][1] - r["chars"][0], r["backup"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
