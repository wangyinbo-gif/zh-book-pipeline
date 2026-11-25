"""版式解析公共层：栏位判定 / 行归栏 / 组装 / 不变式断言。

依据 `交接文档\\流程\\转换器切块修复方案.md` §2 改动 A 实现。

设计要点（与 pdf2book.parse_page 的旧逻辑的根本差别）：
1. 栏位判定输入是「行结构」（x0 峰 + 行数均衡 + y 重叠 + 行高一致），不是墨迹投影凹谷；
2. 单栏页永远返回 gutter=None —— 旧逻辑「x0>0.45W 的行够多就当双栏」会把
   「单栏 + 右页边图版区」的书凭空切出一条栏缝，这是 bug 1.1 的根源；
3. 本模块**不切文本**。跨栏行由调用方走 span 通道整行保留（改动 B）。

本模块不依赖 pandas/numpy 之外的东西；numpy 已在项目环境内。
"""

from __future__ import annotations

from statistics import median

# ---- 判定常量（全部相对页面宽高，禁止硬编码像素）----
SECOND_LO = 0.40         # 第二峰搜索下界（相对 W）
SECOND_HI = 0.72         # 第二峰搜索上界（相对 W）
MIN_SECOND_SHARE = 0.25  # 第二峰行数至少占 body 行数比例
MIN_GAP_W = 0.12         # 第二峰与左栏左界的最小间距
BALANCE_LO, BALANCE_HI = 0.25, 0.75  # 两簇行数各占比区间
MIN_Y_OVERLAP = 0.5      # 两簇 y 区间重叠率下限（并行阅读）
MAX_LINEH_DIFF = 0.20    # 两簇中位行高最大相对差
MIN_LINES_PER_COL = 8    # 每簇最少行数（少于则不可信）
MAX_X0_DEVIATION = 0.20  # 右栏行 x0 低分位与推测右栏左界的最大偏差（相对 W）
                         # 标定过程：0.09 → 弗莱彻双栏 693 页；0.20 → 885 页；
                         # 1.0（即不做此检查）→ 仍是 885 页。而批评学在这三档
                         # 都是 7 页不变 —— 说明这个判据在 0.09 处只误杀双栏、
                         # 不拦伪双栏，是无区分力的阈值。
                         # 取 0.20 作为「明显散乱」的兜底，不做主要拦截手段
                         # （主要拦截靠 2b 横穿行占比 + 正文门 + 峰规模门槛）。
MAX_CROSSING_RATIO = 0.25  # 横穿中缝的行占比上限；真双栏只有跨栏标题/图注会横穿，
                           # 单栏页通常 40%~80% 的行都从中缝排到页右缘

DUAL_PAGE_TYPES = {"double"}          # 允许判双栏的页型
NEVER_DUAL = {                        # 一律强制单栏的页型
    "body_margin", "figure_page", "single", "table", "toc", "index",
}


PEAK_MERGE_W = 0.030      # x0 聚峰的串联容差（相对 W）
                         # 栏的左界是「齐整边界 + 缩进行」两丛，阶梯间隔约 0.02W；
                         # 容差太小会把一丛拆成多峰、每峰都不够规模门槛。

BODY_PAGE_MIN_MEDIAN_W = 0.32    # 正文页的中位行宽下限（相对 W）
                                 # 目录页 / 年表页 / 地图注记页的 x0 分布与双栏极像
                                 # （左列条目 + 右列页码），差别只在行宽：
                                 # 实测中位行宽 —— 目录/年表 0.07~0.21，参考文献 0.42，
                                 # 正文双栏 0.43~0.46。取 0.32 落在两个群体中间。
                                 # 这一条是 detect_columns 能对目录页说「不」的唯一依据。
                                 # 注意：参考文献页虽然短行多（折行造成），但中位行宽正常，
                                 # 所以必须用中位数而不是「短行占比」，否则会把参考文献误杀。


