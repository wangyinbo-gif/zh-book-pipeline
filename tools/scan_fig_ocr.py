# -*- coding: utf-8 -*-
"""扫描一本书里「其实是文字截图」的假插图：OCR 每张裁图，与同页正文比对。

用法：python tools/scan_fig_ocr.py "books/某本书"
输出：<书目录>/_fig_ocr.json（每条：id/page/w/h/ocr/cjk/in_page/caption）
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def norm(s):
    return re.sub(r"[^\u4e00-\u9fff]", "", s or "")


def main():
    import fix_captions
    book_dir = sys.argv[1]
    bj = os.path.join(book_dir, "book.json")
    out = os.path.join(book_dir, "_fig_ocr.json")
    if os.path.exists(out):
        print("已有结果，跳过:", os.path.basename(book_dir)[:24])
        return
    book = json.load(open(bj, encoding="utf-8"))
    blocks = book["blocks"]
    figs = [b for b in blocks if b.get("type") == "figure"]
    page_text = {}
    for b in blocks:
        if b.get("type") in ("paragraph", "heading"):
            pg = b.get("page")
            page_text[pg] = page_text.get(pg, "") + norm(b.get("text"))
    engine = fix_captions.ocr_engine()
    rows = []
    for i, f in enumerate(figs):
        p = os.path.join(book_dir, f.get("src") or "")
        txt = ""
        if os.path.exists(p):
            try:
                res, _ = engine(str(p))
                txt = "".join((t[1] or "") for t in (res or []))
            except Exception:
                txt = ""
        key = norm(txt)
        pt = page_text.get(f.get("page"), "")
        # 文字截图判定：裁图文字能在同页正文里找到（或文字本身够长且带标点）
        in_page = bool(key) and (key in pt)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", txt))
        sentence = bool(re.search(r"[。，；：？！]", txt)) and cjk >= 8
        texty = in_page or (sentence and cjk >= 12)
        rows.append(dict(id=f.get("id"), page=f.get("page"), w=f.get("w"), h=f.get("h"),
                         caption=(f.get("caption") or "")[:40], cjk=cjk,
                         in_page=in_page, texty=texty, ocr=re.sub(r"\s+", "", txt)[:120]))
        if (i + 1) % 100 == 0:
            print("  %d/%d" % (i + 1, len(figs)), flush=True)
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    n = sum(1 for r in rows if r["texty"])
    print("%s | 图 %d | 文字截图 %d" % (os.path.basename(book_dir)[:22], len(rows), n))


if __name__ == "__main__":
    main()
