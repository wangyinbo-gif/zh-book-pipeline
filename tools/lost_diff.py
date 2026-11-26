#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""丢行全量归因 lost_diff.py v2：OCR 行 vs blocks 的 text+caption+displayCaption+rows+cells 全语料包含。
输出按页聚簇的丢行清单 JSON。只读，不改数据。
用法: python lost_diff.py <书目录> <输出json>"""
import json, os, re, sys

def norm(s):
    return re.sub(r"[\s，。、：；！？“”‘’（）()·\-—…,.:;?!'\"|〈〉<>【】\[\]〔〕]+", "", s)

def main():
    d, out = sys.argv[1], sys.argv[2]
    book = json.load(open(os.path.join(d, "book.json"), encoding="utf-8"))
    parts = []
    for b in book.get("blocks", []):
        for k in ("text", "caption", "displayCaption"):
            if b.get(k): parts.append(b[k])
        for r in (b.get("rows") or []):
            if isinstance(r, str): parts.append(r)
        for r in (b.get("cells") or []):
            if isinstance(r, str): parts.append(r)
    full = norm("".join(parts))
    lost, tot = {}, 0
    pdir = os.path.join(d, "pages")
    for f in sorted(os.listdir(pdir)):
        m = re.match(r"page_(\d+)_(ocr2|text)\.json$", f)
        if not m: continue
        pno = int(m.group(1))
        arr = json.load(open(os.path.join(pdir, f), encoding="utf-8"))
        for l in arr:
            t = (l.get("text") or "").strip()
            n = norm(t)
            if len(n) < 6: continue
            frag = n[:10]
            tot += 1
            if frag not in full:
                lost.setdefault(pno, []).append(t[:24])
    bypage = sorted(((p, len(a)) for p, a in lost.items()), key=lambda x: -x[1])
    res = {"dir": os.path.basename(d), "checked": tot, "lost": sum(v for _, v in bypage),
           "lostRate": round(sum(v for _, v in bypage) / max(1, tot), 4),
           "pageHist": bypage[:40], "pages": lost}
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print("%s lost=%d/%d rate=%.3f top=%s" % (res["dir"][:14], res["lost"], tot, res["lostRate"],
          ",".join("p%d:%d" % (p, n) for p, n in bypage[:8])))

if __name__ == "__main__":
    main()
