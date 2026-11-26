# -*- coding: utf-8 -*-
r"""竖排单页 OCR（S2 生产模块，参数化版）

来源：竖排识别的分阶段实验（S1 单页原型 → S2 生产模块）；实验脚本与逐页记录不随本仓库分发。
（实测 p41 原始 CER 1.82% / 异体归一 0.40%，p121 2.97% / 1.91%，标点 0.0% / 0.0%，杂符 0）。
本模块是它的**生产化移植**：不依赖任何实验目录，参数（pdf/页号/scale/书耳/模型）全部显式；
同时带上两个崩溃修复（整列饱和黑边清零、列距窗口越界兜底）——修复前整本约 7% 页会整页无输出。

路线（与 S1 逐原语一致，不改算法）：
  全列宽墨迹投影 → 自相关估列距 → 每条列距窗口取墨谷作列边界
  → 列内按 tall_thr165 切字 → 每 10 字直立横拼（44×52 cell, 38×40 thumb）
  → PP-OCRv5 mobile rec → 单字笔画校正 → 标点归一 → 右→左成文

对外接口：
  VerticalOCR(pdf, scale=3.0, ears=[...], model_path=None, cache_dir=None)
    .recognize(page) -> {"page", "text", "columns": [...], "diag": {...}}
    .cache_key(page) / .pdf_digest()
  page_scan(engine, gray, ears) -> dict          # 无状态单页原语（灰度图入）
  basic_flags(diag) -> list[str]                  # 页级基础异常标记
  enrich_flags(recs)                              # 补「低字/栏数异常/重切多」三类相对标记
  页级去倾斜（2026-11-26 投产，任务⑤；只挂在竖排识别入口，横排/gate 不经过）：
  estimate_skew / skew_peaks / deskew_trigger / skew_medians /
  deskew_verify / choose_deskew_angle / page_scan_deskew
  VerticalOCR.recognize_deskew(page, med)          # 触发页二道识别（变体缓存键）

缓存键：sha1(pdf 内容) + page + scale + PARAM_VERSION → cache_dir\<key>.json
（整本书的缓存目录由 vertical_book.py 定为 book_dir\.ocr_cache\）

页级去倾斜的缓存纪律（重要，勿改动）：
  · PARAM_VERSION 保持 "vertical-ocr-v1" 不动 —— 递增会全书缓存失效重跑；
  · 非触发页：只走原基础键，输出与既有 .ocr_cache **逐字节一致**（本模块对
    recognize()/page_scan() 的基础路径零改动）；
  · 触发页：去倾斜结果只写变体子键 sha1+page+scale+"vertical-ocr-v1+deskew-v1"，
    与基础缓存互不覆盖；校验失败也落一条「维持原结果+尝试记录」的变体缓存，
    避免重跑时反复搜索；
  · 子键用 param_version 拼后缀实现（cache_key 第四段），旧缓存文件全部原样保留，
    删除任何 *+deskew-v1.json 即回到投产前行为（可整体回退）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE / "models" / "ch_PP-OCRv5_rec_mobile.onnx"
MODEL_ENV = "VERTICAL_OCR_MODEL"

PARAM_VERSION = "vertical-ocr-v1"   # 改切分/校正/归一参数必须递增，否则缓存会假命中
DEFAULT_SCALE = 3.0

# ---------------- 版面/切分参数（与 S1 逐值一致，勿随意动） ----------------
COL_THR = 160           # 列槽投影阈值（墨 = 灰度 < COL_THR）
COL_MIN_W = 8           # 列槽最小宽
COL_MIN_INK = 0.004     # 列槽最小墨量占比（保留字段，S1 未直接用）
SPLIT_THR = 165         # 列内切字阈值（tall_thr165）
STRIP_N = 10            # 每 10 字横拼一行
CELL_W, CELL_H = 44, 52
THUMB = (38, 40)
WIDE_CUT = 44           # 列槽过宽阈值（超过则收紧到墨迹核心）
EAR_LEN = 8             # 书耳文本最长长度

PUNCT_ALL = "、。，；：？！…—·「」『』（）《》〈〉"
BAD_CHARS = "’°_O"      # 与 S1 基线「杂符」口径一致
SUSPECT_CHARS = "_．|"  # 疑似误读（不进 S1 判据，只进页级诊断）

# 常见正俗/异体字：右 → 左（CER 归一口径用；不含 鐘/鍾 这类语义不同字）
VARIANTS = {
    "裏": "里", "裡": "里", "煙": "烟", "菸": "烟", "於": "于", "爲": "為", "黃": "黄",
    "內": "内", "朶": "朵", "圜": "圓", "羣": "群", "眞": "真", "靑": "青", "敎": "教",
    "麤": "粗", "蹤": "踪", "靣": "面", "戶": "户", "兌": "兑", "兪": "俞", "賸": "剩",
    "牀": "床", "晉": "晋", "況": "况", "涼": "凉", "淨": "净", "減": "减", "湊": "凑",
    "淒": "凄", "爐": "炉", "爭": "争", "牋": "笺", "盃": "杯", "竈": "灶", "糉": "粽",
    "綑": "捆", "縧": "绦", "鉤": "钩", "鋪": "铺", "峯": "峰", "島": "岛", "嶽": "岳",
    "巖": "岩", "災": "灾", "祕": "秘", "脣": "唇", "悅": "悦", "躭": "耽", "慙": "惭",
    "潔": "洁", "兇": "凶", "汙": "污", "棄": "弃", "搾": "榨", "稜": "棱", "簷": "檐",
    "敘": "叙", "尙": "尚", "吿": "告", "吳": "吴", "呂": "吕", "佈": "布", "滙": "汇",
    "彙": "汇", "麪": "面", "麫": "面", "嗇": "啬", "稟": "禀", "冪": "幂",
    "鳧": "凫", "覩": "睹", "迺": "乃", "徧": "遍", "歎": "嘆", "啟": "啓",
    "麁": "粗", "麼": "么", "麽": "么", "稺": "稚", "穉": "稚", "峩": "峨",
}
# 笔画校正：只改「rec 输出成了非汉字杂形」的字块（— _ . 丶 丨 | －），用墨带数还原成一/二/三
AMBIG_CHARS = set("—_.丶丨|－")
BAND_CHARS = {1: "一", 2: "二", 3: "三"}
PUNCT_MAP = {"’": "、", "‘": "、", "°": "。", "“": "「", "”": "」"}

# 句末/段末标点（成段用；与 pdf2book.merge_cross_page 的口径一致）
TERM_PUNCT = "。！？…；：”』」）)】.．!?"


# ---------------------------------------------------------------- 文本工具

def is_cjk(c: str) -> bool:
    return unicodedata.name(c, "").startswith("CJK")


def cjk_count(s: str) -> int:
    return sum(1 for c in s if is_cjk(c))


def norm_variants(s: str) -> str:
    """异体归一（CER 归一口径）。"""
    return "".join(VARIANTS.get(c, c) for c in s)


def normalize_punct(s: str) -> str:
    return "".join(PUNCT_MAP.get(c, c) for c in s)


def count_punct(s: str) -> int:
    return sum(1 for c in s if c in PUNCT_ALL)


# ---------------------------------------------------------------- 页面与列槽

def _runs(on: np.ndarray, min_w: int) -> list[tuple[int, int]]:
    edges = np.flatnonzero(np.diff(np.r_[False, on, False])).reshape(-1, 2)
    out: list[tuple[int, int]] = []
    for s, e in edges.tolist():
        if e - s < min_w:
            continue
        if out and s - out[-1][1] <= 2:      # 相邻 2px 内合并（书耳/框线）
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _cols_pitch(ink: np.ndarray) -> int:
    """自相关估列距；若峰落在二次谐波（2 倍列距）则取回基频。"""
    x = ink - ink.mean()
    if not np.any(x):
        return 52
    ac = np.correlate(x, x, "full")[len(x) - 1:]
    if ac[0] <= 0:
        return 52
    ac = ac / ac[0]
    lo, hi = 18, min(140, len(ac))
    if hi <= lo:
        return 52
    p = int(np.argmax(ac[lo:hi]) + lo)
    half = p // 2
    if p >= 75 and half >= lo and ac[half] > 0.45 * ac[p]:
        p = half
    return max(18, p)


def column_slots(gray: np.ndarray) -> tuple[list[tuple[int, int]], int]:
    """列槽检测：自相关估列距 → 每个列距窗口内取墨量最低点作列边界。

    比「全局阈值切段」稳：不受界行、网点、灰尘影响（竖排页列距实测 52±2 px）。
    返回 (slots 从左到右, 列距)。
    """
    H = gray.shape[0]
    band = gray[int(H * 0.06):int(H * 0.94)]
    ink = (band < COL_THR).sum(axis=0).astype(np.float64)
    # 去黑边/装订线：整列饱和（≥90% 行是墨）不可能是正文列，留着会把自相关带偏
    ink[ink >= 0.9 * band.shape[0]] = 0
    pitch = _cols_pitch(ink)
    sens = max(3, int(H * 0.004))
    on = ink > sens
    if not on.any():
        return [], pitch
    bounds = [int(np.argmax(on))]
    while bounds[-1] + pitch * 0.6 < len(ink):
        target = bounds[-1] + pitch
        win = int(pitch * 0.35)
        a, b = max(0, target - win), min(len(ink), target + win + 1)
        if b <= a:                      # 列距估计异常时窗口可能越界（空页/极窄页）
            break
        bounds.append(a + int(np.argmin(ink[a:b])))
    slots: list[tuple[int, int]] = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        nz = np.flatnonzero(ink[a:b] > sens)
        if not len(nz):
            continue
        s, e = a + int(nz[0]), a + int(nz[-1]) + 1
        if e - s >= 8:
            slots.append((s, e))
    # 兜底：列距检测失败（图版页/半页）时退回全局高灵敏切段
    if len(slots) < 3:
        return _runs(ink > sens, 8), pitch
    return slots, pitch


# ---------------------------------------------------------------- 列内切字

def glyph_boxes(col: np.ndarray) -> list[tuple[int, int]]:
    proj = np.count_nonzero(col < SPLIT_THR, axis=1)
    rows = proj > 1
    edges = np.flatnonzero(np.diff(np.r_[False, rows, False])).reshape(-1, 2)
    boxes: list[tuple[int, int]] = []
    for start, end in edges.tolist():
        if boxes:
            ps, pe = boxes[-1]
            if start - pe <= 4 and end - ps <= 33:
                boxes[-1] = (ps, end)
                continue
            if start - pe <= 11 and end - ps <= 26:
                boxes[-1] = (ps, end)
                continue
        if end - start > 1:
            boxes.append((start, end))
    return boxes


def ink_bands(cell: np.ndarray) -> int:
    """字形盒内「横墨带」数量：一=1、二=2、三=3（用于纠正细横笔误读）。"""
    rows = (cell < SPLIT_THR).sum(axis=1) > 0
    return int(np.sum(np.diff(np.r_[False, rows, False]) == 1))


def strips_of(col: np.ndarray, boxes, n=STRIP_N):
    """每 n 个字横向拼成一行；返回 {row, metas, cells}，cells 供长度不符时二分重识别。"""
    out = []
    for begin in range(0, len(boxes), n):
        cells, metas = [], []
        for top, bottom in boxes[begin:begin + n]:
            band = col[top:bottom]
            cols_ink = np.count_nonzero(band < 170, axis=0)
            nz = np.flatnonzero(cols_ink > 0)
            if not len(nz):
                continue
            left, right = max(0, nz[0] - 6), min(band.shape[1], nz[-1] + 7)
            band = band[:, left:right]
            if band.size == 0 or band.shape[1] < 2:
                continue
            g = Image.fromarray(band)
            g.thumbnail(THUMB, Image.Resampling.LANCZOS)
            cell = Image.new("L", (CELL_W, CELL_H), 255)
            cell.paste(g, ((CELL_W - g.width) // 2, (CELL_H - 8) - g.height))
            cells.append(np.asarray(cell))
            metas.append({"bands": ink_bands(band), "h": int(bottom - top),
                          "w": int(right - left), "gw": int(band.shape[1])})
        if not cells:
            continue
        out.append({"row": hstack_row(cells), "metas": metas, "cells": cells})
    return out


def hstack_row(cells) -> np.ndarray:
    row = np.hstack(cells)
    row = np.pad(row, ((0, 0), (8, 8)), constant_values=255)
    return np.repeat(row[:, :, None], 3, axis=2)


def _rec(engine, img) -> str:
    texts, _ = engine.text_rec(img)
    return "".join(t for t, _c in (texts or []))


def _fix_chars(raw: str, metas: list[dict]) -> tuple[str, int]:
    """1:1 对齐后的几何校正：细宽字块 → 一；rec 输出杂形 → 按墨带数归一。"""
    fixed, n = [], 0
    for ch, meta in zip(raw, metas):
        h, gw, band = meta["h"], meta["gw"], meta["bands"]
        if h <= 6 and gw >= 18 and ch not in "、。，；：？！":
            fixed.append("一")          # 细横笔：只可能是「一」（、类标点已排除）
            n += 1
            continue
        want = BAND_CHARS.get(band)
        if ch in AMBIG_CHARS and want:
            fixed.append(want)
            n += 1
            continue
        fixed.append(ch)
    return "".join(fixed), n


def ocr_column(engine, col: np.ndarray) -> dict:
    """单列识别。返回 text/glyphs/corrections/resplit/heights（heights 供页级诊断）。"""
    boxes = glyph_boxes(col)
    heights = [b - a for a, b in boxes]
    pieces, corrections, resplit = [], 0, 0
    for st in strips_of(col, boxes):
        raw = _rec(engine, st["row"])
        metas, cells = st["metas"], st["cells"]
        if len(raw) == len(metas):
            t, n = _fix_chars(raw, metas)
            pieces.append(t)
            corrections += n
            continue
        # 输出长度与字块数不符时二分重识别，恢复 1:1 后再校正
        resplit += 1
        k = max(1, len(cells) // 2)
        for part in (slice(0, k), slice(k, None)):
            sub_cells, sub_metas = cells[part], metas[part]
            if not sub_cells:
                continue
            r = _rec(engine, hstack_row(sub_cells))
            if len(r) == len(sub_metas):
                t, n = _fix_chars(r, sub_metas)
                pieces.append(t)
                corrections += n
            else:
                pieces.append(r)
    return {"text": "".join(pieces), "glyphs": len(boxes),
            "corrections": corrections, "resplit": resplit, "heights": heights}


# ---------------------------------------------------------------- 版心要素归类

def classify(text: str, ears=None) -> str:
    """按文本归类版心要素：卷标 / 页码 / 书耳 / 正文 / 空。"""
    t = (text or "").strip()
    if not t:
        return "空"
    if re.match(r"^卷[之第]", t) or re.match(r"^第[一二三四五六七八九十]+卷", t):
        return "卷标"
    if re.fullmatch(r"[一二三四五六七八九十〇零百0-9]{1,4}", t):
        return "页码"
    for e in (ears or []):
        if e and (t == e or (len(t) <= EAR_LEN + 2 and t in e)):
            return "书耳"
    return "正文"


def _core_crop(gray: np.ndarray, x0: int, x1: int, pad: int = 3) -> tuple[int, int]:
    """把列槽收紧到墨迹核心（去掉边界上的邻居残墨/灰尘，避免 rec 幻觉出标点）。"""
    sub = (gray[:, x0:x1] < COL_THR).sum(axis=0).astype(np.float64)
    if not sub.any():
        return x0, x1
    m = sub.max()
    core = np.flatnonzero(sub >= max(4.0, 0.25 * m))
    if not len(core):
        return x0, x1
    return max(0, x0 + int(core[0]) - pad), min(gray.shape[1], x0 + int(core[-1]) + pad)


# ---------------------------------------------------------------- 页级诊断

def _page_diag(page: int, slots, pitch, columns, seconds: float = 0.0) -> dict:
    body = sorted([c for c in columns if c["kind"] == "正文"], key=lambda c: -c["x0"])
    heads = sorted([c for c in columns if c["kind"] == "卷标"], key=lambda c: -c["x0"])
    text = "".join(c["text"] for c in body)
    heights = [h for c in columns for h in c.get("heights", [])]
    bad_list = sorted({c for c in text if c in BAD_CHARS})
    diag = {
        "slots": len(slots), "pitch": pitch,
        "han": cjk_count(text),
        "punct": count_punct(text),
        "punct_per100": round(count_punct(text) / max(1, cjk_count(text)) * 100, 1),
        "bad": len([c for c in text if c in BAD_CHARS]), "bad_list": bad_list,
        "suspect": len([c for c in text if c in SUSPECT_CHARS]),
        "corrections": sum(c.get("corrections", 0) for c in columns),
        "resplit": sum(c.get("resplit", 0) for c in columns),
        "glyphs": sum(c.get("glyphs", 0) for c in columns),
        "heads": [h["text"] for h in heads],
        "other": {c["kind"]: c["text"] for c in columns if c["kind"] in ("页码", "书耳", "空")},
        "h_med": int(np.median(heights)) if heights else 0,
        "h_small_share": round(sum(1 for h in heights if h < 20) / len(heights), 3) if heights else 0.0,
        "col_errors": [c["error"] for c in columns if c.get("error")],
        "seconds": round(seconds, 2),
    }
    diag["flags"] = basic_flags(diag)
    return diag


def basic_flags(diag: dict) -> list[str]:
    """页面级基础异常标记（与「书籍中位数」无关的那部分）。"""
    if diag.get("error"):
        return ["抛错"]
    f: list[str] = []
    han, punct = diag.get("han", 0), diag.get("punct", 0)
    if han == 0:
        f.append("零字")
    elif han < 30:
        f.append("极低字")
    if han >= 300 and punct == 0:
        f.append("无句读")
    if han >= 100 and punct / max(1, han) * 100 > 25:
        f.append("句读过密")
    if diag.get("bad", 0) > 0:
        f.append("杂符")
    if diag.get("suspect", 0) > 0:
        f.append("疑似误读")
    if diag.get("col_errors"):
        f.append("列抛错")
    slots = diag.get("slots", 0)
    if 0 < slots < 5:
        f.append("少栏")
    if slots > 22:
        f.append("多栏")
    if slots == 0:
        f.append("无列槽")
    if not (45 <= diag.get("pitch", 0) <= 60):
        f.append("列距异常")
    if diag.get("h_small_share", 0) >= 0.25:
        f.append("小字多")
    return f


def enrich_flags(recs: list[dict]) -> None:
    """用全书分布补三类相对标记（低字 / 栏数异常 / 重切多）。recs 需含 diag。"""
    ok = [r for r in recs if not r["diag"].get("error")]
    hans = [r["diag"].get("han", 0) for r in ok]
    slots = [r["diag"].get("slots", 0) for r in ok if r["diag"].get("slots", 0) > 0]
    med_han = float(np.median(hans)) if hans else 0
    med_slot = float(np.median(slots)) if slots else 0
    for r in recs:
        d = r["diag"]
        d["flags"] = [f for f in d.get("flags") or [] if f not in ("低字", "栏数异常", "重切多")]
        if d.get("error"):
            continue
        if med_han and 30 <= d.get("han", 0) < 0.40 * med_han:
            d["flags"].append("低字")
        s = d.get("slots", 0)
        if med_slot and s > 0 and abs(s - med_slot) / med_slot > 0.40:
            d["flags"].append("栏数异常")
        if d.get("resplit", 0) >= max(3, s // 2) and d.get("han", 0) < 0.5 * med_han:
            d["flags"].append("重切多")


# ---------------------------------------------------------------- 模型与引擎

def resolve_model(model_path=None) -> Path:
    """模型解析顺序：显式参数 → 环境变量 VERTICAL_OCR_MODEL → tools/models/ 默认。"""
    cand = model_path or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    p = Path(cand)
    if not p.is_file():
        raise FileNotFoundError(
            "竖排 rec 模型不存在：%s\n"
            "  把 ch_PP-OCRv5_rec_mobile.onnx 放到 tools/models/，"
            "或用 --model / 环境变量 %s 指定路径。" % (p, MODEL_ENV))
    return p


def make_engine(model_path=None, intra_op_num_threads: int = 4):
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=intra_op_num_threads, inter_op_num_threads=2,
                    rec_model_path=str(resolve_model(model_path)))


# ---------------------------------------------------------------- 单页原语（无状态）

def page_scan(engine, gray: np.ndarray, ears=None) -> dict:
    """灰度图 → 单页结构（S1 等价的纯函数入口）。"""
    t0 = time.monotonic()
    slots, pitch = column_slots(gray)
    columns = []
    for x0, x1 in slots:                     # 从左到右
        if x1 - x0 > WIDE_CUT:
            # 槽过宽：含邻居残墨/灰尘，收紧到墨迹核心再识别
            cx0, cx1 = _core_crop(gray, x0, x1)
        else:
            # 正常列（25–40px）：左右留 3px 余量，避免边界字被切掉笔画
            cx0, cx1 = max(0, x0 - 3), min(gray.shape[1], x1 + 3)
        try:
            r = ocr_column(engine, gray[:, cx0:cx1])
        except Exception as e:              # 单列失败不拖垮整页
            r = {"text": "", "glyphs": 0, "corrections": 0, "resplit": 0,
                 "heights": [], "error": "%s: %s" % (type(e).__name__, e)}
        r.update({"x0": x0, "x1": x1, "crop": [cx0, cx1],
                  "text": normalize_punct(r["text"])})
        r["kind"] = classify(r["text"], ears)
        columns.append(r)
    # 竖排右起：正文按 x 从大到小
    body = sorted([c for c in columns if c["kind"] == "正文"], key=lambda c: -c["x0"])
    text = "".join(c["text"] for c in body)
    diag = _page_diag(0, slots, pitch, columns, time.monotonic() - t0)
    return {"page": 0, "text": text, "columns": columns, "diag": diag}


# ---------------------------------------------------------------- 页级去倾斜（识别入口增量）
# 依据：合页/倾斜拆分定向实验（2026-11-26；实验记录与中间 json 不随本仓库分发）。
# 一类整页乱码的根因是 1.5–2.0° 页级扫描倾斜：斜切把列向墨迹投影糊平，
# column_slots 自相关列距从 52px 崩到 18px，真列被切成半字窄槽 → rec 幻觉；
# p264 是同一病的 2× 谐波形态（pitch 105≈2×52，10 栏）。算法原语（page_scan/
# column_slots/切字/rec）一律不动，只在外层做「触发 → 找角 → 转正 → 结构校验」。

DESKW_VERSION = "deskew-v1"          # 缓存变体子键版本位（拼在 param_version 后）
DESKW_LO, DESKW_HI, DESKW_STEP = -3.0, 3.0, 0.25   # 投影方差网格：±3°、0.25° 步长
DESKW_PITCH_RATIO = 0.80             # 触发 A：列距崩（pitch < 0.8×全书中位）
DESKW_SLOT_RATIO = 1.60              # 触发 A：栏数翻倍（slots ≥ 1.6×全书中位）
DESKW_HARMONIC_RATIO = 1.80          # 触发 B：2× 谐波翻倍（pitch ≥ 1.8×中位，p264 型）
DESKW_HAN_RATIO = 0.40               # 正文页前置条件：han ≥ 0.4×全书中位（剔书影/封面/目录）
DESKW_VERIFY_PITCH = (0.80, 1.25)    # 校验：转正后 pitch 应落回正常带 [0.8,1.25]×中位
DESKW_VERIFY_SLOT = 0.40             # 校验：|slots−中位| ≤ 0.40×中位（与「栏数异常」同界）
DESKW_TOP_K = 4                      # 逐峰校验的最大候选数（2× 谐波回退在此生效）


def score_profile(gray: np.ndarray) -> tuple[float, float]:
    """列向墨迹投影方差 + 白沟占比（越大越「竖得直」）。投影口径与 column_slots 一致。"""
    H = gray.shape[0]
    band = gray[int(H * 0.06):int(H * 0.94)]
    ink = (band < COL_THR).sum(axis=0).astype(np.float64)
    ink[ink >= 0.9 * band.shape[0]] = 0        # 饱和黑边/装订线清零（同 Lead 修过的口径）
    sens = max(3, int(H * 0.004))
    return float(ink.var()), float((ink <= sens).mean())


def rotate_gray(gray: np.ndarray, ang: float) -> np.ndarray:
    """绕页心旋转（双线性、白底、不扩边）——与实验 deskew_hopage.rotate 逐参一致。"""
    if abs(ang) < 1e-6:
        return gray
    im = Image.fromarray(gray)
    return np.array(im.rotate(ang, resample=Image.BILINEAR, fillcolor=255, expand=False))


def estimate_skew(gray: np.ndarray, lo: float = DESKW_LO, hi: float = DESKW_HI,
                  step: float = DESKW_STEP) -> tuple[float, list]:
    """投影方差网格找角（61 行实验核心移植）。返回 (最优角, 曲线[(角,方差,白沟)])。"""
    curve: list = []
    best = None
    for a in np.arange(lo, hi + 1e-9, step):
        g = rotate_gray(gray, float(a)) if abs(a) > 1e-6 else gray
        v, w = score_profile(g)
        curve.append((round(float(a), 2), round(v, 1), round(w, 3)))
        if best is None or v > best[1]:
            best = curve[-1]
    return best[0], curve


def skew_peaks(curve: list, top_k: int = DESKW_TOP_K) -> list:
    """方差曲线局部极大（含端点），按方差降序 —— 2× 谐波回退的候选队列。"""
    vals = [c[1] for c in curve]
    peaks = []
    for i, (a, v, _w) in enumerate(curve):
        left = vals[i - 1] if i > 0 else -1.0
        right = vals[i + 1] if i + 1 < len(curve) else -1.0
        if v >= left and v >= right:
            peaks.append((a, v))
    peaks.sort(key=lambda x: -x[1])
    return peaks[:max(1, top_k)]


def skew_medians(recs: list, min_pages: int = 50) -> dict:
    """全书中位（无抛错页、0 值不进样本），口径同实验 a_现状量化.dist。pages<min_pages 不启用。"""
    ok = [r.get("diag") or {} for r in recs if not (r.get("diag") or {}).get("error")]
    pitches = [d.get("pitch", 0) for d in ok if d.get("pitch")]
    slots = [d.get("slots", 0) for d in ok if d.get("slots")]
    hans = [d.get("han", 0) for d in ok if d.get("han")]
    return {"pages": len(ok), "ok": len(ok) >= min_pages,
            "pitch": float(np.median(pitches)) if pitches else 0.0,
            "slots": float(np.median(slots)) if slots else 0.0,
            "han": float(np.median(hans)) if hans else 0.0}


def deskew_trigger(diag: dict, med: dict) -> bool:
    """触发判据（正文页前置 + A/B 二选一）。

    前置条件 han ≥ 0.4×中位 是硬要求：只按 A/B 判据曾在整页书影多的书里误触 18 个
    书影/图版页（han 4–53），加此前置后全部排除（结论.md 建议 2）。
    A 列距崩+栏数翻倍 = p57/77/215 型；B 2× 谐波列距 = p264 型（pitch≈2×中位）。
    """
    if not med or not med.get("ok") or diag.get("error"):
        return False
    mp, ms, mh = med.get("pitch") or 0, med.get("slots") or 0, med.get("han") or 0
    if not (mp and ms and mh):
        return False
    han, pitch, slots = diag.get("han", 0), diag.get("pitch", 0), diag.get("slots", 0)
    if han < DESKW_HAN_RATIO * mh:
        return False                            # 书影/封面/目录/空白：不碰
    if pitch < DESKW_PITCH_RATIO * mp and slots >= DESKW_SLOT_RATIO * ms:
        return True
    if pitch >= DESKW_HARMONIC_RATIO * mp:
        return True
    return False


def deskew_verify(gray: np.ndarray, med: dict) -> tuple[int, int, bool]:
    """转正后结构校验：pitch 落回正常带 且 栏数不异常。不达标不得采用（防谐波假峰）。"""
    slots, pitch = column_slots(gray)
    mp, ms = med.get("pitch") or 0, med.get("slots") or 0
    if not (mp and ms):
        return len(slots), pitch, False
    ok = (DESKW_VERIFY_PITCH[0] * mp <= pitch <= DESKW_VERIFY_PITCH[1] * mp
          and abs(len(slots) - ms) <= DESKW_VERIFY_SLOT * ms)
    return len(slots), pitch, bool(ok)


def choose_deskew_angle(gray: np.ndarray, med: dict, candidates: list | None = None
                        ) -> tuple:
    """候选角逐个「转正→结构校验」，返回 (首个通过角|None, tried, curve)。

    谐波回退：方差最高峰若把列距带偏到 2× 谐波（p247/p347 型：校正后 pitch 104/105），
    结构校验不过 → 自动退到曲线次峰继续试；全不过返回 None（维持原结果，不动基础缓存）。
    """
    curve = None
    if candidates is None:
        _a, curve = estimate_skew(gray)
        candidates = skew_peaks(curve)
    tried = []
    for cand in candidates:
        ang = cand[0] if isinstance(cand, (tuple, list)) else float(cand)
        s, p, ok = deskew_verify(rotate_gray(gray, ang), med)
        tried.append({"angle": ang, "slots": s, "pitch": p, "ok": bool(ok)})
        if ok:
            return ang, tried, curve
    return None, tried, curve


def page_scan_deskew(engine, gray: np.ndarray, ears=None, med: dict | None = None,
                     page: int = 0) -> tuple:
    """触发页专用：找角（含谐波回退）→ 转正 → 走原 page_scan。返回 (rec|None, report)。

    rec 与基础管线同构（可直接装配），diag.deskew 记录角度/曲线/逐峰校验，页级可复查；
    无通过角时 rec=None，调用方维持原结果。
    """
    t0 = time.monotonic()
    ang, tried, curve = choose_deskew_angle(gray, med or {})
    report = {"version": DESKW_VERSION, "angle": ang, "candidates": tried,
              "seconds_search": round(time.monotonic() - t0, 2)}
    if ang is None:
        return None, report
    g2 = rotate_gray(gray, ang)
    res = page_scan(engine, g2, ears)
    res["page"] = int(page)
    res["cached"] = False
    res["deskewed"] = True
    res["diag"]["deskew"] = {"angle": ang, "curve": curve, "candidates": tried,
                             "seconds_search": report["seconds_search"],
                             "version": DESKW_VERSION}
    return res, report


# ---------------------------------------------------------------- 有状态整页识别（带缓存）

def _sha1_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def cache_key(pdf_sha1: str, page: int, scale: float, param_version: str = PARAM_VERSION) -> str:
    raw = "%s|%s|%.3f|%s" % (pdf_sha1, int(page), float(scale), param_version)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class VerticalOCR:
    """整本复用一个实例：pdfium 文档与 RapidOCR 引擎只开一次，页级结果可落缓存。

    用法：
        v = VerticalOCR(pdf, scale=3.0, ears=["某城风物志"])
        r = v.recognize(41)                      # 单页
        r = v.recognize(41, cache_dir=cache)     # 命中 book_dir\\.ocr_cache 后跳过识别
        v.close()
    """

    def __init__(self, pdf, scale: float = DEFAULT_SCALE, ears=None, model_path=None,
                 cache_dir=None, intra_op_num_threads: int = 4, param_version: str = PARAM_VERSION):
        self.pdf = str(Path(pdf).resolve())
        if not Path(self.pdf).is_file():
            raise FileNotFoundError("PDF 不存在：%s" % self.pdf)
        self.scale = float(scale)
        self.ears = [e.strip() for e in (ears or []) if e and e.strip()]
        self.model_path = str(resolve_model(model_path))
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.param_version = param_version
        self.intra = intra_op_num_threads
        self._pdf_doc = None
        self._engine = None
        self._digest = None
        self.pages = None

    # -- 资源 --------------------------------------------------------
    def pdf_digest(self) -> str:
        if self._digest is None:
            self._digest = _sha1_file(self.pdf)
        return self._digest

    def _doc(self):
        if self._pdf_doc is None:
            import pypdfium2 as pdfium
            self._pdf_doc = pdfium.PdfDocument(self.pdf)
            self.pages = len(self._pdf_doc)
        return self._pdf_doc

    def engine(self):
        if self._engine is None:
            self._engine = make_engine(self.model_path, self.intra)
        return self._engine

    def render(self, page: int) -> np.ndarray:
        doc = self._doc()
        return np.array(doc[page - 1].render(scale=self.scale).to_pil().convert("L"))

    def close(self):
        if self._pdf_doc is not None:
            try:
                self._pdf_doc.close()
            except Exception:
                pass
            self._pdf_doc = None
        self._engine = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 缓存 --------------------------------------------------------
    def cache_key(self, page: int, variant: str = "") -> str:
        """variant="" → 基础键（PARAM_VERSION，与既有 .ocr_cache 全兼容）；
        variant=DESKW_VERSION → 页级去倾斜变体子键，与基础缓存互不覆盖。"""
        pv = "%s+%s" % (self.param_version, variant) if variant else self.param_version
        return cache_key(self.pdf_digest(), page, self.scale, pv)

    def _cache_path(self, key: str) -> Path:
        return Path(self.cache_dir) / ("%s.json" % key)

    def load_cache(self, page: int, variant: str = ""):
        if not self.cache_dir:
            return None
        key = self.cache_key(page, variant)
        p = self._cache_path(key)
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if obj.get("key") != key:
            return None
        res = obj.get("result")
        return res if isinstance(res, dict) and "diag" in res else None

    def save_cache(self, page: int, result: dict, variant: str = "") -> None:
        if not self.cache_dir:
            return
        key = self.cache_key(page, variant)
        pv = "%s+%s" % (self.param_version, variant) if variant else self.param_version
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        p = self._cache_path(key)
        tmp = p.with_suffix(".json.tmp")
        rec = {"key": key, "page": int(page), "scale": self.scale,
               "paramVersion": pv, "pdfSha1": self.pdf_digest(),
               "model": os.path.basename(self.model_path),
               "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
               "result": result}
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    # -- 识别 --------------------------------------------------------
    def recognize(self, page: int, use_cache: bool = True) -> dict:
        """识别单页。命中缓存时完全不渲染、不跑模型。"""
        if use_cache:
            hit = self.load_cache(page)
            if hit is not None:
                hit = dict(hit)
                hit["cached"] = True
                return hit
        gray = self.render(page)
        res = page_scan(self.engine(), gray, self.ears)
        res["page"] = int(page)
        res["cached"] = False
        res["diag"]["seconds"] = res["diag"].get("seconds", 0.0)
        if use_cache:
            self.save_cache(page, res)
        return res

    def recognize_deskew(self, page: int, med: dict, use_cache: bool = True) -> dict:
        """页级去倾斜二道识别（仅对 deskew_trigger 命中的页调用）。

        缓存纪律：本方法**从不读写基础键**——命中变体子键直接返回；未命中则
        找角（含 2× 谐波回退）→ 转正 → 原 page_scan → 结果写变体子键。
        校验全不过时把「原结果 + deskewAttempt 记录」也落变体子键，重跑不再搜角。
        非触发页返回原 rec 原样（调用方一般不会把非触发页送进来，此为保险）。
        """
        if use_cache:
            hit = self.load_cache(page, variant=DESKW_VERSION)
            if hit is not None:
                hit = dict(hit)
                hit["cached"] = True
                return hit
        base = self.recognize(page, use_cache=use_cache)
        if not deskew_trigger(base.get("diag") or {}, med):
            return base
        gray = self.render(page)
        res, report = page_scan_deskew(self.engine(), gray, self.ears, med, page=page)
        if res is None:
            out = dict(base)
            out["diag"] = dict(base["diag"])
            out["diag"]["deskewAttempt"] = report
            out["deskewed"] = False
            out["cached"] = False
            if use_cache:
                self.save_cache(page, out, variant=DESKW_VERSION)
            return out
        if use_cache:
            self.save_cache(page, res, variant=DESKW_VERSION)
        return res

    def recognize_safe(self, page: int, use_cache: bool = True) -> dict:
        """识别单页；整页抛错时返回带 error 的诊断结构，不中断整本。"""
        try:
            return self.recognize(page, use_cache=use_cache)
        except Exception as e:
            import traceback
            return {"page": int(page), "text": "", "columns": [], "cached": False,
                    "diag": {"error": "%s: %s" % (type(e).__name__, e),
                             "traceback": traceback.format_exc()[-1500:],
                             "slots": 0, "pitch": 0, "han": 0, "punct": 0,
                             "punct_per100": 0.0, "bad": 0, "bad_list": [],
                             "suspect": 0, "corrections": 0, "resplit": 0,
                             "glyphs": 0, "heads": [], "other": {}, "h_med": 0,
                             "h_small_share": 0.0, "col_errors": [],
                             "seconds": 0.0, "flags": ["抛错"]}}