def _merge_peaks(lines, W, tol_w=PEAK_MERGE_W, min_y_overlap=0.35):
    """把行按 x0 聚成「峰」，返回 [{"x0", "n", "x1", "lines"}]，按起点 x0 升序。

    分两步：
    1) 串联合并：x0 排序后，与上一行距离 ≤ tol 的归入同一峰。
       用「上一行」而不是「峰起点」，因为栏内呈「齐整边界 → 缩进行」的阶梯。
    2) y 重叠回接：相邻两峰若 **y 区间重叠率 ≥ min_y_overlap**，说明它们是
       同一纵列里被缩进切开的两段（如 p1852 左栏 x0=91 与 x0=179），并回一峰。
       这一步只合并「纵向并列」的峰；真正的左右两栏 y 区间也重叠，
       所以第 2 步只在 **两峰 x0 间距 < MIN_GAP_W·W**（还不构成独立栏）时才做。

    为什么必须第 2 步：弗莱彻参考文献 p1852 的缺口 102→179 = 77px > tol(62px)，
    左栏被拆成 11+10 两峰、各自都差一点不到门槛，结果整页报 no_peak。
    """
    xs = sorted(lines, key=lambda l: l["x0"])
    if not xs:
        return []
    tol = tol_w * W
    groups = []
    cur = [xs[0]]
    for l in xs[1:]:
        if l["x0"] - cur[-1]["x0"] <= tol:
            cur.append(l)
        else:
            groups.append(cur)
            cur = [l]
    groups.append(cur)

    # 第 2 步：y 重叠回接（只接 x0 间距不够一个栏距的相邻组）
    merged = []
    for g in groups:
        if merged:
            prev = merged[-1]
            gap_w = g[0]["x0"] - prev[0]["x0"]
            if gap_w < MIN_GAP_W * W and _y_overlap_ratio(prev, g) >= min_y_overlap:
                merged[-1] = prev + g
                continue
        merged.append(g)

    return [{"x0": g[0]["x0"], "n": len(g),
             "x1": max(l["x1"] for l in g), "lines": g} for g in merged]


def _y_overlap_ratio(a_lines, b_lines):
    """两簇行在 y 轴上的区间重叠率（以较短者覆盖范围归一）。"""
    if not a_lines or not b_lines:
        return 0.0
    a0 = min(l["y0"] for l in a_lines)
    a1 = max(l["y1"] for l in a_lines)
    b0 = min(l["y0"] for l in b_lines)
    b1 = max(l["y1"] for l in b_lines)
    lo, hi = max(a0, b0), min(a1, b1)
    if hi <= lo:
        return 0.0
    span = min(a1 - a0, b1 - b0)
    return (hi - lo) / span if span > 0 else 0.0


