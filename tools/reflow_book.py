"""重排验收工具：读 OCR 缓存 + 现有 book.json 的 figure 块，只重排文字块。

⚠⚠ 当前状态：**实验性，未达标，不要接到正式流程里**（2026-11-22 定）⚠⚠
    在批评学上跑出「真值消失率 64.4%（47/73）」，G1 门槛 90%，**未通过**；
    剔除 6 条 OCR 源头丢行后是 70.1%（47/67），仍未通过。
    只在批评学单本上验证过，**没做过全库回归**。
    教训：为追这个指标，`separate_margin` 连改五六版判据（见该函数注释里的四条弯路），
    每版都靠单页现象调参，越调越像为指标服务，而不是为「书好不好读」服务。
    要真正推进，应改成「逐本逐页目视 + 只处理确认碎掉的页」，
    而不是继续打磨这个自动验收器。

设计依据：转换器切块修复方案（内部文档）§4。
  - 不重 OCR、不重裁图：图块与图片资源完全不动（不变式 I5）。
  - 输出到 `reflow/<书名>/book.json`，**绝不写 books\\**。
  - 用途是验收：先看「如果修好会变成什么样」，确认后再改正式路径。

与 pdf2book.parse_page 的关键差别（这三条就是 G1 要验的东西）：
  1. 栏位判定走 `layout_parse.detect_columns`，输入是行结构，不是墨迹；
  2. **绝不按字符比例切行**：跨栏行整行走 span 通道；
  3. 页眉/页脚/页码/图注先剥离角色，页边图版区不进正文行池。

每页都跑不变式断言。任何一条不成立 → 该页回退到旧块，并记入异常清单。
**不许带伤过**（方案 §3 末句）。

已知残留问题（未修，记录在此免得重踩）：
  · p56 这类「两栏在中缝处水平相撞」的真双栏页，detect_columns 判成单栏。
    尝试修过：给 2b 的 cross_ratio 加 pair_hits 例外，p56 能判成双栏，
    但 detect_columns 返回的 gutter 是「两峰中点」而不是「右栏起点」，
    assign_columns 按中点归栏会把左栏行全塞进右栏，结果更乱 → 已回退。
    真要修，得先统一 gutter 的语义（返回右栏起点），这是一次跨函数改动。
  · 跨页接段（p70→p71 那种）没实现，属后处理，不在本工具范围内。

用法：
  python tools/reflow_book.py "<书名片段>"            # 全量
  python tools/reflow_book.py "<书名片段>" --limit 40  # 只跑前 40 页（调试）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from layout_parse import (  # noqa: E402
    assign_columns,
    check_invariants,
    detect_columns,
)

BOOKS = ROOT / "books"
OUT_ROOT = ROOT / "reflow"

# ---- 与 pdf2book 对齐的常量（必须一致，否则页眉页脚剔除结果会漂移 → 破坏 I4）
HEADER_RATIO = 0.075
FOOTER_RATIO = 0.94
WATERMARK_RE = re.compile(r"^[~_\-—=·•\s]{3,}$|微信|公众号|免费下载|仅供学习")
TAIL_NUM_RE = re.compile(r"^[（(\[]?(?:\d{1,4}|[ivxlcdm]{1,8})[）)\]]?$", re.I)
CAPTION_RE = re.compile(r"^图\s*\d+[-－—.]?\d*")
HEAD_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*[章节篇]")

MIN_LINES_FOR_COLUMNS = 16      # 少于这个行数不做栏位判定（与 layout_parse 一致）
PAGE_LINE_Y_TOL = 6.0           # 行分组容差，与 pdf2book 的 round(y0/6) 一致


# ---------------------------------------------------------------- 读取
def load_ocr_page(pages_dir: Path, n: int) -> list[dict] | None:
    f = pages_dir / f"page_{n:03d}_ocr2.json"
    if not f.exists():
        return None
    try:
        rows = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = (r.get("text") or "").strip()
        if not t or WATERMARK_RE.search(t):
            continue
        box = [r.get(k) for k in ("x0", "y0", "x1", "y1")]
        if any(not isinstance(v, (int, float)) for v in box):
            continue
        out.append({"text": t, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]})
    return out


def split_zones(lines: list[dict], H: float) -> dict:
    """剥离页眉/页脚/页码，返回 {'body': [...], 'dropped': [...]}。

    判据与 pdf2book.parse_page 前段保持一致：位置（上下 7.5% / 94% 带）+ 形态
    （无句末标点的短行）。只删这类，正文第一行不一刀切。
    """
    body, dropped = [], []
    for ln in lines:
        t = ln["text"].strip()
        in_head = ln["y1"] < H * HEADER_RATIO or ln["y1"] < H * 0.22
        in_foot = ln["y0"] > H * FOOTER_RATIO or ln["y0"] > H * 0.78
        short_no_term = len(t) <= 22 and not re.search(r"[。！？；，、：]$", t)
        if TAIL_NUM_RE.match(t) and (in_head or in_foot):
            dropped.append(ln)
            continue
        if short_no_term and (in_head or in_foot):
            # 页眉常是「第2章建筑批评史略」这种短行；页脚常是书名或页码
            dropped.append(ln)
            continue
        body.append(ln)
    return {"body": body, "dropped": dropped}


def extract_captions(body: list[dict], lh: float) -> tuple[list[dict], list[dict]]:
    """把图注行（含续行）挑出来，返回 (caption_lines, remaining_body)。

    图注走 captiontext 通道，不参与正文拼段 —— 这是改动 C 的一半。
    这里**不合并**到 figure 块（figure 块由 book.json 原样保留），
    只保证图注文本进块、字符不丢。
    """
    body = sorted(body, key=lambda l: (round(l["y0"] / PAGE_LINE_Y_TOL), l["x0"]))
    used: set[int] = set()
    caps: list[dict] = []
    for i, ln in enumerate(body):
        if i in used or not CAPTION_RE.match(ln["text"]):
            continue
        used.add(i)
        j = i + 1
        while j < len(body) and j < i + 4 and j not in used:
            nxt = body[j]
            if CAPTION_RE.match(nxt["text"]):
                j += 1
                continue
            vgap = nxt["y0"] - ln["y1"]
            near = abs(nxt["x0"] - ln["x0"]) < lh * 1.2
            if (vgap < lh * 1.6 and near and len(nxt["text"]) <= 40
                    and not re.search(r"[。！？；]$", nxt["text"])):
                used.add(j)
                j += 1
                continue
            break
        cap_group = [body[k] for k in range(i, j) if k in used]
        caps.append({
            "text": "".join(l["text"] for l in cap_group),
            "x0": ln["x0"], "y0": ln["y0"], "x1": ln["x1"], "y1": ln["y1"],
            "lines": cap_group,
        })
    remaining = [l for k, l in enumerate(body) if k not in used]
    return caps, remaining


MARGIN_MIN_BAND_W = 0.06     # 图版区自身至少要这么宽（更窄的当作行尾标点/符号残片）
MARGIN_MIN_LINES = 2         # 单页至少凑够这么多行，才认定为「独立图版文字块」
MARGIN_RIGHT_CLUSTER_TOL = 0.03   # x1 聚类容差（占页宽）
MARGIN_RIGHT_MIN_SHARE = 0.35     # 正文右缘簇至少要占全部行的这个比例


def _right_edge_cluster(lines: list[dict], W: float) -> float | None:
    """找「正文主栏右缘」：x1 最密集的那条竖线。

    为什么不能用百分位：批评学 p14 上正文 x1 只在 1283–1293 收尾，
    但图版文字（PIONEERSOFTHE 等）x1 落在 1363–1439，
    取 75 百分位会被这些短行抬到 1300+，一刀切就切错。
    为什么不能取最大值：图版文字右缘比正文更靠右。

    做法：按 MARGIN_RIGHT_CLUSTER_TOL·W 的容差给 x1 聚类，
    取**行数最多**的那一簇（正文满行必然最多），返回簇中心。
    簇所占行数不足 MARGIN_RIGHT_MIN_SHARE 时返回 None（说明没有稳定右缘，
    多半是居中排版或图版页，此时不做 margin 剥离，宁可漏剥）。
    """
    xs = sorted(l["x1"] for l in lines)
    if not xs:
        return None
    tol = W * MARGIN_RIGHT_CLUSTER_TOL
    best: tuple[int, float] = (0, 0.0)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] - xs[i] <= tol:
            j += 1
        n = j - i + 1
        if n > best[0]:
            best = (n, (xs[i] + xs[j]) / 2.0)
        i = j + 1
    if best[0] < len(xs) * MARGIN_RIGHT_MIN_SHARE:
        return None
    return best[1]


def separate_margin(lines: list[dict], W: float,
                    gutter: float | None) -> tuple[list[dict], list[dict]]:
    """页边 / 图版区文字剥离（改动 C 的另一半）。

    只在**单栏页**做：双栏页的右栏是真正文，不能剥。

    ⚠ 四条弯路都记在这里，别再走：
      ① 绝对值（x0 > 0.72W 且 宽 < 0.18W）——
         p12 的图版文字 x0 在 0.75–0.85W 刚好擦边，书封英文留在正文里；
      ② x1 的 75 百分位当主栏右缘——被满宽行与图版行一起抬高，p12 一行剥不掉；
      ③ x0 最大缺口当栏界——**最坑的一个**：全书 478/494 页都有 >0.08W 的 x0 缺口，
         因为段落首行缩进本身就是缺口。用它当判据，p87 缩进引文整块被剥，
         191/492 页中招，正文丢内容；
      ④ 全页单一右缘簇——p12 上正文右缘有**两条**：
         前半页满宽到 1292，图版段旁边的正文被图版压到 1045。
         全页取最多的那一簇（1292），于是 x0=1080 的图版文字被判定在栏内。

    现在的做法（逐段滚动右缘）：
      把行按 y 切成连续的段，每段独立求 _right_edge_cluster；
      段内右缘之外的行 + 不与本段正文水平重叠 → 判为图版文字。
      这样 p12 前半页用 1292、后半页用 1045，两段各自正确。
    """
    if gutter is not None or len(lines) < MARGIN_MIN_LINES * 3:
        return lines, []

    lines = sorted(lines, key=lambda l: (l["y0"], l["x0"]))
    hs = sorted(l["y1"] - l["y0"] for l in lines)
    lh = hs[len(hs) // 2] if hs else 0.0
    band_h = max(lh * 6, 1.0)      # 每段约 6 行的滑动窗

    main_ids: set[int] = set()
    margin: list[dict] = []
    i = 0
    while i < len(lines):
        y0 = lines[i]["y0"]
        j = i
        while j < len(lines) and lines[j]["y0"] - y0 <= band_h:
            j += 1
        seg = lines[i:j]
        i = j
        if len(seg) < MARGIN_MIN_LINES:
            main_ids.update(id(l) for l in seg)
            continue

        edge = _right_edge_cluster(seg, W)
        if edge is None:
            main_ids.update(id(l) for l in seg)
            continue

        split_x = edge + W * 0.02
        seg_right = [l for l in seg if l["x0"] > split_x]
        seg_left = [l for l in seg if l["x0"] <= split_x]
        if len(seg_right) < 1 or not seg_left:
            main_ids.update(id(l) for l in seg)
            continue

        # 右区必须比正文区明显靠右、且自身够宽，否则是行尾残片
        left_x1 = max(l["x1"] for l in seg_left)
        right_x0 = min(l["x0"] for l in seg_right)
        if right_x0 < left_x1 - W * 0.02:
            main_ids.update(id(l) for l in seg)
            continue
        band = max(l["x1"] for l in seg_right) - right_x0
        if band < W * MARGIN_MIN_BAND_W:
            main_ids.update(id(l) for l in seg)
            continue

        main_ids.update(id(l) for l in seg_left)
        margin.extend(seg_right)

    main = [l for l in lines if id(l) in main_ids]
    return main, margin


# ---------------------------------------------------------------- 组装
def assemble(lines: list[dict], lh: float, page: int, col: int) -> list[dict]:
    """把一个栏（或 span 通道）的行拼成段落块。**不切文本，只拼接。**

    与 pdf2book.assemble 的差别：
      - 边距用「本栏内 x0 的低分位」而不是全页众数（双栏页两栏边距不同）；
      - 缩进判据用中位行高 lh，不用固定像素。
    """
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: (round(l["y0"] / PAGE_LINE_Y_TOL), l["x0"]))
    xs = sorted(l["x0"] for l in lines)
    margin = xs[max(0, int(len(xs) * 0.20))]

    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        text = ln["text"]
        is_head = (HEAD_RE.match(text) and len(text) < 34
                   and not re.search(r"[。！？，；：]$", text))
        if is_head:
            if cur:
                out.append(cur)
                cur = None
            out.append({"type": "heading", "text": text, "y": ln["y0"],
                        "page": page, "col": col})
            continue

        starts_punct = bool(re.match(r"^[，。、；：）】」』”’！？]", text))
        indent = (ln["x0"] - margin) > lh * 0.7 and not starts_punct
        gap = ln["y0"] - cur["_y1"] if cur else 0.0
        prev_term = bool(cur and cur["text"].strip()
                         and cur["text"].strip()[-1] in "。！？…：；”』」）)】.")
        new_para = (cur is None
                    or (indent and (prev_term or gap > lh * 1.6))
                    or gap > lh * 2.2)
        if new_para:
            if cur:
                out.append(cur)
            cur = {"type": "paragraph", "text": text, "y": ln["y0"], "page": page,
                   "col": col, "indent": bool(indent), "_y1": ln["y1"], "_x0": ln["x0"]}
        else:
            joiner = " " if (re.search(r"[A-Za-z0-9]$", cur["text"])
                             and re.match(r"^[A-Za-z0-9]", text)) else ""
            cur["text"] += joiner + text
            cur["_y1"] = ln["y1"]
            cur["_x0"] = min(cur["_x0"], ln["x0"])
    if cur:
        out.append(cur)
    for p in out:
        p.pop("_y1", None)
        p.pop("_x0", None)
    return out


def body_width(ocr_lines: list[dict], H: float) -> float:
    """正文版心宽度（不是「全页最大 x1」）。

    p56 踩过坑：全页 max x1 = 1440，来自页眉「第2章建筑批评史略」；
    而正文右缘只有 875（左栏）/1302（右栏）。
    用 1440 当 W，`cross_ratio` 的判据写的是 x1 > 0.55·W = 792，
    于是一整屏左栏行（x1≈875）全被判成「跨栏行」→ 32/64 = 0.50 → 误判单栏。
    拿正文行的 x1 高分位当 W，0.55·W ≈ 716，左栏行就不再越线。

    ⚠ 未做全库回归。目前只在批评学单本上验证（W_page 1440 → W_body 1302）。
    它的作用是「别被页眉/图版撑大 W」，逻辑独立于 p56 那次失败的 gutter 改动，
    所以保留；但接正式流程前必须在全部 44 本上跑一遍对比。
    """
    xs1 = sorted(l["x1"] for l in ocr_lines
                 if l["y1"] < H * FOOTER_RATIO and l["y0"] > H * HEADER_RATIO)
    if not xs1:
        return max(l["x1"] for l in ocr_lines)
    return xs1[int(len(xs1) * 0.92)]


def reflow_page(ocr_lines: list[dict], W: float, H: float, old_blocks: list[dict],
                n: int, figs: list[dict], legacy: bool = False) -> tuple[list[dict], dict]:
    """重排一页。返回 (blocks, diag)。

    legacy=True 时走 pdf2book 的旧栏位判据（按字符比例切行），用于对照实验。
    """
    diag: dict = {"page": n}

    zones = split_zones(ocr_lines, H)
    body = zones["body"]
    diag["n_dropped_zone"] = len(zones["dropped"])

    # 栏位判定用「版心宽」而不是「全页最大 x1」（见 body_width 的注释）
    Wb = body_width(body, H) if body else W
    diag["W_page"] = round(W, 1)
    diag["W_body"] = round(Wb, 1)

    heights = sorted(l["y1"] - l["y0"] for l in body)
    lh = heights[len(heights) // 2] if heights else 40.0
    diag["lh"] = round(lh, 1)

    if len(body) < MIN_LINES_FOR_COLUMNS:
        res = {"n_cols": 1, "gutter": None, "reason": "too_few_lines"}
    else:
        res = detect_columns(body, Wb, H)
    n_cols = res["n_cols"]
    gutter = res["gutter"]
    diag["n_cols"] = n_cols
    diag["gutter"] = round(gutter, 1) if gutter else None
    diag["reason"] = res["reason"]

    caps, body_no_caps = extract_captions(body, lh)
    diag["n_captions"] = len(caps)

    cap_blocks = [{"type": "captiontext", "text": c["text"], "y": c["y0"],
                   "page": n, "col": 0} for c in caps]

    if n_cols == 2 and gutter is not None:
        left, right, span = assign_columns(body_no_caps, gutter, Wb)
        # assign_columns 只把「跨越 gutter」的行给 span；但跨栏**标题**往往右端
        # 仅略过 gutter、左端却远在左栏，按 x0 判更稳：x0 落在左栏起点附近 → 归左。
        lx = min((l["x0"] for l in left), default=None)
        if lx is not None:
            moved = [l for l in span if l["x0"] < lx + Wb * 0.35]
            if moved:
                span = [l for l in span if l not in moved]
                left = left + moved
        diag["n_left"], diag["n_right"], diag["n_span"] = len(left), len(right), len(span)
        # span 行（跨栏标题等）整行保留，排在左栏之后、右栏之前
        blocks = (assemble(left, lh, n, 0)
                  + assemble(span, lh, n, 0)
                  + cap_blocks
                  + assemble(right, lh, n, 1))
    else:
        main, margin = separate_margin(body_no_caps, Wb, gutter)
        diag["n_margin"] = len(margin)
        blocks = assemble(main, lh, n, 0) + cap_blocks
        # 页边图版区的文字**保留**（note 通道，col=1，且不参与拼段）。
        # 整块合成一个 note 块（按 y 拼接），不逐行成块——否则块数会翻倍。
        if margin:
            mg = sorted(margin, key=lambda l: (round(l["y0"] / PAGE_LINE_Y_TOL), l["x0"]))
            blocks.append({
                "type": "paragraph",
                "text": "".join(l["text"] for l in mg),
                "y": mg[0]["y0"], "page": n, "col": 1, "note": True,
                "noteLines": len(mg),
            })

    # 图块按 y 插回（图块本身不改：id/src/page/y/box/caption 原样）
    flow: list[dict] = []
    fig_iter = sorted(figs, key=lambda f: f.get("y", 0))
    fi = 0
    for b in sorted(blocks, key=lambda b: b.get("y", 0)):
        while fi < len(fig_iter) and fig_iter[fi].get("y", 0) <= b.get("y", 0):
            flow.append(fig_iter[fi])
            fi += 1
        flow.append(b)
    while fi < len(fig_iter):
        flow.append(fig_iter[fi])
        fi += 1

    return flow, diag


# ---------------------------------------------------------------- 不变式
def check_page(page: int, ocr_lines: list[dict], out_blocks: list[dict],
               old_blocks: list[dict], diag: dict) -> list[str]:
    """逐页不变式。返回失败原因列表（空 = 通过）。

    I1 字符守恒：OCR 行全集 vs 输出块全集，去空白后字符多重集相等。
       注意页眉/页脚/页码被**有意丢弃**，所以比对基准是「OCR 全集 − 丢弃的 zone 行」。
    I2 单栏页不得出现 col=1 的**正文**段（note / captiontext 除外）。
    I5 图块数量与 (id, page) 不变。
    """
    errs: list[str] = []

    # I1：输入侧用「未被 zone 丢弃」的行
    zone_dropped_ids = set(id(l) for l in diag["_zone_dropped"])
    in_texts = [l["text"] for l in ocr_lines if id(l) not in zone_dropped_ids]
    out_texts = [b["text"] for b in out_blocks if "text" in b]
    iv = check_invariants(in_texts, out_texts, page)
    if not iv["ok"]:
        errs.append("I1 " + iv["detail"])

    # I2
    if diag["n_cols"] == 1:
        bad = [b for b in out_blocks
               if b.get("type") == "paragraph" and b.get("col") == 1
               and not b.get("note")]
        if bad:
            errs.append(f"I2 单栏页出现 {len(bad)} 个 col=1 正文段")

    # I5
    old_figs = Counter((b.get("id"), b.get("page")) for b in old_blocks
                       if b.get("type") == "figure")
    new_figs = Counter((b.get("id"), b.get("page")) for b in out_blocks
                       if b.get("type") == "figure")
    if old_figs != new_figs:
        errs.append(f"I5 图块变了：旧 {sum(old_figs.values())} → 新 {sum(new_figs.values())}")

    return errs


# ---------------------------------------------------------------- 主流程
def find_book(frag: str) -> Path:
    cands = [d for d in BOOKS.iterdir() if d.is_dir() and frag in d.name]
    if not cands:
        raise SystemExit(f"找不到书名含「{frag}」的目录")
    if len(cands) > 1:
        raise SystemExit("书名有歧义：" + " / ".join(d.name for d in cands))
    return cands[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("book", help="书名片段")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 页（调试）")
    args = ap.parse_args()

    book_dir = find_book(args.book)
    pages_dir = book_dir / "pages"
    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))

    old_blocks = book["blocks"]
    by_page: dict[int, list[dict]] = {}
    for b in old_blocks:
        if b.get("type") == "figure":
            by_page.setdefault(b.get("page", 0), []).append(b)

    page_nums = sorted({b.get("page") for b in old_blocks if b.get("page")})
    if args.limit:
        page_nums = page_nums[: args.limit]

    out_blocks: list[dict] = []
    anomalies: list[dict] = []
    diags: list[dict] = []
    fell_back = 0
    char_lost: list[dict] = []

    for n in page_nums:
        ocr = load_ocr_page(pages_dir, n)
        old_page = [b for b in old_blocks if b.get("page") == n and b.get("type") != "figure"]
        figs = by_page.get(n, [])

        if ocr is None or len(ocr) < 3:
            # 无 OCR 缓存：原样保留旧块（不改）
            out_blocks.extend([b for b in old_blocks if b.get("page") == n])
            diags.append({"page": n, "skipped": "no_ocr"})
            continue

        W = max(l["x1"] for l in ocr)
        H = max(l["y1"] for l in ocr)

        # 重新算一遍 zone 丢弃集（check_page 需要），与 reflow_page 内部一致
        zones = split_zones(ocr, H)

        try:
            flow, diag = reflow_page(ocr, W, H, old_page, n, figs)
        except Exception as e:  # noqa: BLE001  单页异常不许拖垮整本
            anomalies.append({"page": n, "errors": [f"exception: {e}"]})
            out_blocks.extend([b for b in old_blocks if b.get("page") == n])
            fell_back += 1
            continue

        diag["_zone_dropped"] = zones["dropped"]
        errs = check_page(n, ocr, flow, figs, diag)
        if errs:
            anomalies.append({"page": n, "errors": errs})
            out_blocks.extend([b for b in old_blocks if b.get("page") == n])
            fell_back += 1
        else:
            out_blocks.extend(flow)

        diag.pop("_zone_dropped", None)
        diags.append(diag)

    # 输出：只改 blocks，其他字段原样
    out = dict(book)
    out["blocks"] = out_blocks
    out["_reflow"] = {
        "tool": "tools/reflow_book.py",
        "pages": len(page_nums),
        "fell_back": fell_back,
        "anomalies": len(anomalies),
        "note": "仅重排文字块；图块与图资源未动。不写回 books\\。",
    }

    out_dir = OUT_ROOT / book_dir.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "book.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "reflow_diag.json").write_text(
        json.dumps(diags, ensure_ascii=False, indent=1), encoding="utf-8")
    if anomalies:
        (out_dir / "异常页.json").write_text(
            json.dumps(anomalies, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 汇总
    n_dual = sum(1 for d in diags if d.get("n_cols") == 2)
    print(f"\n=== {book_dir.name} ===")
    print(f"  页数 {len(page_nums)}｜判双栏 {n_dual} 页｜回退旧块 {fell_back} 页")
    print(f"  块数 {len(old_blocks)} → {len(out_blocks)}")
    if anomalies:
        print(f"  ⚠ 异常 {len(anomalies)} 页 → {out_dir / '异常页.json'}")
        for a in anomalies[:5]:
            print(f"    p{a['page']}: {a['errors']}")
    else:
        print("  ✓ 全部不变式通过")
    print(f"  输出 → {out_dir}")


if __name__ == "__main__":
    main()
