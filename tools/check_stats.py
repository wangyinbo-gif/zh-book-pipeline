# -*- coding: utf-8 -*-
"""P2-03 一致性闸门：重算统计并与 README 标记块（STATS-DATA）比对。

2026-11-27 增补（批处理护栏配套，全部只读，一个文件都不改）：
    C1 chars-drift   逐书 stats.chars vs pipeline.count_chars 的差额与合计
    C2 qc-fresh      陈旧质检。两个信号都算（与 _工作记录/rebuild_qc_summary.py 的
                     「质检早于书稿」同口径）：checkedAt 早于 book.json 落盘、
                     以及 质检.json 文件 mtime 早于 book.json。判序：
                       1) 质检.json 有 bookFingerprint → 按内容指纹（剔除 stats）判
                          一致=WARN（只动了统计字段）/ 变了=FAIL
                       2) 无指纹（历史质检件）→ 命中 reports/stats_fix.json 的
                          stats-only 台账 = WARN；不在台账 = FAIL
                       3) 台账文件缺失/读不出 → 一律退回旧规则（mtime 落后即 FAIL）
    C3 doc-stale     只扫「现状段」（实盘规模/现状速览/整体进度/当前数据/统计口径…）：
                     过期数字 vs 现算，以及晚于当前系统日的未来日期
    C4 dead-pointer  失效指针：引用的 .md 不存在、§N 在目标文档里没有、
                     「见 … §N」独立指针句指向的节里并没有所述内容

用法：
    python tools/check_stats.py                     # 全查，不一致 exit 1
    python tools/check_stats.py --warn-only         # 降级为警告，exit 0
    python tools/check_stats.py --only chars,qc     # 只跑部分（readme,chars,qc,docs,pointer）
    python tools/check_stats.py --skip-docs         # 不查文档（文档轨改文档时用）
    python tools/check_stats.py --root <路径> --readme <路径> --today 2026-11-27 --json
退出码：0 一致；1 不一致（含 --warn-only 之外的全部 FAIL 类）。只读，不改任何文件。
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import project_stats as ps   # noqa: E402  唯一统计口径
import pipeline as pl        # noqa: E402  count_chars / book_fingerprint / GATE_CHECK_NAMES

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 比对这些字段（STATS-DATA 里的机器可校验字段）
CHECK_FIELDS = ("books", "pages", "characters", "chapters", "figures",
                "paired_figures", "questions", "question_sets",
                "gate_pass", "gate_total", "rule_version")

DEFAULT_DOCS = ("README.md", "交接文档/交接文档.md", "交接文档/流程/转换流程.md")
STATS_FIX_DEFAULT = os.path.join("reports", "stats_fix.json")
QC_GRACE_SECONDS = 120        # checkedAt 只到分钟，留 2 分钟余量免得舍入误报

DATE_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
SEC_REF_RE = re.compile(u"§\\s*(\\d{1,2})(?:[.．]\\d+)?")
POINTER_SENT_RE = re.compile(u"^(?P<pre>[^。；]{4,}?)见\\s*(?P<doc>[^\\s，。；]*?\\.md)?[^§]{0,8}(?P<secs>§\\s*\\d{1,2}(?:\\s*/\\s*§?\\s*\\d{1,2})*)\\s*[。.]?$")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
SECTION_RE = re.compile(r"^#{1,3}\s*([零一二三四五六七八九十百]+|\d+)[、.．\s]")
CN_DIG = {u"零": 0, u"一": 1, u"二": 2, u"三": 3, u"四": 4, u"五": 5, u"六": 6, u"七": 7,
          u"八": 8, u"九": 9, u"十": 10, u"百": 100}

# 「现状段」范围：只有这些小节里比数字，其余段落一律不看（含「别名」小节，里面全是历史时点）
SCOPE_HEAD_RE = re.compile(u"(实盘规模|现状速览|整体进度|当前数据|统计口径|状态引用口径)")
SCOPE_OFF_RE = re.compile(u"(索引|题库|交付|备份|历史|明细|说明|别名)")
# 含这些标记的行是「历史时点/变动史」叙述，按 lead 口径最多 WARN，不 FAIL
TIME_MARK_RE = re.compile(r"(20\d\d-\d\d|\d{1,2}-\d{1,2}|\d{1,2}月|历史值|旧值|旧版|旧口径|时点|快照|v1\.\d|→|↑|↓|⇒|当时|发布记录|远端|变动史|仍是|待授权|待拍板|到期|有效期|截止|证书)")

# 只认「全库总量」写法（宁可少报）：逗号分组的大数 + 单位，逐本/子集数值一律不判
LIB_TOTAL = (
    ("characters", u"(\\d{1,3}(?:,\\d{3}){2,})\\s*字", "characters"),
    ("pages", u"(\\d{2,3},\\d{3})\\s*页", "pages"),
    ("figures", u"(\\d{1,2},\\d{3})\\s*图", "figures"),
    ("paired_figures", u"配注\\s*\\**\\s*(\\d{1,2},\\d{3})", "paired_figures"),
    ("questions", u"(\\d{1,2},\\d{3})\\s*题", "questions"),
)
CHAPTERS_RE = re.compile(u"(?:第)?(\\d{3})\\s*章")
CHAPTERS_CTX_RE = re.compile(u"(主库|全库|实盘规模|合计|汇总|章节)")
BOOK_TOTAL_RES = [re.compile(p) for p in (
    u"主库[^。\\n]{0,16}?(\\d{1,3})\\s*本",
    u"现存\\s*\\**\\s*(\\d{1,3})\\s*本",
    u"本地\\s*books/?\\s*有\\s*(\\d{1,3})\\s*本",
    u"(\\d{1,3})\\s*本（含\\s*book\\.json",
    u"实盘规模[^\\n]{0,40}?\\**\\s*(\\d{1,3})\\s*本",
    u"(\\d{1,3})\\s*本全部",
)]
GATE_FRACTION_RE = re.compile(r"gate\s*\**\s*(\d+)\s*/\s*(\d+)", re.I)
UPDATE_DECL_RE = re.compile(u"(最后更新|现状速览|更新时间|收口时间)\\D{0,12}?(20\\d\\d-\\d\\d-\\d\\d)(?:[ T](\\d{1,2})[:：](\\d{2}))?")
ALIAS_DOC_RE = re.compile(r"别名")
EXPIRY_LINE_RE = re.compile(u"(到期|有效期|截止|到期前|证书|Not After|失效日)|until", re.I)
ALIAS_MARK_RE = re.compile(u"(批次别名|别名表|日期别名)")
STALE_POINTER_MARK_RE = re.compile(u"(旧指针|已失效|作废|待清理|历史编号|精简前编号)")
MD_INDEX_DIRS = ("交接文档", "_工作记录", "books", "reports", "GitHub", "public")
MD_INDEX_CAP = 40000


def extract_block(text):
    """从 README 文本取 STATS-DATA 块，返回 dict；找不到返回 None。"""
    if ps.BEGIN not in text or ps.END not in text:
        return None, "no-anchors"
    i = text.index(ps.BEGIN)
    j = text.index(ps.END) + len(ps.END)
    seg = text[i:j]
    m = ps.DATA_RE.search(seg)
    if not m:
        return None, "no-data-line"
    try:
        return json.loads(m.group(1)), "ok"
    except Exception as e:
        return None, "bad-json:%s" % e


def finding(kind, severity, key, msg):
    return {"kind": kind, "severity": severity, "key": key, "msg": msg}


def read_text(path):
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _book_dirs(root):
    bdir = os.path.join(root, "books")
    if not os.path.isdir(bdir):
        return None
    return bdir


# --------------------------------------------------------------------------- C1
def check_chars_drift(root, limit=12):
    """逐书 stats.chars vs pipeline.count_chars（写盘口径）差额 + 合计。"""
    bdir = _book_dirs(root)
    if bdir is None:
        return [finding("chars", "fail", "books", "books 目录不存在：%s" % os.path.join(root, "books"))], {}
    out, rows = [], []
    sum_stats = sum_cc = nb = 0
    for d in sorted(os.listdir(bdir)):
        bp = os.path.join(bdir, d, "book.json")
        if not os.path.isfile(bp):
            continue
        try:
            b = json.loads(read_text(bp))
        except Exception as e:
            out.append(finding("chars", "fail", d, "book.json 解析失败：%s" % e))
            continue
        nb += 1
        sc = int(((b.get("stats") or {}).get("chars", 0)) or 0)
        cc = int(pl.count_chars(b))
        sum_stats += sc
        sum_cc += cc
        if sc != cc:
            rows.append((d, sc, cc, cc - sc))
    for d, sc, cc, dd in sorted(rows, key=lambda x: -abs(x[3]))[:limit]:
        out.append(finding("chars", "fail", d,
                           "stats.chars=%d vs count_chars=%d 差 %+d（字段陈旧，非口径差）" % (sc, cc, dd)))
    if len(rows) > limit:
        out.append(finding("chars", "fail", "_more_", "另有 %d 本 stats.chars 陈旧" % (len(rows) - limit)))
    if rows:
        out.append(finding("chars", "fail", "_total_",
                           "合计 stats.chars=%s · count_chars=%s · 净差 %+d · 陈旧 %d/%d 本"
                           % (format(sum_stats, ","), format(sum_cc, ","), sum_cc - sum_stats, len(rows), nb)))
    return out, {"books": nb, "stale": len(rows), "sumStats": sum_stats,
                 "sumCount": sum_cc, "delta": sum_cc - sum_stats,
                 "worst": rows[:limit] and rows[0][0] or "",
                 "dirs": [r[0] for r in rows]}


# --------------------------------------------------------------------------- C2
def load_stats_fix(root, path=None):
    """stats-only 校正台账 → {归一化 dir: 条目}。缺失/坏 → {}（退回旧规则，一律 FAIL）。"""
    p = path or os.path.join(root, STATS_FIX_DEFAULT)
    if not os.path.isabs(p):
        p = os.path.join(root, p)
    if not os.path.isfile(p):
        return {}, p, "missing", None
    try:
        data = json.loads(read_text(p))
    except Exception as e:
        return {}, p, "corrupt:%s" % str(e)[:60], None
    books = data.get("books") if isinstance(data, dict) else data
    idx = {}
    for it in (books or []):
        if isinstance(it, str):
            idx[pl.norm_key(it)] = {"dir": it}
        elif isinstance(it, dict):
            key = pl.norm_key(it.get("dir") or it.get("name") or it.get("book") or "")
            if key:
                idx[key] = it
    meta = data.get("runId") if isinstance(data, dict) else None
    return idx, p, ("ok" if idx else "empty"), meta


def parse_checked_at(v):
    if not v:
        return None
    s = str(v).strip().replace("T", " ")
    for fmt, cut in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(s[:cut], fmt)
        except Exception:
            continue
    return None


def qc_staleness(book_path, qc, qc_path, grace=QC_GRACE_SECONDS):
    """返回 (stale?, 说明)。两个信号任一超阈值即算陈旧。"""
    checked = parse_checked_at(qc.get("checkedAt"))
    btime = dt.datetime.fromtimestamp(os.path.getmtime(book_path))
    qtime = dt.datetime.fromtimestamp(os.path.getmtime(qc_path))
    reasons = []
    if checked and (btime - checked).total_seconds() > grace:
        reasons.append("book.json 落盘 %s 晚于 checkedAt %s（%.0f 分钟）"
                       % (btime.strftime("%Y-%m-%d %H:%M:%S"), qc.get("checkedAt"),
                          (btime - checked).total_seconds() / 60.0))
    if (btime - qtime).total_seconds() > 1:
        reasons.append("book.json mtime 晚于 质检.json mtime %.0f 分钟（rebuild_qc_summary 同口径）"
                       % ((btime - qtime).total_seconds() / 60.0))
    return (bool(reasons), "；".join(reasons), btime)


def check_qc_freshness(root, fix_ledger=None, grace=QC_GRACE_SECONDS, limit=12, ledger_path=None,
                       ledger_state=None):
    """陈旧质检：指纹 → stats-only 台账 → FAIL。"""
    if fix_ledger is None:
        fix_ledger, ledger_path, ledger_state = load_stats_fix(root)[:3]
    bdir = _book_dirs(root)
    stat = {"stale": 0, "warn": 0, "fail": 0, "ok": 0, "missing": 0, "byFingerprint": 0, "byLedger": 0}
    if bdir is None:
        return [finding("qc", "fail", "books", "books 目录不存在：%s" % os.path.join(root, "books"))], stat
    out = []
    expected = len(pl.GATE_CHECK_NAMES)
    for d in sorted(os.listdir(bdir)):
        bd = os.path.join(bdir, d)
        bp = os.path.join(bd, "book.json")
        if not os.path.isfile(bp):
            continue
        qp = os.path.join(bd, "质检.json")
        if not os.path.isfile(qp):
            stat["missing"] += 1
            out.append(finding("qc", "fail", d, "缺 质检.json（无落盘质检记录）"))
            continue
        try:
            q = json.loads(read_text(qp))
        except Exception as e:
            stat["missing"] += 1
            out.append(finding("qc", "fail", d, "质检.json 读不出：%s" % e))
            continue
        stale, detail, _bt = qc_staleness(bp, q, qp, grace)
        if not stale:
            stat["ok"] += 1
            continue
        stat["stale"] += 1
        n = len(((q.get("gate") or {}).get("checks")) or [])
        count_note = "" if n == expected else "（质检 %d 项 vs 当前 gate %d 项，建议重跑 recheck）" % (n, expected)
        fp_qc = q.get("bookFingerprint") or (q.get("gate") or {}).get("bookFingerprint") or ""
        if fp_qc:
            cur = pl.book_fingerprint(json.loads(read_text(bp)))
            if cur == fp_qc:
                stat["warn"] += 1
                stat["byFingerprint"] += 1
                out.append(finding("qc", "warn", d, "陈旧但内容指纹一致（只改了 stats 一类字段）：%s%s"
                                   % (detail, count_note)))
            else:
                stat["fail"] += 1
                out.append(finding("qc", "fail", d, "陈旧质检：内容指纹已变（质检后书稿又被改过，需重跑 recheck）：%s%s"
                                   % (detail, count_note)))
            continue
        entry = fix_ledger.get(pl.norm_key(d))
        if entry is not None:
            stat["warn"] += 1
            stat["byLedger"] += 1
            live = int(((json.loads(read_text(bp)).get("stats") or {}).get("chars", 0)) or 0)
            nc = entry.get("newChars")
            tag = "" if nc in (None, live) else "，台账 newChars=%s 与现值 %s 不符" % (nc, live)
            bak = entry.get("backupPath") or entry.get("backup") or "-"
            out.append(finding("qc", "warn", d,
                               "陈旧质检但已登记 stats-only 台账（runId=%s reason=%s backup=%s qcItems=%s%s）：%s%s"
                               % (entry.get("runId") or (ledger_state or ""), entry.get("reason") or "-",
                                  os.path.basename(str(bak)), entry.get("qcItems", "-"), tag, detail, count_note)))
        else:
            stat["fail"] += 1
            out.append(finding("qc", "fail", d,
                               "陈旧质检：不在 stats-only 台账（%s，状态 %s）里 → 未登记的书稿改动：%s%s"
                               % (os.path.relpath(ledger_path, root) if ledger_path else STATS_FIX_DEFAULT,
                                  ledger_state, detail, count_note)))
    return out, stat


# --------------------------------------------------------------------------- C3
def doc_scope_lines(path):
    """只取「现状段」内的 (行号, 行)。段 = 标题命中关键词的小节，遇到同级/更高级标题或
    命中「索引/题库/备份…」的标题即结束。"""
    lines = read_text(path).splitlines()
    out, level = [], None
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^(#+)\s*(.*)$", ln)
        if m:
            lvl, head = len(m.group(1)), m.group(2)
            if SCOPE_OFF_RE.search(head):
                level = None
            elif SCOPE_HEAD_RE.search(head):
                level = lvl
            elif level and lvl <= level:
                level = None
            continue
        if level:
            out.append((i, ln))
    return out


def load_date_aliases(root, today=None):
    """从批次别名表（交接文档 §零-0.5 / 工作记录索引 §零-2）收集「被声明为别名」的日期。
    别名侧一定是晚于今天的日期，实际时点一定 ≤ 今天，所以只收 > today 的即可。
    找不到别名表 → 返回空集合（未来日期一律照旧 FAIL）。"""
    today = today or dt.date.today()
    out = set()
    for rel in ("交接文档/交接文档.md", "交接文档/工作记录索引.md"):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        lines = read_text(p).splitlines()
        start = None
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("#") and ALIAS_DOC_RE.search(ln):
                start = i
                break
        if start is None:
            for i, ln in enumerate(lines):
                if u"别名表" in ln or u"批次别名" in ln:
                    start = i
                    break
        if start is None:
            continue
        chunk = "\n".join(lines[start:start + 60])
        for d, _raw in doc_dates(chunk):
            if d > today:
                out.add(d)
    return out


def check_docs_numbers(root, stats, docs=DEFAULT_DOCS, limit=15):
    """现状段里的「全库总量」数字 vs 现算。逐本/子集数值（目视 N 本、gate 12/12、
    单本字数页码、百分比）一律不判；含时点标注的行最多 WARN。"""
    expected = {"books": stats["books"], "pages": stats["pages"], "characters": stats["characters"],
                "chapters": stats["chapters"], "figures": stats["figures"],
                "paired_figures": stats["paired_figures"], "questions": stats["questions"]}
    gs = stats["gate_status_summary"]
    out = []
    for rel in docs:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            out.append(finding("docs", "fail", rel, "文档不存在：%s" % rel))
            continue
        for lineno, line in doc_scope_lines(p):
            sev = "warn" if TIME_MARK_RE.search(line) else "fail"
            tag = "%s:%d" % (rel, lineno)
            for _key, pat, field in LIB_TOTAL:
                want = expected.get(field)
                if want is None:
                    continue
                for m in re.finditer(pat, line):
                    got = int(m.group(1).replace(",", ""))
                    if got == want:
                        continue
                    out.append(finding("docs", sev, tag,
                                       u"全库总量数字过期：「%s」文档=%s 现算=%s"
                                       % (m.group(0).strip()[:22], format(got, ","), format(want, ","))))
            if CHAPTERS_CTX_RE.search(line):
                for m in CHAPTERS_RE.finditer(line):
                    got = int(m.group(1))
                    if got != expected["chapters"]:
                        out.append(finding("docs", sev, tag,
                                           u"全库总量数字过期：「%s」文档=%d 现算=%d"
                                           % (m.group(0).strip()[:16], got, expected["chapters"])))
            for rx in BOOK_TOTAL_RES:
                for m in rx.finditer(line):
                    got = int(m.group(1))
                    if got == expected["books"] or got > expected["books"]:
                        continue          # 比全库还多 → 含复习笔记/历史口径，不判
                    out.append(finding("docs", sev, tag,
                                       u"全库书数过期：「%s」文档=%d 本 · 现算=%d 本"
                                       % (m.group(0).strip()[:22], got, expected["books"])))
            for m in GATE_FRACTION_RE.finditer(line):
                g1, g2 = int(m.group(1)), int(m.group(2))
                if g2 != gs["total_with_qc"]:
                    continue              # 分母不是全库受检数（单本 12/12 一类）不判
                if (g1, g2) != (gs["pass"], gs["total_with_qc"]):
                    out.append(finding("docs", sev, tag,
                                       u"全库 gate 过期：gate %d/%d（现算 %d/%d）"
                                       % (g1, g2, gs["pass"], gs["total_with_qc"])))
    fails = [x for x in out if x["severity"] == "fail"]
    if len(fails) > limit:
        out.append(finding("docs", "fail", "_more_", "现状段另有 %d 处全库总量数字过期" % (len(fails) - limit)))
    return out


def doc_dates(text):
    out = []
    for m in DATE_RE.finditer(text):
        try:
            out.append((dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)))
        except ValueError:
            continue
    return out


def check_docs_dates(root, today, docs=DEFAULT_DOCS, aliases=None, now=None):
    """未来日期三判（lead 2026-11-27 收窄口径）：
       1) 证书/服务到期一类的行（到期|有效期|截止|until）——本来就该在未来，不判；
       2) 文件里建了「批次别名/别名表」标记的，正文里的 2026-12-01 一类写法属批次别名 → INFO；
       3) 真正 FAIL 的只有：文档自称的最后更新/现状速览时点晚于当前系统时间，
          且该时点没有别名表解释（有别名表解释的降为 WARN，属文档轨待统一）。"""
    if aliases is None:
        aliases = load_date_aliases(root, today)
    now = now or (dt.datetime.combine(today, dt.time(23, 59, 59))
                  if today != dt.date.today() else dt.datetime.now())
    out = []
    for rel in docs:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        text = read_text(p)
        has_alias_mark = bool(ALIAS_MARK_RE.search(text))
        lines = text.splitlines()
        future, on_expiry = [], 0
        for i, ln in enumerate(lines, 1):
            for d, raw in doc_dates(ln):
                if d <= today:
                    continue
                if EXPIRY_LINE_RE.search(ln):
                    on_expiry += 1
                    continue
                future.append((d, raw, i))
        uniq = sorted({raw for _d, raw, _i in future})
        if uniq:
            if has_alias_mark:
                out.append(finding("docs", "info", rel,
                                   u"未来日期 %d 处（%s%s）——本文已建批次别名表，按别名处理，不判过期"
                                   % (len(future), "、".join(uniq[:3]), "…" if len(uniq) > 3 else "")))
            else:
                out.append(finding("docs", "fail", rel,
                                   u"未来日期 %d 处（%s%s）且本文没有批次别名表；文档 mtime=%s · 系统日=%s"
                                   % (len(future), "、".join(uniq[:3]), "…" if len(uniq) > 3 else "",
                                      dt.date.fromtimestamp(os.path.getmtime(p)).isoformat(), today.isoformat())))
        if on_expiry:
            out.append(finding("docs", "info", rel, u"%d 处晚于今天的日期属「到期/有效期」语义，不计未来日期" % on_expiry))
        for m in UPDATE_DECL_RE.finditer(text):
            try:
                claimed = dt.datetime.strptime(m.group(2), "%Y-%m-%d").replace(
                    hour=int(m.group(3) or 0), minute=int(m.group(4) or 0))
            except ValueError:
                continue
            if claimed.date() <= today and claimed <= now:
                break
            sev = "warn" if has_alias_mark else "fail"
            out.append(finding("docs", sev, rel,
                               u"文档自称「%s」时点 %s%s 晚于当前系统时间 %s%s"
                               % (m.group(1), m.group(2), (" %s:%s" % (m.group(3), m.group(4))) if m.group(3) else "",
                                  now.strftime("%Y-%m-%d %H:%M"),
                                  "（本文有别名表，按 WARN 提示待人工统一）" if has_alias_mark else "")))
            break
    return out


# --------------------------------------------------------------------------- C4
def cn_to_int(s):
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        v = CN_DIG.get(ch)
        if v is None:
            return None
        if v >= 10:
            total += (num or 1) * v
            num = 0
        else:
            num = v
    return total + num


def doc_sections(path):
    """按标题切节 → {节号: {"heading":..,"text":..}}（只认中文数字/阿拉伯数字开头的节号）。"""
    lines = read_text(path).splitlines()
    heads = []
    for i, ln in enumerate(lines):
        m = SECTION_RE.match(ln)
        if m:
            n = cn_to_int(m.group(1))
            if n is not None:
                heads.append((i, n, ln.strip()))
    out = {}
    for k, (i, n, head) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        out.setdefault(n, {"heading": head, "text": "\n".join(lines[i:end])})
    return out


_MD_INDEX = {}


def md_index(root):
    """<工作区 md/txt 文件基名索引（有上限，够找引用件就行）。"""
    key = os.path.abspath(root)
    if key in _MD_INDEX:
        return _MD_INDEX[key]
    names = set()
    n = 0
    for sub in MD_INDEX_DIRS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("pages", "assets", "node_modules", ".git")]
            for fn in filenames:
                if fn.endswith((".md", ".txt")):
                    names.add(fn)
                n += 1
            if n > MD_INDEX_CAP:
                break
    for fn in os.listdir(root):
        if fn.endswith(".md"):
            names.add(fn)
    _MD_INDEX[key] = names
    return names


def find_doc(root, from_doc, token):
    """中文正文里 .md 前面没有分隔符（「见交接文档.md」会被整段吞掉前缀），
    所以从长到短剥前缀，取第一个能解析到真实文件（或在文件索引里）的名字。"""
    for i in range(len(token)):
        cand = token[i:]
        if not cand.endswith(".md"):
            continue
        got = resolve_doc(root, from_doc, cand)
        if got:
            return got, cand
        if os.path.basename(cand) in md_index(root):
            return None, os.path.basename(cand)
    return None, token


def resolve_doc(root, from_doc, token):
    token = token.replace("\\", os.sep).replace("/", os.sep)
    base = os.path.basename(token)
    for c in (os.path.join(os.path.dirname(from_doc), token),
              os.path.join(os.path.dirname(os.path.dirname(from_doc)), base),
              os.path.join(root, token),
              os.path.join(root, "交接文档", base)):
        if os.path.isfile(c):
            return c
    return None


def check_dead_pointers(root, docs=DEFAULT_DOCS):
    """失效指针三查：文件/链接存在性、§N 是否存在、「见 … §N」句的所指内容是否在目标节里。"""
    out, cache = [], {}
    index = md_index(root)
    for rel in docs:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        text = read_text(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in MD_LINK_RE.finditer(line):
                t = m.group(1)
                if t.startswith(("http", "mailto:", "#", "<")) or "*" in t:
                    continue
                if not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(p), t))):
                    out.append(finding("pointer", "fail", "%s:%d" % (rel, lineno),
                                       "失效指针：Markdown 链接的文件不存在「%s」" % t))
            secs_in_line = SEC_REF_RE.findall(line)
            if not secs_in_line:
                continue
            mdoc = re.search(r'([^\s，。；、（）()【】`"\x27]{1,44}\.md)[^§\n]{0,10}§', line)
            tgt, used = find_doc(root, p, mdoc.group(1)) if mdoc else (None, "")
            if line.count("§") >= 2 and STALE_POINTER_MARK_RE.search(line):
                out.append(finding("pointer", "info", "%s:%d" % (rel, lineno),
                                   "该行已自我声明为失效旧指针（%d 处 §引用）→ 不判" % line.count("§")))
                continue
            if mdoc and not tgt:
                if used not in index and os.path.basename(used) not in index:
                    out.append(finding("pointer", "fail", "%s:%d" % (rel, lineno),
                                       "失效指针：引用的文档在库里找不到「%s」" % used))
                continue
            tgt = tgt or p
            if tgt not in cache:
                cache[tgt] = doc_sections(tgt)
            secs = cache[tgt]
            if len(secs) < 2:
                continue                    # 目标文档不是按数字分节的，判不了
            clean = line.strip()
            pm = POINTER_SENT_RE.match(clean.replace("**", "")) if len(secs_in_line) == 1 else None
            for r in secs_in_line:
                n = int(r)
                if n not in secs:
                    out.append(finding("pointer", "fail", "%s:%d" % (rel, lineno),
                                       "失效指针：§%s 在「%s」里不存在（该文数字节号只有 %s）"
                                       % (n, os.path.relpath(tgt, root),
                                          ",".join(str(x) for x in sorted(secs)[:14]) or "无")))
                    continue
                if not pm:
                    continue
                topics = [t for t in re.split(u"[、,，/／\\s]+", pm.group("pre"))
                          if len(t) >= 2 and not re.search(r"[\\d.\\s]", t)][:6]
                if len(topics) < 2:
                    continue
                body = pl.norm_key(secs[n]["text"])
                if all(pl.norm_key(t) not in body for t in topics):
                    out.append(finding("pointer", "fail", "%s:%d" % (rel, lineno),
                                       "失效指针：说「%s见…§%s」，但「%s」§%s（%s）里没有这些内容（%s）"
                                       % (pm.group("pre")[:28], n, os.path.relpath(tgt, root), n,
                                          secs[n]["heading"][:20], "、".join(topics[:4]))))
        if u"现已不存在" in text:
            out.append(finding("pointer", "fail", rel, "文内自陈备份「（历史备份，现已不存在）」——指针悬空"))
    return out


# --------------------------------------------------------------------------- main
def run_checks(root, opts):
    """跑一遍全部只读校验，返回 (exit_code, findings, info)。"""
    findings, info = [], {}
    stats = ps.compute(root)
    info["stats"] = {k: stats[k] for k in ("books", "pages", "characters", "chapters",
                                           "figures", "paired_figures", "questions")}
    only = opts.only or {"readme", "chars", "qc", "docs", "pointer"}

    if "readme" in only:
        rpath = os.path.join(root, "README.md") if (not opts.readme or opts.readme == "auto") else opts.readme
        if not os.path.exists(rpath):
            findings.append(finding("readme", "fail", "README", "README 不存在: %s" % rpath))
        else:
            block, reason = extract_block(read_text(rpath))
            if block is None:
                findings.append(finding("readme", "fail", "README", "%s 标记块问题: %s" % (rpath, reason)))
            else:
                expected = ps.data_dict(stats)
                for k in CHECK_FIELDS:
                    got = block.get(k, "<missing>")
                    if got != expected.get(k):
                        findings.append(finding("readme", "fail", k,
                                               "README=%s 现算=%s（%s）" % (got, expected.get(k), rpath)))

    if "chars" in only:
        f, info["chars"] = check_chars_drift(root)
        findings += f

    if "qc" in only:
        ledger, lpath, lstate, lrun = load_stats_fix(root, opts.ledger)
        info["ledger"] = {"path": lpath, "state": lstate, "entries": len(ledger), "runId": lrun}
        f, info["qc"] = check_qc_freshness(root, ledger, grace=opts.grace, ledger_path=lpath,
                                          ledger_state=lrun or lstate)
        findings += f

    docs = tuple(opts.docs) if opts.docs else DEFAULT_DOCS
    if "docs" in only:
        findings += check_docs_numbers(root, stats, docs)
        aliases = load_date_aliases(root, opts.today)
        info["aliases"] = sorted(str(x) for x in aliases)
        findings += check_docs_dates(root, opts.today, docs, aliases)
        if aliases:
            findings.append(finding("docs", "info", "别名表",
                                    "已载入批次别名（晚于今天的日期按别名处理）："
                                    + "、".join(str(x) for x in sorted(aliases))))
    if "pointer" in only:
        findings += check_dead_pointers(root, docs)

    return report(findings, opts), findings, info


LABEL = {"readme": "README 统计块", "chars": "stats.chars vs count_chars",
         "qc": "陈旧质检", "docs": "文档现状段", "pointer": "失效指针"}


def report(findings, opts):
    groups = {}
    for f in findings:
        groups.setdefault(f["kind"], []).append(f)
    for kind in ("readme", "chars", "qc", "docs", "pointer"):
        fs = groups.get(kind) or []
        if not fs:
            print("OK   %s：一致" % LABEL[kind])
            continue
        nf = len([x for x in fs if x["severity"] == "fail"])
        nw = len([x for x in fs if x["severity"] == "warn"])
        print("%s %s：FAIL %d · WARN %d" % ("FAIL" if nf else "WARN", LABEL[kind], nf, nw))
        cap = 10 ** 6 if opts.verbose else 25
        shown = 0
        for x in fs:
            if x["severity"] == "warn" and not (opts.verbose or opts.show_warn):
                continue
            shown += 1
            if shown > cap:
                continue
            print("   [%s] %s: %s" % (x["severity"].upper(), x["key"], x["msg"]))
        if shown > cap:
            print("   …（--verbose 看全部）")
    return 1 if any(x["severity"] == "fail" for x in findings) and not opts.warn_only else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--readme", default=None, help="README 路径；auto/不给 = <root>/README.md")
    ap.add_argument("--warn-only", action="store_true", help="FAIL 降级为警告，exit 0")
    ap.add_argument("--only", default="", help="逗号分隔：readme,chars,qc,docs,pointer")
    ap.add_argument("--skip-docs", action="store_true", help="不查文档现状段与失效指针")
    ap.add_argument("--skip-chars", action="store_true")
    ap.add_argument("--skip-qc", action="store_true")
    ap.add_argument("--docs", default="", help="逗号分隔的文档路径（相对 root）")
    ap.add_argument("--ledger", default=None, help="stats-only 台账 JSON（默认 reports/stats_fix.json）")
    ap.add_argument("--grace", type=int, default=QC_GRACE_SECONDS, help="checkedAt 分钟舍入余量（秒）")
    ap.add_argument("--today", default=None, help="覆盖「当前日」（YYYY-MM-DD，测试用）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--show-warn", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出 findings/info 的 JSON")
    args = ap.parse_args()

    root = ps.root_from_args(args.root)
    args.only = set(x.strip() for x in args.only.split(",") if x.strip()) or None
    args.docs = [x.strip() for x in args.docs.split(",") if x.strip()]
    if args.skip_docs:
        args.only = (args.only or set(("readme", "chars", "qc", "docs", "pointer"))) - set(("docs", "pointer"))
    if args.skip_chars:
        args.only = (args.only or set(("readme", "chars", "qc", "docs", "pointer"))) - set(("chars",))
    if args.skip_qc:
        args.only = (args.only or set(("readme", "chars", "qc", "docs", "pointer"))) - set(("qc",))
    args.today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    args.verbose = bool(getattr(args, "verbose", False))
    args.show_warn = bool(getattr(args, "show_warn", False))

    code, findings, info = run_checks(root, args)
    if args.json:
        print(json.dumps({"exit": code, "info": info, "findings": findings}, ensure_ascii=False, indent=1))
    nf = len([f for f in findings if f["severity"] == "fail"])
    nw = len([f for f in findings if f["severity"] == "warn"])
    if code == 0:
        print("OK: check_stats 全部通过（%s）%s"
              % (root, "· 有 WARN 但 --warn-only 降级" if (args.warn_only and nf) else ""))
    else:
        print("\n合计 FAIL %d · WARN %d（--warn-only 可降级；文档由文档轨改，本工具只报不改）" % (nf, nw))
    return code


if __name__ == "__main__":
    sys.exit(main())