def detect_columns(lines, W, H, page_type=None):
    """判定本页栏数。

    lines: [{"x0","x1","y0","y1","text",...}]，**调用方须只传正文行**
    （已剔除 header/footer/pageno/caption/页边 right）
    page_type: pagetypes.py 给出的 13 类页型之一；None 时不做页型门

    返回 {"n_cols": 1|2, "gutter": float|None, "left_x": float, "right_x": float,
          "conf": float, "reason": str}
    """
    empty = {"n_cols": 1, "gutter": None, "left_x": None, "right_x": None,
             "conf": 0.0, "reason": "no_body_lines"}
    body = [l for l in lines if l.get("text", "").strip()]
    if len(body) < MIN_LINES_PER_COL * 2:
        empty["reason"] = "too_few_lines"
        return empty

    # 页型门：非 double 页一律单栏（改动 A 第 6 步）
    if page_type is not None:
        if page_type in NEVER_DUAL:
            return {"n_cols": 1, "gutter": None,
                    "left_x": min(l["x0"] for l in body), "right_x": None,
                    "conf": 0.9, "reason": f"page_type_gate:{page_type}"}
        if page_type not in DUAL_PAGE_TYPES:
            return {"n_cols": 1, "gutter": None,
                    "left_x": min(l["x0"] for l in body), "right_x": None,
                    "conf": 0.7, "reason": f"page_type_not_dual:{page_type}"}

    # 0) 正文门：中位行宽太小 → 这是目录页 / 年表页 / 地图注记页，不是正文
    #    它们的 x0 分布与真双栏几乎一样（左列条目 + 右列页码），
    #    唯一稳定的差别是行宽（见 BODY_PAGE_MIN_MEDIAN_W 注释）。
    mid_w = median((l["x1"] - l["x0"]) for l in body) / W
    if mid_w < BODY_PAGE_MIN_MEDIAN_W:
        return {"n_cols": 1, "gutter": None,
                "left_x": min(l["x0"] for l in body), "right_x": None,
                "conf": 0.8, "reason": f"not_body_median_w:{mid_w:.2f}"}

    # 1) 主峰 = **最左的显著峰** = 左栏左界（改动 A 第 2 步）
    #    不能取「行数最多的桶」：真双栏页右栏略多时会把右栏当左界，
    #    后续第二峰搜索区间随之整体右移、永远落空。
    #    也不能用 6px 单桶计数：栏界是「齐整边界 + 缩进行」连续一丛，
    #    单桶行数远低于门槛，必须先按容差合并成峰再判规模。
    peak_min_n = max(MIN_LINES_PER_COL, MIN_SECOND_SHARE * len(body))
    peaks = _merge_peaks(body, W)
    main_peak = next((p for p in peaks if p["n"] >= peak_min_n), None)
    if main_peak is None:
        empty["reason"] = "no_peak"
        return empty
    left_x = main_peak["x0"]

    # 2) 第二峰：起点必须在 [0.40W, 0.72W]，与左界隔开 ≥0.12W（第 3 步）
    sec_lo = max(W * SECOND_LO, left_x + MIN_GAP_W * W)
    sec_hi = W * SECOND_HI
    sec_peak = None
    for p in peaks:
        if sec_lo <= p["x0"] < sec_hi and p["n"] >= peak_min_n:
            if sec_peak is None or p["n"] > sec_peak["n"]:
                sec_peak = p
    if sec_peak is None:
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": None,
                "conf": 0.6, "reason": "no_second_peak"}
    right_x = sec_peak["x0"]
    if left_x + MIN_GAP_W * W > right_x:
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": None,
                "conf": 0.6, "reason": "gap_too_small"}

    # 2b) 满宽行保护（关键补充，方案 §2 未写死但必须有）：
    #     单栏页的段落缩进行同样会形成「第二峰」，所以不能只看 x0 峰。
    #     真双栏的每栏行**只在自己那半页**结束，横穿中缝的行极少（仅跨栏标题/图注）；
    #     单栏页则大量行从中缝一路排到页右缘。
    crossing = sum(1 for l in body if l["x0"] < W * 0.45 and l["x1"] > W * 0.55)
    cross_ratio = crossing / len(body)
    if cross_ratio > MAX_CROSSING_RATIO:
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": right_x,
                "conf": 0.55, "reason": f"wide_lines:{cross_ratio:.2f}"}

    # 2c) 右栏内部应当有一条齐整的左界：右栏行 x0 的**低分位**要贴近 right_x。
    #
    # 三条都踩过坑，记下来：
    #  (a) 不能拿中位数 —— 栏内是「首行齐整 + 续行缩进」，中位数落在缩进位置
    #      （p016 右栏齐整左界 793、续行 972，中位数 972，差 0.10W，真双栏全误杀）；
    #  (b) 不能拿「x 中心在右半页」来选右栏行 —— 跨栏标题中心也在右半页，
    #      但它的 x0 在最左边，会把第 10 百分位拽到左栏去（偏差 0.4W，710 页误杀）；
    #  (c) 所以必须先用**行左端 x0 落在右栏区** 来选行，再看它们的低分位。
    #      跨栏行的 x0 在左栏区，天然被排除。
    right_half = [l for l in body if l["x0"] >= (left_x + right_x) / 2.0]
    if len(right_half) >= MIN_LINES_PER_COL:
        rx = sorted(l["x0"] for l in right_half)
        low = rx[max(0, int(len(rx) * 0.10))]
        dev = abs(low - right_x) / W
        if dev > MAX_X0_DEVIATION:
            return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": right_x,
                    "conf": 0.55, "reason": f"x0_peak_scattered:{dev:.2f}"}

    # 3) 两簇切分 + 均衡（第 4 步）
    boundary = (left_x + right_x) / 2.0
    left_lines = [l for l in body if l["x0"] < boundary]
    right_lines = [l for l in body if l["x0"] >= boundary]
    if not left_lines or not right_lines:
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": None,
                "conf": 0.5, "reason": "empty_cluster"}
    share = len(right_lines) / len(body)
    if not (BALANCE_LO <= share <= BALANCE_HI):
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": None,
                "conf": 0.5, "reason": f"imbalance:{share:.2f}"}

    # 4) y 重叠（并行阅读）
    overlap = _y_overlap_ratio(left_lines, right_lines)
    if overlap < MIN_Y_OVERLAP:
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": right_x,
                "conf": 0.5, "reason": f"y_overlap_low:{overlap:.2f}"}

    # 5) 行高一致（第 5 步）
    lh_l = median(l["y1"] - l["y0"] for l in left_lines)
    lh_r = median(l["y1"] - l["y0"] for l in right_lines)
    if max(lh_l, lh_r) > 0:
        diff = abs(lh_l - lh_r) / max(lh_l, lh_r)
    else:
        diff = 0.0
    if diff > MAX_LINEH_DIFF:
        return {"n_cols": 1, "gutter": None, "left_x": left_x, "right_x": right_x,
                "conf": 0.5, "reason": f"lineh_mismatch:{diff:.2f}"}

    # 6) gutter：两簇之间的中点（仅用于判定跨栏，不用于切文本）
    left_max_x1 = max(l["x1"] for l in left_lines)
    right_min_x0 = min(l["x0"] for l in right_lines)
    gutter = (left_max_x1 + right_min_x0) / 2.0 if left_max_x1 < right_min_x0 else boundary

    conf = 0.5 + 0.2 * min(1.0, share / 0.5) + 0.2 * min(1.0, overlap) + 0.1 * (1 - diff)
    return {"n_cols": 2, "gutter": gutter, "left_x": left_x, "right_x": right_x,
            "conf": round(min(conf, 0.99), 3), "reason": "ok"}


