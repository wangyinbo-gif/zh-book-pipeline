# -*- coding: utf-8 -*-
"""竖排整本 → book.json（S2 生产模块）

输入：扫描件 PDF（竖排右起的线装/影印古籍，实测一本 200–820 页）
输出：与现有一致 schema 的 book.json + 页级诊断/竖排质检/OCR 行缓存

装配规则（只在竖排分支生效，不影响横排 12 项 gate 语义）：
  1) 逐页识别（tools/vertical_ocr.py，页级缓存 book_dir\\.ocr_cache\\）
  2) 页级合理性闸：零字/低字（极低字+低字）/无句读/抛错页全部记录进 竖排质检.json，
     分「硬异常 / 参考（前置页、原书空白页、卷首页）」两级，不静默通过
  3) 版心剔行：页首 running head（书名 + 卷之N + 卷名 + 印刷页码）与页尾残留剔除
  4) 成段：栏右→左、列内上→下；列末无句末标点就跨栏/跨页续接；够长且句末才断段
  5) 章结构：页首「卷之N / 卷N」running head 定卷 → chapters（含 startPage/chars）；
     卷首页（「书名 + 卷之五」+ 篇名列表）出 heading + 篇目 note
  6) 目录页（總目/总目）、封面/空白页剔除并写进 _fix.removedTocPages / droppedPages
  7) stats 字段齐全（pages/chars/paragraphs/figures/paired/chapters），避免 gate 误判

用法：
  python tools/vertical_book.py --pdf "inbox/scan.pdf" --out "books/<书目录>" --title 书名
  python tools/vertical_book.py --pdf ... --out ... --pages 21-30      # 冒烟
  python tools/vertical_book.py --pdf ... --out ... --ear 书名,书名末三字
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vertical_ocr as VO  # noqa: E402

# ---------------- 装配参数（改这些要同步递增 vertical_ocr.PARAM_VERSION 之外的版本号） ----------------
ASSEMBLY_VERSION = "vertical-book-v1"
FRONT_DROP_HAN = 30       # 前置页汉字 < 30：封面/空白/涂鸦页，剔除（记录）
TITLE_PAGE_HAN = 160      # 卷首页汉字上限（卷首页 = 卷标 + 篇名列表，字少、几乎无句读）
TITLE_PAGE_MIN_HAN = 15
TITLE_PAGE_PUNCT = 8
MIN_PARA = 200            # 段落缓冲 ≥ 该长度且列末有句末标点 → 断段
PAGE_FLUSH = 600          # 页末缓冲 ≥ 该长度强制断段（避免段落无限长）
TOC_MARKERS = ("總目", "总目")

CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}
CN_NUM = "〇零一二三四五六七八九十百"
VOL_RE = re.compile(r"^卷\s*之?\s*([一二三四五六七八九十])")


# ---------------------------------------------------------------- 中文数字

def cn_digits_to_int(s: str):
    """竖排印刷页码的逐位写法：一四→14、二五→25、一〇→10；单字 十→10。

    只处理 1–99 的逐位/个位写法（版心页码够用），不可解析返回 None。
    """
    s = (s or "").strip()
    if not s or any(c not in CN_NUM for c in s):
        return None
    if len(s) == 1:
        return CN_DIGITS.get(s[0], 10) if s in ("十",) else CN_DIGITS.get(s)
    if len(s) == 2:
        if s[0] == "十":
            return 10 + CN_DIGITS.get(s[1], 0)
        a, b = CN_DIGITS.get(s[0]), CN_DIGITS.get(s[1])
        return None if a is None or b is None else a * 10 + b
    return None


def int_to_cn(n: int) -> str:
    d = "〇一二三四五六七八九"
    if n <= 10:
        return "十" if n == 10 else d[n]
    if n < 20:
        return "十" + d[n - 10]
    if n < 100:
        return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")
    return str(n)


def parse_volume_head(text: str):
    """版心「卷之N 卷名 页码」→ (num, name)。

    num 只取单字数字（多卷书实测均为「卷之一…卷之八」）；
    name 去掉尾部页码/噪声（「卷之一城北内外一一一」→ 城北内外）。
    """
    t = (text or "").strip()
    m = VOL_RE.match(t)
    if not m:
        return None
    num = CN_DIGITS.get(m.group(1))
    if not num:
        return None
    rest = t[m.end():]
    rest = re.sub(r"[\s:：.。,，、·\-—■_!！?？…;；]+$", "", rest)
    rest = re.sub(r"[一二三四五六七八九十百0-9]{1,4}$", "", rest)
    rest = re.sub(r"[\s:：.。,，、·\-—■_!！?？…;；]+", "", rest)
    name = rest[:8]
    if len(name) < 2 or not all(VO.is_cjk(c) for c in name):
        name = ""
    return num, name


def explicit_volume(rec: dict):
    """该页显式带出的卷号：优先版心卷标列，其次正文前 20 字里的「卷之N」。"""
    for h in (rec["diag"].get("heads") or []):
        v = parse_volume_head(h)
        if v:
            return v
    m = re.search(r"卷\s*之?\s*([一二三四五六七八九十])", (rec.get("text") or "")[:20])
    if m:
        num = CN_DIGITS.get(m.group(1))
        if num:
            return num, ""
    return None


# ---------------------------------------------------------------- 版心剔行

def digitwise_cn(n: int) -> str:
    """印刷页码的逐位写法：14→一四、25→二五、4→四、10→一〇。"""
    d = "〇一二三四五六七八九"
    if n < 1 or n > 999:
        return ""
    if n < 10:
        return d[n]
    return "".join(d[int(ch)] for ch in str(n))


def strip_running_head(text: str, cores, vol_names, expect_num=None):
    """剔页首 running head（OCR 变体书名 + 卷之N + 卷名 + 印刷页码）。

    返回 (清理后文本, 解析到的印刷页码或 None, 是否命中版心)。
    只在页首前几列调用；命中要求书名核心出现在前 5 字内，避免误吃正文。
    expect_num 已知该页印刷页码时按精确数字剔，避免把「十刹海」的「十」当页码吃掉。
    """
    if not text:
        return text, None, False
    t = text.lstrip(" 　")
    for core in cores:
        idx = t.find(core)
        if not (0 <= idx <= 4):
            continue
        rest = t[idx + len(core):]
        num = None
        rest = re.sub(r"^[\s:：.。,，、·\-—■_]+", "", rest)
        m = re.match(r"^卷\s*之?\s*([一二三四五六七八九十])", rest)
        if m:
            rest = rest[m.end():]
            rest = re.sub(r"^[\s:：.。,，、·\-—■_]+", "", rest)
        for vn in sorted(vol_names, key=len, reverse=True):
            if vn and rest.startswith(vn):
                rest = rest[len(vn):]
                break
        stripped = False
        if expect_num is not None:
            form = digitwise_cn(expect_num)
            if rest.startswith(form):
                rest = rest[len(form):]
                num = expect_num
                stripped = True
        if not stripped:
            m = re.match(r"^[\s:：.。,，、·\-—■_]*([一二三四五六七八九十百0-9]{1,4})?"
                         r"[\s:：.。,，、·\-—■_]*", rest)
            if m:
                if m.group(1):
                    num = cn_digits_to_int(m.group(1))
                    if num is None and m.group(1).isdigit():
                        num = int(m.group(1))
                rest = rest[m.end():]
        return rest, num, True
    return text, None, False


def strip_running_foot(text: str, phrases) -> str:
    """剔页尾残留版心（「书名 + 卷之一」之类），只在尾 16 字内命中才动。

    phrases 用完整书名/书耳（不用 3 字核心），避免把书名残字留在正文里。
    """
    if not text or len(text) < 4:
        return text
    tail = text[-16:]
    for ph in sorted(phrases, key=len, reverse=True):
        if len(ph or "") < 2:
            continue
        idx = tail.find(ph)
        if idx < 0:
            continue
        cut = len(text) - len(tail) + idx
        # 尾部最多再允许 14 字噪声（卷之N/页码/卷名残字）
        if len(text) - cut <= 14:
            return text[:cut].rstrip(" 　:：.。,，、·-—■_")
    return text


def clean_page_body(rec: dict, cores, phrases, vol_names, offset=None, ear_keys=()) -> list[str]:
    """把一页的正文列（右→左）洗成可入段落的列文本列表。

    offset 为学到的印刷页偏移；已知该页印刷页码时按精确数字剔版心页码，
    避免把「十刹海」的「十」当页码吃掉。
    """
    expect = (rec["page"] - offset) if offset else None
    if expect is not None and expect < 1:
        expect = None
    cols = sorted([c for c in (rec.get("columns") or []) if c.get("kind") == "正文"],
                  key=lambda c: -c["x0"])
    out: list[str] = []
    header_done = False
    header_only = False     # 上一列是纯书名版心 → 本列可能以印刷页码开头
    for i, c in enumerate(cols):
        t = (c.get("text") or "").strip()
        if not t:
            continue
        # 纯版心列（书名+页码）与页脚列（卷N+页码）：整列不是正文
        if ear_keys and len(t) <= 12:
            k = _ear_key(t)
            if any(k.startswith(ek) for _ear, ek in ear_keys):
                continue
        if len(t) <= 10 and re.match(
                r"^卷\s*[一二三四五六七八九十]{1,3}[一二三四五六七八九十0-9〇=．.．\-—_■|]{0,5}$", t):
            continue
        if not header_done:
            t2, num, hit = strip_running_head(t, cores, vol_names, expect_num=expect)
            header_done = True
            if hit:
                if t2:
                    out.append(t2)
                else:
                    header_only = True
                continue
        if header_only and not out and i < 3:
            # 书名独立成列，下一列以印刷页码开头（如「四必繇太學」）
            if expect is not None:
                form = digitwise_cn(expect)
                if form and t.startswith(form):
                    t = t[len(form):]
            else:
                t = re.sub(r"^[\s:：.。,，、·\-—■_]*[一二三四五六七八九十百0-9]{1,4}"
                           r"[\s:：.。,，、·\-—■_]*", "", t)
            header_only = False
        if t:
            if not out and i < 3:
                # 页首/卷首列：去掉重复卷名与残留杂标（「．，麻·城·劉」→「麻·城·劉」）
                for vn in sorted(vol_names, key=len, reverse=True):
                    if vn and t.startswith(vn):
                        t = t[len(vn):]
                        break
                t = t.lstrip(" \u3000:：.．。,，、；;·-—■_）」』】〕")
            if t:
                out.append(t)
    if out:
        out[-1] = strip_running_foot(out[-1], phrases)
    return [t for t in out if t]


# ---------------------------------------------------------------- 页级闸

HARD_FLAGS = {"抛错", "零字", "极低字", "低字", "无句读", "句读过密",
              "列距异常", "栏数异常", "无列槽"}


def is_toc_page(rec: dict) -> bool:
    """目录页：有「總目」抬头，且是篇目清单（几乎没有句读）。

    正文里顺带提到「四庫全書總目」「續修四庫全書總目提要」的页有大量句号逗号，不能当目录剔掉。
    """
    t = rec.get("text") or ""
    if not any(k in t for k in TOC_MARKERS):
        return False
    return t.count("。") <= 2 and t.count("，") <= 4


def is_title_page(rec: dict) -> bool:
    d = rec["diag"]
    han, punct = d.get("han", 0), d.get("punct", 0)
    if d.get("error") is not None or not (TITLE_PAGE_MIN_HAN <= han <= TITLE_PAGE_HAN):
        return False
    if punct > TITLE_PAGE_PUNCT:
        return False
    if explicit_volume(rec):
        return True
    # 有的卷首页版心列被当成正文（无「卷标」列），靠「卷之N」字样兜底识别
    return bool(re.search(r"卷\s*之?\s*[一二三四五六七八九十]", rec.get("text") or ""))


def page_gate(recs: list[dict], body_start: int, toc_pages: set, title_pages: set,
              dropped_cover: set) -> dict:
    """页级合理性闸：分级记录所有异常页（不静默通过）。"""
    pages = {r["page"] for r in recs}
    issues, hard = [], []
    counts = {}
    for r in recs:
        d = r["diag"]
        flags = list(d.get("flags") or [])
        if not flags:
            continue
        pg = r["page"]
        for f in flags:
            counts[f] = counts.get(f, 0) + 1
        severity, note = "观察", ""
        if pg in toc_pages:
            severity, note = "参考", "目录页（已剔除）"
        elif pg in dropped_cover:
            severity, note = "参考", "封面/空白/涂鸦页（已剔除）"
        elif pg < body_start:
            severity, note = "参考", "前置页（版权/序）"
        elif pg in title_pages:
            severity, note = "参考", "卷首页（卷标+篇目）"
        elif "抛错" in flags:
            severity, note = "硬", "整页识别抛错"
        elif "零字" in flags:
            prev_ok = any(x["page"] == pg - 1 and x["diag"].get("han", 0) > 50 for x in recs)
            next_ok = any(x["page"] == pg + 1 and x["diag"].get("han", 0) > 50 for x in recs)
            severity = "参考" if (prev_ok or next_ok) else "硬"
            note = "原书空白页（前后有正文）" if severity == "参考" else "零字页"
        else:
            hit = sorted(HARD_FLAGS & set(flags))
            if hit:
                severity, note = "硬", "、".join(hit)
            else:
                note = "观察项：" + "、".join(flags)
        item = {"page": pg, "flags": flags, "severity": severity, "note": note,
                "han": d.get("han", 0), "punct": d.get("punct", 0),
                "slots": d.get("slots", 0), "error": d.get("error", "")}
        issues.append(item)
        if severity == "硬":
            hard.append(pg)
    return {"issues": issues, "hardPages": sorted(hard),
            "hardCount": len(hard), "issueCount": len(issues), "flagCounts": counts,
            "pagesTotal": len(pages)}


# ---------------------------------------------------------------- 装配

def _name_key(name: str) -> str:
    """卷名归一：去掉版心误读带出的前导数字，再正俗归一，用于聚类比较。"""
    n = (name or "").strip()
    if len(n) >= 3:
        n = re.sub(r"^[一二三四五六七八九十]", "", n)
    return VO.norm_variants(n)


def _prefer_clean_name(counter: dict) -> str:
    """同一卷名的多个 OCR 写法里，优先不带前导误读数字的那个。"""
    clean = {n: c for n, c in counter.items()
             if len(n) < 3 or not re.match(r"^[一二三四五六七八九十]", n)}
    if clean:
        return max(clean, key=clean.get)
    n = max(counter, key=counter.get)
    return re.sub(r"^[一二三四五六七八九十]", "", n)


def canon_volume_name(raw_names) -> str:
    """一组卷名候选 → 众数（同一卷名的 OCR 变体聚成一类）。"""
    groups: dict[str, dict] = {}
    for n in raw_names:
        n = (n or "").strip()
        if len(n) < 2 or not all(VO.is_cjk(c) for c in n):
            continue
        groups.setdefault(_name_key(n), {})
        groups[_name_key(n)][n] = groups[_name_key(n)].get(n, 0) + 1
    if not groups:
        return ""
    best = max(groups.values(), key=lambda d: sum(d.values()))
    return _prefer_clean_name(best)


def all_volume_names(raw_names) -> list[str]:
    groups: dict[str, dict] = {}
    for n in raw_names:
        n = (n or "").strip()
        if len(n) < 2 or not all(VO.is_cjk(c) for c in n):
            continue
        groups.setdefault(_name_key(n), {})
        groups[_name_key(n)][n] = groups[_name_key(n)].get(n, 0) + 1
    return [_prefer_clean_name(d) for d in groups.values()]


def _ear_key(s: str) -> str:
    """书耳/书名比对键：菴↔庵 互认 + 异体归一。"""
    return VO.norm_variants((s or "").replace("菴", "庵"))


_EAR_SWAP = {"庵": "菴", "菴": "庵", "夢": "梦", "梦": "夢", "憶": "忆", "忆": "憶",
             "尋": "寻", "寻": "尋", "記": "记", "记": "記", "樓": "楼", "楼": "樓"}


def ear_variants(ear: str) -> list[str]:
    """同一书名的常见异写组合（用于剔版心/认书名；不影响正文输出）。"""
    out = [ear]
    queue = [ear]
    while queue:
        s = queue.pop()
        for i, ch in enumerate(s):
            if ch not in _EAR_SWAP:
                continue
            v = s[:i] + _EAR_SWAP[ch] + s[i + 1:]
            if v not in out:
                out.append(v)
                queue.append(v)
    return out


def is_toc_like(rec: dict, min_han: int = 20, ratio: float = 0.45,
                min_colon: int = 12) -> bool:
    """无「總目」抬头的目录页：点线引导（：：：）使句读密度异常高。"""
    d = rec.get("diag") or {}
    han = d.get("han", 0)
    if han < min_han:
        return False
    t = rec.get("text") or ""
    return (d.get("punct", 0) >= ratio * han
            and (t.count("：") + t.count(":")) >= min_colon)


def page_work(rec: dict, ear_keys) -> str:
    """本页版心/正文列里能认出的书耳（用于多部合装的部归属）。"""
    for c in rec.get("columns") or []:
        t = (c.get("text") or "").strip()
        if not t:
            continue
        k = _ear_key(t[:16])
        for ear, ek in ear_keys:
            if k.startswith(ek):
                return ear
    return None


def match_colophon(rec: dict, ear_keys) -> str:
    """卷端页：某列以「书名 + 卷」开头（如「南菴夢記卷一某郡某氏編」）。

    只认「书名紧跟卷字」，避免把「某书五卷，康熙刻」这类版权说明误当卷端。
    """
    for c in rec.get("columns") or []:
        t = (c.get("text") or "").strip()
        if not t:
            continue
        k = _ear_key(t)
        for ear, ek in ear_keys:
            if not k.startswith(ek):
                continue
            rest = t[len(ear):].lstrip(" 　:：．.·—_-■")
            if not rest.startswith("卷"):
                continue
            # 卷端页的题署列很短（卷N + 编者名）；正文里「卷末所附…跋也…」这类长句不算
            if len(rest) > 16:
                continue
            if not re.search(r"[一二三四五六七八九十〇0-9]", rest[:8]):
                continue
            return ear
    return None


def _flush_para(state: dict, blocks: list) -> None:
    if not state["buf"]:
        return
    text = "".join(state["buf"])
    if text.strip():
        text = text.lstrip(" \u3000:：.．。,，、；;·-—■_）」』】〕")
    if text.strip():
        blk = {"type": "paragraph", "page": state["start"], "text": text}
        if len(state["pages"]) > 1:
            blk["pages"] = sorted(state["pages"])
        blocks.append(blk)
    state["buf"] = []
    state["pages"] = set()
    state["start"] = None


def _add_text(state: dict, text: str, page: int, blocks: list) -> None:
    if not text:
        return
    if not state["buf"]:
        state["start"] = page
    state["buf"].append(text)
    state["pages"].add(page)


def assemble(recs: list[dict], total_pages: int, title: str, book_title: str,
             pdf_name: str, params: dict) -> tuple[dict, dict]:
    """页级结果 → (book.json, 竖排质检.json)。"""
    recs = sorted(recs, key=lambda r: r["page"])
    ears = params.get("ears") or params.get("headCores") or params.get("head_cores") or []
    ear_keys = [(e, _ear_key(e)) for e in ears if len(e) >= 2]
    cores = []
    for e in ears:
        cores.extend(ear_variants(e))
    if not cores:
        cores = [c for c in (params.get("headCores") or params.get("head_cores") or []) if c]
    phrases = params.get("headPhrases") or params.get("head_phrases") or list(cores)
    phrases = sorted({p for e in phrases for p in [e] + (ear_variants(e) if ear_keys else [])},
                     key=len, reverse=True)
    toc_pages = {r["page"] for r in recs if is_toc_page(r)}
    # ---- 部归属：多部合装（两部各若干卷合订一本）按版心能认出的书名前向填充
    work_of: dict[int, str] = {}
    cur_work = None
    for r in recs:
        w = page_work(r, ear_keys) if ear_keys else None
        if w:
            cur_work = w
        work_of[r["page"]] = cur_work
    works: list[str] = []
    for r in recs:
        w = work_of.get(r["page"])
        if w and w not in works:
            works.append(w)
    multi = len(works) > 1
    if multi:
        # 多部合装需要每部都能找到卷端页，否则退回单部逻辑（不硬凑）
        colo_works = {w for r in recs if (w := match_colophon(r, ear_keys))}
        if len(colo_works) < 2:
            multi = False
    if multi:
        toc_pages |= {r["page"] for r in recs if is_toc_like(r)}
    # ---- 卷号/卷名统计（目录页不参与投票）
    name_votes: dict[int, dict[str, int]] = {}
    for r in recs:
        if r["page"] in toc_pages:
            continue
        v = explicit_volume(r)
        if not v:
            continue
        num, name = v
        if name:
            name_votes.setdefault(num, {})
            name_votes[num][name] = name_votes[num].get(name, 0) + 1
    raw_names = [n for votes in name_votes.values() for n in votes]
    all_names = sorted(all_volume_names(raw_names), key=len, reverse=True)

    # ---- body_start：单部书=首个带卷号页；多部合装=首个卷端页
    if multi:
        colo_pages = [r["page"] for r in recs if match_colophon(r, ear_keys)]
        body_start = min(colo_pages) if colo_pages else 1
        title_pages = set()
    else:
        vol_pages = [r["page"] for r in recs
                     if r["page"] not in toc_pages and explicit_volume(r)]
        body_start = min(vol_pages) if vol_pages else 1
        title_pages = {r["page"] for r in recs if is_title_page(r) and r["page"] >= body_start}
    p_offset = learn_page_offset(recs)      # 印刷页偏移（pdf页 - 印刷页），用于精确剔版心页码

    # ---- 前置页：封面/空白剔除、目录剔除、其余进「卷首」章
    front = [r for r in recs if r["page"] < body_start and r["page"] not in toc_pages]
    dropped_cover = {r["page"] for r in front if r["diag"].get("han", 0) < FRONT_DROP_HAN}
    front_keep = [r for r in front if r["page"] not in dropped_cover]

    blocks: list[dict] = []
    chapters: list[dict] = []
    state = {"buf": [], "pages": set(), "start": None}

    def new_chapter(ch_title: str, start_page) -> dict:
        _flush_para(state, blocks)
        ci = len(blocks)
        head = {"type": "heading", "page": start_page, "text": ch_title}
        blocks.append(head)
        ch = {"id": "ch-%d" % (len(chapters) + 1), "title": ch_title,
              "startPage": start_page, "startBlock": ci, "blocks": []}
        chapters.append(ch)
        return ch

    # ---- 卷首
    if front_keep:
        ch = new_chapter("卷首", front_keep[0]["page"])
        for r in front_keep:
            for t in clean_page_body(r, cores, phrases, all_names, p_offset, ear_keys):
                _add_text(state, t, r["page"], blocks)
                if t and t[-1] in VO.TERM_PUNCT and len("".join(state["buf"])) >= MIN_PARA:
                    _flush_para(state, blocks)
            if len("".join(state["buf"])) >= PAGE_FLUSH:
                _flush_para(state, blocks)
        ch["_front"] = True

    # ---- 正文
    body = [r for r in recs if r["page"] >= body_start and r["page"] not in toc_pages]
    body_by_page = {r["page"]: r for r in body}

    def add_page_flow(pages) -> None:
        for r in pages:
            for t in clean_page_body(r, cores, phrases, all_names, p_offset, ear_keys):
                _add_text(state, t, r["page"], blocks)
                if t and t[-1] in VO.TERM_PUNCT and len("".join(state["buf"])) >= MIN_PARA:
                    _flush_para(state, blocks)
            if len("".join(state["buf"])) >= PAGE_FLUSH:
                _flush_para(state, blocks)

    anchor_info, digit_warnings = [], []
    if multi:
        # ---- 多部合装（两部合订）：卷端页「书名+卷」定章，部内卷号重启。
        # 版心卷号 OCR 常把 三/六 读丢或读错（实测 8 卷只认出 6 个数字），所以序号按卷端页顺序，
        # 版心数字只做交叉校验（写进 qc.volumeDigitWarnings，不静默）。
        anchors = []
        for r in body:
            w = match_colophon(r, ear_keys)
            if w:
                anchors.append((r["page"], w))
        merged = []
        for pg, w in anchors:
            if merged and pg - merged[-1]["page"] <= 2 and w == merged[-1]["work"]:
                continue
            merged.append({"page": pg, "work": w})
        work_pages: dict[str, list[int]] = {}
        for r in body:
            w = work_of.get(r["page"])
            if w:
                work_pages.setdefault(w, []).append(r["page"])
        plan: list[tuple[str, list[dict]]] = []
        # 部起点：上一部结束后的空白段之后，本部的整理前言/序并入本部「卷首」；
        # 部终点 = 下一部起点 - 1（避免同一页被两个部重复收进正文）。
        w_start_map: dict[str, int] = {}
        for wi, w in enumerate(works):
            wps = sorted(work_pages.get(w, []))
            if not wps:
                continue
            s = wps[0]
            if wi > 0:
                cand = s - 1
                if (cand in body_by_page and work_of.get(cand) != w
                        and body_by_page[cand]["diag"].get("han", 0) >= FRONT_DROP_HAN):
                    k, nb = cand - 1, 0
                    while (k >= body_start and k in body_by_page
                           and body_by_page[k]["diag"].get("han", 0) < FRONT_DROP_HAN):
                        nb += 1
                        k -= 1
                    if nb >= 3:
                        s = cand
            w_start_map[w] = s
        for wi, w in enumerate(works):
            wa = [a for a in merged if a["work"] == w]
            wps = sorted(work_pages.get(w, []))
            if not wa or not wps:
                continue
            w_start = w_start_map.get(w, wps[0])
            w_end = (w_start_map[works[wi + 1]] - 1) if wi + 1 < len(works) else total_pages
            w_end = max(w_end, wa[-1]["page"])
            if wi > 0:
                pre = [body_by_page[p] for p in range(w_start, wa[0]["page"]) if p in body_by_page]
                if pre:
                    plan.append((w + " 卷首", pre))
            for k, a in enumerate(wa):
                startp = a["page"]
                endp = wa[k + 1]["page"] - 1 if k + 1 < len(wa) else w_end
                pages = [body_by_page[p] for p in range(startp, endp + 1) if p in body_by_page]
                if k == len(wa) - 1:
                    split_bu, split_fu = None, None
                    for r in pages:
                        if r["page"] <= startp + 1:
                            continue
                        cl = clean_page_body(r, cores, phrases, all_names, p_offset, ear_keys)
                        if not cl:
                            continue
                        head = cl[0]
                        if split_bu is None and head.startswith(("補遺", "补遗")):
                            split_bu = r["page"]
                        elif split_fu is None and head.startswith(("附錄", "附録", "附录", "跋")):
                            split_fu = r["page"]
                    if split_fu or split_bu:
                        # 卷内尾部：…卷N 正文 | [補遺] | [附錄]
                        cuts = sorted({p for p in (split_bu, split_fu) if p})
                        segs = []
                        prev_cut = startp
                        for cut in cuts:
                            segs.append((prev_cut, cut))
                            prev_cut = cut
                        segs.append((prev_cut, None))
                        for si, (a0, a1) in enumerate(segs):
                            seg = [r for r in pages
                                   if r["page"] >= a0 and (a1 is None or r["page"] < a1)]
                            if not seg:
                                continue
                            if si == 0:
                                title = w + " 卷%s" % int_to_cn(k + 1)
                            elif a0 == split_bu:
                                title = w + " 補遺"
                            else:
                                title = w + " 附錄"
                            plan.append((title, seg))
                        continue
                plan.append((w + " 卷%s" % int_to_cn(k + 1), pages))
        for ch_title, pages in plan:
            if not pages:
                continue
            new_chapter(ch_title, pages[0]["page"])
            add_page_flow(pages)
            _flush_para(state, blocks)
            digits: dict[int, int] = {}
            for r in pages:
                for h in (r["diag"].get("heads") or []):
                    v = parse_volume_head(h)
                    if v:
                        digits[v[0]] = digits.get(v[0], 0) + 1
            m = re.search(r"卷\s*([一二三四五六七八九十]+)$", ch_title)
            if digits and m:
                exp = cn_digits_to_int(m.group(1))
                if exp and max(digits, key=digits.get) != exp:
                    digit_warnings.append({"chapter": len(anchor_info) + 1, "title": ch_title,
                                           "startPage": pages[0]["page"], "digitVotes": digits})
            anchor_info.append({"chapter": len(anchor_info) + 1, "title": ch_title,
                                "startPage": pages[0]["page"], "endPage": pages[-1]["page"],
                                "digitVotes": digits})
        _flush_para(state, blocks)
    else:
        # ---- 单部书：卷首页簇定章
        # 逐页「卷之N」不可靠：整本实测版心常把 二/三 读成「一一/二」，按数字切会碎成十几章。
        # 稳定锚点是每卷卷端的卷首页（低字、无句读、含卷标+篇目列表）：连续卷首页 = 一卷之始。
        clusters: list[list[int]] = []
        for r in body:
            if r["page"] not in title_pages:
                continue
            if clusters and r["page"] == clusters[-1][-1] + 1:
                clusters[-1].append(r["page"])
            else:
                clusters.append([r["page"]])

        if not clusters:
            # 没有卷首页：整体作为一章（记录在 qc.chapterAnchors，不静默）
            if body:
                new_chapter(title or "正文", body[0]["page"])
                add_page_flow(body)
            ranges: list[tuple[int, list[int], int]] = []
        else:
            if clusters[0][0] > body_start:
                pre = [body_by_page[p] for p in range(body_start, clusters[0][0]) if p in body_by_page]
                if pre and chapters and chapters[-1].get("_front"):
                    add_page_flow(pre)
            ranges = []
            for i, cl in enumerate(clusters):
                start = cl[0]
                end = clusters[i + 1][0] - 1 if i + 1 < len(clusters) else total_pages
                ranges.append((start, cl, end))

        for i, (start, cl, end) in enumerate(ranges):
            vnum = i + 1
            pages = [body_by_page[p] for p in range(start, end + 1) if p in body_by_page]
            heads_names, digits = [], {}
            for r in pages:
                for h in (r["diag"].get("heads") or []):
                    v = parse_volume_head(h)
                    if v:
                        if v[1]:
                            heads_names.append(v[1])
                        digits[v[0]] = digits.get(v[0], 0) + 1
            name = canon_volume_name(heads_names)
            if name and len(all_names) > 1:
                name = next((n for n in all_names if _name_key(n) == _name_key(name)), name)
            ch_title = ("卷%s %s" % (int_to_cn(vnum), name)).strip()
            if digits and max(digits, key=digits.get) != vnum:
                digit_warnings.append({"chapter": i + 1, "startPage": start, "title": ch_title,
                                       "digitVotes": digits})
            anchor_info.append({"chapter": i + 1, "startPage": start, "endPage": end,
                                "titlePageCount": len(cl), "title": ch_title,
                                "digitVotes": digits})
            lead_note = []
            for pg in cl:
                lead_note.extend(clean_page_body(body_by_page[pg], cores, phrases, all_names,
                                                 p_offset, ear_keys))
            rest = [r for r in pages if r["page"] not in set(cl)]
            new_chapter(ch_title, start)
            if lead_note:
                blocks.append({"type": "note", "page": start,
                               "text": "本卷篇目：" + "".join(lead_note)})
            add_page_flow(rest)
            _flush_para(state, blocks)
        _flush_para(state, blocks)

    # ---- 章节统计 + endBlock + 嵌套 blocks
    for i, ch in enumerate(chapters):
        nxt = chapters[i + 1]["startBlock"] if i + 1 < len(chapters) else len(blocks)
        end = nxt - 1
        ch["endBlock"] = end
        ch["blocks"] = blocks[ch["startBlock"]:end + 1]
        ch["chars"] = sum(len(b.get("text") or "") for b in ch["blocks"]
                          if b.get("type") == "paragraph")
        ch["figures"] = sum(1 for b in ch["blocks"] if b.get("type") == "figure")
    # 去掉内部标记
    for ch in chapters:
        ch.pop("_front", None)

    # ---- 页码映射（版心尾部数字 + 偶数页页首数字）
    printed = printed_pages(recs, offset=p_offset)

    # ---- 页级闸（在剔除信息齐备后做）
    gate = page_gate(recs, body_start, toc_pages, title_pages, dropped_cover)

    stats = {
        "pages": total_pages,
        "chars": sum(len(b.get("text") or "") for b in blocks if b.get("type") == "paragraph"),
        "paragraphs": sum(1 for b in blocks if b.get("type") == "paragraph"),
        "figures": sum(1 for b in blocks if b.get("type") == "figure"),
        "paired": 0,
        "chapters": len(chapters),
    }
    book = {
        "bookTitle": book_title,
        "title": title,
        "source": pdf_name,
        "pageRange": [1, total_pages],
        "printedPages": {str(k): v for k, v in sorted(printed.items())},
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": {"direction": "vertical", "note": "竖排右起；由 tools/vertical_book.py 装配"},
        "chapters": chapters,
        "blocks": blocks,
        "figureMap": {},
        "stats": stats,
        "_fix": {
            "verticalBook": ASSEMBLY_VERSION,
            "removedTocPages": sorted(toc_pages),
            "droppedPages": sorted(dropped_cover),
            "bodyStart": body_start,
            "titlePages": sorted(title_pages),
            "pageGate": {"hardCount": gate["hardCount"], "issueCount": gate["issueCount"],
                         "hardPages": gate["hardPages"], "flagCounts": gate["flagCounts"]},
            "printedPageCount": len(printed),
        },
    }
    qc = {
        "generatedAt": book["generatedAt"],
        "source": pdf_name,
        "pages": total_pages,
        "layout": "vertical",
        "params": params,
        "stats": stats,
        "chapters": [{"title": c["title"], "startPage": c["startPage"], "chars": c["chars"]}
                     for c in chapters],
        "chapterAnchors": anchor_info,
        "works": works if multi else [],
        "volumeDigitWarnings": digit_warnings,
        "bodyStart": body_start,
        "droppedPages": {"coverOrBlank": sorted(dropped_cover),
                         "toc": sorted(toc_pages),
                         "reason": "封面/空白按汉字<30 剔除；目录按「總目/总目」抬头剔除"},
        "pageGate": {"hardPages": gate["hardPages"], "hardCount": gate["hardCount"],
                     "issueCount": gate["issueCount"], "flagCounts": gate["flagCounts"],
                     "issues": gate["issues"]},
        "printedPageCount": len(printed),
        "verdict": "OK" if gate["hardCount"] == 0 else "需人工看",
    }
    return book, qc


def _page_number_pairs(recs: list[dict]) -> list[tuple[int, int, int]]:
    """(pdf页, 解析到的印刷页码, offset) 候选对，来自版心卷标尾数与页首数字。"""
    pairs: list[tuple[int, int, int]] = []      # (pdf, parsed, offset)
    for r in recs:
        nums = []
        for h in (r["diag"].get("heads") or []):
            m = re.search(r"([%s]{1,4})$" % CN_NUM, h.strip())
            if m:
                n = cn_digits_to_int(m.group(1))
                if n:
                    nums.append(n)
        m = re.search(r"([%s]{1,4})$" % CN_NUM, (r.get("text") or "")[:8])
        if m:
            n = cn_digits_to_int(m.group(1))
            if n:
                nums.append(n)
        for n in nums:
            off = r["page"] - n
            if 0 <= off <= 40:
                pairs.append((r["page"], n, off))
    return pairs


def learn_page_offset(recs: list[dict]):
    """印刷页偏移众数（pdf页 - 印刷页）；样本不足返回 None。"""
    pairs = _page_number_pairs(recs)
    if len(pairs) < 3:
        return None
    return statistics.mode([p[2] for p in pairs])


def printed_pages(recs: list[dict], offset=None) -> dict[int, int]:
    """版心数字 → 印刷页映射：由 offset 众数推算，逐页校验（±10/±100 修正误读）。"""
    pairs = _page_number_pairs(recs)
    if len(pairs) < 3:
        return {}
    if offset is None:
        offset = statistics.mode([p[2] for p in pairs])
    out: dict[int, int] = {}
    for pg, n, _off in pairs:
        expected = pg - offset
        if expected < 1:
            continue
        for k in (0, 10, 100):
            if n + k == expected or n - k == expected:
                out[pg] = expected
                break
    return out


# ---------------------------------------------------------------- 主流程

def parse_pages(spec: str, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), min(int(b), total) + 1))
        else:
            out.append(int(part))
    return sorted({p for p in out if 1 <= p <= total})


def write_outputs(out: Path, recs: list[dict], book: dict, qc: dict,
                  elapsed: float, timings: list[float], cache_hits: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "book.json").write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "book_raw.json").write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    diag = [{"page": r["page"], "text": r.get("text") or "", "diag": r["diag"]} for r in recs]
    (out / "页级诊断.json").write_text(json.dumps(diag, ensure_ascii=False, indent=1), encoding="utf-8")
    qc = dict(qc)
    qc["elapsedMinutes"] = round(elapsed / 60, 2)
    qc["pageSeconds"] = {"median": round(statistics.median(timings), 2) if timings else 0,
                         "p90": round(sorted(timings)[int(len(timings) * 0.9)] if timings else 0, 2)}
    qc["cacheHits"] = cache_hits
    (out / "竖排质检.json").write_text(json.dumps(qc, ensure_ascii=False, indent=1), encoding="utf-8")
    # OCR 行缓存（与 pdf2book 的 pages/page_NNN_ocr2.json 同格式，供 pipeline textcheck/coverage 复用）
    pages_dir = out / "pages"
    pages_dir.mkdir(exist_ok=True)
    for r in recs:
        lines = []
        for c in r.get("columns") or []:
            if c.get("kind") != "正文":
                continue
            t = (c.get("text") or "").strip()
            if not t:
                continue
            lines.append({"text": t, "x0": c.get("x0", 0), "x1": c.get("x1", 0),
                          "y0": 0, "y1": 0, "conf": 1.0})
        (pages_dir / ("page_%03d_ocr2.json" % r["page"])).write_text(
            json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    # 全文（带页头，便于抽读/对照）
    lines = []
    for r in recs:
        d = r["diag"]
        lines.append("【P%03d 栏%d 汉字%d 句读%.1f】" % (
            r["page"], d.get("slots", 0), d.get("han", 0), d.get("punct_per100", 0.0)))
        if d.get("heads"):
            lines.append("〔卷标〕" + " / ".join(d["heads"]))
        lines.append(r.get("text") or "")
        lines.append("")
    (out / "全文.txt").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="竖排整本 OCR → book.json")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True, help="书目录（book.json 落这里）")
    ap.add_argument("--title", default="")
    ap.add_argument("--book", default="")
    ap.add_argument("--scale", type=float, default=VO.DEFAULT_SCALE)
    ap.add_argument("--ear", default="", help="书耳/版心书名，逗号分隔")
    ap.add_argument("--model", default="",
                    help="PP-OCRv5 rec onnx。缺省时按 resolve_model_path 顺序查找："
                         "环境变量 VERTICAL_OCR_MODEL → <项目根>/models/ → tools/models/")
    ap.add_argument("--pages", default="", help="只跑指定页：1-30,41")
    ap.add_argument("--intra", type=int, default=4)
    ap.add_argument("--no-cache", action="store_true", help="不读不写页级缓存")
    ap.add_argument("--cache-dir", default="", help="页级缓存目录（默认 <out>\\.ocr_cache）")
    args = ap.parse_args()

    pdf = Path(args.pdf).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = None if args.no_cache else Path(args.cache_dir) if args.cache_dir else out / ".ocr_cache"
    book_name = args.book or args.title or pdf.stem
    title = args.title or book_name
    ears = [e.strip() for e in args.ear.split(",") if e.strip()] or [book_name, title, title[-3:]]
    # 版心书名核心：书名末 3 字，另加末 2 字兜底
    cores = []
    for base in [book_name, title] + ears:
        b = re.sub(r"[\s·…．\-—（）()]", "", base or "")
        if len(b) >= 3:
            cores.append(b[-3:])
        if len(b) >= 2:
            cores.append(b[-2:])
    cores = list(dict.fromkeys(cores))
    # 版心完整书名（页尾残留剔除用）：书名/书名 ear/末 3–6 字
    phrases: list[str] = []
    for base in [book_name, title] + ears:
        b = re.sub(r"[\s·…．\-—（）()]", "", base or "")
        if 2 <= len(b) <= 12:
            phrases.append(b)
        for k in (3, 4, 5, 6):
            if len(b) > k:
                phrases.append(b[-k:])
    phrases = list(dict.fromkeys(phrases))

    t_all = time.monotonic()
    with VO.VerticalOCR(pdf, scale=args.scale, ears=ears, model_path=args.model or None,
                        cache_dir=cache_dir, intra_op_num_threads=args.intra) as ocr:
        ocr._doc()
        total = ocr.pages or 0
        pages = parse_pages(args.pages, total)
        print("[1/4] 识别 %d/%d 页 · scale=%.1f · 缓存=%s" % (
            len(pages), total, args.scale, cache_dir or "off"), flush=True)
        recs, timings, hits = [], [], 0
        for i, pg in enumerate(pages, 1):
            t0 = time.monotonic()
            r = ocr.recognize_safe(pg, use_cache=not args.no_cache)
            dt = time.monotonic() - t0
            timings.append(dt)
            if r.get("cached"):
                hits += 1
            recs.append(r)
            d = r["diag"]
            print("p%-4d 栏%2d 汉字%-4d 句读%5.1f 校正%-3d 重切%-2d %4.1fs%s %s" % (
                pg, d.get("slots", 0), d.get("han", 0), d.get("punct_per100", 0.0),
                d.get("corrections", 0), d.get("resplit", 0), dt,
                " [缓存]" if r.get("cached") else "",
                ",".join(d.get("flags") or [])), flush=True)
        # ---- [2/3] 页级去倾斜道（任务⑤投产；只动触发页，非触发页 rec 与既有
        # .ocr_cache 逐字节一致，触发页结果走 *+deskew-v1 变体子键，见 vertical_ocr 头部说明）
        med = VO.skew_medians(recs)
        deskew_rep = {"version": VO.DESKW_VERSION, "medians": med,
                      "candidates": [], "applied": [], "rejected": [],
                      "skipped": not med.get("ok")}
        if med.get("ok"):
            idx = {r["page"]: i for i, r in enumerate(recs)}
            trig = [r["page"] for r in recs if VO.deskew_trigger(r["diag"], med)]
            deskew_rep["candidates"] = trig
            print("[2/4] 页级去倾斜：中位 列距%.0f 栏%.0f 汉字%.0f · 触发 %d 页 %s" % (
                med["pitch"], med["slots"], med["han"], len(trig), trig or "（无）"), flush=True)
            for pg in trig:
                old = recs[idx[pg]]
                o0 = {"han": old["diag"].get("han"), "pitch": old["diag"].get("pitch"),
                      "slots": old["diag"].get("slots")}
                new = ocr.recognize_deskew(pg, med, use_cache=not args.no_cache)
                d = new["diag"].get("deskew") or new["diag"].get("deskewAttempt") or {}
                a1 = {"han": new["diag"].get("han"), "pitch": new["diag"].get("pitch"),
                      "slots": new["diag"].get("slots")}
                ent = {"page": pg, "before": o0, "after": a1, "angle": d.get("angle"),
                       "peaks_tried": d.get("candidates")}
                if new["diag"].get("deskew"):
                    deskew_rep["applied"].append(ent)
                    recs[idx[pg]] = new
                    print("  p%-4d %+0.2f° 栏 %d→%d · 列距 %d→%d · 汉字 %d→%d [采用]" % (
                        pg, d.get("angle", 0.0), o0["slots"], a1["slots"],
                        o0["pitch"], a1["pitch"], o0["han"], a1["han"]), flush=True)
                else:
                    deskew_rep["rejected"].append(ent)
                    recs[idx[pg]] = new
                    print("  p%-4d 找角全不过校验 → 维持原结果（峰：" % pg
                          + ",".join("%+0.2f°:%s" % (t["angle"], "OK" if t["ok"] else
                                                    "pitch%d/栏%d" % (t["pitch"], t["slots"]))
                                    for t in d.get("candidates") or []) + "）", flush=True)
        else:
            print("[2/4] 页级去倾斜：样本不足（有效页 %d < 50）跳过" % med.get("pages", 0), flush=True)
        ocr.close()

    VO.enrich_flags(recs)
    print("[3/4] 装配 book.json …", flush=True)
    params = {"scale": args.scale, "paramVersion": VO.PARAM_VERSION,
              "assemblyVersion": ASSEMBLY_VERSION, "ears": ears,
              "headCores": cores, "headPhrases": phrases,
              "model": Path(ocr.model_path).name}
    book, qc = assemble(recs, total, title, book_name, pdf.name, params)
    qc["deskew"] = deskew_rep      # 页级去倾斜：触发/采用/拒绝全记录（可审计）
    print("[4/4] 写出 …", flush=True)
    elapsed = time.monotonic() - t_all
    write_outputs(out, recs, book, qc, elapsed, timings, hits)

    g = qc["pageGate"]
    print("")
    print("竖排整本：《%s》%d 页 · 缓存命中 %d · %.1f 分钟（单页中位 %.2fs）" % (
        book_name, total, hits, elapsed / 60, statistics.median(timings) if timings else 0))
    if deskew_rep.get("candidates") is not None:
        print("  页级去倾斜：触发 %d · 采用 %d（%s）· 拒绝 %d" % (
            len(deskew_rep["candidates"]), len(deskew_rep["applied"]),
            "、".join("p%d%+.2f°" % (a["page"], a["angle"] or 0) for a in deskew_rep["applied"]) or "无",
            len(deskew_rep["rejected"])))
    print("  汉字 %d · 段落 %d · 章 %d · 印刷页映射 %d" % (
        book["stats"]["chars"], book["stats"]["paragraphs"],
        book["stats"]["chapters"], qc["printedPageCount"]))
    print("  章：" + " / ".join("%s@p%d" % (c["title"], c["startPage"]) for c in qc["chapters"]))
    print("  页级闸：硬异常 %d 页%s · 观察/参考 %d 页 · 目录剔除 %d 页 · 封面空白剔除 %d 页 → %s" % (
        g["hardCount"],
        ("（%s）" % "、".join(map(str, g["hardPages"][:20]))) if g["hardPages"] else "",
        g["issueCount"] - g["hardCount"],
        len(qc["droppedPages"]["toc"]), len(qc["droppedPages"]["coverOrBlank"]),
        qc["verdict"]))
    print("  输出：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())