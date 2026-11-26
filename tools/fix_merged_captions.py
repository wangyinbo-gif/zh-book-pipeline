# -*- coding: utf-8 -*-
"""把「熔进正文段落/独立成段」的图注找回，挂到同页没有图注的 figure 块上。

适用：插图说明行「初刻本插图X：题名。」被 OCR/分块熔进 paragraph，导致 figure 无 caption。
做法（确定性规则，可重跑）：
  1) 只在同页 paragraph/caption 块里找「(初刻本)?插图[N]：题名」；
  2) 图注行的 y 落在 figure 的 box 内 → 距离记 0，优先；否则按 |y 差| 取最近（≤1000）；
  3) 若同一 figure 的 box 内有多条独立图注（一图两版），合并成一条（用「；」分隔）；
  4) 从段落里剔除已消费的图注文本（连带其句末「。」），段落被剔空则删块；
  5) 更新 stats.paired/paragraphs/chars。

用法：
  python -X utf8 tools/fix_merged_captions.py "<书目录>" [--dry]
"""
import json
import os
import re
import sys

# 「初刻本插图-一九：花机。」「插图--一七：没水采珠船。」「插图七○：南方独推车。」
CAP = re.compile(
    r"(?P<head>(?:初刻本|原图))\s*"
    r"插图\s*[-—–－−﹣]*\s*"
    r"(?P<num>[0-9〇○◯ＯO一二三四五六七八九十]{1,4})\s*"
    r"[：:、]\s*"
    r"(?P<title>[^。；，！？\n]{0,24})"
)
BARE = re.compile(
    r"插图\s*[-—–－−﹣]*\s*(?P<num>[0-9〇○◯ＯO一二三四五六七八九十]{1,4})\s*"
    r"[：:、]\s*(?P<title>[^。；，！？\n]{0,24})"
)


def norm_num(s):
    return s.replace("○", "〇").replace("◯", "〇").replace("Ｏ", "〇").replace("O", "〇")


def norm_txt(s):
    """比对用归一：去空白、圈号统一、去破折号。"""
    s = norm_num(s or "")
    s = re.sub(r"[\s　]", "", s)
    return re.sub(r"[-—–－−﹣]", "", s)


def match_iter(text):
    """返回 [(start, end, caption_text)]；带「初刻本/原图」前后缀的优先，避免重复。"""
    spans = []
    for m in CAP.finditer(text):
        cap = m.group("head") + "插图" + norm_num(m.group("num")) + \
              ("：" + m.group("title").strip(" ：:、") if m.group("title").strip(" ：:、") else "")
        spans.append((m.start(), m.end(), cap))
    if not spans:
        for m in BARE.finditer(text):
            cap = "插图" + norm_num(m.group("num")) + \
                  ("：" + m.group("title").strip(" ：:、") if m.group("title").strip(" ：:、") else "")
            spans.append((m.start(), m.end(), cap))
    return spans


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    book_dir = args[0]
    path = os.path.join(book_dir, "book.json")
    book = json.load(open(path, encoding="utf-8"))
    blocks = book["blocks"]
    figs = [b for b in blocks if b.get("type") == "figure"]
    missing = [f for f in figs if not (f.get("caption") or "").strip()]
    drop_ids = set()
    consumed = set()          # (block_index, start) 已消费的图注文本，防重复
    detail = []

    def block_index(b):
        for i, x in enumerate(blocks):
            if x is b:
                return i
        return -1

    for f in missing:
        pg = f.get("page")
        box = f.get("box") or []
        by0, by1 = (box[1], box[3]) if len(box) == 4 else (None, None)
        fy = f.get("y") or 0
        cands = []
        for pb in blocks:
            if pb is f or pb.get("page") != pg:
                continue
            if pb.get("type") not in ("paragraph", "caption"):
                continue
            ptxt = pb.get("text") or ""
            if "插图" not in ptxt:
                continue
            for s, e, cap in match_iter(ptxt):
                if (id(pb), s) in consumed:
                    continue
                py = pb.get("y") or 0
                inside = by0 is not None and by0 <= py <= by1
                dist = 0 if inside else abs(py - fy)
                cands.append({"pb": pb, "s": s, "e": e, "cap": cap,
                              "dist": dist, "inside": inside})
        if not cands:
            continue
        cands.sort(key=lambda c: (c["dist"], c["s"]))
        chosen = [cands[0]]
        # 同一页 box 内还有别的独立图注（一图两版：如 p470 下井采宝/宝气饱闷）
        if cands[0]["inside"]:
            for c in cands[1:]:
                if c["inside"] and c["pb"] is not chosen[0]["pb"] and c["pb"].get("type") == "paragraph":
                    txt = (c["pb"].get("text") or "").strip()
                    if norm_txt(txt) in (norm_txt(c["cap"]), norm_txt(c["cap"]) + "。"):
                        chosen.append(c)
        caption = "；".join(c["cap"] for c in chosen)
        f["caption"] = caption
        f["originalCaption"] = caption
        title = chosen[0]["cap"]
        tm = re.search(r"插图[0-9〇一二三四五六七八九十]+[：:]\s*(.+)$", title)
        f["displayCaption"] = ("图%s %s" % (f.get("number"), tm.group(1).strip())) if tm else ("图%s" % f.get("number"))
        # 剔除正文里被消费的图注文本
        for c in chosen:
            pb = c["pb"]
            consumed.add((id(pb), c["s"]))
            t = pb.get("text") or ""
            end = c["e"]
            if end < len(t) and t[end] == "。":
                end += 1
            new = (t[:c["s"]] + t[end:]).strip()
            pb["text"] = new
            if not new:
                drop_ids.add(pb.get("id") or ("idx-%d" % block_index(pb)))
        detail.append("  %s p%-4s num=%-4s ← %s" % (f.get("id"), pg, f.get("number"), caption))

    if dry:
        print("可补图注: %d | 仍缺: %d | 图: %d" %
              (len(missing) - sum(1 for f in missing if not (f.get("caption") or "").strip()), 0, len(figs)))
        for d in detail:
            print(d)
        return

    if drop_ids:
        blocks = [b for b in blocks
                  if (b.get("id") or ("idx-%d" % block_index(b))) not in drop_ids]
        book["blocks"] = blocks
    paras = [b for b in blocks if b.get("type") == "paragraph"]
    figs = [b for b in blocks if b.get("type") == "figure"]
    st = book.setdefault("stats", {})
    st["figures"] = len(figs)
    st["paired"] = sum(1 for f in figs if (f.get("caption") or "").strip())
    st["paragraphs"] = len(paras)
    st["chars"] = sum(len(b.get("text") or "") for b in blocks
                      if b.get("type") in ("paragraph", "heading", "table"))
    json.dump(book, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    still = [f for f in figs if not (f.get("caption") or "").strip()]
    print("补图注: %d | 仍缺: %d | 图: %d | paired=%s"
          % (len(missing) - len(still), len(still), len(figs), st["paired"]))
    for d in detail:
        print(d)


if __name__ == "__main__":
    main()