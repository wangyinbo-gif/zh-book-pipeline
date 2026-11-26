# -*- coding: utf-8 -*-
"""筑学 · 一键转换工作流

把「PDF → 可读书」的整条流水线固定下来，后面加书只用一条命令。

用法：
    python tools/pipeline.py one "待转换书籍/某书.pdf"            # 转换一本
    python tools/pipeline.py batch "待转换书籍"                   # 转换整个文件夹
    python tools/pipeline.py batch "待转换书籍" --workers 2       # 并行 2 本（8 核以上）
    python tools/pipeline.py post "books/某本书"                  # 只重跑后处理（不重新识别）

阶段（自动按顺序执行，已完成的会跳过，断点可续跑）：
    convert      渲染 + OCR（或电子版直接读文字层）→ book.json / 插图
    bookfix      清书名、去目录页、重建章节
    postfix      合并跨页/跨插图续段，图例并入图注
    readability  删广告页与误检图、合并逗号断段、补图注
    toc          把「第N篇」并入「第N章」，目录只留章
    dims         给插图补宽高（阅读器排版预留位置）
    proofread    生成 校对清单.md / proofread.json
    preview      生成翻页预览 preview.html

每个阶段在书目录写 .pipeline.json 记录进度；改参数（--scale）会重跑 convert 之后的阶段。
"""
import argparse
import concurrent.futures
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
BOOKS = os.path.join(ROOT, "books")
PY = sys.executable
MARKER = ".pipeline.json"

POST_STAGES = ["bookfix", "postfix", "readability", "captions", "captionfix",
               "cleanfigs", "toc", "headertoc", "chapters2", "dims", "proofread", "preview"]
ALL_STAGES = ["convert"] + POST_STAGES


def log(msg, fp=None):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if fp:
        fp.write(line + "\n")
        fp.flush()


def run(cmd, fp=None, quiet=False):
    log("  $ " + " ".join(os.path.basename(c) if i else c for i, c in enumerate(cmd)), fp)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        log("  ! 失败（exit=%d）\n%s" % (p.returncode, out[-2000:]), fp)
    elif not quiet and out.strip():
        tail = "\n".join(out.strip().splitlines()[-4:])
        log("  " + tail.replace("\n", "\n  "), fp)
    return p.returncode, out


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    # Windows 不允许目录名以点/空格结尾，也不允许设备保留名
    name = re.sub(r"[. ]+$", "", name).strip() or "book"
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(COM|LPT)[1-9]", name.upper()):
        name = "_" + name
    return name[:120]


def pdf_page_count(pdf):
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return 0


def convert(pdf, book_dir, title, book_name, scale, fp):
    pages = pdf_page_count(pdf)
    if pages <= 0:
        raise RuntimeError("读不出 PDF 页数：%s" % pdf)
    display = (title or book_name).strip() or book_name
    cmd = [PY, os.path.join(TOOLS, "pdf2book.py"), pdf, "1", str(pages), book_dir,
           "--title", display, "--book", display, "--scale", str(scale)]
    # 单书覆盖：目录里有 force_ocr 标记就强制 OCR（忽略不可靠的内嵌文字层）
    if os.path.exists(os.path.join(book_dir, "force_ocr")):
        cmd += ["--text-layer", "off"]
    return run(cmd, fp)


def convert_vertical(pdf, book_dir, title, book_name, scale, fp, ears="", model=""):
    """竖排分支：tools/vertical_book.py（右→左成文 + 卷章 + 页级闸 + 页级缓存）。

    与横排 convert 完全并列、互不影响；不跑 pdf2book，也不依赖内嵌文字层
    （文字层字序乱的竖排扫描书由调用方直接走本分支）。
    """
    display = (title or book_name).strip() or book_name
    cmd = [PY, os.path.join(TOOLS, "vertical_book.py"), "--pdf", pdf, "--out", book_dir,
           "--title", display, "--book", display, "--scale", str(scale)]
    if ears:
        cmd += ["--ear", ears]
    if model:
        cmd += ["--model", model]
    return run(cmd, fp)


def post_stage(stage, book_dir, fp):
    scripts = {
        "bookfix": ("bookfix.py", ["book.json"]),
        "postfix": ("postfix_book.py", []),
        "readability": ("fix_readability.py", []),
        "toc": ("rebuild_toc.py", []),
        "dims": ("add_dims.py", []),
        "proofread": ("proofread.py", []),
        "captions": ("fix_unpaired_captions.py", []),
        "cleanfigs": ("clean_watermark_images.py", []),
        "headertoc": ("toc_from_headers.py", []),
        "captionfix": ("fix_captions.py", []),
        "chapters2": ("c1_chapters.py", []),
    }
    if stage == "preview":
        log("  生成翻页预览 preview.html", fp)
        # 用当前 book.json 重建预览
        code = (
            "import json,sys,os;sys.path.insert(0,%r);from book_preview import build_preview;"
            "p=os.path.join(%r,'book.json');b=json.load(open(p,encoding='utf-8'));"
            "build_preview(b,os.path.join(%r,'preview.html'))" % (TOOLS, book_dir, book_dir)
        )
        return run([PY, "-c", code], fp, quiet=True)
    name, extra = scripts[stage]
    args = [os.path.join(book_dir, e) if e == "book.json" else book_dir for e in extra]
    if not extra:
        args = [book_dir]
    return run([PY, os.path.join(TOOLS, name)] + args, fp)



def need_chapter_fix(book_dir):
    """章节少于 2 才需要通用章节修复（第N讲/篇/卷、竖排标题）。"""
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    return len(book.get("chapters") or []) < 2


def need_header_toc(book_dir):
    """目录是否需要改用页眉重建：章太少、标题是“第N节”、或大量重名。"""
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    chapters = book.get("chapters") or []
    if len(chapters) < 3:
        return True
    titles = [str(c.get("title") or "") for c in chapters]
    secs = sum(1 for t in titles if re.match(r"第\s?[0-9一二三四五六七八九十百零两]+\s?节", t.strip()))
    if secs >= max(2, len(titles) * 0.3):
        return True
    if len(set(titles)) < len(titles) * 0.8:
        return True
    return False


REF_RE = re.compile(r"\[[MJC]\]|^[A-Z][A-Za-z&.,;:\-\s’'\"()]{4,}|^[A-Z]{2,}")


def is_reference(t):
    """判断是不是参考文献/注释行（中文字符占比很低）。"""
    t = (t or "").strip()
    if not t:
        return False
    if REF_RE.search(t):
        return True
    letters = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    return len(t) >= 8 and letters / len(t) > 0.45


def label_like(t):
    t = (t or "").strip()
    if not t or len(t) > 120:
        return False
    if any(ch in t for ch in "。！？"):
        return False
    if len(re.findall(r"[（(][a-zA-Z0-9][）)]", t)) >= 2:
        return True
    if t.count("；") >= 2 and len(t) < 90:
        return True
    if re.fullmatch(r"[A-Za-z0-9\s.,;:，。；：\-—=+*/%°㎡]+", t):
        return True
    if len(t) <= 40 and (sum(1 for ch in t if ch.isdigit()) >= 2 or "图" in t):
        return True
    func = sum(1 for ch in t if ch in "的了是在和与有为也就都而及或等")
    return len(t) <= 30 and func <= 1



def toc_line_like(t):
    """目录条目行（「第1章 绪论 …… 1」「1.1 建筑力学的使命 … 2」）：本身没有句末标点，
    不应参与「句中截断」判定。"""
    t = (t or "").strip()
    if not t:
        return False
    if not re.search(r"\d{1,4}\s*$", t):
        return False
    if re.search(r"[…⋯]{2,}|\.{4,}|·{4,}", t):
        return True
    return bool(re.match(r"^(第\s*[0-9一二三四五六七八九十]+\s*章|[0-9]+\.[0-9]+(\.[0-9]+)?|"
                         r"附录\s*[ⅠⅡⅢⅣIV]+|参考文献|习题)\s", t))


def hard_splits(book, min_page=25):
    """统计“真·句中截断”：上一段没有句末标点、中间没有小标题，且不是脚注/参考文献。"""
    term = "。！？…：；”』」）)】.．!?：;》"
    blocks = book.get("blocks", [])
    hard = 0
    body = 0
    last_para = None
    for i, b in enumerate(blocks):
        if b.get("type") != "paragraph" or b.get("role") == "footnote":
            continue
        if (b.get("page") or 0) >= min_page:
            body += 1
        if last_para is not None:
            pj, prev = last_para
            between = blocks[pj + 1:i]
            blocked = any(x.get("type") == "heading" for x in between)
            pt = (prev.get("text") or "").strip()
            ct = (b.get("text") or "").strip()
            same_page = prev.get("page") == b.get("page")
            # 书页手术后加的隔离块（图内标注/表格文字）不是正文，不参与「句中截断」判定
            LABELBLK = ("（图内标注", "（图内标注/表格文字", "（广告页残留文字",
                          "（本段原与图题", "（此处原为图")
            if pt.startswith(LABELBLK) or ct.startswith(LABELBLK):
                last_para = (i, b)
                continue
            if (same_page and not blocked and pt and pt[-1] not in term and len(pt) >= 22
                    and not ct.startswith(tuple("①②③④⑤")) and not is_reference(ct)
                    and not label_like(pt) and not label_like(ct)
                    and not toc_line_like(pt)
                    and not (len(ct) < 18 and not any(ch in ct for ch in "。！？"))):
                hard += 1
        last_para = (i, b)
    return hard, body


