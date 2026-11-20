# -*- coding: utf-8 -*-
"""筑学 · 书稿校对报告

扫描 book.json，输出「校对清单」：把可疑的地方按类别列出来，带页码，并按
「必看 / 参考」分级，人工只需要真正去看「必看」那几十条。

用法：
    python tools/proofread.py "books/<书名>"
输出：
    books/<书名>/校对清单.md
    books/<书名>/proofread.json

分级口径（2026-11-06 起）：
    必看 = 大概率是真问题：正文句子被切断、段落以标点开头、重复段、乱码、
           图注吞正文、整页文字没进书稿（覆盖率）
    参考 = 结构性噪声：目录页断行、页眉页脚数字、编号/标题行、极短块、
           可疑拉丁串、占位图注、前置页/整页图版造成的覆盖率偏低
注意：分级只影响「校对清单.md」的阅读顺序和 summary.levels，issues 里的条目与
旧版一一对应（数量不变），所以 gate 的 split/no_caption 门槛不受影响。
"""
import json
import os
import re
import sys

TERM = "。！？…：；”』」）)】.．!?：;》"
HAN = re.compile(r"[\u4e00-\u9fff]")
CHAPTER_LIKE = re.compile(r"^(第\s*[0-9一二三四五六七八九十百]+\s*[章节篇讲部]"
                          r"|[0-9]+(\.[0-9]+){0,3}[\s\u3000]"
                          r"|绪论|前言|序|目录|附录|参考文献|索引)")

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pipeline  # 逐页覆盖率只在 pipeline 里维护一份
except Exception:
    pipeline = None


def load(book_dir):
    with open(os.path.join(book_dir, "book.json"), encoding="utf-8") as f:
        return json.load(f)


def hz(s):
    return len(HAN.findall(s or ""))


def is_annotation(t):
    """像图内标注/做法说明而不是正文句子：短、没有句读、数字/符号或材料词密集。

    「0厚混凝土卷材防水层20厚水泥砂浆找平100厚混凝土垫层」这类被 OCR 当成段落的
    施工标注，不应该让人去逐条核对断句。
    """
    t = (t or "").strip()
    if not t or len(t) > 60:
        return False
    if re.search(r"[，。；：？]", t):
        return False
    non_han = sum(1 for c in t if not HAN.match(c) and c.strip())
    if non_han / float(max(1, len(t))) >= 0.22:
        return True
    if len(re.findall(r"[0-9]", t)) >= 3:
        return True
    if len(re.findall(r"(层|板|筋|墙|缝|砂|混凝土|砂浆|涂料|卷材|防水|保温)", t)) >= 3:
        return True
    return False


