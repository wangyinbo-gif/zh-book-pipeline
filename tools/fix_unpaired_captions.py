# -*- coding: utf-8 -*-
"""给没有图注的插图，从同页 OCR 行里找回图注（按距离最近匹配）。"""
import glob
import json
import os
import re
import sys
import shutil
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CAP = re.compile(r"图\s?\d+\s?[-—~－.．·一]\s?\d+")
CAP2 = re.compile(r"^图\s?\d{1,4}(?![0-9])")
# 建筑图纸上常见的标注，本身可作图的说明
CAP3 = re.compile(r"^\d{1,2}\s?[.·]\s?\d{1,2}(\s?[.·]\s?\d{1,2}){0,2}$")
TABLE_CAP = re.compile(r"^表\s?\d{1,3}(\s?[—\-–.·]\s?\d{1,2}){0,3}")
DRAW = re.compile(r"(总平面图|总平面|平面图|立面图|剖面图|透视图|鸟瞰图|轴测图|详图|大样图|示意图|布置图|效果图|施工图|结构图|室内图|外观|内景|平面|立面|剖面)$")
# 坑2（2026-11-25，天工开物实证）：图版书把「（左/右…初刻本）插图N：题名。」熔进正文段落，
# 段首/段尾/段中都出现。位置字开头时允许跟一段无句读的出处说明（如「左一六三七年(明崇祯十年)《天工开物》初刻本」）。
GJ = re.compile(r"(?P<pre>[左右]\s*[^。；，！？]{0,18}?(?:初刻本|原图)?|(?:初刻本|原图)?)?"
                r"插图\s*(?P<n>[0-9〇一二三四五六七八九十]{1,4})\s*[：:]\s*(?P<title>[^。；，！？|]{0,16})?")


def load_pages(book_dir):
    """B5：每页固定一套有效缓存，且与 gate/textcheck 同源。

    旧版 glob 把 page_*_text/_ocr/_ocr2.json 混进同一个 dict（后读的覆盖先读的），
    实测同一页 _ocr2 与 _ocr 行数可差 3 倍（30 vs 93），补出来的图注与质检口径不同源。
    现在一律走 pipeline.load_ocr_pages：优先 _ocr2，缺缓存才降级 _ocr，_text 不参与。
    返回 (pages, kinds)：kinds[页] = "ocr2"|"ocr"，供输出标注降级页。
    """
    try:
        import pipeline as pl
        raw = pl.load_ocr_pages(book_dir)
    except Exception as ex:
        print("  ! pipeline.load_ocr_pages 不可用（%s），退回本文件内同口径读取" % ex)
        raw = _load_ocr_pages_local(book_dir)
    pages, kinds = {}, {}
    for pg, (variant, ls) in raw.items():
        pages[pg] = [l for l in ls if (l.get("text") or "").strip()]
        kinds[pg] = variant
    return pages, kinds


def _load_ocr_pages_local(book_dir):
    """与 pipeline.load_ocr_pages 同规则的兜底（优先 ocr2，同页只留一套，不读 _text）。"""
    out = {}
    d = os.path.join(book_dir, "pages")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        m = re.fullmatch(r"page_(\d+)_(ocr2?)\.json", name)
        if not m:
            continue
        pg, variant = int(m.group(1)), m.group(2)
        if pg in out and out[pg][0] == "ocr2":
            continue
        try:
            arr = json.load(open(os.path.join(d, name), encoding="utf-8"))
        except Exception:
            continue
        if isinstance(arr, list):
            out[pg] = (variant, [l if isinstance(l, dict) else {"text": str(l)}
                                 for l in arr if l is not None])
    return out


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def detect_json_format(raw):
    """按字节探测换行与缩进（写回复用，不顺手统一格式）。"""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    ls = raw.decode("utf-8-sig", "replace").split("\n")
    indent, compact = None, len([x for x in ls if x.strip()]) <= 1
    if not compact:
        for line in ls[1:8]:
            m = re.match(r"^([ \t]+)\S", line)
            if m:
                indent = "\t" if m.group(1)[0] == "\t" else len(m.group(1))
                break
        if indent is None:
            indent = 2
    return {"newline": newline, "indent": indent, "crlf": crlf, "lf": lf, "compact": compact}


def dump_json_preserving(obj, fmt):
    ind = fmt.get("indent")
    out = json.dumps(obj, ensure_ascii=False) if (fmt.get("compact") or ind is None) else json.dumps(obj, ensure_ascii=False, indent=ind)
    nl = fmt.get("newline", "\n")
    return out if nl == "\n" else out.replace("\n", nl)