def book_profile(book):
    """按实测数据给书打分类标签，供 gate 用不同阈值。
    A 图录型：图多字少 / B 图注型：配对不足 / C 目录型：章节不足 / D 工程问题（不在这里判断）
    """
    stats = book.get("stats", {}) or {}
    pages = max(1, stats.get("pages", 0))
    chars = stats.get("chars", 0)
    figs = stats.get("figures", 0)
    paired = stats.get("paired", 0)
    chapters = stats.get("chapters", 0)
    cpp = chars / pages
    fpp = figs / pages
    cls = []
    if cpp < 200 or fpp >= 2.5:
        cls.append("A")
    if figs and paired / max(1, figs) < 0.98:
        cls.append("B")
    if chapters < 2:
        cls.append("C")
    return {"classes": cls, "charsPerPage": round(cpp, 1), "figsPerPage": round(fpp, 2),
            "pairing": round(paired / max(1, figs), 3) if figs else None, "chapters": chapters}


# 水印/夹带广告关键词：gate 判 0；textcheck 跳过这些 OCR 行（它们不是书的内容）
WATERMARK = ("大康学长", "超级笔记", "Bilibili", "KKKarchi",
             "阿里手绘", "国内一流考研快题教育研究机构",
             # Anna's Archive / DuXiu 转换元数据（2026-11-12 张文忠版实测漏检后补，见交接文档 §八.2）
             "Documentgenerated", "Document generated", "annas-blog", "losslessly embedded",
             "PDF attachments", "header_md5", "pdf_generation_missing_pages", "pdg_main_pages",
             "total_pixels", "total_pages", "zip_password", "uncompressed_size")


def has_watermark(s):
    s = s or ""
    return any(k in s for k in WATERMARK)


def block_text(b):
    """统一取块文本；表格块把表头与各行拼出来，否则表格内容在统计里是隐形的。"""
    if b.get("type") == "table":
        rows = [" ".join(str(c) for c in (r or [])) for r in (b.get("rows") or [])]
        return (b.get("caption") or "") + " " + " ".join(rows)
    return b.get("text") or b.get("caption") or ""


def load_ocr_pages(book_dir):
    """Load OCR by source page, preferring usable ocr2 over legacy ocr."""
    directory = os.path.join(book_dir, "pages")
    result = {}
    if not os.path.isdir(directory):
        return result
    for name in sorted(os.listdir(directory)):
        match = re.fullmatch(r"page_(\d+)_(ocr2?)\.json", name)
        if not match:
            continue
        page = int(match[1])
        variant = match[2]
        if page in result and result[page][0] == "ocr2":
            continue
        lines = read_json(os.path.join(directory, name))
        if isinstance(lines, list):
            result[page] = (variant, [line if isinstance(line, dict) else {"text": str(line)}
                                      for line in lines if line is not None])
    return dict(sorted(result.items()))


def count_chars(book):
    return sum(len(block_text(b)) for b in (book.get("blocks") or [])
               if b.get("type") in ("paragraph", "heading", "table"))


def structure_diag(book):
    """结构体检（只报数，不做门槛）：全库扫描发现 43 本里大量书天然存在重复页眉，
    若把这两条做成硬性 gate 会让整库挂掉，因此作为诊断指标输出，供人工判断。"""
    blocks = book.get("blocks") or []
    cnt = {}
    for b in blocks:
        if b.get("type") != "heading":
            continue
        k = re.sub(r"[\s\u3000·…\.\-—]+", "", block_text(b) or "")
        if k:
            cnt[k] = cnt.get(k, 0) + 1
    rep5 = sum(1 for v in cnt.values() if v >= 5)
    dup_blocks = sum(v - 1 for v in cnt.values() if v > 1)
    chs = book.get("chapters") or []
    title_pat = re.compile(r"^(第\s*[0-9零一二三四五六七八九十百]+\s*[章篇节讲部]|前言|序|绪论|索引|附录|参考文献|目录)")
    mis = 0
    for c in chs:
        sb = c.get("startBlock")
        if not isinstance(sb, int) or sb < 0 or sb >= len(blocks):
            mis += 1
            continue
        b = blocks[sb]
        if b.get("type") != "heading" and not title_pat.match((block_text(b) or "").strip()):
            mis += 1
    return {"headingKinds": len(cnt), "headingRep5": rep5, "headingDupBlocks": dup_blocks,
            "startNonTitle": mis, "chapters": len(chs),
            "tables": sum(1 for b in blocks if b.get("type") == "table"),
            "chars": count_chars(book)}


def textcheck(book_dir):
    """文字层体检（只诊断，不计入门槛）：找出「结构对但文字错」的问题。

    起因：2026-11-06 审刘云月版时发现 gate 全过、但书稿有 15 处 OCR/转换错误
    （「剖面」被识成「部面」、旁注合并丢字、乱码残句），gate 只查字数/图注/截断，
    查不出这些。所以补这一层，用 pages/*_ocr2.json 的 OCR 原始行做对照。

    五项：
      1. frag       碎片块：以标点开头的块（旁注被拆碎后顺序错乱的症状）
      2. tiny       极短块：<=12 字且非编号开头（参考；多数是图内标注）
      3. longnote   长句旁注：note 却 >=25 字且有句末标点（参考；旁注本身可长）
      4. 逐原页检查 OCR 行，分别报告疑缺、文本在异页、源页无书稿块和 OCR 缓存缺失。
      （注：块级 4 字片比对因旁注/正文交错噪声过大，已移除；错字靠 check/audit 的引文核对发现）
    """
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    blocks = book.get("blocks") or []
    PUNCT_HEAD = "，。；、：）」』】！？,.;:)]}％%"
    HAN = re.compile(r"[\u4e00-\u9fff]")
    out = {"frag": [], "tiny": [], "longnote": [], "lost_lines": [], "_ocr_lines": 0,
           "other_page_lines": [], "missing_cache_pages": [], "no_block_pages": [],
           "legacy_cache_pages": []}

    def han_only(s):
        return "".join(HAN.findall(s or ""))

    def chunks4(s):
        return [s[i:i + 4] for i in range(0, max(1, len(s) - 3), 4)]

    def cover(chs, hay):
        if not chs:
            return 1.0
        return sum(1 for c in chs if c in hay) / len(chs)

    # 1) 碎片块：以标点开头
    for i, b in enumerate(blocks):
        t = (block_text(b) or "").strip()
        if t and t[0] in PUNCT_HEAD:
            out["frag"].append((i, b.get("page"), b.get("type"), t[:40]))

    # 2) 极短块（排除编号/图号/表单类）
    for i, b in enumerate(blocks):
        if b.get("type") not in ("paragraph", "note"):
            continue
        t = (block_text(b) or "").strip()
        if not t:
            continue
        if len(re.sub(r"\s", "", t)) <= 12 and not re.match(r"^[（(]?[0-9A-Za-z一二三四五六七八九十图]", t):
            out["tiny"].append((i, b.get("page"), b.get("type"), t))

    # 3) 长句旁注（参考项）
    for i, b in enumerate(blocks):
        if b.get("type") != "note":
            continue
        t = (block_text(b) or "").strip()
        if len(re.sub(r"\s", "", t)) >= 25 and re.search(r"[。！？]\s*$", t):
            out["longnote"].append((i, b.get("page"), t[:40]))

    cache = load_ocr_pages(book_dir)
    ocr_by_page = {p: [str(line["text"]).strip() for line in lines if line.get("text")]
                   for p, (_, lines) in cache.items()}
    book_pages = {b["page"] for b in blocks if type(b.get("page")) is int}
    count = (book.get("stats") or {}).get("pages")
    expected = set(range(1, count + 1)) if type(count) is int else set()
    out["missing_cache_pages"] = sorted((expected | book_pages) - cache.keys())
    out["no_block_pages"] = sorted(cache.keys() - book_pages)
    out["legacy_cache_pages"] = [p for p, (variant, _) in cache.items() if variant == "ocr"]
    out["_ocr_lines"] = sum(len(v) for v in ocr_by_page.values())
    if not ocr_by_page:
        return out

    text_by_page = {}
    all_text = []
    for block in blocks:
        text = han_only(block_text(block))
        if not text:
            continue
        all_text.append(text)
        pages = block.get("pages")
        pages = {page for page in pages if type(page) is int} if isinstance(pages, list) else set()
        pages.add(block.get("page"))
        for page in pages:
            if type(page) is int:
                text_by_page.setdefault(page, []).append(text)
    text_by_page = {page: "".join(texts) for page, texts in text_by_page.items()}

    head_pat = re.compile(r"^(公共建筑设计原理|第\s?\d+\s?章)")

    all_text = "".join(all_text)
    all_chunks = None
    front_pat = re.compile(r"^(上架建议|ISBN|图书在版编目|责任编辑|版权所有|定价|出版发行|全国百佳|如有印装)")
    for p, lines in ocr_by_page.items():
        current = text_by_page.get(p, "")
        nearby = {page: text_by_page.get(page, "") for page in range(p - 3, p + 4) if page != p}
        for ln in lines:
            if head_pat.match(ln) or front_pat.match(ln) or re.match(r"^图\s*\d", ln):
                continue
            if has_watermark(ln):  # 扫描版夹带的广告贴纸文字，不算书稿内容
                continue
            h = han_only(ln)
            if len(h) < 10:
                continue
            chunks = chunks4(h)
            if cover(chunks, current) >= 0.5:
                continue
            matched_pages = [page for page, text in nearby.items() if cover(chunks, text) >= 0.8]
            if matched_pages:
                out["other_page_lines"].append((p, ln[:46], matched_pages))
                continue
            if all_chunks is None:
                all_chunks = set(chunks4(all_text)) if len(all_text) >= 4 else set()
            if cover(chunks, all_chunks) < 0.5:
                out["lost_lines"].append((p, ln[:46]))
            else:
                out["other_page_lines"].append((p, ln[:46], []))

    return out