def is_toc_page(page_blocks, page_text):
    """保守判定目录页：块多、普遍很短、且大量章节号样式的行。

    误判代价：真断句会被降级成「参考」。所以门槛取高：至少 8 块、70% 短块、
    4 行以上像章节条目（或整页出现「目录」且 60% 短块）。
    """
    if len(page_blocks) < 8:
        return False
    short = sum(1 for b in page_blocks if len((b.get("text") or "").strip()) <= 40)
    chap = sum(1 for b in page_blocks if CHAPTER_LIKE.match((b.get("text") or "").strip()))
    if "目录" in (page_text or ""):
        return short >= 0.6 * len(page_blocks)
    return short >= 0.7 * len(page_blocks) and chap >= 4


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/proofread.py <book目录>")
        return
    book_dir = sys.argv[1]
    book = load(book_dir)
    blocks = book["blocks"]
    paras = [x for x in blocks if x["type"] == "paragraph"]
    figs = [x for x in blocks if x["type"] == "figure"]
    chapters = book.get("chapters", [])

    issues = {
        "split": [],       # 疑似被错误断开
        "punct_start": [], # 以标点开头
        "short": [],       # 极短段落
        "no_caption": [],  # 图没有图注
        "caption_swallow": [],  # 图注疑似吞了正文
        "dup": [],         # 重复段落
        "odd": [],         # 含可疑字符
        "latin": [],       # 中文里夹着可疑拉丁串
    }

    # ---- 页级信息：目录页 / 正文起始页 ----
    page_blocks = {}
    for b in blocks:
        pg = b.get("page")
        if pg is None:
            continue
        page_blocks.setdefault(pg, []).append(b)
    page_text = {pg: "".join((b.get("text") or b.get("caption") or "") for b in bs)
                 for pg, bs in page_blocks.items()}
    toc_pages = set(pg for pg, bs in page_blocks.items() if is_toc_page(bs, page_text.get(pg, "")))
    starts = [c.get("startPage") for c in chapters if isinstance(c.get("startPage"), int)]
    if not starts:
        starts = [b.get("page") for b in blocks if isinstance(b.get("page"), int)]
    body_start = min(starts) if starts else 1
    exempt = bool(book.get("figure_caption_exempt"))

    def put(kind, item, level, reason):
        item["level"] = level
        item["reason"] = reason
        issues[kind].append(item)

    para_index = {}
    for i, b in enumerate(blocks):
        if b.get("type") == "paragraph":
            para_index[id(b)] = i

    def figure_between(prev_block, cur_block):
        i, j = para_index.get(id(prev_block)), para_index.get(id(cur_block))
        if i is None or j is None or j <= i + 1:
            return False
        return any(blocks[k].get("type") == "figure" for k in range(i + 1, j))

    prev = None
    seen = {}
    for i, x in enumerate(paras):
        t = (x.get("text") or "").strip()
        pg = x.get("page")
        if not t:
            continue
        if re.match(r"^[，。；：、）】」』”’]", t):
            put("punct_start", {"page": pg, "text": t[:60]}, "必看",
                "段落以标点开头（多半是旁注/换行被拆碎）")
        if len(t) < 12:
            put("short", {"page": pg, "text": t[:40]}, "参考",
                "极短块（多为图内标注/编号，可略过）")
        if prev is not None:
            pt = (prev.get("text") or "").strip()
            if pt and pt[-1] not in TERM:
                if pg in toc_pages:
                    lvl, why = "参考", "目录页断行（目录条目本身没有句末标点）"
                elif isinstance(pg, int) and pg < body_start:
                    lvl, why = "参考", "前置页（版权/封面/序）"
                elif re.search(r"[0-9]{2,}\s*$", pt):
                    lvl, why = "参考", "上一段以页码/数字结尾（页眉页脚）"
                elif CHAPTER_LIKE.match(pt) or len(pt) < 8:
                    lvl, why = "参考", "上一段像标题/编号（不是正文句子）"
                elif prev.get("page") != pg:
                    lvl, why = "参考", "跨页断句（页末/页首，原书如此）"
                elif x.get("role") == "footnote" or prev.get("role") == "footnote":
                    lvl, why = "参考", "已核实的独立脚注"
                elif re.search(r"(图|表)\s*\d", pt) or re.match(r"^[（(]?[a-zA-Z][）)]", pt):
                    lvl, why = "参考", "图题/表题/子图标注（不是正文句子）"
                elif re.match(r"^[0-9]+(\.[0-9]+)+", pt):
                    lvl, why = "参考", "编号标题行（如 11.5…）"
                elif re.match(r"^([一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)]|[0-9]+、)", pt):
                    lvl, why = "参考", "序号小标题行（一、二、…）"
                elif len(pt) <= 16 and not re.search(r"[，。；：、？！]", pt):
                    lvl, why = "参考", "短标题行（无句读，多半是小标题）"
                elif figure_between(prev, x):
                    lvl, why = "参考", "两段之间隔着插图（图注/标注被当成正文）"
                elif is_annotation(pt):
                    lvl, why = "参考", "上一段像图内标注/做法说明（不是正文句子）"
                elif re.match(r"^[①-⑳]|^注\s*[0-9①②③④⑤⑥⑦⑧⑨⑩]", t):
                    lvl, why = "参考", "脚注行（原书脚注另排，不是正文断句）"
                elif len(re.findall(r"[A-Za-z]", t)) > len(re.findall(r"[\u4e00-\u9fff]", t)):
                    lvl, why = "参考", "英文页眉/双语版式（下一段主要是外文）"
                elif re.search(r"ISBN|规划教材|出版社|主编|版权所有", pt):
                    lvl, why = "参考", "版权/教材信息行（不是正文句子）"
                elif t.startswith(("（图内标注", "（广告页残留文字", "（本段原与图题", "（此处原为图")) \
                        or pt.startswith(("（图内标注", "（广告页残留文字", "（本段原与图题", "（此处原为图")):
                    # 书页手术（page_surgery / rebuild_text）隔离出来的标注块，不是正文
                    lvl, why = "参考", "书页手术隔离块（图内标注/表格文字），不是正文"
                else:
                    lvl, why = "必看", "正文句子被切断（上一段没有句末标点）"
                put("split", {"page": pg, "prev": pt[-40:], "next": t[:40]}, lvl, why)
        if "□" in t or "\ufffd" in t:
            put("odd", {"page": pg, "text": t[:60]}, "必看", "含乱码字符（□ / 替换符）")
        if re.search(r"[A-Za-z]{6,}", t) and not re.search(r"[A-Za-z]{6,}\s*[，。；]", t):
            grip = re.findall(r"[A-Za-z]{6,}", t)
            weird = [g for g in grip if not re.search(r"[aeiouAEIOU]", g) or len(re.findall(r"[A-Z]", g)) > 3]
            if weird:
                put("latin", {"page": pg, "text": t[:60], "words": weird[:3]}, "参考",
                    "可疑拉丁串（多为英文图注/人名，可略过）")
        key = re.sub(r"\s+", "", t)[:60]
        if len(key) >= 30:
            if key in seen:
                put("dup", {"page": pg, "text": t[:50]}, "必看", "重复段落（同一段出现两次）")
            seen[key] = pg
        prev = x

    for f in figs:
        cap = (f.get("caption") or "").strip()
        disp = (f.get("displayCaption") or "").strip()
        pg = f.get("page")
        if not cap:
            put("no_caption", {"page": pg, "id": f.get("id")},
                "参考" if exempt else "必看",
                "豁免书：原书图注在图片里" if exempt else "插图没有图注")
        elif len(cap) > 70:
            put("caption_swallow", {"page": pg, "caption": cap[:80]}, "必看",
                "图注超过 70 字（疑似吞入正文）")
        if re.fullmatch(r"图\s*\d+", disp):
            put("caption_swallow", {"page": pg, "caption": "(图注没有标题) " + disp}, "参考",
                "占位图注（只有图号，原书没有标题）")

    # ---- 逐页覆盖率（修好此前一直空跑的检查：原先只找 page_*_ocr.json）----
    low_pages = []
    cov_note = ""
    if pipeline is not None:
        try:
            cov = pipeline.page_coverage(book_dir)
            cov_note = cov.get("note") or ""
            for x in cov.get("low", []):
                low_pages.append({"page": x["page"], "ocr": x["ocr"], "out": x["out"],
                                  "ratio": x["ratio"], "level": x["severity"],
                                  "reason": x["reason"]})
        except Exception as e:
            cov_note = "覆盖率计算失败：%s" % e
    else:
        cov_note = "未导入 pipeline，跳过覆盖率"

    total_chars = sum(len(x.get("text") or "") for x in paras)

    # 人工核销：book.json 的 proofreadWaivers = [{kind, page, text, reason}]
    # 命中后该条降级为「参考」，理由记为「已核销（不适用）：reason」，不参与必看计数。
    waivers = book.get("proofreadWaivers") or []
    waive_hits = 0
    if waivers:
        def _hit(kind, item):
            for w in waivers:
                wk = w.get("kind")
                if wk and wk != kind:
                    continue
                if w.get("page") is not None and item.get("page") != w.get("page"):
                    continue
                pref = (w.get("text") or "").strip()
                if pref:
                    ok = False
                    for key in ("text", "caption", "next"):
                        c = item.get(key) or ""
                        if c and c.startswith(pref):
                            ok = True
                            break
                    if not ok:
                        pv = item.get("prev") or ""
                        if pv and pv.endswith(pref):
                            ok = True
                    if not ok:
                        continue
                return w.get("reason") or "人工核销"
            return None

        for kind, items in issues.items():
            for it in items:
                rs = _hit(kind, it)
                if rs:
                    it["level"] = "参考"
                    it["reason"] = "已核销（不适用）：" + rs
                    waive_hits += 1

    hard = sum(1 for k in issues for x in issues[k] if x.get("level") == "必看")
    hard += sum(1 for x in low_pages if x.get("level") == "必看")
    soft = sum(len(v) for v in issues.values()) + len(low_pages) - hard
    summary = {
        "book": book.get("bookTitle") or book.get("title"),
        "chapters": len(chapters),
        "paragraphs": len(paras),
        "figures": len(figs),
        "chars": total_chars,
        "issues": {k: len(v) for k, v in issues.items()},
        "low_pages": len(low_pages),
        "levels": {"必看": hard, "参考": soft},
        "waived": waive_hits,
        "tocPages": len(toc_pages),
        "coverageNote": cov_note,
    }

    md = []
    md.append("# 校对清单 · %s\n" % summary["book"])
    md.append("- 章节：%d" % summary["chapters"])
    md.append("- 正文：%d 字 / %d 段" % (summary["chars"], summary["paragraphs"]))
    md.append("- 插图：%d 幅" % summary["figures"])
    md.append("- **分级：必看 %d 条 · 参考 %d 条**（参考项是目录页/编号/拉丁串一类的结构性噪声，扫一眼即可）"
              % (hard, soft))
    if page_blocks:
        md.append("- 目录页（断行自动降级为参考）：%d 页" % len(toc_pages))
    if cov_note:
        md.append("- 覆盖率：%s" % cov_note)
    md.append("")
    md.append("## 统计\n")
    md.append("| 类别 | 必看 | 参考 | 说明 |")
    md.append("| --- | --- | --- | --- |")
    names = {
        "split": "疑似被错误断开（上一段没有句末标点）",
        "punct_start": "段落以标点开头",
        "short": "极短段落（可能误切）",
        "no_caption": "插图没有图注",
        "caption_swallow": "图注疑似吞入正文 / 缺标题",
        "dup": "重复段落",
        "odd": "含乱码字符",
        "latin": "中文里夹可疑拉丁串",
    }
    for k, v in issues.items():
        h = sum(1 for x in v if x.get("level") == "必看")
        md.append("| %s | %d | %d | |" % (names[k], h, len(v) - h))
    if low_pages:
        h = sum(1 for x in low_pages if x.get("level") == "必看")
        md.append("| 页面文字偏少 | %d | %d | 该页 OCR 有字、书稿明显更少 |" % (h, len(low_pages) - h))
    md.append("")

    fmt = {
        "split": lambda x: "p.%s｜…%s ▸ %s…（%s）" % (x["page"], x["prev"], x["next"], x["reason"]),
        "punct_start": lambda x: "p.%s｜%s（%s）" % (x["page"], x["text"], x["reason"]),
        "short": lambda x: "p.%s｜%s（%s）" % (x["page"], x["text"], x["reason"]),
        "no_caption": lambda x: "p.%s｜%s（%s）" % (x["page"], x["id"], x["reason"]),
        "caption_swallow": lambda x: "p.%s｜%s（%s）" % (x["page"], x["caption"], x["reason"]),
        "dup": lambda x: "p.%s｜%s（%s）" % (x["page"], x["text"], x["reason"]),
        "odd": lambda x: "p.%s｜%s（%s）" % (x["page"], x["text"], x["reason"]),
        "latin": lambda x: "p.%s｜%s（%s，%s）" % (x["page"], x["text"], x["words"], x["reason"]),
    }

    md.append("## 必看（%d 条）\n" % hard)
    if not hard:
        md.append("本次扫描没有必看项。\n")
    else:
        for k in issues:
            sel = [x for x in issues[k] if x.get("level") == "必看"]
            if not sel:
                continue
            md.append("### %s（%d 条）\n" % (names[k], len(sel)))
            for it in sel:
                md.append("- " + fmt[k](it))
            md.append("")
        hsel = [x for x in low_pages if x.get("level") == "必看"]
        if hsel:
            md.append("### 页面文字偏少（%d 页）\n" % len(hsel))
            for it in hsel:
                md.append("- p.%s｜OCR %d 字 → 书稿 %d 字（%.0f%%，%s）"
                          % (it["page"], it["ocr"], it["out"], it["ratio"] * 100, it["reason"]))
            md.append("")

    md.append("## 参考（%d 条，扫一眼即可）\n" % soft)
    for k in issues:
        sel = [x for x in issues[k] if x.get("level") == "参考"]
        if not sel:
            continue
        md.append("### %s（%d 条，列前 %d）\n" % (names[k], len(sel), min(len(sel), 15)))
        for it in sel[:15]:
            md.append("- " + fmt[k](it))
        md.append("")
    ssel = [x for x in low_pages if x.get("level") == "参考"]
    if ssel:
        md.append("### 页面文字偏少（%d 页，列前 %d）\n" % (len(ssel), min(len(ssel), 15)))
        for it in ssel[:15]:
            md.append("- p.%s｜OCR %d 字 → 书稿 %d 字（%.0f%%，%s）"
                      % (it["page"], it["ocr"], it["out"], it["ratio"] * 100, it["reason"]))
        md.append("")

    out_md = os.path.join(book_dir, "校对清单.md")
    with open(out_md, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md))
    with open(os.path.join(book_dir, "proofread.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"summary": summary, "issues": issues, "low_pages": low_pages}, f,
                  ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))
    print("报告：", out_md)


if __name__ == "__main__":
    main()
