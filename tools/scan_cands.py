# -*- coding: utf-8 -*-
"""对全库「横向扁长」候选图做 OCR，判定是否为文字截图，结果写 tools/_fake_candidates.json"""
import json
from paths import ROOT
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
B = os.path.join(ROOT, "books")
norm = lambda s: re.sub(r"[^\u4e00-\u9fff]", "", s or "")


def main():
    import fix_captions
    eng = fix_captions.ocr_engine()
    out = {}
    for d in sorted(os.listdir(B)):
        dd = os.path.join(B, d)
        bj = os.path.join(dd, "book.json")
        if not os.path.isdir(dd) or not os.path.exists(bj):
            continue
        book = json.load(open(bj, encoding="utf-8"))
        figs = [b for b in book["blocks"] if b.get("type") == "figure"]
        cand = [f for f in figs if (f.get("h") or 0) <= 140 and (f.get("w") or 0) / max(1, f.get("h") or 1) >= 1.3]
        if not cand:
            continue
        page_text = {}
        for b in book["blocks"]:
            if b.get("type") in ("paragraph", "heading"):
                pg = b.get("page")
                page_text[pg] = page_text.get(pg, "") + norm(b.get("text"))
        hits = []
        for f in cand:
            p = os.path.join(dd, f.get("src") or "")
            txt = ""
            if os.path.exists(p):
                try:
                    res, _ = eng(str(p))
                    txt = "".join((t[1] or "") for t in (res or []))
                except Exception:
                    txt = ""
            key = norm(txt)
            cjk = len(re.findall(r"[\u4e00-\u9fff]", txt))
            inpage = bool(key) and key in page_text.get(f.get("page"), "")
            prose = bool(re.search(r"[。，；：？！]", txt))
            texty = (inpage and cjk >= 4) or (prose and cjk >= 6) or cjk >= 8
            if texty:
                hits.append(dict(id=f.get("id"), page=f.get("page"), w=f.get("w"), h=f.get("h"),
                                 caption=(f.get("caption") or "")[:30], cjk=cjk, inpage=inpage,
                                 prose=prose, ocr=re.sub(r"\s+", "", txt)[:70]))
        if hits:
            out[book.get("title") or d] = dict(dir=d, total=len(figs), cand=len(cand), hits=hits)
            print("%-28s 候选%-4d 文字截图 %d" % ((book.get("title") or d)[:26], len(cand), len(hits)), flush=True)
    json.dump(out, open(os.path.join(ROOT, "tools", "_fake_candidates.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("完成，命中书数", len(out))


if __name__ == "__main__":
    main()