def page_coverage(book_dir, min_ocr_han=60, low_ratio=0.60, win_ratio=0.80,
                  max_list=40, offset_range=6):
    """逐页文字覆盖率：这一页的 OCR 汉字，有多少进了书稿。

    为什么需要：gate 只看全书平均字/页，整页或半页正文丢了，平均值未必掉出 60–2000
    的区间（2026-11-06 审《外国建筑历史图说_14651002》就是这么漏过去的）。
    只诊断，不计入门槛：整页图版型 / 无图注排版的书天然偏低，必须人工判断。

    口径：
      - OCR 汉字取自 pages/page_NNN_ocr2.json（没有则用 page_NNN_ocr.json）；
      - 书稿汉字按块的字号算（block_text，含表格、图注）；
      - 页码偏移：OCR 页码与书稿 block.page 历史上差 1–3 页，先按 ±offset_range 找
        总覆盖率最高的偏移，再逐页比；
      - 覆盖率 = 书稿该页 / OCR 该页；「偏低」= < low_ratio 且把左右各一页补进来
        仍 < win_ratio（后者用来排除「一段被拆到相邻页」的假警报）。
    返回 dict：pages/offset/median/lowCount/low/totalOcr/totalOut/note。
    """
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    pages_dir = os.path.join(book_dir, "pages")
    res = {"pages": 0, "offset": 0, "median": 0.0, "lowCount": 0, "low": [],
           "totalOcr": 0, "totalOut": 0, "note": ""}
    if not os.path.isdir(pages_dir):
        res["note"] = "无 pages/ OCR 缓存"
        return res

    han = re.compile(r"[\u4e00-\u9fff]")

    def hz(s):
        return len(han.findall(s or ""))

    ocr_text = {p: " ".join(str(line.get("text") or "") for line in lines)
                for p, (_, lines) in load_ocr_pages(book_dir).items()}
    ocr = {p: hz(text) for p, text in ocr_text.items()}
    cand = sorted(pg for pg, n in ocr.items() if n >= min_ocr_han)
    if not cand:
        res["note"] = "OCR 行太少（无可用页码）"
        return res

    by_page = {}
    for b in book.get("blocks") or []:
        pg = b.get("page")
        if pg is None:
            continue
        by_page[pg] = by_page.get(pg, 0) + hz(block_text(b))

    best_o, best_score = 0, -1.0
    tot_ocr = sum(ocr[p] for p in cand)
    for o in range(-offset_range, offset_range + 1):
        score = sum(by_page.get(p + o, 0) for p in cand) / float(tot_ocr or 1)
        if score > best_score + 1e-9:
            best_o, best_score = o, score

    ratios = []
    for p in cand:
        o_han = ocr[p]
        b_han = by_page.get(p + best_o, 0)
        win_han = sum(by_page.get(p + best_o + k, 0) for k in (-1, 0, 1))
        ratios.append((p, o_han, b_han, b_han / float(o_han), min(1.0, win_han / float(o_han))))
    vals = sorted(r[3] for r in ratios)
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    low = [r for r in ratios if r[3] < low_ratio and r[4] < win_ratio]
    low.sort(key=lambda r: (r[3], -r[1]))

    # 分级：前置页、整页图版页天然没有文字，标「参考」；其余「必看」
    chapters = book.get("chapters") or []
    starts = [c.get("startPage") for c in chapters if isinstance(c.get("startPage"), int)]
    if not starts:
        starts = [b.get("page") for b in (book.get("blocks") or []) if isinstance(b.get("page"), int)]
    body_start = min(starts) if starts else 1
    fig_pages = set(b.get("page") for b in (book.get("blocks") or [])
                    if b.get("type") == "figure" and isinstance(b.get("page"), int))
    plate_book = median < 0.35          # 整页图版型：书稿本来就不承载这一页的文字
    book_han = "".join(han.findall("".join(block_text(b) for b in (book.get("blocks") or []))))
    # 正文最后一页（用于判断「正文之后的附页」）
    body_pages = [b.get("page") for b in (book.get("blocks") or [])
                  if b.get("type") in ("paragraph", "heading") and isinstance(b.get("page"), int)]
    last_body_page = max(body_pages) if body_pages else 0
    BACKMATTER = re.compile(r"索引|图录|参考文献|编者的话|附录|目录|插图索引|图片索引|作者索引|评注|凡例")

    def head_lines(pg):
        """该页 OCR 前 3 行（用于识别「图片索引/编者的话」这类附页抬头）。"""
        for fn in ("page_%03d_ocr2.json" % pg, "page_%03d_ocr.json" % pg):
            fp3 = os.path.join(pages_dir, fn)
            if os.path.exists(fp3):
                try:
                    ls = read_json(fp3, []) or []
                except Exception:
                    return ""
                return " ".join(((x.get("text") if isinstance(x, dict) else str(x)) or "").strip()
                                for x in ls[:3])
        return ""

    def found_elsewhere(pg):
        """该页 OCR 的 8 字片，在全书中能否找到 ≥50%：能，说明文字在书里、只是页归属不同。"""
        fp2 = None
        for fn in ("page_%03d_ocr2.json" % pg, "page_%03d_ocr.json" % pg):
            cand2 = os.path.join(pages_dir, fn)
            if os.path.exists(cand2):
                fp2 = cand2
                break
        if not fp2:
            return True
        try:
            lines = read_json(fp2, []) or []
        except Exception:
            return True
        mine = "".join(han.findall("".join((x.get("text") if isinstance(x, dict) else str(x)) or "" for x in lines)))
        if len(mine) < 40:
            return True
        chunks = [mine[i:i + 8] for i in range(0, max(1, len(mine) - 7), 8)]
        hit = sum(1 for c in chunks if c in book_han) / float(len(chunks))
        return hit >= 0.5

    waivers = book.get("coverageWaivers") or {}
    items = []
    hard = 0
    for p, o, b, r, w in low:
        if str(p) in waivers:
            sev, why = "参考", "已核销（不适用）：" + str(waivers[str(p)])
        elif p < body_start:
            sev, why = "参考", "前置页（版权/目录/序）"
        elif plate_book:
            sev, why = "参考", "整页图版型书（书稿按图承载，逐页覆盖率不适用）"
        elif re.search(r"大康学长|超级笔记", ocr_text.get(p, "")):
            sev, why = "参考", "水印/讲义页（原书外的笔记扫描页）"
        elif re.search(r"^(索引|图片索引|插图索引|图录|参考文献|编者的话|附录|目录|评注|凡例|序言)"
                       r"|^S{0,6}(索引|图录|编者的话)$", head_lines(p) or ""):
            sev, why = "参考", "附页抬头（索引/图录/编者的话/目录）"
        elif re.search(r"评注|凡例", ocr_text.get(p, "")):
            sev, why = "参考", "评注/凡例附页（图录体例，不进正文）"
        elif BACKMATTER.search(ocr_text.get(p, "")) and (
                p > last_body_page
                or (not re.search(r"[。！？]", ocr_text.get(p, ""))
                    and (len(re.findall(r"\d+", ocr_text.get(p, ""))) >= 4
                         or "··" in ocr_text.get(p, "")))):
            sev, why = "参考", "前后附页（索引/目录/图录/参考文献，本来就不进正文）"
        elif b == 0 and not re.search(r"[。！？]", ocr_text.get(p, "")) and (
                len(re.findall(r"\d+", ocr_text.get(p, ""))) >= 6 or "··" in ocr_text.get(p, "")):
            sev, why = "参考", "索引/目录类附页（数字密集、没有正文句读）"
        elif b == 0 and not re.search(r"[。！？]", ocr_text.get(p, "")) and \
                len(ocr_text.get(p, "")) < 900 and len(ocr_text.get(p, "")) > 60:
            sev, why = "参考", "图库/术语索引页（全是短标签、没有正文句读）"
        elif b == 0 and p in fig_pages:
            sev, why = "参考", "整页图版（该页只有图，文字在图片里）"
        elif p in fig_pages and r < 0.35:
            sev, why = "参考", "图版/图库页（该页以图为主，文字在图里）"
        elif found_elsewhere(p):
            sev, why = "参考", "该页文字在全书里（页面归属偏移/被拆到别页）"
        else:
            sev, why = "必看", "该页文字几乎没进书稿"
            hard += 1
        items.append({"page": p, "ocr": o, "out": b, "ratio": round(r, 3),
                      "ratioWin": round(w, 3), "severity": sev, "reason": why})
    if plate_book:
        res["note"] = "整页图版型书（覆盖率中位 %.2f），逐页覆盖率只作参考" % median
    res.update({
        "pages": len(ratios), "offset": best_o, "median": round(median, 3),
        "lowCount": len(low), "lowHard": hard, "low": items[:max_list],
        "totalOcr": tot_ocr,
        "totalOut": sum(by_page.get(p + best_o, 0) for p in cand),
    })
    return res


