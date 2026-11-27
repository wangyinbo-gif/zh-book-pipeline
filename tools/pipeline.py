# -*- coding: utf-8 -*-
"""筑学 · 一键转换工作流

把「PDF → 可读书」的整条流水线固定下来，后面加书只用一条命令。

用法：
    python tools/pipeline.py batch "待转换书籍"                   # 只预演（默认，零写入）：分类表 + 依据
    python tools/pipeline.py batch "待转换书籍" --apply           # 确认后真跑（新增书；已在库的要显式授权）
    python tools/pipeline.py batch "待转换书籍" --apply --workers 2   # 并行 2 本（8 核以上；仍受单实例锁约束）
    python tools/pipeline.py one "待转换书籍/某书.pdf"            # 转换一本
    python tools/pipeline.py one "books/某本书" 之类已在库的      # 无 --force / 不在批准清单 → G3 零写入跳过
    python tools/pipeline.py post "books/某本书"                  # 只重跑后处理（不重新识别）

批处理护栏（2026-11-27 加，起因见 §4.13 重复批处理事故，实现在「批处理护栏 G1–G5」一节）：
    G1 batch --apply 单实例互斥锁（活锁拒绝并打印持有者；陈旧锁标记未完成再接管）
    G2 batch 默认只预演，--apply 才写盘
    G3 已在库（有 book.json）的书稿未获 --force / 批准清单授权 → 任何写盘之前返回，零写入
    G4 run-id（北京时间 YYYYMMDD_HHMM）+「批处理计划/结束/中止（未完成清单）」运行记录
    G5 recheck 在 质检.json 记 bookFingerprint（sha256，剔除 stats），供 check_stats.py 判陈旧质检

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
from paths import ROOT
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


# 后处理阶段 → 脚本与参数（抽成数据，便于单测 argv 组装）
STAGE_SCRIPTS = {
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

# B5（2026-11-27）配套：fix_unpaired_captions.py 改成「默认只预演、--apply 才写盘」。
# 流水线里的 captions 阶段是明确的授权写盘动作，所以由流水线显式补 --apply；
# 人工在命令行单独调用时仍是 dry-run，避免「一跑就改稿」。
APPLY_REQUIRED = {"fix_unpaired_captions.py"}


def stage_cmd(stage, book_dir):
    """返回某个后处理阶段的完整 argv（纯函数，供单测）。"""
    name, extra = STAGE_SCRIPTS[stage]
    args = [os.path.join(book_dir, e) if e == "book.json" else book_dir for e in extra]
    if not extra:
        args = [book_dir]
    if name in APPLY_REQUIRED:
        args = args + ["--apply"]
    return [PY, os.path.join(TOOLS, name)] + args


def post_stage(stage, book_dir, fp):
    if stage == "preview":
        log("  生成翻页预览 preview.html", fp)
        # 用当前 book.json 重建预览
        code = (
            "import json,sys,os;sys.path.insert(0,%r);from book_preview import build_preview;"
            "p=os.path.join(%r,'book.json');b=json.load(open(p,encoding='utf-8'));"
            "build_preview(b,os.path.join(%r,'preview.html'))" % (TOOLS, book_dir, book_dir)
        )
        return run([PY, "-c", code], fp, quiet=True)
    return run(stage_cmd(stage, book_dir), fp)



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


def p0_block_pages(block):
    listed = block.get("pages")
    candidates = listed if isinstance(listed, list) else []
    return {n for n in [block.get("page"), *candidates] if type(n) is int and n > 0}


def p0_grams(text):
    return [text[i:i + 4] for i in range(len(text) - 3)]


def p0_source_counts(blocks, source_text, han):
    """Allocate only unambiguous evidence in multi-page blocks, once per character."""
    counts = {}
    unresolved = 0
    page_grams = {}

    def index(page):
        if page not in page_grams:
            page_grams[page] = set(p0_grams(han(source_text.get(page, ""))))
        return page_grams[page]

    for block in blocks:
        text = han(block_text(block))
        pages = p0_block_pages(block)
        if len(pages) <= 1:
            if pages:
                p, = pages
                counts[p] = counts.get(p, 0) + len(text)
            else:
                unresolved += len(text)
            continue
        owners = [set() for char in text]
        for i, gram in enumerate(p0_grams(text)):
            matches = [p for p in pages if gram in index(p)]
            if len(matches) == 1:
                p, = matches
                for pos in range(i, i + 4):
                    owners[pos].add(p)
        for possible in owners:
            if len(possible) == 1:
                p, = possible
                counts[p] = counts.get(p, 0) + 1
            else:
                unresolved += 1
    return counts, unresolved


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
    book_pages = set().union(*(p0_block_pages(block) for block in blocks))
    out["diagnosticVersion"] = "p0-sliding4-pages-v1"
    out["short_continuation_candidates"] = []
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
        for page in p0_block_pages(block):
            text_by_page.setdefault(page, []).append(text)
    text_by_page = {page: "".join(texts) for page, texts in text_by_page.items()}

    head_pat = re.compile(r"^(公共建筑设计原理|第\s?\d+\s?章)")

    all_chunks = None
    front_pat = re.compile(r"^(上架建议|ISBN|图书在版编目|责任编辑|版权所有|定价|出版发行|全国百佳|如有印装)")
    for p, lines in ocr_by_page.items():
        current = text_by_page.get(p, "")
        nearby = {page: text_by_page.get(page, "") for page in range(p - 3, p + 4) if page != p}
        for i, ln in enumerate(lines):
            if head_pat.match(ln) or front_pat.match(ln) or re.match(r"^图\s*\d", ln):
                continue
            if has_watermark(ln):  # 扫描版夹带的广告贴纸文字，不算书稿内容
                continue
            h = han_only(ln)
            if 4 <= len(h) <= 12:
                out["short_continuation_candidates"].append(
                    {"page": p, "line": i + 1, "text": ln, "han": len(h),
                     "previous": lines[i - 1] if i else None,
                     "next": lines[i + 1] if i + 1 < len(lines) else None,
                     "status": "review", "matched_on_page": h in current})
            if len(h) < 10:
                continue
            chunks = p0_grams(h)
            if cover(chunks, current) >= 0.5:
                continue
            matched_pages = [page for page, text in nearby.items() if cover(chunks, text) >= 0.8]
            if matched_pages:
                out["other_page_lines"].append((p, ln[:46], matched_pages))
                continue
            if all_chunks is None:
                all_chunks = {g for text in all_text for g in p0_grams(text)}
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

    by_page, unattributed = p0_source_counts(
        book.get("blocks") or [], ocr_text, lambda s: "".join(han.findall(s or "")))
    res["sourceAttributedChars"] = sum(by_page.values())
    res["sourceUnattributedChars"] = unattributed
    res["diagnosticVersion"] = "p0-sliding4-pages-v1"

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
    fig_pages = set().union(*(p0_block_pages(b) for b in book.get("blocks") or []
                              if b.get("type") == "figure"))
    plate_book = median < 0.35          # 整页图版型：书稿本来就不承载这一页的文字
    book_han = "".join(han.findall("".join(block_text(b) for b in (book.get("blocks") or []))))
    # 正文最后一页（用于判断「正文之后的附页」）
    body_pages = set().union(*(p0_block_pages(b) for b in book.get("blocks") or []
                               if b.get("type") in ("paragraph", "heading")))
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
        # G5 内容指纹（剔除 stats 后规范化 sha256）：只改统计字段时不变，
        # check_stats.py 据此区分「stats-only 校正」与「书稿内容又变了」的陈旧质检。
        "bookFingerprint": book_fingerprint(book),
        "gateNames": [c["name"] for c in checks],
        "gate": {"ok": ok, "checks": checks},
        "textcheck": {"frag": len(tc["frag"]), "tiny": len(tc["tiny"]),
                      "longnote": len(tc["longnote"]), "lost_lines": lost,
                      "ocrLines": ocr_lines,
                      "otherPageLines": len(tc["other_page_lines"]),
                      "missingCachePages": tc["missing_cache_pages"],
                      "noBlockPages": tc["no_block_pages"],
                      "legacyCachePages": tc["legacy_cache_pages"],
                      "diagnosticVersion": tc["diagnosticVersion"],
                      "shortContinuationCandidates": len(tc["short_continuation_candidates"]),
                      "lostRate": round(lost / float(ocr_lines), 4) if ocr_lines else 0.0,
                      "samples": [{"page": p, "text": t} for p, t in tc["lost_lines"][:8]]},
        "coverage": {k: cov.get(k) for k in ("pages", "offset", "median",
                                              "lowCount", "lowHard", "low", "note",
                                              "diagnosticVersion", "sourceAttributedChars", "sourceUnattributedChars")},
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
    if tc.get("diagnosticVersion"):
        lines.append("- 诊断口径：%s；4–12 字短行参考候选 %s" %
                     (tc["diagnosticVersion"], tc.get("shortContinuationCandidates", 0)))
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


def qc_summary_path():
    return os.path.join(ROOT, "交接文档", "质检-复查汇总.md")


def qc_summary_rows():
    """读各书**现有** 质检.json（不重跑 gate），供汇总刷新用。

    存在理由：汇总过去只能靠 recheck --all 生成，而 --all 会把 55 本的
    质检.json 与报告「复查」节全部重写一遍——把 24 份 8 项历史记录也刷成
    12 项，历史分布就查不回来。逐本复检 + 本函数刷新汇总才是无损路径。
    """
    rows, missing = [], []
    for d in sorted(os.listdir(BOOKS)):
        bd = os.path.join(BOOKS, d)
        if not os.path.exists(os.path.join(bd, "book.json")):
            continue
        data = read_json(os.path.join(bd, "质检.json"), None)
        if not isinstance(data, dict) or "gate" not in data or "textcheck" not in data:
            missing.append(d)
            continue
        data.setdefault("dir", d)
        data.setdefault("coverage", {})
        rows.append(data)
    return rows, missing


def render_qc_summary(rows):
    """汇总 md。「项数」= 该书 gate.checks 数组长度（现行 12，历史件可能 8）。"""
    rows = sorted(rows, key=lambda x: (-(x["textcheck"].get("lost_lines") or 0),
                                        -((x.get("coverage") or {}).get("lowHard") or 0)))
    cnt = {}
    for x in rows:
        n = len(x["gate"].get("checks") or [])
        cnt[n] = cnt.get(n, 0) + 1
    dist = " · ".join("%d 项 %d 本" % (k, cnt[k]) for k in sorted(cnt, reverse=True))
    nok = sum(1 for x in rows if x["gate"].get("ok"))
    md = ["# 书稿质检复查汇总", "",
          "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M"),
          "- 覆盖：%d 本（门禁通过 %d · 未过 %d）；门禁项数分布：%s" % (len(rows), nok, len(rows) - nok, dist),
          "- 刷新方式：逐本跑 recheck <书名> 之后再跑 recheck --summary-only 刷新本表；"
          "  recheck --all 会重写全库每本的 质检.json 与报告「复查」节（含 8 项历史记录），非必要不用",
          "- 逐本明细见各书 转换报告.md 的「复查」节 与 质检.json", "",
          "| 书 | 项数 | gate | 碎片块 | 极短块 | 长句旁注 | OCR 行丢失 | 丢行率 | 必看页 | 覆盖率中位 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for x in rows:
        tc = x["textcheck"]
        cov = x.get("coverage") or {}
        med = "—" if cov.get("note") else ("%.2f" % (cov.get("median") or 0.0))
        md.append("| %s | %d | %s | %d | %d | %d | %d | %.2f%% | %d | %s |" % (
            x["dir"], len(x["gate"].get("checks") or []), "✅" if x["gate"].get("ok") else "❌",
            tc.get("frag", 0), tc.get("tiny", 0), tc.get("longnote", 0), tc.get("lost_lines", 0),
            (tc.get("lostRate") or 0) * 100, cov.get("lowHard") or 0, med))
    md.append("")
    md.append("> OCR 行丢失 = pages/ 里的 OCR 原行在书稿（本页±3 页 / 全书）找不到；"
              "丢行率 = 缺失行 / OCR 总行。参考口径见 docs/书稿文字层质检-textcheck全库扫描-20261106.md。")
    md.append(">")
    md.append("> 覆盖率中位显示「—」的书：无 pages/ OCR 缓存、或整页图版型（书稿按图承载文字），"
              "逐页覆盖率不适用，见该书 质检.json 的 coverage.note。")
    md.append(">")
    md.append("> 「项数」8 与 12 都是**通过**记录：8 项是 gate 增补前落盘的历史件（未复跑，保持原样），"
              "12 项是现行 gate 下的记录。项数差异只代表该记录有没有在新版 gate 下复跑过，不代表质量差异。")
    return md


def cmd_recheck(args):
    """跑 gate + textcheck + 逐页覆盖率 → 写 质检.json，并重生成 转换报告.md 的「复查」节。"""
    if getattr(args, "summary_only", False):
        rows, missing = qc_summary_rows()
        md = render_qc_summary(rows)
        mp = qc_summary_path()
        with open(mp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(md) + "\n")
        cnt = {}
        for x in rows:
            n = len(x["gate"].get("checks") or [])
            cnt[n] = cnt.get(n, 0) + 1
        print("汇总（只读刷新，未重跑 gate）：%s" % mp)
        print("  覆盖 %d 本 · 门禁项数分布 %s · 无质检件 %d 本%s"
              % (len(rows), " ".join("%d项:%d" % (k, cnt[k]) for k in sorted(cnt, reverse=True)),
                 len(missing), (" → " + "、".join(missing[:5])) if missing else ""))
        return 0
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
        md = render_qc_summary(rows)
        mp = qc_summary_path()
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
    #   替代逐本人手 figure_caption_exempt 标记（整页书影多的竖排书即为该类）
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


RESTORE_RATIO_LO, RESTORE_RATIO_HI = 0.90, 1.15      # 与 check_text_kept 同区间
RESTORE_STALE_DAYS = 7                                # book_raw.json 早于现行稿超过 N 天 = 旧快照


def para_chars(book):
    return sum(len(b.get("text") or "") for b in ((book or {}).get("blocks") or [])
               if b.get("type") == "paragraph")


def restore_raw_verdict(raw_book, cur_book, raw_mtime=None, cur_mtime=None,
                        lo=RESTORE_RATIO_LO, hi=RESTORE_RATIO_HI, stale_days=RESTORE_STALE_DAYS):
    """restore_raw 硬闸（纯函数，便于单测）。

    返回 (action, ratio, reason)：
      skip              没有 book_raw.json 可覆盖
      blocked           raw 与现行稿差异大 / raw 明显更旧 → 覆盖等于回退深审成果
      allow             基本一致，可以覆盖（调用方仍必须先快照现行稿）
    比值口径与 check_text_kept 一致：现行 paragraph 正文 / raw paragraph 正文。
    """
    if raw_book is None:
        return "skip", None, "无 book_raw.json，不涉及覆盖"
    rc, cc = para_chars(raw_book), para_chars(cur_book)
    if not rc:
        return "skip", None, "book_raw.json 无 paragraph 正文，check_text_kept 口径不适用"
    ratio = cc / float(rc)
    reasons = []
    if not (lo <= ratio <= hi):
        reasons.append("现行/raw 正文比 %.4f 超出 %.2f–%.2f（raw=%d 字，现行=%d 字）"
                       % (ratio, lo, hi, rc, cc))
    if raw_mtime and cur_mtime:
        days = (cur_mtime - raw_mtime) / 86400.0
        if days > stale_days:
            reasons.append("book_raw.json 比现行 book.json 早 %.1f 天（>阈值 %d 天）→ 现行稿含其后的深审成果"
                           % (days, stale_days))
    if reasons:
        return "blocked", round(ratio, 4), "；".join(reasons)
    return "allow", round(ratio, 4), "raw 与现行稿正文基本一致（比值 %.4f）" % ratio


def backup_root_for(book_dir):
    """现行稿快照目录：books/ 下的书 → <root>/_book_backups/<书名>/（沿用项目命名口径）；
    临时夹具目录同样只在夹具内落盘，绝不写真实 _book_backups。"""
    bd = os.path.abspath(book_dir)
    parent = os.path.dirname(bd)
    grand = os.path.dirname(parent) if os.path.basename(parent).lower() in ("books", "需人工处理") else parent
    return os.path.join(grand, "_book_backups", os.path.basename(bd))


def snapshot_book_json(book_dir, tag="restore_raw前"):
    src = os.path.join(book_dir, "book.json")
    if not os.path.isfile(src):
        return ""
    d = backup_root_for(book_dir)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "book.json.bak_%s_%s" % (time.strftime("%Y%m%d_%H%M"), tag))
    import shutil
    shutil.copy2(src, dst)
    return dst


def restore_raw(book_dir, fp=None, allow=False, log_fn=None):
    """用 book_raw.json 覆盖现行稿。G6：默认拒绝危险覆盖；放行前一定先快照现行稿。"""
    log_fn = log_fn or log
    raw = os.path.join(book_dir, RAW_NAME)
    if not os.path.exists(raw):
        return False, {"action": "skip", "reason": "无 %s" % RAW_NAME}
    raw_book = read_json(raw, {}) or {}
    cur_book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    rm, cm = os.path.getmtime(raw), os.path.getmtime(os.path.join(book_dir, "book.json"))
    action, ratio, reason = restore_raw_verdict(raw_book, cur_book, rm, cm)
    info = {"action": action, "ratio": ratio, "reason": reason, "rawChars": para_chars(raw_book),
            "curChars": para_chars(cur_book),
            "rawMtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rm)),
            "backup": ""}
    if action == "blocked" and not allow:
        log_fn("  ! 拒绝 restore_raw：%s → 现行稿保留不动（确要回退加 --allow-restore-raw）" % reason, fp)
        return False, info
    info["backup"] = snapshot_book_json(book_dir)
    log_fn("  ! 即将用 %s（mtime %s · 正文 %d 字）覆盖现行 book.json（正文 %d 字）· 覆盖前快照：%s · 依据：%s"
           % (RAW_NAME, info["rawMtime"], info["rawChars"], info["curChars"],
              info["backup"] or "（无 book.json 可快照）", reason), fp)
    import shutil
    shutil.copyfile(raw, os.path.join(book_dir, "book.json"))
    return True, info


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
    # --- G3 已入库零写入（护栏）：必须排在任何写盘之前 ---------------------
    # 事故路径就是「book.json 在 + .pipeline.json 的 done 非空」时一路「已完成，跳过」
    # 跑下来，照样 gate(write_profile=True) 重写 book.json、write_report 重写 转换报告.md。
    if os.path.exists(os.path.join(book_dir, "book.json")) and not reconvert_authorized(book_dir, args):
        log("== %s 已在库（book.json 存在）· 未获重转授权 → 护栏 G3：零写入跳过"
            "（不跑阶段、不写 预检.json / .pipeline.json / 转换报告.md / 质检.json）" % book_name, fp)
        log("   确需重转：--force，或把书名写进批准清单 %s" % default_approval_path(), fp)
        return True
    # -----------------------------------------------------------------------
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
        # G6：12-01 事故里「后处理丢失正文（已回滚）」真因是 restore_raw 用 11-02 的
        # book_raw.json 盖掉了 22 天的深审成果。这里默认拒绝回滚，只保留现场。
        did, info = restore_raw(book_dir, fp, allow=getattr(args, "allow_restore_raw", False), log_fn=log)
        if not did and info.get("action") == "blocked":
            record_problem(out_dir, pdf, book_name, "正文比值异常且已拒绝 restore_raw（G6 硬闸）",
                           "%s｜%s" % (kept_detail, info.get("reason", ""))[:180])
            log("  ! 未回滚：现行稿保留，需人工判断（比值 %s）" % info.get("ratio"), fp)
            return False
        if did:
            record_problem(out_dir, pdf, book_name, "后处理丢失正文（已回滚，覆盖前已快照 %s）"
                           % os.path.basename(info.get("backup") or "-"), kept_detail)
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


# =============================================================================
# 批处理护栏 G1–G5（2026-11-27 加）
# =============================================================================
# 起因：交接文档 §4.13「重复批处理事故」——两份 pipeline.py batch 并发
# （books\转换日志.txt 里 03:27:52 与 03:28:40 两条「批处理开始：26 本」，都没有结束记录），
# 把 15 本已在库的书重跑了后处理，其中《外国近现代建筑史（第二版）》被改成 227 章 / 378,188 字，
# 事后靠离线包内嵌 __OFFLINE__.book 才恢复。当时「batch 只能单进程」只写在 §五 的禁止条目里，
# 没有任何代码拦截。事故路径：book.json 已存在 + .pipeline.json 的 done 非空 → 一路
# 「已完成，跳过」→ 照样 gate(write_profile=True)（重写 book.json）+ write_report（重写 转换报告.md）。
#
#   G1 单实例互斥锁  batch --apply 启动前抢锁（锁文件含 PID/启动时间/run-id/folder/out/workers/
#                    scale/心跳）；活锁 → 拒绝并打印持有者；PID 已死 → 报「陈旧锁」，把上一批
#                    标为未完成后再接管
#   G2 默认只预演    不带 --apply 时零写入，只按目标目录状态打印分类表：
#                    新增 / 重复·已入库 / 在途半成品 / 需人工判定
#   G3 已入库零写入  book.json 已存在且未获重转授权（--force 或批准清单）时，process_one 在任何
#                    写盘之前返回：不跑阶段、不写 预检.json / .pipeline.json / 转换报告.md / 质检.json
#   G4 运行编号与结束记录  run-id = 北京时间 YYYYMMDD_HHMM；日志开头「批处理计划（run-id）」+ 清单，
#                    finally 写「批处理结束」或「批处理中止（未完成清单：…）」，并 append
#                    batch_runs.jsonl；下次启动据此识别「上一批缺结束记录」
#   G5 内容指纹      book_fingerprint() = sha256(规范化 book.json，剔除 stats)，recheck 落进
#                    质检.json，供 check_stats.py 判「陈旧质检」时区分「只改统计字段」与「内容变了」
# =============================================================================
import datetime as _dt
import hashlib

BJ_TZ = _dt.timezone(_dt.timedelta(hours=8))          # 固定 UTC+8，不依赖 Windows 上常缺的 tzdata
GUARD_DIR = os.path.join(ROOT, "_工作记录", "批处理护栏")
LOCK_NAME = "batch.lock.json"
JOURNAL_NAME = "batch_runs.jsonl"
APPROVAL_NAME = "重转批准清单.json"
FENCE = chr(96) * 3
RUN_MARK_PLAN = "批处理计划"
RUN_MARK_START_LEGACY = "批处理开始"      # 旧日志用的词，扫描时同样算「开始」
RUN_MARK_END = "批处理结束"
RUN_MARK_ABORT = "批处理中止"
LOG_NAME = "转换日志.txt"

# 当前 gate() 恒 12 项；check_stats.py 用「项数是否等于这个表」区分新版 12 项与历史 8 项质检
GATE_CHECK_NAMES = ["每页字数", "图注配对", "章节数", "目录结构", "卷序连续", "章结构下限",
                    "可读性抽样", "句读密度", "句中截断", "水印文字", "缺图注", "结构诊断"]

CAT_NEW = "新增"
CAT_INGESTED = "重复·已入库"
CAT_PARTIAL = "在途半成品"
CAT_MANUAL = "需人工判定"
CAT_RAW_RISK = "高危·raw覆盖"
CAT_ORDER = (CAT_RAW_RISK, CAT_INGESTED, CAT_PARTIAL, CAT_MANUAL, CAT_NEW)

_ZLIB_JUNK = re.compile(r"[(（]?\s*(z-library\.sk|1lib\.sk|z-lib\.sk|annas[-\s]?archive|libgen)[^)）]*[)）]?", re.I)
_BOOKID = re.compile(r"[_-]\d{6,}")
_PAREN_BLOCK = re.compile(r"[（(][^（()）]*[)）]?")
_EDITION = re.compile(r"(第\s*[0-9零一二三四五六七八九十百]+\s*[版卷册]|修订版|全新增订版|最新校订版|数字版|上下册|[上下]版?)")
_ISBN = re.compile(r"97[89][\s-]?\d{1,5}[\s-]?\d{1,7}[\s-]?\d{1,7}[\s-]?[\dXx]")


def bj_now():
    """北京时间（护栏统一用它，日志与文档里的日期才有可比性）。"""
    return _dt.datetime.now(BJ_TZ)


def make_run_id(when=None):
    """G4 运行编号：北京时间 YYYYMMDD_HHMM（带时区的入参一律先换算到 +08:00）。"""
    t = when or bj_now()
    if t.tzinfo is not None:
        t = t.astimezone(BJ_TZ)
    return t.strftime("%Y%m%d_%H%M")


def norm_key(s):
    """书名/目录名比对键：抹掉空白、括号、点号、破折号一类排版噪声。"""
    return re.sub(r"[\s\u3000·….\-—_（）()【】\[\]，,、:：/\\|*?]+", "", str(s or "")).casefold()


def title_stem(name):
    """书名主干：去 z-library 尾巴、括号内作者/译者、书签 id、版次标记后的可比对主干。
    例：「建筑力学 (吕令毅，吕子华编著」与「建筑力学（第三版） (吕令毅、吕子华)」都归一成「建筑力学」。"""
    s = os.path.splitext(os.path.basename(str(name or "")))[0]
    s = _ZLIB_JUNK.sub(" ", s)
    s = _BOOKID.sub(" ", s)
    s = _PAREN_BLOCK.sub(" ", s)
    s = _EDITION.sub(" ", s)
    return re.sub(r"[\s\u3000·….、,，。:：/\\|_*?《》〈〉【】()（）\-\u2014~]+", "", s)


def _win_pid_alive(pid):
    """Windows 探活。绝不用 os.kill —— 它在 Windows 上走 TerminateProcess，会误杀持有者。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259                 # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None                                      # 判不了 → 上层保守当作活着


def pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        r = _win_pid_alive(pid)
        return True if r is None else r
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def default_lock_path(args=None):
    p = getattr(args, "lock", None) if args is not None else None
    return os.path.abspath(p) if p else os.path.join(GUARD_DIR, LOCK_NAME)


def default_journal_path(args=None, lock_path=None):
    p = getattr(args, "journal", None) if args is not None else None
    if p:
        return os.path.abspath(p)
    return os.path.join(os.path.dirname(lock_path or default_lock_path(args)), JOURNAL_NAME)


def default_approval_path():
    return os.path.join(GUARD_DIR, APPROVAL_NAME)


def read_batch_lock(path):
    """返回 (state, data)，state ∈ missing/ok/corrupt。只读。"""
    if not os.path.exists(path):
        return "missing", None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return "corrupt", {"pid": None, "error": str(e)[:120]}
    if not isinstance(d, dict):
        return "corrupt", {"pid": None, "error": "锁文件不是对象"}
    return "ok", d


def format_batch_lock(data, title="锁持有者"):
    if not data:
        return "(无锁信息)"
    return ("\n  %s：PID %s（%s）· run-id %s\n  %s 启动 · 最近心跳 %s · 计划 %s 本\n  inbox=%s\n  out=%s · workers=%s · scale=%s\n  命令行：%s"
            % (title, data.get("pid"), "活" if pid_alive(data.get("pid")) else "已死",
               data.get("runId"), data.get("startedAt"), data.get("hbAt") or "-",
               data.get("planned"), data.get("folder"), data.get("out"),
               data.get("workers"), data.get("scale"), str(data.get("cmdline") or "")[:160]))


def journal_append(path, rec):
    rec = dict(rec or {})
    rec.setdefault("at", bj_now().isoformat(timespec="seconds"))
    rec.setdefault("pid", os.getpid())
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def journal_events(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


def _journal_closed_runs(events):
    return set(e.get("runId") for e in events if e.get("event") in ("end", "abort", "unfinished"))


def journal_open_batches(path):
    """G4：运行记录里「有 start 无 end/abort」的批次（进程被杀时留下的）。只读。"""
    events = journal_events(path)
    closed = _journal_closed_runs(events)
    return [e for e in events if e.get("event") == "start" and e.get("runId") not in closed]


RUN_ID_RE = re.compile(r"run-id\s*[=＝:：]\s*([0-9A-Za-z_\-]{4,})")


def legacy_log_unfinished(log_path):
    """G4 收尾核对（只读）。判据是「有 批处理开始/计划 而无同 run-id 的 结束/中止」，
    不看时间差：
      - 带 run-id 的行按 run-id 分组配对 → unmatchedRunIds（本护栏时代的真遗留，可据此拦截）
      - G4 之前的旧行没有 run-id，只能按行序配对 → noRunIdUnmatched，仅历史提示，不作判据
    """
    res = {"starts": 0, "ends": 0, "runs": 0, "unmatched": [], "unmatchedRunIds": [],
           "noRunIdUnmatched": [], "detail": {}}
    if not os.path.exists(log_path):
        return res
    by_run, stack, n_start, n_end = {}, [], 0, 0
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            started = (RUN_MARK_PLAN in ln) or (RUN_MARK_START_LEGACY in ln)
            ended = (RUN_MARK_END in ln) or (RUN_MARK_ABORT in ln)
            if not (started or ended):
                continue
            if started:
                res["starts"] += 1
            else:
                res["ends"] += 1
            m = RUN_ID_RE.search(ln)
            rid = m.group(1) if m else ""
            if rid:
                rec = by_run.setdefault(rid, {"start": 0, "end": 0, "first": ln.strip()[:90]})
                rec["start" if started else "end"] += 1
            elif started:
                stack.append(ln.strip()[:90])
                n_start += 1
            elif stack:
                stack.pop()
                n_end += 1
    for rid, rec in sorted(by_run.items()):
        if rec["start"] and not rec["end"]:
            res["unmatchedRunIds"].append(rid)
            res["detail"][rid] = rec["first"]
    res["runs"] = len(by_run)
    res["noRunIdUnmatched"] = stack
    res["unmatched"] = ["run-id %s（%s）" % (r, res["detail"][r]) for r in res["unmatchedRunIds"]] + stack
    return res


def restore_raw_banner(book_dir):
    """post/one 用的只读提示行：把比值与 G6 判定打在 stdout 最前面。"""
    risky, ratio, detail = raw_overwrite_risk(book_dir)
    if ratio is None and risky is False and detail.startswith("无 "):
        return "[G6] %s ｜ 判定：不涉及覆盖" % detail
    verdict = "高危：拒绝 restore_raw（重跑会退回原始态）" if risky else "安全：比值在放行区间内"
    return "[G6] %s ｜ 判定：%s%s" % (detail, verdict,
                                      "｜需 --force --allow-restore-raw 才放行" if risky else "")


def _write_lock(path, meta):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def acquire_batch_lock(path, meta, alive=None, journal_path=None, log_fn=None, force=False):
    """G1 互斥锁。返回 (status, holder)：
       acquired  新建锁成功（holder=None）
       stale     陈旧锁（PID 已死）→ 已把上一批标记未完成并接管（holder=上一批）
       live      已有活锁 → 拒绝（holder=持有者）
       corrupt   锁文件读不出 → 拒绝（除非 force）
    """
    alive = alive or pid_alive
    log_fn = log_fn or (lambda m: None)
    st, data = read_batch_lock(path)
    if st == "missing":
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
            return "acquired", None
        except FileExistsError:
            st2, d2 = read_batch_lock(path)          # 竞争：另一进程抢先建了锁
            return ("live", d2) if st2 == "ok" else ("corrupt", d2)
    if st == "ok" and not alive((data or {}).get("pid")):
        prev_run = (data or {}).get("runId") or "(无 run-id)"
        if journal_path:
            events = journal_events(journal_path)
            started = any(e.get("event") == "start" and e.get("runId") == prev_run for e in events)
            if started and prev_run not in _journal_closed_runs(events):
                journal_append(journal_path, {
                    "event": "unfinished", "runId": prev_run,
                    "reason": "陈旧锁接管：持有者 PID %s 已死" % (data or {}).get("pid"),
                    "startedAt": (data or {}).get("startedAt"),
                    "planned": (data or {}).get("planned"),
                    "folder": (data or {}).get("folder"), "out": (data or {}).get("out"),
                    "takenOverBy": meta.get("runId")})
            else:
                journal_append(journal_path, {"event": "unfinished", "runId": prev_run,
                                              "reason": "陈旧锁接管（该批无 start 记录，仅锁文件残留）",
                                              "startedAt": (data or {}).get("startedAt"),
                                              "takenOverBy": meta.get("runId")})
        log_fn("  ! 发现陈旧锁（run-id %s · PID %s 已死 · 启动于 %s）→ 已标记该批「未完成」并接管"
               % (prev_run, (data or {}).get("pid"), (data or {}).get("startedAt")))
        _write_lock(path, meta)
        return "stale", data
    if st == "ok" and not force:
        return "live", data
    if st == "corrupt" and not force:
        return "corrupt", data
    _write_lock(path, meta)                          # force 覆盖
    return "acquired", data


def touch_batch_lock(path, run_id):
    """心跳：证明这把锁还属于一个正在推进的批次。run-id 不符就不动别人的锁。"""
    st, data = read_batch_lock(path)
    if st != "ok" or not isinstance(data, dict) or data.get("runId") != run_id:
        return False
    data["hbAt"] = bj_now().isoformat(timespec="seconds")
    _write_lock(path, data)
    return True


def release_batch_lock(path, run_id):
    """正常收尾删锁。锁文件是本护栏自建的运行期文件（不是书稿、不是生成物），
    且只删自己 run-id 那一把；进程被杀时不会走到这里，锁留下次被判陈旧。"""
    st, data = read_batch_lock(path)
    if st == "missing":
        return "absent"
    if st == "ok" and data.get("runId") != run_id:
        return "not-mine"
    try:
        os.remove(path)
        return "removed"
    except OSError:
        return "failed"


def normalize_book_for_fingerprint(book):
    """指纹输入：整本 book.json 剔除 stats（统计字段校正只动它）。"""
    if not isinstance(book, dict):
        return book
    return {k: v for k, v in book.items() if k != "stats"}


def book_fingerprint(book):
    """G5 内容指纹：sha256(规范化 book.json，剔除 stats)。只改 stats.chars 时指纹不变。"""
    payload = json.dumps(normalize_book_for_fingerprint(book), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_reconvert_approvals(path=None):
    """重转批准清单：接受 {"books":[{"dir":...,"reason":...}]} / JSON 数组 / 一行一个书名的文本。"""
    p = path or default_approval_path()
    if p and not os.path.isabs(p):
        p = os.path.join(ROOT, p)
    keys = set()
    if not os.path.exists(p):
        return keys
    with open(p, encoding="utf-8") as f:
        txt = f.read()
    try:
        data = json.loads(txt)
    except Exception:
        data = None
    if data is None:
        for ln in txt.splitlines():
            ln = ln.strip().lstrip("-*| ").strip()
            if ln and not ln.startswith("#"):
                keys.add(norm_key(ln))
        return keys
    items = data.get("books") if isinstance(data, dict) else data
    for it in (items or []):
        if isinstance(it, str):
            keys.add(norm_key(it))
        elif isinstance(it, dict):
            for k in ("dir", "name", "book", "bookTitle", "title"):
                if it.get(k):
                    keys.add(norm_key(it[k]))
    return keys


def approvals_for(args):
    a = getattr(args, "_reconvert_approvals", None)
    if a is None:
        a = load_reconvert_approvals(getattr(args, "approve_list", None) or default_approval_path())
        try:
            args._reconvert_approvals = a
        except Exception:
            pass
    return a


def reconvert_authorized(book_dir, args, approvals=None):
    """G3 重转授权：--force，或书名/目录名在批准清单里。"""
    if getattr(args, "force", False):
        return True
    keys = approvals if approvals is not None else approvals_for(args)
    if not keys:
        return False
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    for t in (os.path.basename(book_dir), book.get("bookTitle"), book.get("title")):
        if t and norm_key(t) in keys:
            return True
    return False


def raw_overwrite_risk(book_dir, lo=0.90, hi=1.15):
    """复刻 check_text_kept 的比值口径，判「重跑后处理会不会用 book_raw.json 覆盖现行稿」。

    _run_stages 只在 check_text_kept 不过时 restore_raw()，cmd_post 更是无条件先 restore_raw()。
    所以只要 现行正文/原始快照 的比值落在 [0.90, 1.15] 之外，重跑就等于把书稿退回 11-02 原始态。
    返回 (risky, ratio, detail)，纯读文件，不写。
    """
    raw = read_json(os.path.join(book_dir, RAW_NAME), None)
    if not raw:
        return False, None, "无 %s（重跑不会覆盖）" % RAW_NAME
    rc = sum(len(b.get("text") or "") for b in (raw.get("blocks") or []) if b.get("type") == "paragraph")
    if rc < 1000:
        return False, None, "%s 正文 <1000 字，check_text_kept 不适用" % RAW_NAME
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    cur = sum(len(b.get("text") or "") for b in (book.get("blocks") or []) if b.get("type") == "paragraph")
    ratio = cur / float(rc)
    risky = not (lo <= ratio <= hi)
    return risky, round(ratio, 4), "现行 %d / %s %d = %.4f（放行区间 %.2f–%.2f）" % (
        cur, RAW_NAME, rc, ratio, lo, hi)


def build_identity_index(books_dir):
    """四键索引（只读）：目标目录名 / book.json 的 source / 预检.json 的 pdf / 页数。
    给 batch 在 process_one 之前拦「异名同书」用。"""
    recs = []
    if not os.path.isdir(books_dir):
        return recs
    for d in sorted(os.listdir(books_dir)):
        bd = os.path.join(books_dir, d)
        if not os.path.isdir(bd):
            continue
        bp = os.path.join(bd, "book.json")
        pp = os.path.join(bd, "预检.json")
        src, pages, pf = "", 0, ""
        if os.path.isfile(bp):
            b = read_json(bp, {}) or {}
            src = str(b.get("source") or "")
            pages = int(((b.get("stats") or {}).get("pages", 0)) or 0)
        if os.path.isfile(pp):
            pf = str((read_json(pp, {}) or {}).get("pdf") or "")
        recs.append({"dir": d, "book": os.path.isfile(bp), "name": norm_key(d),
                     "nameStem": norm_key(title_stem(d)),
                     "srcBase": norm_key(os.path.basename(src)) if src else "",
                     "srcStem": norm_key(os.path.splitext(os.path.basename(src))[0]) if src else "",
                     "pdfBase": norm_key(os.path.basename(pf)) if pf else "",
                     "pdfFull": norm_key(pf.replace("\\", "/")) if pf else "",
                     "pages": pages})
    return recs


def identity_hits(pdf_path, pages, recs):
    """按四键找这个 PDF 对应哪个已有目录：命中 ≥2 键即认定同书（异名也能查出）。"""
    bn = os.path.basename(pdf_path)
    stem = os.path.splitext(bn)[0]
    k_base, k_stem = norm_key(bn), norm_key(stem)
    k_dir = norm_key(safe_name(stem))
    k_full = norm_key(os.path.abspath(pdf_path).replace("\\", "/"))
    k_tstem = norm_key(title_stem(bn))
    out = []
    for r in recs:
        keys = []
        if r["name"] in (k_dir, k_stem, k_base):
            keys.append("目录名")
        if r["srcBase"] and r["srcBase"] in (k_base, k_stem):
            keys.append("book.json.source")
        if r["pdfBase"] and r["pdfBase"] in (k_base, k_stem, k_full):
            keys.append("预检.json.pdf")
        if r["pages"] and pages and int(r["pages"]) == int(pages):
            keys.append("页数")
        if r["nameStem"] and k_tstem and r["nameStem"] == k_tstem:
            keys.append("书名主干")
        if keys:
            out.append({"dir": r["dir"], "book": r["book"], "keys": keys, "pages": r["pages"],
                        "strong": len([k for k in keys if k != "页数"]) >= 1 and (
                            "目录名" in keys or "book.json.source" in keys or "预检.json.pdf" in keys
                            or (len(keys) >= 2))})
    out.sort(key=lambda x: (-len(x["keys"]), not x["book"]))
    return out


def book_dir_state(book_dir):
    """目标目录状态（只读）：book.json / 预检.json / pages / .pipeline.json 进度。"""
    marker = read_json(os.path.join(book_dir, MARKER), {}) or {}
    done = marker.get("done", {}) or {}
    return {
        "exists": os.path.isdir(book_dir),
        "book": os.path.isfile(os.path.join(book_dir, "book.json")),
        "preflight": os.path.isfile(os.path.join(book_dir, "预检.json")),
        "pages": os.path.isdir(os.path.join(book_dir, "pages")),
        "assets": os.path.isdir(os.path.join(book_dir, "assets")),
        "marker": bool(marker),
        "doneStages": sorted(done.keys()),
        "markerPdf": marker.get("pdf"),
        "markerScale": marker.get("scale"),
    }


def pdf_brief(pdf, want_meta=True):
    """PDF 侧证据（只读）：页数、体积、元数据标题/作者、前 12 页文字层里的 ISBN。"""
    out = {"pages": 0, "bytes": 0, "isbn": [], "metaTitle": "", "metaAuthor": "", "error": ""}
    try:
        out["bytes"] = os.path.getsize(pdf)
    except OSError as e:
        out["error"] = str(e)[:80]
    if not want_meta:
        return out
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
        out["pages"] = doc.page_count
        md = doc.metadata or {}
        out["metaTitle"] = (md.get("title") or "").strip()
        out["metaAuthor"] = (md.get("author") or "").strip()
        txt = ""
        for i in range(min(doc.page_count, 12)):
            try:
                txt += doc.load_page(i).get_text("text") or ""
            except Exception:
                pass
        out["isbn"] = sorted({x.replace(" ", "").replace("-", "") for x in _ISBN.findall(txt)})
        doc.close()
    except Exception as e:
        out["error"] = (out["error"] + " " + str(e))[:120]
    return out


def list_inbox_pdfs(folder):
    seen, uniq = set(), []
    for p in sorted(glob.glob(os.path.join(folder, "*.pdf"))):
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def build_library_index(books_dir, limit=None):
    """在库索引（只读）：目录名主干、书名、源 PDF 名、页数。需要模糊比对时才建。"""
    idx = []
    if not os.path.isdir(books_dir):
        return idx
    for d in sorted(os.listdir(books_dir)):
        bd = os.path.join(books_dir, d)
        if not os.path.isfile(os.path.join(bd, "book.json")):
            continue
        rec = {"dir": d, "stem": title_stem(d), "source": "", "title": "", "pages": 0}
        try:
            b = read_json(os.path.join(bd, "book.json"), {}) or {}
            rec["source"] = str(b.get("source") or "")
            rec["title"] = str(b.get("bookTitle") or b.get("title") or "")
            rec["pages"] = (b.get("stats") or {}).get("pages", 0) or 0
        except Exception:
            pass
        idx.append(rec)
        if limit and len(idx) >= limit:
            break
    return idx


def match_library(pdf_path, index, pdf_pages=0):
    """找「疑似同一本书」的在库书稿：source 原名命中 / 主干相同或被包含 / 相近 + 页数佐证。"""
    base = os.path.basename(pdf_path)
    stem = title_stem(base)
    hits = []
    for rec in index:
        reasons, score = [], 0.0
        if rec["source"] and norm_key(rec["source"]) in (norm_key(base), norm_key(os.path.splitext(base)[0])):
            reasons.append("在库 book.json 的 source 就是这个 PDF 名")
            score = 1.0
        if stem and rec["stem"] and rec["stem"] == stem:
            reasons.append("书名主干相同（%s）" % stem)
            score = max(score, 0.92)
        if rec["title"] and stem and title_stem(rec["title"]) == stem:
            reasons.append("在库 bookTitle 主干相同（%s）" % rec["title"])
            score = max(score, 0.92)
        r = difflib.SequenceMatcher(None, stem, rec["stem"]).ratio() if stem and rec["stem"] else 0.0
        if 0.75 <= r < 1.0:
            reasons.append("目录名主干相似度 %.2f" % r)
            score = max(score, round(r * 0.9, 3))
        if stem and rec["stem"] and stem != rec["stem"]:
            a, b = (stem, rec["stem"]) if len(stem) <= len(rec["stem"]) else (rec["stem"], stem)
            if len(a) >= 4 and b.find(a) >= 0:
                reasons.append("书名主干包含：%s ⊂ %s" % (a, b))
                score = max(score, 0.86)
        if score >= 0.75:
            pdiff = None
            if pdf_pages and rec["pages"]:
                pdiff = int(pdf_pages) - int(rec["pages"])
                if pdiff == 0:
                    score = max(score, 0.9)
                    reasons.append("页数一致（%s 页）" % pdf_pages)
                elif abs(pdiff) <= max(3, int(rec["pages"]) * 0.03):
                    score = min(score + 0.02, 0.99)
            hits.append({"dir": rec["dir"], "score": round(score, 3), "source": rec["source"],
                         "pages": rec["pages"], "title": rec["title"], "pageDiff": pdiff,
                         "reasons": reasons})
    hits.sort(key=lambda h: -h["score"])
    return hits


def classify_inbox(folder, out_dir, want_pdf_meta=True, books_dir=None, args=None):
    """G2 预演分类：全程只读（不建目录、不写文件），按目标目录状态 + 在库比对给结论。"""
    books_dir = books_dir or out_dir
    pdfs = list_inbox_pdfs(folder)
    targets = {}
    for p in pdfs:
        targets.setdefault(safe_name(os.path.splitext(os.path.basename(p))[0]), []).append(p)
    index = build_library_index(books_dir)
    idindex = build_identity_index(books_dir)
    approvals = approvals_for(args) if args is not None else set()
    rows = []
    for p in pdfs:
        stem = os.path.splitext(os.path.basename(p))[0]
        tdir = os.path.join(out_dir, safe_name(stem))
        st = book_dir_state(tdir)
        meta = pdf_brief(p, want_pdf_meta)
        ev = []
        if meta.get("pages"):
            ev.append("PDF %s 页" % meta["pages"])
        if meta.get("isbn"):
            ev.append("ISBN %s" % "/".join(meta["isbn"][:2]))
        if meta.get("error"):
            ev.append("PDF 读取异常：%s" % meta["error"][:40])
        if len(targets.get(safe_name(stem), [])) > 1:
            ev.append("inbox 内 %d 个 PDF 目标目录同名（并发写同一目录）" % len(targets[safe_name(stem)]))
        dup = None
        idhits = identity_hits(p, meta.get("pages", 0), idindex)
        strong = [h for h in idhits if h.get("strong")]
        risky, rratio, rdetail = raw_overwrite_risk(tdir) if st["book"] else (False, None, "")
        if st["book"]:
            cat = CAT_INGESTED
            ev.append("目标目录已有 book.json（已完成阶段 %d 个）" % len(st["doneStages"]))
            src = norm_key(os.path.basename(p))
            hit = [h["dir"] for h in match_library(p, index, meta.get("pages", 0))
                   if norm_key(h.get("source") or "") == src]
            if hit:
                ev.append("在库 source 字段命中同一 PDF：%s" % hit[0])
        elif st["exists"] and (st["preflight"] or st["pages"] or st["assets"] or st["marker"]):
            cat = CAT_PARTIAL
            bits = [x for x, on in (("预检.json", st["preflight"]), ("pages/", st["pages"]),
                                    ("assets/", st["assets"]), (".pipeline.json", st["marker"])) if on]
            ev.append("目录存在但无 book.json，已有 %s" % ("+".join(bits) if bits else "空目录"))
            phits = match_library(p, index, meta.get("pages", 0))
            if phits:
                dup = phits[0]
                ev.append("疑似与在库同一本书：%s｜%s（同书不同文件名，须人工判定，别当新书跑）"
                          % (dup["dir"], "、".join(dup["reasons"])))
            if dup and meta.get("pages") and dup.get("pages"):
                ev.append("页数比对 PDF %s vs 在库 %s（差 %s）"
                          % (meta["pages"], dup["pages"], dup.get("pageDiff")))
        else:
            hits = match_library(p, index, meta.get("pages", 0))
            dup = hits[0] if hits else None
            if not st["exists"] and not hits:
                sh = [h for h in strong if h["dir"] != safe_name(stem)]
                if any(h["book"] for h in sh):
                    d2 = next(h for h in sh if h["book"])
                    cat = CAT_INGESTED
                    tdir = os.path.join(out_dir, d2["dir"])
                    risky, rratio, rdetail = raw_overwrite_risk(tdir)
                    st = book_dir_state(tdir)
                    ev.append("四键命中已在库目录（异名同书）：%s｜命中键 %s → 本批禁写"
                              % (d2["dir"], "+".join(d2["keys"])))
                elif sh:
                    d2 = sh[0]
                    cat = CAT_MANUAL
                    ev.append("四键命中在途目录（异名同书）：%s｜命中键 %s → 需人工判定"
                              % (d2["dir"], "+".join(d2["keys"])))
                else:
                    cat = CAT_NEW
                    ev.append("目标目录不存在，四键（目录名/source/预检pdf/页数）与书名主干均无在库命中")
            else:
                cat = CAT_MANUAL
                if hits:
                    h = hits[0]
                    ev.append("疑似同一本书：%s｜%s" % (h["dir"], "、".join(h["reasons"])))
                    if meta.get("pages") and h.get("pages"):
                        ev.append("页数比对 PDF %s vs 在库 %s%s"
                                  % (meta["pages"], h["pages"],
                                     "（一致）" if int(meta["pages"]) == int(h["pages"]) else "（不一致）"))
                    if len(hits) > 1:
                        ev.append("另有 %d 个候选（%s）"
                                  % (len(hits) - 1, "、".join(x["dir"][:20] for x in hits[1:3])))
                else:
                    ev.append("目标目录已存在但状态无法归类，需人工看")
        rows.append({"pdf": p, "name": stem, "target": tdir, "category": cat, "dup": dup, "evidence": ev,
                     "pages": meta.get("pages", 0), "isbn": meta.get("isbn", []), "state": st,
                     "rawRisk": risky, "rawRatio": rratio, "rawDetail": rdetail,
                     "idKeys": idhits[:3],
                     "authorized": bool(st["book"] and args is not None
                                         and reconvert_authorized(tdir, args, approvals))})
    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return {"folder": folder, "out": out_dir, "booksDir": books_dir, "rows": rows,
            "approvals": approvals,
            "rawRisk": [r["name"] for r in rows if r.get("rawRisk")],
            "noWrite": len([r for r in rows if r["category"] == CAT_INGESTED and not r.get("authorized")]),
            "dups": [r["name"] for r in rows if r.get("dup")],
            "counts": {k: counts.get(k, 0) for k in (CAT_NEW, CAT_INGESTED, CAT_PARTIAL, CAT_MANUAL)}}


def row_label(r):
    """表内标签：把「高危 raw 覆盖」「本批不得写入」「疑似重复」写死在分类名里，别被当成可跑的书。"""
    cat = r["category"]
    if cat == CAT_INGESTED:
        if r.get("rawRisk"):
            return CAT_RAW_RISK
        return CAT_INGESTED + ("·已授权" if r.get("authorized") else "·本批禁写")
    if cat == CAT_PARTIAL and r.get("dup"):
        return CAT_PARTIAL + "·疑重复"
    return cat


def render_classify_table(res, width=46):
    lines = ["%-24s  %-44s  %-6s  %s" % ("分类", "源文件", "PDF页", "依据 / 目标目录状态"),
             "%-24s  %-44s  %-6s  %s" % ("-" * 24, "-" * 44, "-" * 6, "-" * 34)]
    for cat in CAT_ORDER:
        for r in res["rows"]:
            if r["category"] != cat:
                continue
            nm = r["name"] if len(r["name"]) <= width else r["name"][:width - 1] + "…"
            lines.append("%-24s  %-44s  %-6s  %s"
                         % (row_label(r), nm, r["pages"] or "-", "；".join(r["evidence"][:3]) or "-"))
    risky = [r for r in res["rows"] if r.get("rawRisk")]
    if risky:
        lines.append("")
        lines.append("!! %s（%d 本）——重跑后处理会用 %s（原始态）覆盖现行稿：" % (CAT_RAW_RISK, len(risky), RAW_NAME))
        for r in risky:
            lines.append("   ✗ %-46s 比值 %s ｜ %s" % (r["name"][:44], r.get("rawRatio"), r.get("rawDetail")))
        lines.append("   判据复算：python tools/pipeline.py rawrisk \"%s\"  （或 --all 扫全库；口径同 check_text_kept：现行/raw 正文比 ∉ 0.90–1.15）"
                     % risky[0]["target"])
        lines.append("   处置：这些本在 --apply 模式下直接拒绝执行，除非同时给 --force --allow-restore-raw。")
    c = res["counts"]
    lines.append("合计 %d 个 PDF：新增 %d · 重复·已入库 %d · 在途半成品 %d · 需人工判定 %d"
                 % (len(res["rows"]), c[CAT_NEW], c[CAT_INGESTED], c[CAT_PARTIAL], c[CAT_MANUAL]))
    lines.append("其中：本批不得写入（G3）%d 本 · 高危·会被 restore_raw 覆盖 %d 本（%s）· 疑似同书不同名 %d 本（%s）· 真正可跑（新增）%d 本"
                 % (res.get("noWrite", 0), len(res.get("rawRisk") or []),
                    "、".join((res.get("rawRisk") or [])[:4]) or "-",
                    len(res.get("dups") or []), "、".join((res.get("dups") or [])[:4]) or "-", c[CAT_NEW]))
    return "\n".join(lines)


def dryrun_report_md(res, run_id, lock_info="", unfinished=""):
    c = res["counts"]
    out = ["# 批处理预演报告（run-id %s）" % run_id, "",
           "- 模式：**只预演，零写入**（未建目录，未写 预检.json / .pipeline.json / 转换报告.md / 质检.json，未跑任何阶段）",
           "- inbox：「%s」" % res["folder"],
           "- out：「%s」" % res["out"],
           "- 生成时间：%s（北京）· PID %s" % (bj_now().isoformat(timespec="seconds"), os.getpid()),
           "- 分类合计：新增 %d · 重复·已入库 %d · 在途半成品 %d · 需人工判定 %d"
           % (c[CAT_NEW], c[CAT_INGESTED], c[CAT_PARTIAL], c[CAT_MANUAL]),
           "- 本批不得写入（G3 已入库零写入）：%d 本；疑似同书不同名：%d 本（%s）"
           % (res.get("noWrite", 0), len(res.get("dups") or []), "、".join(res.get("dups") or []) or "-"), ""]
    if lock_info:
        out += ["## 并发锁现场", "", FENCE, lock_info, FENCE, ""]
    if unfinished:
        out += ["## 上一批遗留（缺结束记录）", "", FENCE, unfinished, FENCE, ""]
    out += ["## 逐件判定", "", FENCE, render_classify_table(res, width=64), FENCE, "",
            "## 怎么放行", "",
            "- 真跑：「python tools/pipeline.py batch 「%s」 --apply」（仍只跑「新增」，其余分类要显式授权）" % res["folder"],
            "- 重转已在库的书：把书名写进「%s」，或单本「one ... --force」" % default_approval_path(),
            "- 在途半成品：先人工确认断点，batch 默认不接手（要接手加 --takeover-partial）",
            "- 需人工判定：先确认是不是同一本书（页数/ISBN/书名主干依据见上表）", ""]
    return "\n".join(out)


def plan_from_classify(res, args):
    """apply 模式真正要跑的书：新增默认放行；其余分类必须显式授权，避免重演 12-01 事故。"""
    run, skip = [], []
    for r in res["rows"]:
        cat = r["category"]
        if r.get("rawRisk") and not (getattr(args, "force", False)
                                     and getattr(args, "allow_restore_raw", False)):
            skip.append((r, "G6 高危：重跑会用 %s（原始态）覆盖现行稿（比值 %s，%s）；"
                            "确要做须同时给 --force --allow-restore-raw"
                            % (RAW_NAME, r.get("rawRatio"), r.get("rawDetail"))))
            continue
        if cat == CAT_NEW:
            run.append(r)
        elif cat == CAT_INGESTED and reconvert_authorized(r["target"], args, res.get("approvals")):
            run.append(r)
        elif cat == CAT_PARTIAL and getattr(args, "takeover_partial", False):
            run.append(r)
        elif cat == CAT_MANUAL and getattr(args, "allow_manual_review", False):
            run.append(r)
        else:
            why = {CAT_INGESTED: "已在库·未获重转授权（G3 零写入）",
                   CAT_PARTIAL: "在途半成品（batch 接手需 --takeover-partial）",
                   CAT_MANUAL: "需人工判定（确认后加 --allow-manual-review 或走 one）",
                   CAT_NEW: ""}[cat]
            skip.append((r, why))
    return run, skip


def execute_batch(pdfs, args, fp, run_id, journal_path, log_fn=None, lock_path=None, process_fn=None):
    """真跑批处理：开头写「批处理计划（run-id）」+ 清单，收尾/中止写结束记录（含未完成清单）。"""
    log_fn = log_fn or log
    process_fn = process_fn or process_one
    pending = list(pdfs)
    results = []
    t0 = time.time()
    log_fn("%s（run-id=%s）：%d 本 · workers=%s · scale=%s · out=%s"
           % (RUN_MARK_PLAN, run_id, len(pdfs), args.workers, args.scale, args.out), fp)
    for p in pdfs:
        log_fn("  计划｜%s" % os.path.basename(p), fp)
    if journal_path:
        journal_append(journal_path, {"event": "start", "runId": run_id, "planned": len(pdfs),
                                      "pdfs": [os.path.basename(p) for p in pdfs],
                                      "folder": args.folder, "out": args.out,
                                      "workers": args.workers, "scale": args.scale})
    closed = False
    try:
        if args.workers <= 1:
            for p in pdfs:
                results.append((p, process_fn(p, args.out, args, fp)))
                pending[:] = [x for x in pending if x != p]
                if lock_path:
                    touch_batch_lock(lock_path, run_id)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(process_fn, p, args.out, args, fp): p for p in pdfs}
                for fut in concurrent.futures.as_completed(futs):
                    p = futs[fut]
                    results.append((p, fut.result()))
                    pending[:] = [x for x in pending if x != p]
                    if lock_path:
                        touch_batch_lock(lock_path, run_id)
        okn = sum(1 for _, ok in results if ok)
        log_fn("%s（run-id=%s）：%d/%d 通过 · 总耗时 %.1f 分钟"
               % (RUN_MARK_END, run_id, okn, len(results), (time.time() - t0) / 60), fp)
        log_fn("结果清单：", fp)
        for p, ok in results:
            log_fn("  %s %s" % ("✅" if ok else "⚠️", os.path.basename(p)), fp)
        if journal_path:
            journal_append(journal_path, {"event": "end", "runId": run_id, "passed": okn,
                                          "total": len(results),
                                          "seconds": round(time.time() - t0, 1)})
        closed = True
    except BaseException as e:
        log_fn("%s（run-id=%s）：%r" % (RUN_MARK_ABORT, run_id, e), fp)
        log_fn("  未完成清单（%d 本）：" % len(pending), fp)
        for p in pending:
            log_fn("    ✗ %s" % os.path.basename(p), fp)
        if journal_path:
            journal_append(journal_path, {"event": "abort", "runId": run_id, "error": repr(e)[:300],
                                          "pending": [os.path.basename(p) for p in pending],
                                          "done": [os.path.basename(p) for p, _ in results]})
        closed = True
        raise
    finally:
        if not closed and journal_path:
            journal_append(journal_path, {"event": "abort", "runId": run_id,
                                          "error": "finally：批次未正常收尾",
                                          "pending": [os.path.basename(p) for p in pending]})
    return results


def cmd_rawrisk(args):
    """只读：按 check_text_kept 口径列出「重跑会被 book_raw.json 覆盖」的书稿。"""
    targets = []
    if getattr(args, "all", False):
        targets = [os.path.join(BOOKS, d) for d in sorted(os.listdir(BOOKS))
                   if os.path.isfile(os.path.join(BOOKS, d, "book.json"))]
    else:
        if not args.bookdir:
            print("用法：python tools/pipeline.py rawrisk <书名或目录>  或  --all")
            return 2
        targets = [find_book_dir(args.bookdir)]
    risky = []
    for bd in targets:
        ok, ratio, detail = raw_overwrite_risk(bd)
        mark = "!! 高危" if ok else "  安全"
        print("%s｜%-46s｜%s" % (mark, os.path.basename(bd)[:44], detail))
        if ok:
            risky.append(os.path.basename(bd))
    print("共 %d 本 · 高危 %d 本（判据：现行/book_raw 正文比 ∉ %.2f–%.2f）"
          % (len(targets), len(risky), RESTORE_RATIO_LO, RESTORE_RATIO_HI))
    return 0


def cmd_one(args):
    return process_one(args.pdf, args.out, args, None)


def cmd_batch(args):
    """batch 护栏入口：默认只预演（G2，零写入），--apply 才真跑；真跑前先抢单实例锁（G1），
    全程带 run-id 与结束/中止记录（G4），已在库的书稿未获授权一律零写入（G3）。"""
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
    if not os.path.isdir(folder):
        print("inbox 不存在：%s" % folder)
        return 2
    run_id = getattr(args, "run_id", None) or make_run_id()
    args.run_id = run_id
    lock_path = default_lock_path(args)
    journal_path = default_journal_path(args, lock_path)
    args.approve_list = getattr(args, "approve_list", None) or default_approval_path()
    args._reconvert_approvals = load_reconvert_approvals(args.approve_list)
    log_path = os.path.join(args.out, LOG_NAME)
    pdfs = list_inbox_pdfs(folder)

    print("批处理 run-id=%s（北京时间）· 模式=%s"
          % (run_id, "执行（--apply）" if args.apply else "预演（默认，零写入）"))
    print("  inbox：%s" % folder)
    print("  out  ：%s" % args.out)
    print("  锁  ：%s" % lock_path)
    print("  运行记录：%s" % journal_path)
    print("  重转批准清单：%s（%d 条）" % (args.approve_list, len(args._reconvert_approvals)))

    # G4：先报「上一批缺结束记录」，再决定要不要动手（只读探测）
    msgs = []
    for e in journal_open_batches(journal_path):
        msgs.append("运行记录：run-id %s（PID %s · %s 本 · %s）有 start 无 end/abort → 疑似中止/被杀"
                    % (e.get("runId"), e.get("pid"), e.get("planned"), e.get("at")))
    legacy = legacy_log_unfinished(log_path)
    for rid in legacy["unmatchedRunIds"]:
        if rid in [e.get("runId") for e in journal_open_batches(journal_path)]:
            continue
        msgs.append("日志按 run-id 配对：run-id %s 有「批处理计划/开始」无「结束/中止」→ 上一批缺结束记录（%s）"
                    % (rid, legacy["detail"].get(rid, "")))
    if legacy["noRunIdUnmatched"]:
        msgs.append("历史提示（非判据）：G4 之前的旧日志有 %d 处「批处理开始」没有配对的结束记录，"
                    "末条：%s；这些行没有 run-id，只作背景，不参与拦截"
                    % (len(legacy["noRunIdUnmatched"]), legacy["noRunIdUnmatched"][-1]))
    if msgs:
        print("\n! 上一批遗留（缺结束记录）：")
        for m in msgs:
            print("  - " + m)

    if not pdfs:
        print("没有找到 PDF：", folder)
        return 0

    if not args.apply:                                  # G2 预演：一个字都不写
        st, holder = read_batch_lock(lock_path)
        lock_info = ""
        if st == "ok" and pid_alive((holder or {}).get("pid")):
            lock_info = "活跃锁（预演只读，不受影响）：" + format_batch_lock(holder, "持有者")
        elif st == "ok":
            lock_info = "陈旧锁（PID 已死，--apply 时会标记未完成并接管）：" + format_batch_lock(holder, "持有者")
        elif st == "corrupt":
            lock_info = "锁文件损坏：" + json.dumps(holder, ensure_ascii=False)
        if lock_info:
            print("\n! " + lock_info)
        res = classify_inbox(folder, args.out, want_pdf_meta=not args.no_pdf_meta,
                             books_dir=args.out, args=args)
        print("\n" + render_classify_table(res))
        if args.report:
            rp = args.report if os.path.isabs(args.report) else os.path.join(ROOT, args.report)
            if not os.path.abspath(rp).startswith(GUARD_DIR):
                print("\n! 预演报告只允许写到 %s 下（预演不得碰 books/），已忽略 --report" % GUARD_DIR)
            else:
                os.makedirs(os.path.dirname(rp), exist_ok=True)
                with open(rp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(dryrun_report_md(res, run_id, lock_info, "\n".join(msgs)))
                print("\n预演报告：%s" % rp)
        else:
            print("\n（本次未写预演报告；要落盘加 --report <_工作记录 下的路径>）")
        print("预演模式零写入：没建目录、没写 预检.json/.pipeline.json/转换报告.md/质检.json、没跑任何阶段。")
        print('确认可跑后真跑：python tools/pipeline.py batch "%s" --apply' % folder)
        return 0

    # G1：执行模式必须先拿到锁
    now = bj_now().isoformat(timespec="seconds")
    meta = {"pid": os.getpid(), "runId": run_id, "startedAt": now, "hbAt": now,
            "folder": folder, "out": args.out, "workers": args.workers, "scale": args.scale,
            "planned": len(pdfs), "cmdline": " ".join(sys.argv[:5])}
    status, holder = acquire_batch_lock(lock_path, meta, journal_path=journal_path,
                                        log_fn=lambda m: print(m), force=args.force_lock)
    if status == "live":
        print("\n! 拒绝启动（G1）：已有一个批处理在运行" + format_batch_lock(holder))
        print("  12-01 事故就是两份 batch 并发。真要并发请加 --force-lock（不推荐）。")
        return 3
    if status == "corrupt":
        print("\n! 拒绝启动（G1）：锁文件读不出（%s）%s"
              % (json.dumps(holder, ensure_ascii=False), format_batch_lock(holder)))
        print("  确认没有并发批处理后加 --force-lock 覆盖。")
        return 3
    print("\n已取批处理锁：run-id=%s · PID=%s%s"
          % (run_id, os.getpid(), "（接管陈旧锁）" if status == "stale" else ""))

    try:
        os.makedirs(args.out, exist_ok=True)
        res = classify_inbox(folder, args.out, want_pdf_meta=not args.no_pdf_meta,
                             books_dir=args.out, args=args)
        run, skip = plan_from_classify(res, args)
        with open(log_path, "a", encoding="utf-8") as fp:
            log("批处理预演分类（run-id=%s）：新增 %d · 重复·已入库 %d · 在途半成品 %d · 需人工判定 %d"
                % (run_id, res["counts"][CAT_NEW], res["counts"][CAT_INGESTED],
                   res["counts"][CAT_PARTIAL], res["counts"][CAT_MANUAL]), fp)
            for r, why in skip:
                log("  跳过｜%s｜%s" % (r["name"], why), fp)
            if not run:
                log("没有可执行的书（新增 0 本，其余按护栏跳过）。确要重转已在库的书："
                    "--force 或写进批准清单；在途半成品要 batch 接手加 --takeover-partial。", fp)
                log("%s（run-id=%s）：0/0 通过 · 总耗时 0.0 分钟" % (RUN_MARK_END, run_id), fp)
                print("\n本轮没有书要跑（详见日志跳过清单）。")
                return 0
            results = execute_batch([r["pdf"] for r in run], args, fp, run_id, journal_path,
                                    log_fn=log, lock_path=lock_path)
        okn = sum(1 for _, ok in results if ok)
        print("批处理完成（run-id=%s）：%d/%d 通过" % (run_id, okn, len(results)))
        return 0
    finally:
        print("释放锁：%s" % release_batch_lock(lock_path, run_id))


def cmd_post(args):
    book_dir = args.bookdir
    _bk = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    if ((_bk.get("layout") or {}).get("direction") == "vertical"):
        print("  - 竖排书稿：convert 阶段已内建成段/卷章/页级闸，横排后处理阶段不适用，跳过。")
        print("    复查请用：python tools/pipeline.py recheck \"%s\"" % book_dir)
        return 0
    fp = None
    ok = True
    print("  " + restore_raw_banner(book_dir))
    # G6：原来是「无条件 restore_raw」，等于每次 post 都用 book_raw.json 盖掉现行稿。
    did, info = restore_raw(book_dir, None, allow=getattr(args, "allow_restore_raw", False),
                            log_fn=lambda m, _f=None: print(m))
    if did:
        print("  - 已从原始快照 book_raw.json 重建（覆盖前已快照：%s · 比值 %s）"
              % (info.get("backup") or "-", info.get("ratio")))
    elif info.get("action") == "blocked":
        record_problem(os.path.dirname(os.path.abspath(book_dir)), "(post)",
                       os.path.basename(book_dir), "拒绝 restore_raw（G6 硬闸，现行稿保留）",
                       info.get("reason", ""))
        print("  ! 拒绝用 book_raw.json 覆盖现行稿：%s" % info.get("reason"))
        print("    现行稿保留不动；确要回退请加 --allow-restore-raw（仍会先自动快照）")
        return 4
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
    one.add_argument("--vmodel", default="", help="竖排 rec 模型 onnx（缺省自动找：根 models/ → tools/models/，可用 VERTICAL_OCR_MODEL 覆盖）")
    one.add_argument("--approve-list", default="", metavar="PATH",
                     help="重转批准清单；已在库的书稿不在清单里也没给 --force 时按 G3 零写入跳过")
    one.add_argument("--allow-restore-raw", action="store_true",
                     help="G6：允许 restore_raw 用 book_raw.json 覆盖现行稿（默认拒绝；仍会先自动快照）")
    one.set_defaults(func=cmd_one)

    batch = sub.add_parser("batch", help="转换整个文件夹（默认只预演，--apply 才真跑）")
    batch.add_argument("folder", nargs="?", default=None)
    batch.add_argument("--out", default=BOOKS)
    batch.add_argument("--scale", type=float, default=3.0)
    batch.add_argument("--workers", type=int, default=1)
    batch.add_argument("--force", action="store_true",
                       help="解除 G3：允许重转已在库的书稿（会重写 book.json/报告，慎用）")
    batch.add_argument("--from-stage", default="", choices=[""] + ALL_STAGES)
    batch.add_argument("--vertical", action="store_true", help="竖排右起扫描件（同 one --vertical）")
    batch.add_argument("--ear", default="", help="竖排书耳/版心书名，逗号分隔")
    batch.add_argument("--vmodel", default="", help="竖排 rec 模型 onnx（缺省自动找根 models/ 与 tools/models/）")
    # --- 批处理护栏（G1/G2/G3/G4）---
    batch.add_argument("--apply", action="store_true",
                       help="真正执行；不加此旗标只做预演（零写入）")
    batch.add_argument("--report", default="", metavar="PATH",
                       help="预演报告落盘路径（只允许写在 _工作记录/批处理护栏 下）")
    batch.add_argument("--lock", default="", metavar="PATH", help="互斥锁文件位置（默认 _工作记录/批处理护栏/batch.lock.json）")
    batch.add_argument("--journal", default="", metavar="PATH", help="运行记录 jsonl 位置（默认与锁同目录）")
    batch.add_argument("--approve-list", default="", metavar="PATH",
                       help="重转批准清单（JSON 或每行一个书名），默认读 _工作记录/批处理护栏/重转批准清单.json")
    batch.add_argument("--run-id", default="", help="指定运行编号（默认北京时间 YYYYMMDD_HHMM）")
    batch.add_argument("--force-lock", action="store_true",
                       help="忽略/覆盖活锁强行启动（12-01 事故做法，仅限人工确认无并发后使用）")
    batch.add_argument("--takeover-partial", action="store_true",
                       help="允许 batch 接手「在途半成品」目录")
    batch.add_argument("--allow-manual-review", action="store_true",
                       help="允许 batch 跑「需人工判定」分类的书（默认不跑）")
    batch.add_argument("--allow-restore-raw", action="store_true",
                       help="G6：允许 restore_raw 用 book_raw.json 覆盖现行稿（默认拒绝；仍会先自动快照）")
    batch.add_argument("--no-pdf-meta", action="store_true",
                       help="预演不读 PDF（不取页数/ISBN，快但依据少）")
    batch.set_defaults(func=cmd_batch)

    post = sub.add_parser("post", help="只重跑后处理（不重新 OCR）")
    post.add_argument("bookdir")
    post.add_argument("--allow-restore-raw", action="store_true",
                      help="G6：允许先用 book_raw.json 覆盖现行稿（默认比值异常即拒绝，现行稿保留）")
    post.set_defaults(func=cmd_post)

    rr = sub.add_parser("rawrisk", help="只读：列出现行稿/raw 正文比超区间（重跑会被覆盖）的书稿")
    rr.add_argument("bookdir", nargs="?", default="")
    rr.add_argument("--all", action="store_true", help="扫全库")
    rr.set_defaults(func=cmd_rawrisk)

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
    rc.add_argument("--all", action="store_true", help="复查全库并写 交接文档/质检-复查汇总.md（会重写每本质检件，含 8 项历史件）")
    rc.add_argument("--summary-only", action="store_true", help="只读各书现有 质检.json 刷新汇总，不重跑 gate、不动任何书稿")
    rc.set_defaults(func=cmd_recheck)

    args = ap.parse_args()
    r = args.func(args)
    # bool 语义（one/post 等返回 True/False）→ 0/1；int 语义（batch 的 0/2）原样透传
    if isinstance(r, bool):
        return 0 if r else 1
    return r or 0


if __name__ == "__main__":
    sys.exit(main())