def main():
    argv = sys.argv[1:]
    apply = ("--apply" in argv)
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        print("用法: python tools/fix_unpaired_captions.py <书目录> [--apply]　（默认只预演，不写盘）")
        return 2
    book_dir = pos[0]
    path = os.path.join(book_dir, "book.json")
    raw_before = read_bytes(path)
    fmt = detect_json_format(raw_before)
    book = json.loads(raw_before.decode("utf-8-sig"))
    pages, kinds = load_pages(book_dir)
    degraded = sorted(pg for pg, k in kinds.items() if k != "ocr2")
    print("缓存口径：每页一套（优先 _ocr2）｜有缓存页 %d ｜降级 _ocr 页 %d %s"
          % (len(pages), len(degraded), ("如 " + ",".join(str(x) for x in degraded[:8])) if degraded else ""))
    blocks = book["blocks"]
    figs = [b for b in blocks if b["type"] == "figure"]
    cap_before = {id(f): (f.get("caption") or "") for f in figs}
    orig_before = {id(f): (f.get("originalCaption") or "") for f in figs}
    paras_by_page = {}
    for b in blocks:
        if b["type"] == "paragraph":
            paras_by_page.setdefault(b.get("page"), []).append(b.get("text") or "")

    used = set()
    table_used = set()
    reuse_count = {}   # 表格被切成多块时，同一条「表N」图注可以复用
    for f in figs:
        if f.get("originalCaption"):
            used.add(f["originalCaption"].replace(" ", ""))

    fixed = 0
    for f in figs:
        if (f.get("caption") or "").strip():
            continue
        pg = f.get("page")
        lines = pages.get(pg) or []
        fy = f.get("y") or 0
        best, bestd = None, 1e18
        for l in lines:
            t = (l.get("text") or "").strip()
            m = CAP.search(t)
            if not m:
                m = CAP2.match(t)
            if not m:
                m = CAP3.match(t)
            if not m or len(t) > 60:
                continue
            key = m.group(0).replace(" ", "")
            if key in used:
                continue
            # 不能是正文里已经出现的句子（避免把正文当图注）
            if any(t in p for p in paras_by_page.get(pg, [])):
                continue
            d = abs((l.get("y0") or 0) - fy)
            if d < bestd:
                best, bestd = (t, m.group(0)), d
        if best and bestd >= 900:
            best = None   # 距离太远的候选不算，让后面几类规则继续找

        if not best:
            # 表格被切成图片：用同页「表N …」段落当图注（限近距离，避免误配正文）
            cand = []
            for pb in blocks:
                if pb.get("page") != pg or pb.get("type") not in ("paragraph", "heading"):
                    continue
                t = (pb.get("text") or "").strip()
                m2 = TABLE_CAP.match(t)
                if not m2 or (t in used and t not in table_used):
                    continue
                d2 = abs((pb.get("y") or 0) - fy)
                if d2 <= 400:
                    cand.append((d2, t[:40]))
            if cand:
                cand.sort()
                best = (cand[0][1], TABLE_CAP.match(cand[0][1]).group(0))
                bestd = cand[0][0]   # 关键：更新距离，否则最后 bestd<900 判断会拦住

        if not best:
            # 坑2：同页正文/说明块里的「(右…初刻本)插图N：题名。」熔段 → 提出配对并从段里剔除该行
            cands2 = []
            for pb in blocks:
                if pb.get("page") != pg or pb.get("type") not in ("paragraph", "caption"):
                    continue
                ptxt = pb.get("text") or ""
                if "插图" not in ptxt:
                    continue
                for m3 in GJ.finditer(ptxt):
                    key3 = "插图" + m3.group("n")
                    if key3 in used:
                        continue
                    cands2.append((abs((pb.get("y") or 0) - fy), pb, m3.start(), m3.end(),
                                   key3, m3.group("title")))
            cands2.sort(key=lambda x: x[0])
            for d3, pb, s3, e3, key3, ttl in cands2:
                if d3 >= 900:
                    break
                title = (ttl or "").strip(" ：:、")
                cap = "插图%s" % key3[2:] + (("：" + title) if title else "")
                if e3 < len(pb.get("text") or "") and (pb.get("text") or "")[e3] == "。":
                    e3 += 1
                pb["text"] = ((pb.get("text") or "")[:s3] + (pb.get("text") or "")[e3:]).strip()
                best = (cap, key3)
                bestd = d3
                break

        if not best:
            # 图纸型插图：用「一层平面图/西立面图/总平面」这类标注当图注
            for l in lines:
                t = (l.get("text") or "").strip()
                if not t or len(t) > 26 or not DRAW.search(t):
                    continue
                if t in used or any(t in p for p in paras_by_page.get(pg, [])):
                    continue
                d = abs((l.get("y0") or 0) - fy)
                if d < bestd:
                    best, bestd = (t, t), d
        if not best:
            # 描述型图注（无编号，如「哥特教堂彩色玻璃窗」）：仅取独立的短行，且不能出现在正文段落里
            for l in lines:
                t = (l.get("text") or "").strip()
                if abs((l.get("y0") or 0) - fy) > 260:
                    continue
                if not (4 <= len(t) <= 40) or re.search(r"[。！？，、；]\s*$", t) or re.search(r"[。！？，；]", t):
                    continue
                if re.match(r"^(第|图|表|注|见图|来源|摄影)", t):
                    continue
                if any(t in p for p in paras_by_page.get(pg, [])):
                    continue
                if t in used:
                    continue
                d = abs((l.get("y0") or 0) - fy)
                if d < bestd:
                    best, bestd = (t, t), d
        if not best:
            # 正文内嵌图注：段落里出现「图2-1 …」「图-2 」「版号 2·2·16」这类，截取一段当图注
            INLINE = re.compile(r"(?:声|照|示意|平|立|剖)?图\s?\d{1,2}\s?[-—–.·]\s?\d{1,2}(?:\s?[-—–.·]\s?\d{1,3})?|图\s?[-—–]\s?\d{1,3}|\d{1,2}\s?[.·]\s?\d{1,2}\s?[.·]\s?\d{1,3}")
            for pb in blocks:
                if pb.get("page") != pg or pb.get("type") != "paragraph":
                    continue
                pdx = abs((pb.get("y") or 0) - fy)
                if pdx > 500:
                    continue
                m3 = INLINE.search(pb.get("text") or "")
                if not m3:
                    continue
                start = m3.start()
                seg = (pb.get("text") or "")[start:start + 40].strip()
                if len(seg) < 3:
                    continue
                if seg in used:
                    continue
                best = (seg, seg.split()[0])
                bestd = pdx
                break

        if not best:
            # 1) 同一页邻近的图已有图注 → 复用（一张图被 OCR 切成多块 / 一注多图）
            for g in figs:
                if g is f or g.get("page") != pg:
                    continue
                gc = (g.get("caption") or "").strip()
                if not gc:
                    continue
                if abs((g.get("y") or 0) - fy) <= 300 and reuse_count.get(gc, 0) < 4:
                    best = (gc, g.get("originalCaption") or gc)
                    bestd = 0
                    break

        if not best:
            # 2) 篇/章 分隔页上的装饰图：用该页标题当图注
            for h in blocks:
                if h.get("page") != pg or h.get("type") != "heading":
                    continue
                ht = (h.get("text") or "").strip()
                if not re.match(r"^第\s?[0-9一二三四五六七八九十百零两]+\s?[篇章讲]", ht):
                    continue
                if sum(1 for g in figs if g.get("page") == pg) <= 2:
                    best = (ht, ht)
                    bestd = abs((h.get("y") or 0) - fy)
                    break

        if best and bestd < 900:
            t, orig = best
            key = orig.replace(" ", "")
            f["caption"] = t
            f["originalCaption"] = orig
            if re.search(r"图\s?\d", t):
                title = CAP.sub("", t, count=1).strip(" 　:：·、-—")
                f["displayCaption"] = ("图%s" % f.get("number") + ((" " + title) if title else ""))
            else:
                f["displayCaption"] = t
            reuse_count[t] = reuse_count.get(t, 0) + 1
            if orig.startswith("表"):
                table_used.add(orig)
            else:
                used.add(key)
            fixed += 1

    missing = [f for f in figs if not (f.get("caption") or "").strip()]
    print("补图注:", fixed, "| 仍缺:", len(missing), "| 图:", len(figs))
    changed = [(f.get("page"), f.get("id"), orig_before.get(id(f), ""), (f.get("caption") or ""))
               for f in figs if (f.get("caption") or "") != cap_before.get(id(f), "")]
    if changed:
        print("  计划改动 %d 条（前 12 条：页 / 图 id / 原 / 新）：" % len(changed))
        for pg, fid, orig, cap in changed[:12]:
            print("   p%-4s %-12s %r → %r" % (pg, str(fid)[:12], orig[:22], cap[:30]))
    if not apply:
        print("\n（dry-run：未写盘。确认无误后加 --apply 才会改 book.json；写回保持原换行/缩进并自动备份）")
        return 0
    bak = os.path.join(book_dir, "book.json.bak_%s_图注补全前" % time.strftime("%Y%m%d_%H%M"))
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(dump_json_preserving(book, fmt))
    after = detect_json_format(read_bytes(path))
    if (after["newline"], after["indent"], after["compact"]) != (fmt["newline"], fmt["indent"], fmt["compact"]):
        print("  ! 写后自证失败：格式漂移 %r → %r" % (fmt, after))
        return 1
    print("已写回（备份 %s ｜ 格式保持 %s indent=%s）"
          % (os.path.basename(bak), "CRLF" if fmt["newline"] == "\r\n" else "LF", fmt["indent"]))
    return 0


if __name__ == "__main__":
    main()