def cmd_coverage(args):
    """逐页覆盖率体检：找出「整页/半页正文没进书稿」的页（gate 查不出来）。"""
    if getattr(args, "all", False):
        rows = []
        for d in sorted(os.listdir(BOOKS)):
            bd = os.path.join(BOOKS, d)
            if not os.path.exists(os.path.join(bd, "book.json")):
                continue
            rows.append((d, page_coverage(bd)))
        rows.sort(key=lambda x: (-x[1].get("lowHard", 0), -x[1]["lowCount"], x[1]["median"]))
        print("%-34s %6s %4s %6s %6s %6s" % ("书", "OCR页", "偏移", "中位", "必看", "参考"))
        for d, c in rows:
            print("%-34s %6d %4d %6.2f %6d %6d"
                  % (d[:32], c["pages"], c["offset"], c["median"],
                     c.get("lowHard", 0), c["lowCount"] - c.get("lowHard", 0)))
        print("说明：中位=逐页「书稿汉字/OCR汉字」中位数；必看=该页覆盖率<60% 且左右各补一页仍<80%；"
              "参考=前置页/整页图版（天然没文字）。偏移=书稿与 OCR 的页码差。")
        return 0
    d = find_book_dir(args.bookdir)
    if not d:
        raise SystemExit("用法：python tools/pipeline.py coverage <书名或目录>  或  --all")
    c = page_coverage(d, max_list=15)
    print("# " + os.path.basename(d))
    print("  OCR 页数 %d | 页码偏移 %+d | 覆盖率中位 %.2f | 偏低页 %d（必看 %d / 参考 %d）"
          % (c["pages"], c["offset"], c["median"], c["lowCount"],
             c.get("lowHard", 0), c["lowCount"] - c.get("lowHard", 0)))
    print("  OCR 汉字 %d / 书稿汉字 %d（%.0f%%）"
          % (c["totalOcr"], c["totalOut"], 100.0 * c["totalOut"] / max(1, c["totalOcr"])))
    if c.get("note"):
        print("  " + c["note"])
    for x in c["low"]:
        print("   [%s] p%-5d OCR %5d 字 / 书稿 %5d 字 = %.0f%%（左右各一页 %.0f%%）— %s"
              % (x["severity"], x["page"], x["ocr"], x["out"], x["ratio"] * 100,
                 x["ratioWin"] * 100, x["reason"]))
    return 0


RECHECK_MARK = "## 复查（自动生成）"


def _deep_review_reports(book_dir):
    """人工深审报告（书稿审查报告-*.md / 质检报告-*.md），供 质检.json 的 note 引用。"""
    names = []
    for pat in ("书稿审查报告-*.md", "质检报告-*.md"):
        names += [os.path.basename(p) for p in sorted(glob.glob(os.path.join(book_dir, pat)))]
    return names


def _recheck_note(book_dir):
    reports = _deep_review_reports(book_dir)
    note = "checkedAt 为自动复查时间（gate/textcheck/coverage），不代表人工深审完成"
    if reports:
        note += "；人工深审记录见本目录 " + "、".join(reports)
    return note


def build_recheck(book_dir):
    """跑一遍 gate + textcheck + 逐页覆盖率，返回可写入 质检.json / 报告的结构（不写 book.json）。"""
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    ok, checks = gate(book_dir, write_profile=False)
    tc = textcheck(book_dir)
    cov = page_coverage(book_dir)
    ocr_lines = tc.get("_ocr_lines") or 0
    lost = len(tc["lost_lines"])
    return {
        "checkedAt": time.strftime("%Y-%m-%d %H:%M"),
        "note": _recheck_note(book_dir),
        "book": book.get("bookTitle") or os.path.basename(book_dir),
        "dir": os.path.basename(book_dir),
        "gate": {"ok": ok, "checks": checks},
        "textcheck": {"frag": len(tc["frag"]), "tiny": len(tc["tiny"]),
                      "longnote": len(tc["longnote"]), "lost_lines": lost,
                      "ocrLines": ocr_lines,
                      "otherPageLines": len(tc["other_page_lines"]),
                      "missingCachePages": tc["missing_cache_pages"],
                      "noBlockPages": tc["no_block_pages"],
                      "legacyCachePages": tc["legacy_cache_pages"],
                      "lostRate": round(lost / float(ocr_lines), 4) if ocr_lines else 0.0,
                      "samples": [{"page": p, "text": t} for p, t in tc["lost_lines"][:8]]},
        "coverage": {k: cov.get(k) for k in ("pages", "offset", "median",
                                             "lowCount", "lowHard", "low", "note")},
    }


def render_recheck(data):
    lines = [RECHECK_MARK, ""]
    g = data["gate"]
    tc = data["textcheck"]
    cov = data["coverage"] or {}
    lines.append("- 复查时间：%s" % data["checkedAt"])
    lines.append("- 说明：%s" % data.get("note", "自动复查记录"))
    lines.append("- gate：%s" % (("✅ %d 项门槛全过" % len(g["checks"])) if g["ok"] else "❌ 有未通过项（见下表）"))
    lines.append("")
    lines.append("| 检查项 | 结果 | 数值 |")
    lines.append("| --- | --- | --- |")
    for c in g["checks"]:
        lines.append("| %s | %s | %s |" % (c["name"], "✅" if c["ok"] else "❌", c["detail"]))
    lines.append("")
    lines.append("- 文字层 textcheck：碎片块 %d · 极短块 %d · 长句旁注 %d · **OCR 行丢失 %d**（丢行率 %.2f%%）"
                 % (tc["frag"], tc["tiny"], tc["longnote"], tc["lost_lines"], tc["lostRate"] * 100))
    lines.append("- 原页诊断（候选）：缺 OCR 缓存 %d 页 · 有缓存但无书稿块 %d 页 · 异页文字线索 %d 行"
                 % (len(tc.get("missingCachePages", [])), len(tc.get("noBlockPages", [])), tc.get("otherPageLines", 0)))
    if cov.get("pages"):
        lines.append("- 逐页覆盖率：OCR 页 %d · 页码偏移 %+d · 中位 %.2f · 必看 %d 页 / 参考 %d 页"
                     % (cov["pages"], cov["offset"], cov["median"],
                        cov.get("lowHard") or 0, (cov.get("lowCount") or 0) - (cov.get("lowHard") or 0)))
    elif cov.get("note"):
        lines.append("- 逐页覆盖率：%s" % cov["note"])
    if tc.get("samples"):
        lines.append("")
        lines.append("### OCR 行丢失样例（这些行不在书稿里，需对照原书判断）")
        for s in tc["samples"]:
            lines.append("- p.%s｜%s" % (s["page"], s["text"]))
    hard = [x for x in (cov.get("low") or []) if x.get("severity") == "必看"]
    if hard:
        lines.append("")
        lines.append("### 覆盖率偏低页（必看）")
        for x in hard[:12]:
            lines.append("- p.%d｜OCR %d 字 → 书稿 %d 字（%.0f%%）"
                         % (x["page"], x["ocr"], x["out"], x["ratio"] * 100))
    lines.append("")
    lines.append("> 本节由 python tools/pipeline.py recheck <书名> 生成，重跑会覆盖；"
                 "上面的「质量检查」是转换当时的快照。")
    return "\n".join(lines) + "\n"