def assign_columns(lines, gutter, W):
    """行归栏：按 x 中心归左/右/跨栏。**绝不切文本**（改动 B）。

    返回 (left, right, span) 三个行列表；span 为跨栏行（整行保留）。
    """
    left, right, span = [], [], []
    for l in lines:
        cx = (l["x0"] + l["x1"]) / 2.0
        width = l["x1"] - l["x0"]
        crosses = (l["x0"] < gutter - 30) and (l["x1"] > gutter + 30) and width > W * 0.25
        if crosses:
            span.append(l)
        elif cx <= gutter:
            left.append(l)
        else:
            right.append(l)
    return left, right, span


# ---------------------------------------------------------------- 不变式
def check_invariants(input_texts, output_texts, page, kind="reflow"):
    """字符守恒断言（方案 §3 不变式 I1）。

    input_texts : 该页所有 OCR 行文本（进组装前的全集）
    output_texts: 该页组装后所有块文本（出组装后的全集）
    返回 {"ok": bool, "detail": str}
    """
    from collections import Counter

    def squash(seq):
        return Counter("".join("".join(t.split()) for t in seq))

    a = squash(input_texts)
    b = squash(output_texts)
    if a == b:
        return {"ok": True, "detail": f"{kind} p{page}: 字符多重集守恒 ({sum(a.values())} 字)"}
    missing = a - b
    extra = b - a
    return {
        "ok": False,
        "detail": (f"{kind} p{page}: 不守恒 缺 {sum(missing.values())} 字 / 多 {sum(extra.values())} 字"),
        "missing_sample": "".join(sorted(missing.elements()))[:80],
        "extra_sample": "".join(sorted(extra.elements()))[:80],
    }
