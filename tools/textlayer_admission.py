#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文字层准入审查 textlayer_admission.py  (2026-11-26)

对「PDF 内嵌文字层能否作为正文源」给出七项独立证据与准入判定：
  A 乱码率 PUA/FFFD/控制符 > 1%        （硬）
  B 空层率 CJK<10字页 > 60%            （无层→OCR）
  C 体量  抽样页 层han/基线han < 0.5    （硬，需基线）
  D bigram 与OCR基线二元组保留 < 0.85   （硬，需OCR基线）
  E 缺文  OCR独有二元组 > 80            （硬，需OCR基线）
  F 对基线顺序一致率 < 0.90             （硬，需基线）
  G 流序自查 块流序vs视觉序 < 0.90      （硬，无需基线；治同源盲区）
用法：python textlayer_admission.py <pdf> <输出json> [pages目录]
"""
import json, os, re, sys
import fitz

PUA = re.compile(u"[\\ue000-\\uf8ff\\ufffd\\x00-\\x08\\x0b-\\x1f]")
CJK = re.compile(u"[\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff]")

def han(s): return len(CJK.findall(s))
def bigrams(s):
    s = "".join(CJK.findall(s))
    return set(s[i:i+2] for i in range(len(s) - 1))

def main():
    pdf, out = sys.argv[1], sys.argv[2]
    pages_dir = sys.argv[3] if len(sys.argv) > 3 else None
    doc = fitz.open(pdf)
    n = doc.page_count
    tot_raw = tot_pua = tot_han = empty = 0
    for i in range(n):
        t = doc[i].get_text("text")
        hp = han(t)
        tot_raw += len(t); tot_pua += len(PUA.findall(t)); tot_han += hp
        if hp < 10: empty += 1
    A = tot_pua / max(1, tot_raw)
    B = empty / max(1, n)
    res = {"pdf": os.path.basename(pdf), "pages": n, "raw_chars": tot_raw,
           "han_chars": tot_han, "garble_rate": round(A, 4), "empty_rate": round(B, 3), "hard": []}
    if A > 0.01: res["hard"].append("A乱码率%.3f>1%%" % A)
    if B > 0.60: res["hard"].append("B空层率%.2f>60%%" % B)
    d_keep = e_orphan = f_ok = size_ratio = None
    if pages_dir and os.path.isdir(pages_dir):
        files = sorted(f for f in os.listdir(pages_dir) if f.endswith("_ocr2.json") or f.endswith("_text.json"))
        step = max(1, len(files) // 25)
        sampled = files[::step][:25]
        tb, ob, ymatch, ytot, layer_sample_han, base_sample_han = set(), set(), 0, 0, 0, 0
        for f in sampled:
            m = re.search(r'page_(\\d+)_', f)
            if not m: continue
            pno = int(m.group(1))
            if pno > n: continue
            try:
                arr = json.load(open(os.path.join(pages_dir, f), encoding="utf-8"))
            except Exception:
                continue
            lines_ = sorted([l for l in arr if isinstance(l, dict) and l.get("text", "").strip()],
                            key=lambda l: (round(l.get("y0", 0) / 22), l.get("x0", 0)))
            layer = doc[pno - 1].get_text("text")
            layer_sample_han += han(layer)
            ocr = "".join(l["text"] for l in lines_)
            base_sample_han += han(ocr)
            tb |= bigrams(layer); ob |= bigrams(ocr)
            pos = []
            for l in lines_:
                frag = "".join(CJK.findall(l["text"]))[:8]
                if len(frag) < 4: continue
                k = layer.find(frag)
                if k >= 0: pos.append(k)
            mono = sum(1 for a, b in zip(pos, pos[1:]) if b > a)
            if len(pos) >= 4:
                ytot += 1
                if mono / (len(pos) - 1) >= 0.9: ymatch += 1
        if ob:
            d_keep = round(len(tb & ob) / len(ob), 4)
            e_orphan = len(ob - tb)
        if ytot: f_ok = round(ymatch / ytot, 3)
        if base_sample_han: size_ratio = round(layer_sample_han / base_sample_han, 3)
        res.update({"baseline_kind": "text" if sampled and sampled[0].endswith("_text.json") else "ocr",
                    "baseline_sample_pages": len(sampled), "size_ratio": size_ratio})
        if size_ratio is not None and size_ratio < 0.5:
            res["hard"].append("C体量比%.2f<0.5" % size_ratio)
        if d_keep is not None and d_keep < 0.85:
            res["hard"].append("Dbigram保留%.3f<0.85" % d_keep)
        if (e_orphan or 0) > 80:
            res["hard"].append("E缺文二元组%d>80" % e_orphan)
        if f_ok is not None and f_ok < 0.90:
            res["hard"].append("F顺序一致率%.2f<0.90" % f_ok)
    g_ok = g_tot = 0
    for i in range(n):
        blocks = [b for b in doc[i].get_text("blocks") if han(b[4]) >= 6]
        if len(blocks) < 3: continue
        vis = sorted(blocks, key=lambda b: (round(b[1] / 22), b[0]))
        idx = {id(b): k for k, b in enumerate(vis)}
        seq = [idx[id(b)] for b in blocks]
        good = sum(1 for a, b in zip(seq, seq[1:]) if b > a)
        if len(seq) >= 3:
            g_tot += 1
            if good / (len(seq) - 1) >= 0.9: g_ok += 1
    g = round(g_ok / g_tot, 3) if g_tot else None
    res["g_self_order"] = g
    if g is not None and g < 0.90:
        res["hard"].append("G流序自查%.2f<0.90(多栏交错)" % g)
    res["verdict"] = "REJECT→OCR" if res["hard"] else ("NO_LAYER" if B > 0.6 else "ADMIT")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(res["verdict"], "|", os.path.basename(pdf).split(" (")[0][:18], "|", ";".join(res["hard"]) or "clean",
          "| A%.4f B%.2f C%s D%s E%s F%s G%s" % (A, B, size_ratio, d_keep, e_orphan, f_ok, g))

if __name__ == "__main__":
    main()