def write_recheck(book_dir, data):
    rp = os.path.join(book_dir, "转换报告.md")
    base = ""
    if os.path.exists(rp):
        with open(rp, "r", encoding="utf-8") as f:
            base = f.read()
    i = base.find(RECHECK_MARK)
    head = (base[:i] if i >= 0 else base.rstrip() + "\n\n").rstrip() + "\n\n"
    with open(rp, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + render_recheck(data))
    with open(os.path.join(book_dir, "质检.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def cmd_recheck(args):
    """跑 gate + textcheck + 逐页覆盖率 → 写 质检.json，并重生成 转换报告.md 的「复查」节。"""
    if getattr(args, "all", False):
        rows = []
        for d in sorted(os.listdir(BOOKS)):
            bd = os.path.join(BOOKS, d)
            if not os.path.exists(os.path.join(bd, "book.json")):
                continue
            data = build_recheck(bd)
            write_recheck(bd, data)
            rows.append(data)
        rows.sort(key=lambda x: (-x["textcheck"]["lost_lines"], -(x["coverage"].get("lowHard") or 0)))
        print("%-32s %4s %5s %5s %6s %8s %5s %5s" %
              ("书", "gate", "碎片", "极短", "OCR丢", "丢行率", "必看页", "中位"))
        for x in rows:
            tc = x["textcheck"]
            cov = x["coverage"] or {}
            med = "—" if cov.get("note") else ("%.2f" % (cov.get("median") or 0.0))
            print("%-32s %4s %5d %5d %6d %7.2f%% %5d %6s" %
                  (x["dir"][:30], "OK" if x["gate"]["ok"] else "FAIL",
                   tc["frag"], tc["tiny"], tc["lost_lines"], tc["lostRate"] * 100,
                   cov.get("lowHard") or 0, med))
        md = ["# 书稿质检复查汇总", "",
              "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M"),
              "- 覆盖：%d 本" % len(rows),
              "- 逐本明细见各书 转换报告.md 的「复查」节 与 质检.json", "",
              "| 书 | gate | 碎片块 | 极短块 | 长句旁注 | OCR 行丢失 | 丢行率 | 必看页 | 覆盖率中位 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for x in rows:
            tc = x["textcheck"]
            cov = x["coverage"] or {}
            med = "—" if cov.get("note") else ("%.2f" % (cov.get("median") or 0.0))
            md.append("| %s | %s | %d | %d | %d | %d | %.2f%% | %d | %s |" %
                      (x["dir"], "✅" if x["gate"]["ok"] else "❌", tc["frag"], tc["tiny"],
                       tc["longnote"], tc["lost_lines"], tc["lostRate"] * 100,
                       cov.get("lowHard") or 0, med))
        md.append("")
        md.append("> OCR 行丢失 = pages/ 里的 OCR 原行在书稿（本页±3 页 / 全书）找不到；"
                  "丢行率 = 缺失行 / OCR 总行。参考口径见 docs/书稿文字层质检-textcheck全库扫描-20261106.md。")
        md.append(">")
        md.append("> 覆盖率中位显示「—」的书：无 pages/ OCR 缓存、或整页图版型（书稿按图承载文字），"
                  "逐页覆盖率不适用，见该书 质检.json 的 coverage.note。")
        mp = os.path.join(ROOT, "交接文档", "质检-复查汇总.md")
        with open(mp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(md) + "\n")
        print("\n汇总：%s" % mp)
        return 0
    d = find_book_dir(args.bookdir)
    if not d:
        raise SystemExit("用法：python tools/pipeline.py recheck <书名或目录>  或  --all")
    data = build_recheck(d)
    write_recheck(d, data)
    tc = data["textcheck"]
    cov = data["coverage"] or {}
    print("# %s（%s）" % (data["book"], data["dir"]))
    print("  gate：%s" % ("全过" if data["gate"]["ok"] else "有未通过项"))
    print("  textcheck：碎片 %d · 极短 %d · 长旁 %d · OCR 丢 %d（%.2f%%）"
          % (tc["frag"], tc["tiny"], tc["longnote"], tc["lost_lines"], tc["lostRate"] * 100))
    print("  覆盖率：页 %d · 偏移 %+d · 中位 %.2f · 必看 %d / 参考 %d"
          % (cov.get("pages") or 0, cov.get("offset") or 0, cov.get("median") or 0.0,
             cov.get("lowHard") or 0, (cov.get("lowCount") or 0) - (cov.get("lowHard") or 0)))
    print("  已写入 质检.json 与 转换报告.md 的「复查」节")
    return 0


def body_metrics(book):
    """无参考代理指标（A1 标定）：句读密度、块长分布、整页图版占比。

    只读 book.json 的 blocks，不依赖 OCR 原文，故可用于任何已转换书稿。
    """
    blocks = book.get("blocks") or []
    cjk = lambda c: "\u4e00" <= c <= "\u9fff"  # noqa: E731
    punct_set = set("、。，；：？！…—·「」『』（）《》〈〉")
    han = punct = body_tu = 0
    plens = []
    par_chars = {}
    fig_pages = []
    for b in blocks:
        t = b.get("text") or ""
        btype = b.get("type")
        if btype in ("paragraph", "note"):
            han += sum(1 for c in t if cjk(c))
            punct += sum(1 for c in t if c in punct_set)
            n = len(re.sub(r"\s", "", t))
            plens.append(n)
            pg = b.get("page") or 0
            par_chars[pg] = par_chars.get(pg, 0) + n
            if btype == "paragraph":
                body_tu += t.count("图")
        elif btype == "figure":
            fig_pages.append(b.get("page") or 0)
    lonely = sum(1 for pg in fig_pages if par_chars.get(pg, 0) < 50)
    return {
        "han": han,
        "punct_per100": (100.0 * punct / han) if han else 0.0,
        "avg_blk": (sum(plens) / len(plens)) if plens else 0.0,
        "short_rate": (sum(1 for n in plens if n < 10) / len(plens)) if plens else 0.0,
        "figs": len(fig_pages),
        "lonely_share": (lonely / len(fig_pages)) if fig_pages else 0.0,
        "body_tu": body_tu,
    }


def gate(book_dir, write_profile=True):
    """12 项门槛 + 1 项结构诊断。

    2026-11-25 A1（gate 判据增补 v1）新增 3 项：章结构下限 / 可读性抽样 / 句读密度；
    图注两项新增「整页图版自动豁免」（整页单图页≥80% 且正文无「图」字 → 原书无图注排版）。
    阈值全部由 44 主库 + 8 公开书实测分布标定，主库零误伤（snap2 R4 验证）。

    write_profile=False 时只读不写（比对/复查用），避免把 book.json 的 profile 重写一遍。
    """
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    proof = read_json(os.path.join(book_dir, "proofread.json"), {}) or {}
    stats = book.get("stats", {}) or {}
    pages = max(1, stats.get("pages", 0))
    figs = stats.get("figures", 0)
    paras = max(1, stats.get("paragraphs", 0))
    issues = proof.get("issues", {}) or {}
    split = len(issues.get("split", []) or [])
    nocap = len(issues.get("no_caption", []) or [])
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    prof = book_profile(book)
    classes = prof["classes"]
    sd = structure_diag(book)
    bm = body_metrics(book)
    cpp = max(stats.get("chars", 0), sd["chars"]) / pages
    min_cpp = 20 if "A" in classes else 60
    add("每页字数", min_cpp <= cpp <= 2000, f"{cpp:.0f} 字/页" + ("（A 图录型）" if "A" in classes else ""))
    # stats.paired 缺失时按 figure 块实际 caption 兜底（旧转换书稿常缺该字段，会误报 0%）
    paired_count = stats.get("paired", 0) or 0
    if not paired_count and figs:
        paired_count = sum(1 for b in book.get("blocks", [])
                           if b.get("type") == "figure" and (b.get("caption") or "").strip())
    paired = 1.0 if figs == 0 else paired_count / figs
    # 统一 98% 门槛；只有「原书完全没图注」的书经人工确认后豁免
    # B1（2026-11-25）：小图量书（≤20 图）分母失真，改用绝对量下限——缺 ≤2 视为过
    # B3（2026-11-25 A1）：整页书影页（整页单图 ≥80% 且正文无「图」字）自动豁免，
    #   替代逐本人手 figure_caption_exempt 标记（陶庵梦忆 44 张整页书影即为该类）
    cap_min = 0.98
    auto_exempt = bool(bm["figs"] and bm["lonely_share"] >= 0.8 and bm["body_tu"] == 0)
    cap_exempt = bool(book.get("figure_caption_exempt")) or auto_exempt
    small_figs = figs <= 20
    pair_ok = paired >= cap_min or (small_figs and figs - paired_count <= 2)
    add("图注配对", cap_exempt or figs == 0 or pair_ok,
        "不适用（原书无图注排版）" if cap_exempt
        else f"{paired_count}/{figs}（{paired:.1%}，门槛 98%"
             + ("，小图书按绝对下限放行）" if small_figs and paired < cap_min and figs - paired_count <= 2 else "）"))
    chapter_min = 1 if ("A" in classes or "C" in classes) else 2
    add("章节数", chapter_min <= stats.get("chapters", 0) <= 90,
        str(stats.get("chapters", 0)) + ("（A/C 类放宽到 ≥1）" if chapter_min == 1 else ""))
    # 目录结构检查：重复标题、纯“第N节”标题、空章
    chapters = book.get("chapters") or []
    titles = [str(c.get("title") or "").strip() for c in chapters]
    dup = len(titles) - len(set(titles))
    sec_only = sum(1 for t in titles if re.fullmatch(r"第\s?[0-9一二三四五六七八九十百零两]+\s?节", t))
    tiny = sum(1 for c in chapters
               if (c.get("chars") or 0) < 300 and not re.match(r"^(前言|索引|附录|参考文献)", str(c.get("title") or "")))
    toc_ok = (dup <= max(1, len(titles) // 10)) and sec_only == 0 and tiny <= max(1, len(titles) // 10)
    add("目录结构", toc_ok, f"{len(titles)} 项 · 重名 {dup} · 纯节标题 {sec_only} · 空章 {tiny}")

    def _volno(t):
        m = re.match(r"^(?:[\u4e00-\u9fff]{1,8}\s*)?第\s*([0-9一二三四五六七八九十百]{1,3})\s*卷\s*$", t)
        if not m:
            m = re.match(r"^卷\s*第?\s*([0-9一二三四五六七八九十百]{1,3})(?:\s*[\u4e00-\u9fff]{1,8})?$", t)
        if not m:
            m = re.match(r"^[\u4e00-\u9fff]{2,8}\s*卷\s*([0-9一二三四五六七八九十百]{1,3})\s*$", t)
        return m.group(1) if m else None

    def _vn(s):
        if s.isdigit():
            return int(s)
        d = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if s == "十":
            return 10
        if s.startswith("十"):
            return 10 + d.get(s[1:], 0)
        if s.endswith("十"):
            return d.get(s[0], 0) * 10
        if "十" in s:
            a, b = s.split("十", 1)
            return (d.get(a, 1) * 10) + d.get(b, 0)
        return d.get(s, 0)
    # 卷序连续（2026-11-25 坑1 配套护栏）：仅 ≥2 个卷式标题的章触发；
    # 主库现代书（第N章体）天然不适用 → 恒绿，gate 矩阵不变（回归 R4 验证）。
    nums = sorted(_vn(x) for x in (_volno(t) for t in titles) if x)
    uniq = sorted(set(nums))
    if len(uniq) < 2:
        add("卷序连续", True, "非卷系章节")
    else:
        gaps = [i for i in range(uniq[0], uniq[-1] + 1) if i not in uniq]
        add("卷序连续", not gaps and uniq[0] == 1,
            f"{len(uniq)} 卷 · 缺 {'、'.join(map(str, gaps)) or '无'}"
            + ("" if uniq[0] == 1 else f" · 起点非一（自 {uniq[0]} 起）"))
    # A1 新增三项（阈值见《gate-判据增补_v1_20261125.md》，44 主库零误伤）
    chapters_n = stats.get("chapters", 0)
    big_book = bm["han"] >= 100000
    thin = chapters_n <= 1 and big_book
    add("章结构下限", not thin,
        f"{chapters_n} 章 / {bm['han']} 字" + ("（≥10 万字却仅 1 章，疑卷/章未识别）" if thin else ""))
    # 可读性抽样：三信号计数（≥2 才判红）。单信号在主库有先例，两信号组合 44 本零误伤。
    signals = [
        bm["short_rate"] >= 0.18,                       # 短块（<10 字）占比过高
        0 < bm["avg_blk"] < 70,                         # 平均块长过短（主库最小 70.4）
        not (5.0 <= bm["punct_per100"] <= 18.0),        # 句读密度偏离（主库 5.4–16.4）
    ]
    bad_read = sum(signals) >= 2
    add("可读性抽样", not bad_read,
        f"句读 {bm['punct_per100']:.1f}/百字 · 平均块长 {bm['avg_blk']:.0f} · "
        f"短块率 {bm['short_rate']:.1%} · 信号 {sum(signals)}/3"
        + ("（疑文字乱序或竖排误读）" if bad_read else ""))
    add("句读密度", bm["punct_per100"] >= 3.0,
        f"{bm['punct_per100']:.1f}/百字（下限 3.0，主库实测最低 5.4）")
    hsplit, hbody = hard_splits(book)
    rate = hsplit / max(1, hbody)
    add("句中截断", rate < 0.01, f"{hsplit}/{hbody}（{rate:.2%}）")
    wm = sum(1 for b in book.get("blocks", [])
             if b.get("type") in ("paragraph", "figure", "heading", "note", "caption")
             and has_watermark(block_text(b)))
    add("水印文字", wm == 0, str(wm))
    nocap_rate = nocap / figs if figs else 0.0
    add("缺图注", cap_exempt or nocap_rate < 0.01 or (small_figs and nocap <= 2),
        "不适用（原书无图注排版）" if cap_exempt else f"{nocap}/{figs}（{nocap_rate:.2%}，要求 <1%"
        + ("，小图书按绝对下限放行）" if small_figs and nocap_rate >= 0.01 and nocap <= 2 else "）"))
    add("结构诊断", True,
        f"重复≥5次的标题文本 {sd['headingRep5']} 种 · 重复标题块 {sd['headingDupBlocks']} · 章起点非标题 {sd['startNonTitle']}/{sd['chapters']} · 表格块 {sd['tables']}")
    ok = all(c["ok"] for c in checks)
    if write_profile:
        try:
            book["profile"] = prof
            json.dump(book, open(os.path.join(book_dir, "book.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return ok, checks


def write_report(book_dir, info, checks, ok, seconds):
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    stats = book.get("stats", {}) or {}
    lines = []
    lines.append(f"# 转换报告 · {book.get('bookTitle') or os.path.basename(book_dir)}")
    lines.append("")
    lines.append(f"- 源文件：{info.get('pdf')}")
    lines.append(f"- 转换模式：{'文字层直读（免 OCR）' if info.get('mode') == 'text' else ('OCR 识别（竖排右起）' if info.get('mode') == 'vertical' else 'OCR 识别')}")
    lines.append(f"- 页数：{stats.get('pages')} 页｜正文：{stats.get('chars')} 字｜段落：{stats.get('paragraphs')}")
    lines.append(f"- 插图：{stats.get('figures')} 幅（图注配对 {stats.get('paired')}）")
    lines.append(f"- 章节：{stats.get('chapters')} 章")
    _tb = sum(1 for b in (book.get("blocks") or []) if b.get("type") == "table")
    if _tb:
        lines.append(f"- 表格：{_tb} 张（已转为 table 块）")
    lines.append(f"- 耗时：{seconds/60:.1f} 分钟")
    lines.append("")
    lines.append("## 质量检查（转换当时快照）\n")
    lines.append("| 检查项 | 结果 | 数值 |")
    lines.append("| --- | --- | --- |")
    for c in checks:
        lines.append(f"| {c['name']} | {'✅ 通过' if c['ok'] else '❌ 需人工看'} | {c['detail']} |")
    lines.append("")
    lines.append(f"**结论：{'通过，可直接上架' if ok else '有未通过项，建议先看 校对清单.md'}**")
    lines.append("")
    lines.append("详细可疑点见 `校对清单.md`；**本节是转换当时的快照，最新数字看文末「复查」节**。")
    # 重跑转换/post 时不要把已有的「复查」节抹掉（它由 recheck 维护）
    rp = os.path.join(book_dir, "转换报告.md")
    if os.path.exists(rp):
        with open(rp, encoding="utf-8") as f:
            old = f.read()
        k = old.find(RECHECK_MARK)
        if k >= 0:                      # 保住 recheck 维护的「复查」节，别被 post 覆盖掉
            lines.append("")
            lines.append(old[k:].rstrip())
    with open(rp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


PROBLEM_MD = "转换问题记录.md"
RAW_NAME = "book_raw.json"


def raw_chars(book_dir):
    raw = read_json(os.path.join(book_dir, RAW_NAME), None)
    if not raw:
        return None
    return sum(len(b.get("text") or "") for b in raw.get("blocks", []) if b.get("type") == "paragraph")


def restore_raw(book_dir):
    raw = os.path.join(book_dir, RAW_NAME)
    if os.path.exists(raw):
        import shutil
        shutil.copyfile(raw, os.path.join(book_dir, "book.json"))
        return True
    return False


def check_text_kept(book_dir):
    rc = raw_chars(book_dir)
    if rc is None or rc < 1000:
        return True, ""
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    cur = sum(len(b.get("text") or "") for b in book.get("blocks", []) if b.get("type") == "paragraph")
    ratio = cur / rc
    ok = 0.90 <= ratio <= 1.15
    return ok, f"正文 {cur}/{rc}（{ratio:.0%}）"


def record_problem(out_dir, pdf, book_name, reason, detail=""):
    import datetime
    path = os.path.join(out_dir, PROBLEM_MD)
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write("# 转换问题记录\n\n自动记录无法正常转换或质检不过的书，供人工复核。\n\n")
            f.write("| 时间 | 书名 | 原因 | 详情 |\n| --- | --- | --- | --- |\n")
        f.write("| %s | %s | %s | %s |\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), book_name, reason, detail[:120]))


def quarantine_book(book_dir, out_dir):
    """质检不通过的书稿移出书库，放到「需人工处理/未达标书稿」，读者端就看不到了。"""
    hold = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "需人工处理", "未达标书稿")
    os.makedirs(hold, exist_ok=True)
    dest = os.path.join(hold, os.path.basename(book_dir))
    try:
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(book_dir, dest)
        return dest
    except Exception as e:
        print("  ! 移出书库失败：%s" % e)
        return ""


def preflight(pdf):
    """转换前快速体检：页数、页面尺寸、是否扫描件、预计耗时。"""
    info = {"pdf": pdf, "pages": 0, "size": "", "textLayer": False, "etaMinutes": 0, "warn": ""}
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
        info["pages"] = doc.page_count
        p0 = doc.load_page(0)
        w, h = p0.rect.width, p0.rect.height
        info["size"] = "%d×%dpt" % (round(w), round(h))
        chars = len(p0.get_text("text").strip())
        # 只看封面会误判：用整本抽样（pdf2book.has_text_layer）判断文字层
        try:
            from pdf2book import has_text_layer
            info["textLayer"] = bool(has_text_layer(pdf))
        except Exception:
            info["textLayer"] = chars >= 60
        sec_per_page = 0.3 if info["textLayer"] else 6.0
        if not info["textLayer"] and max(w, h) * 3 > 3000:
            sec_per_page += 2.0
        info["etaMinutes"] = round(info["pages"] * sec_per_page / 60)
        if info["pages"] > 800:
            info["warn"] = "大书，建议单独安排/夜间跑"
        doc.close()
    except Exception as e:
        info["warn"] = "预检失败: %s" % e
    return info


def probe_quality(pdf, sample=6):
    """抽 6 页快速试读，判断是否值得整本转换（图录型/英文书提前跳过）。"""
    info = {"avgChars": 0, "skip": False, "reason": ""}
    try:
        import re as _re
        import numpy as np
        import pymupdf
        from rapidocr_onnxruntime import RapidOCR

        doc = pymupdf.open(pdf)
        n = doc.page_count
        idxs = sorted({min(n - 1, max(0, int(n * f))) for f in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85)})
        engine = RapidOCR()
        chars, latin, cjk, capnums, heads = [], 0, 0, 0, 0
        for i in idxs:
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            res, _ = engine(arr)
            txt = "\n".join((x[1] or "") for x in (res or []))
            chars.append(len(txt))
            latin += len(_re.findall(r"[A-Za-z]", txt))
            cjk += len(_re.findall(r"[\u4e00-\u9fff]", txt))
            capnums += len(_re.findall(r"图\s?\d", txt))
            heads += len(_re.findall(r"第\s?[0-9一二三四五六七八九十百零两]{1,3}\s?[章讲节]|CHAPTER", txt))
        doc.close()
        avg = sum(chars) / max(1, len(chars))
        info.update({"avgChars": round(avg), "captions": capnums, "headings": heads,
                     "latin": latin, "cjk": cjk})
        if avg < 100:
            info.update({"skip": True, "reason": "图录型/正文极少（抽样平均 %.0f 字/页）" % avg})
        elif cjk < 150 and latin > cjk * 2:
            info.update({"skip": True, "reason": "英文书（暂不支持，抽样英文 %d / 中文 %d）" % (latin, cjk)})
    except Exception as e:
        info["reason"] = "试读失败: %s" % e
    return info


def quarantine_pdf(pdf, out_dir):
    import shutil
    hold = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "需人工处理")
    os.makedirs(hold, exist_ok=True)
    try:
        shutil.move(pdf, os.path.join(hold, os.path.basename(pdf)))
        return True
    except Exception:
        return False


def process_one(pdf, out_dir, args, fp):
    book_name = os.path.splitext(os.path.basename(pdf))[0]
    book_dir = os.path.join(out_dir, safe_name(book_name))
    marker_path = os.path.join(book_dir, MARKER)
    marker = read_json(marker_path, {}) or {}
    done = marker.get("done", {}) or {}
    if os.path.exists(os.path.join(book_dir, "book.json")) and not done and not args.force:
        log(f"== {book_name} 已转换过，跳过（重转请加 --force）", fp)
        return True
    params_changed = marker.get("scale") not in (None, args.scale) or marker.get("pdf") not in (None, pdf)
    if params_changed:
        done = {}
    os.makedirs(book_dir, exist_ok=True)
    t0 = time.time()
    log(f"== {book_name}", fp)
    os.makedirs(book_dir, exist_ok=True)  # 目录可能被外部清理，写预检前再兜底一次
    pf = preflight(pdf)
    write_json(os.path.join(book_dir, "预检.json"), pf)
    if not os.path.exists(os.path.join(book_dir, "book.json")) and not args.force:
        if getattr(args, "vertical", False):
            log("  [试读] 竖排分支：跳过横排试读（竖排不适用 det 框/双栏判据）", fp)
        else:
            q = probe_quality(pdf)
            log("  [试读] 平均 %s 字/页 · 图注线索 %s · 章标题线索 %s" % (q.get("avgChars"), q.get("captions"), q.get("headings")), fp)
            if q.get("skip"):
                log("  ! 提前跳过：%s" % q["reason"], fp)
                record_problem(out_dir, pdf, book_name, "提前跳过（试读不合格）", q["reason"])
                quarantine_pdf(pdf, out_dir)
                return False
    log("  [预检] %s 页 · %s · %s · 预计 %s 分钟%s" % (
        pf["pages"], pf["size"], "电子版（有文字层）" if pf["textLayer"] else "扫描件（OCR）",
        pf["etaMinutes"], (" · " + pf["warn"]) if pf["warn"] else ""), fp)

    vertical = bool(getattr(args, "vertical", False))
    stages = ["convert"] if vertical else (["convert"] + POST_STAGES)
    if not vertical:
        if args.from_stage in POST_STAGES:
            stages = POST_STAGES[POST_STAGES.index(args.from_stage):]
            stages = ["convert"] + stages if marker.get("done", {}).get("convert") is None and args.from_stage == "convert" else stages
        if args.from_stage and args.from_stage not in stages:
            stages = [args.from_stage] + POST_STAGES[POST_STAGES.index(args.from_stage):] if args.from_stage in POST_STAGES else ["convert"] + POST_STAGES
    elif args.from_stage:
        log("  ! 竖排分支只跑 convert（后处理由 vertical_book 内建完成），忽略 --from-stage", fp)

    try:
        return _run_stages(pdf, out_dir, book_dir, book_name, args, fp, stages, vertical=vertical)
    except Exception as e:
        log("  ! 异常：%s" % e, fp)
        record_problem(out_dir, pdf, book_name, "转换异常", str(e))
        return False


def _run_stages(pdf, out_dir, book_dir, book_name, args, fp, stages, vertical=False):
    t0 = time.time()
    marker_path = os.path.join(book_dir, MARKER)
    marker = read_json(marker_path, {}) or {}
    done = marker.get("done", {}) or {}
    for st in stages:
        if st == "chapters2" and not need_chapter_fix(book_dir) and not args.force:
            # --force：章节数达标但形态可疑（如卷序断档）的书稿，强制重建（2026-11-25 坑1 回归配套）
            log("  - chapters2: 章节数正常，跳过", fp)
            done[st] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": 0}
            marker = {"version": 1, "pdf": pdf, "scale": args.scale,
                          "layout": "vertical" if vertical else "horizontal", "done": done}
            write_json(marker_path, marker)
            continue
        if st == "headertoc" and not need_header_toc(book_dir):
            log("  - headertoc: 目录结构正常，跳过", fp)
            done[st] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": 0}
            marker = {"version": 1, "pdf": pdf, "scale": args.scale,
                          "layout": "vertical" if vertical else "horizontal", "done": done}
            write_json(marker_path, marker)
            continue
        if done.get(st) and not args.force:
            log(f"  - {st}: 已完成，跳过", fp)
            continue
        t = time.time()

        def do_convert():
            if vertical:
                return convert_vertical(pdf, book_dir, getattr(args, "title", "") or "", book_name,
                                        args.scale, fp, ears=getattr(args, "ear", "") or "",
                                        model=getattr(args, "vmodel", "") or "")
            return convert(pdf, book_dir, getattr(args, "title", "") or "", book_name, args.scale, fp)

        if st == "convert":
            code, _ = do_convert()
        else:
            code, _ = post_stage(st, book_dir, fp)
        if code != 0:
            if st == "convert":
                # 偶发启动卡死（Windows 上启动器未拉起子进程）：等待后重试一次
                log("  ! convert 失败，20 秒后自动重试一次", fp)
                time.sleep(20)
                code, _ = do_convert()
            if st == "convert" and code != 0:
                log("  ! convert 重试仍失败，终止本书", fp)
                return False
            if st != "convert":
                log(f"  ! {st} 失败，继续后续阶段（可在 校对清单 里检查）", fp)
            log(f"  ! {st} 失败，继续后续阶段（可在 校对清单 里检查）", fp)
        if st == "convert" and code == 0:
            bj = read_json(os.path.join(book_dir, "book.json"), {}) or {}
            _ch = (bj.get("stats", {}) or {}).get("chars", 0)
            _pg = max(1, (bj.get("stats", {}) or {}).get("pages", 1))
            if _ch >= 1000 and _pg >= 60 and _ch / _pg < 120:
                log("  ! 正文密度过低（%.0f 字/页），跳过后续处理", fp)
                record_problem(out_dir, pdf, book_name, "正文密度过低，停止后处理", "%.0f 字/页" % (_ch / _pg))
                return False
            if _ch < 1000:
                log("  ! 正文过少（可能倒置/空白扫描），记录并跳过", fp)
                record_problem(out_dir, pdf, book_name, "正文过少（可能倒置/空白扫描）", "chars<1000")
                return False
        done[st] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t, 1)}
        marker = {"version": 1, "pdf": pdf, "scale": args.scale,
                          "layout": "vertical" if vertical else "horizontal", "done": done}
        write_json(marker_path, marker)

    kept_ok, kept_detail = check_text_kept(book_dir)
    if not kept_ok:
        log("  ! 后处理正文丢失（%s），已回滚到原始快照" % kept_detail, fp)
        restore_raw(book_dir)
        record_problem(out_dir, pdf, book_name, "后处理丢失正文（已回滚）", kept_detail)
        return False

    mode = marker.get("mode") or ("text" if read_json(os.path.join(book_dir, "pages", "page_001_text.json")) else "ocr")
    if vertical:
        mode = "vertical"
    seconds = time.time() - t0
    ok, checks = gate(book_dir)
    write_report(book_dir, {"pdf": pdf, "mode": mode}, checks, ok, seconds)
    if not ok:
        detail = "；".join(f"{c['name']}:{c['detail']}" for c in checks if not c["ok"])
        record_problem(out_dir, pdf, book_name, "质检未通过", detail)
        moved = quarantine_book(book_dir, out_dir)
        if moved:
            log(f"  → 未达标，已移出书库：{moved}", fp)
    log(f"  => {'✅ 通过' if ok else '⚠️ 需人工看'} · " +
        " · ".join(f"{c['name']}:{c['detail']}" for c in checks), fp)
    return ok


def cmd_one(args):
    return process_one(args.pdf, args.out, args, None)


def cmd_batch(args):
    cfg = read_json(os.path.join(ROOT, "pipeline.json"), {}) or {}
    folder = args.folder or cfg.get("inbox") or ""
    if not folder:
        print("没有指定文件夹，pipeline.json 里也没有 inbox")
        return 2
    if not os.path.isabs(folder):
        folder = os.path.join(ROOT, folder)
    args.folder = folder
    if not args.out:
        args.out = cfg.get("out") or BOOKS
    if not os.path.isabs(args.out):
        args.out = os.path.join(ROOT, args.out)
    pdfs = sorted(glob.glob(os.path.join(args.folder, "*.pdf")))
    seen = {}
    for p in pdfs:
        seen.setdefault(os.path.normcase(os.path.abspath(p)), p)
    pdfs = sorted(seen.values())
    if not pdfs:
        print("没有找到 PDF：", args.folder)
        return 0
    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "转换日志.txt")
    t0 = time.time()
    results = []
    with open(log_path, "a", encoding="utf-8") as fp:
        log(f"批处理开始：{len(pdfs)} 本 · workers={args.workers} · scale={args.scale}", fp)
        if args.workers <= 1:
            for p in pdfs:
                results.append((p, process_one(p, args.out, args, fp)))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(process_one, p, args.out, args, fp): p for p in pdfs}
                for fut in concurrent.futures.as_completed(futs):
                    results.append((futs[fut], fut.result()))
        okn = sum(1 for _, ok in results if ok)
        log(f"批处理结束：{okn}/{len(results)} 通过 · 总耗时 {(time.time()-t0)/60:.1f} 分钟", fp)
        log("结果清单：", fp)
        for p, ok in results:
            log(f"  {'✅' if ok else '⚠️'} {os.path.basename(p)}", fp)
    return 0


def cmd_post(args):
    book_dir = args.bookdir
    _bk = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    if ((_bk.get("layout") or {}).get("direction") == "vertical"):
        print("  - 竖排书稿：convert 阶段已内建成段/卷章/页级闸，横排后处理阶段不适用，跳过。")
        print("    复查请用：python tools/pipeline.py recheck \"%s\"" % book_dir)
        return 0
    fp = None
    ok = True
    if restore_raw(book_dir):
        print("  - 已从原始快照 book_raw.json 重建，保证后处理可重复")
    for st in POST_STAGES:
        if st == "chapters2" and not need_chapter_fix(book_dir):
            print("  - chapters2: 章节数正常，跳过")
            continue
        if st == "headertoc" and not need_header_toc(book_dir):
            print("  - headertoc: 目录结构正常，跳过")
            continue
        code, _ = post_stage(st, book_dir, fp)
        if code != 0:
            ok = False
    kept_ok, kept_detail = check_text_kept(book_dir)
    if not kept_ok:
        print("  ! 正文丢失（%s），回滚到原始快照" % kept_detail)
        restore_raw(book_dir)
        record_problem(book_dir, "(重跑后处理)", os.path.basename(book_dir), "后处理丢失正文（已回滚）", kept_detail)
        return 1
    ok2, checks = gate(book_dir)
    write_report(book_dir, {"pdf": "(仅重跑后处理)"}, checks, ok2, 0)
    print("后处理完成，质量检查：", "通过" if ok2 else "有未通过项")
    return 0


def find_book_dir(arg):
    """接受完整目录名、目录路径、或能唯一定位的片段（和 qbank 的书名解析保持一致）。"""
    if not arg:
        return None
    for cand in (arg, os.path.join(arg, "book.json"), os.path.join(BOOKS, arg, "book.json")):
        if os.path.isfile(cand):
            return os.path.dirname(cand)
    key = re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", arg)
    hit = []
    for d in sorted(os.listdir(BOOKS)):
        p = os.path.join(BOOKS, d, "book.json")
        if not os.path.exists(p):
            continue
        k = re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", d)
        if key and (key in k or k.startswith(key)):
            hit.append(d)
    if len(hit) == 1:
        return os.path.join(BOOKS, hit[0])
    if not hit:
        raise SystemExit("找不到书稿：%s（跑 python tools/pipeline.py structure --all 看全库）" % arg)
    raise SystemExit("书名“%s”匹配到 %d 本，请写全：\n  %s" % (arg, len(hit), "\n  ".join(hit[:8])))


def cmd_structure(args):
    """结构体检：页眉重复 / 章起点 / 表格块，只报数，不计入门槛。"""
    if getattr(args, "all", False):
        rows = []
        for d in sorted(os.listdir(BOOKS)):
            p = os.path.join(BOOKS, d, "book.json")
            if os.path.exists(p):
                rows.append((d, structure_diag(read_json(p, {}))))
        rows.sort(key=lambda r: (-r[1]["headingDupBlocks"], -r[1]["startNonTitle"]))
        print("%-42s %7s %6s %10s %5s" % ("书", "重复块", "≥5种", "起点非标题", "表格"))
        for d, s in rows:
            print("%-42s %7d %6d %7d/%-3d %5d"
                  % (d[:40], s["headingDupBlocks"], s["headingRep5"], s["startNonTitle"], s["chapters"], s["tables"]))
        print("说明：重复页眉在扫描版教材里普遍存在，故只作诊断，不做门槛。")
        return 0
    d = find_book_dir(args.bookdir)
    if not d:
        raise SystemExit("用法：python tools/pipeline.py structure <书名或目录>  或  --all")
    sd = structure_diag(read_json(os.path.join(d, "book.json"), {}))
    print("# " + os.path.basename(d))
    for k in ("headingKinds", "headingRep5", "headingDupBlocks", "startNonTitle", "chapters", "tables", "chars"):
        print("  %-16s %s" % (k, sd.get(k)))
    return 0


def cmd_textcheck(args):
    """文字层体检：碎片块/极短块/长句旁注/OCR 行丢失/字级多出。只报数，不计入门槛。"""
    if getattr(args, "all", False):
        rows = []
        for d in sorted(os.listdir(BOOKS)):
            bd = os.path.join(BOOKS, d)
            if not os.path.exists(os.path.join(bd, "book.json")):
                continue
            r = textcheck(bd)
            rows.append((d, r))
        rows.sort(key=lambda x: -(len(x[1]["lost_lines"]) / max(1, x[1].get("_ocr_lines", 1)) + len(x[1]["frag"]) * 0.01))
        if getattr(args, "json", False):
            print(json.dumps(dict(rows), ensure_ascii=False, indent=2))
            return 0
        print("%-38s %5s %7s %7s %7s %6s" % ("书", "碎片", "OCR丢", "丢行率", "极短", "长旁"))
        for d, r in rows:
            tot = max(1, r.get("_ocr_lines", 0))
            print("%-38s %5d %7d %6.2f%% %7d %6d"
                  % (d[:36], len(r["frag"]), len(r["lost_lines"]),
                     100.0 * len(r["lost_lines"]) / tot,
                     len(r["tiny"]), len(r["longnote"])))
        print("说明：极短块/长句旁注多为正常（图内标注、旁注本身长）；碎片块与 OCR 行丢失才是要处理的。")
        return 0
    d = find_book_dir(args.bookdir)
    if not d:
        raise SystemExit("用法：python tools/pipeline.py textcheck <书名或目录>  或  --all")
    r = textcheck(d)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("# " + os.path.basename(d))
    print("  碎片块(标点开头)   %d" % len(r["frag"]))
    print("  极短块(<=12字)     %d" % len(r["tiny"]))
    print("  长句旁注           %d" % len(r["longnote"]))
    print("  OCR 行丢失         %d" % len(r["lost_lines"]))
    print("  缺缓存 %d 页 · 无书稿块 %d 页 · 异页文字线索 %d 行 · 旧缓存 %d 页"
          % (len(r["missing_cache_pages"]), len(r["no_block_pages"]),
             len(r["other_page_lines"]), len(r["legacy_cache_pages"])))
    
    for k, title in (("frag", "碎片块"), ("lost_lines", "OCR行丢失"),
                     ("tiny", "极短块"), ("longnote", "长句旁注")):
        if not r[k]:
            continue
        print("\n[" + title + "] 前 15 条")
        for x in r[k][:15]:
            print("   ", x)
    return 0


def main():
    ap = argparse.ArgumentParser(description="筑学 · 一键转换工作流")
    sub = ap.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("one", help="转换一本 PDF")
    one.add_argument("pdf")
    one.add_argument("--out", default=BOOKS)
    one.add_argument("--title", default="")
    one.add_argument("--scale", type=float, default=3.0)
    one.add_argument("--force", action="store_true")
    one.add_argument("--from-stage", default="", choices=[""] + ALL_STAGES)
    one.add_argument("--vertical", action="store_true",
                     help="竖排右起扫描件：走 tools/vertical_book.py（忽略文字层，不跑横排后处理）")
    one.add_argument("--ear", default="", help="竖排书耳/版心书名，逗号分隔（默认取书名）")
    one.add_argument("--vmodel", default="", help="竖排 rec 模型 onnx（默认 tools/models/）")
    one.set_defaults(func=cmd_one)

    batch = sub.add_parser("batch", help="转换整个文件夹")
    batch.add_argument("folder", nargs="?", default=None)
    batch.add_argument("--out", default=BOOKS)
    batch.add_argument("--scale", type=float, default=3.0)
    batch.add_argument("--workers", type=int, default=1)
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--from-stage", default="", choices=[""] + ALL_STAGES)
    batch.add_argument("--vertical", action="store_true", help="竖排右起扫描件（同 one --vertical）")
    batch.add_argument("--ear", default="", help="竖排书耳/版心书名，逗号分隔")
    batch.add_argument("--vmodel", default="", help="竖排 rec 模型 onnx")
    batch.set_defaults(func=cmd_batch)

    post = sub.add_parser("post", help="只重跑后处理（不重新 OCR）")
    post.add_argument("bookdir")
    post.set_defaults(func=cmd_post)

    st = sub.add_parser("structure", help="结构体检：页眉重复/章起点/表格（不计入门槛）")
    st.add_argument("bookdir", nargs="?", default="")
    st.add_argument("--all", action="store_true", help="扫描全库")
    st.set_defaults(func=cmd_structure)

    tc = sub.add_parser("textcheck", help="文字层体检：碎片块/极短块/长句旁注/OCR行丢失/字级多出（不计入门槛）")
    tc.add_argument("bookdir", nargs="?", default="")
    tc.add_argument("--all", action="store_true", help="扫描全库")
    tc.add_argument("--json", action="store_true", help="输出完整候选 JSON")
    tc.set_defaults(func=cmd_textcheck)

    cov = sub.add_parser("coverage", help="逐页覆盖率体检：找整页/半页正文丢失（不计入门槛）")
    cov.add_argument("bookdir", nargs="?", default="")
    cov.add_argument("--all", action="store_true", help="扫描全库")
    cov.set_defaults(func=cmd_coverage)

    rc = sub.add_parser("recheck", help="复查：gate + textcheck + 逐页覆盖率 → 质检.json + 报告「复查」节")
    rc.add_argument("bookdir", nargs="?", default="")
    rc.add_argument("--all", action="store_true", help="复查全库并写 交接文档/质检-复查汇总.md")
    rc.set_defaults(func=cmd_recheck)

    args = ap.parse_args()
    r = args.func(args)
    # bool 语义（one/post 等返回 True/False）→ 0/1；int 语义（batch 的 0/2）原样透传
    if isinstance(r, bool):
        return 0 if r else 1
    return r or 0


if __name__ == "__main__":
    sys.exit(main())
