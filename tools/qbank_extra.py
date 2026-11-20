# -*- coding: utf-8 -*-
"""qbank 的补充校验规则（本次审查暴露的漏检都在这里补齐）。

单独成模块的原因：这些规则要在 check / audit / dump / report 四处复用，
而 qbank.py 本体已经 700 行，直接内联会让 diff 难核对。
"""
import unicodedata
import os
import re
import json

INTERNAL_RE = re.compile(r'\[\d{1,4}\]|(?<![A-Za-z])block\s*\d{1,4}', re.I)
OPT_PREFIX_RE = re.compile(r'^[A-Da-d][.、．]\s*')
CJK = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
DASH = (('一—', '——'), ('—一', '——'), ('一一', '——'))


def norm(s):
    """规范化：只剥标点/空白/控制字符（Unicode 类别 P、Z、C），
    保留字母数字汉字，也保留 Ⅰ Ⅱ Ⅲ Ⅴ、m²、×、㎡ 这类符号。

    以前两版都栽过：一是字符类里混进引号把类截断（空格没剥，误报引文不符）；
    二是改成白名单后把罗马数字削没（采光等级Ⅰ/Ⅱ/Ⅲ/Ⅴ 变成同一串，误报选项重复）。"""
    s = s or ''
    for a, b in DASH:
        s = s.replace(a, b)
    out = []
    for ch in s:
        if unicodedata.category(ch)[0] in ('P', 'Z', 'C'):
            continue
        out.append(ch)
    return ''.join(out)


def cnum(s):
    s = (s or '').strip()
    if s.isdigit():
        return int(s)
    if s == '十':
        return 10
    if '十' in s:
        a, _, b = s.partition('十')
        return (CJK.get(a, 1) if a else 1) * 10 + (CJK.get(b, 0) if b else 0)
    return CJK.get(s, 0)


def chapter_no(title):
    m = re.match(r'^第\s*([0-9零一二三四五六七八九十百]+)\s*章', title or '')
    return cnum(m.group(1)) if m else None


def block_text(b):
    """统一取块文本。表格块把 caption + 每行拼起来，否则表格内容对出题/校验不可见。"""
    if b.get('type') == 'table':
        rows = [' '.join(str(c) for c in (r or [])) for r in (b.get('rows') or [])]
        return (b.get('caption') or '') + ' ' + ' '.join(rows)
    return b.get('text') or b.get('caption') or ''


def table_chars(book):
    return sum(len(block_text(b)) for b in (book.get('blocks') or []) if b.get('type') == 'table')


def count_tables(book):
    return sum(1 for b in (book.get('blocks') or []) if b.get('type') == 'table')


def digits(s):
    s = (s or '').replace(',', '').replace('，', '')
    return re.findall(r'\d{2,}(?:\.\d+)?', s)


def strip_opt(o):
    return OPT_PREFIX_RE.sub('', str(o or '').strip())


def section_index(book):
    """{章号: {该章出现过的节号}}，用来判断 section 标签（如 4.9）是否真的存在。"""
    out = {}
    cur = None
    for b in book.get('blocks') or []:
        if b.get('type') != 'heading':
            continue
        t = (b.get('text') or '').strip()
        m = re.match(r'^第\s*([0-9零一二三四五六七八九十百]+)\s*章', t)
        if m:
            cur = cnum(m.group(1))
            out.setdefault(cur, set())
            continue
        m = re.match(r'^第\s*([0-9零一二三四五六七八九十百]+)\s*节', t)
        if m and cur is not None:
            out.setdefault(cur, set()).add(cnum(m.group(1)))
            continue
        # 数字节标题（如 1.1总体…、2.1.1空间…）：登记第二节号，兼容《公共建筑设计原理(张文忠)》等书
        m = re.match(r'^(\d+(?:\.\d+){1,2})', t)
        if m and cur is not None:
            out.setdefault(cur, set()).add(int(m.group(1).split('.')[1]))
    for c in book.get('chapters') or []:
        n = chapter_no(c.get('title'))
        if n is not None:
            out.setdefault(n, set())
    return out


_CACHE = {}


def whole_norm(book):
    key = id(book)
    if key not in _CACHE:
        _CACHE[key] = norm(' '.join(block_text(b) for b in (book.get('blocks') or [])))
        _CACHE[key + 1] = book
    return _CACHE[key]


def answer_source(q):
    """取“要背的那个东西”：选择取正确项，其余取参考答案。"""
    if q.get('type') == 'choice':
        opts = [strip_opt(o) for o in (q.get('options') or [])]
        ai = ord(q.get('answer') or 'A') - 65
        return opts[ai] if 0 <= ai < len(opts) else ''
    a = q.get('answer')
    if isinstance(a, list):
        return ' '.join(str(x) for x in a)
    return str(a or '')


def check_extra(qs, book, chs, blocks):
    """返回 (errors, warnings)：结构类可自动判定的问题。"""
    errs, warns = [], []
    secs = section_index(book)
    whole = whole_norm(book)
    chtitles = [norm(c.get('title')) for c in (chs or [])]
    for q in qs:
        qid = q.get('id', '?')
        blob = ' '.join([str(q.get('stem') or ''), str(q.get('explain') or '')] +
                        [str(o) for o in (q.get('options') or [])])
        m = INTERNAL_RE.search(blob)
        if m:
            errs.append('%s 内部标记泄漏（学生看得见 [130] 这种内部块号）：%s' % (qid, m.group(0)))
        # 锚点必须是正文块：figure/heading 里没有答案依据（2026-11-06 审张文忠版时补）
        bi = q.get('block')
        if isinstance(bi, int) and 0 <= bi < len(blocks):
            bt = blocks[bi].get('type')
            if bt and bt != 'paragraph':
                errs.append('%s 锚点是 %s 块（应指向 paragraph 正文块）' % (qid, bt))
        if q.get('type') == 'choice':
            opts = [strip_opt(o) for o in (q.get('options') or [])]
            nn = [norm(o) for o in opts]
            if '' in nn:
                errs.append('%s 有空选项' % qid)
            if len(set(nn)) != len(nn):
                errs.append('%s 选项重复' % qid)
            ai = ord(q.get('answer') or 'A') - 65
            if not (0 <= ai < len(opts)):
                errs.append('%s 答案键越界' % qid)
            for mm in re.finditer(r'故\s*([ABCD])\s*(?:正确|对|符合|是答案|为答案)', str(q.get('explain') or '')):
                if mm.group(1) != q.get('answer'):
                    errs.append('%s 解析写“故%s正确/对”但答案键是%s' % (qid, mm.group(1), q.get('answer')))
        if q.get('type') == 'blank':
            st = re.sub(r'_{2,}|＿{2,}', '', str(q.get('stem') or ''))
            nst = norm(st)
            ans = q.get('answer')
            for a in (ans if isinstance(ans, list) else [ans]):
                na = norm(str(a))
                if len(na) >= 4 and na in nst:
                    errs.append('%s 填空答案“%s”已经写在题干里' % (qid, str(a)[:14]))
        sec = str(q.get('section') or '')
        # 支持三种写法：纯编号（4.2）、编号+小节标题（2.1 主要的建筑类型和形制）、纯标题（前言与序）
        m2 = re.match(r'^(\d+(?:\.\d+){0,2})[\s　]*(.*)$', sec)
        if m2:
            parts = [int(x) for x in m2.group(1).split('.') if x]
            n1 = parts[0] if parts else None
            if n1 is not None and secs and n1 not in secs:
                warns.append('%s section=%s：书里没有第%d章' % (qid, sec, n1))
            elif len(parts) >= 2 and secs.get(n1) and parts[1] not in secs[n1]:
                # 口径：小节标题能在全书正文里定位（如写在段落里的「2.4 帕赛玻里斯」）就不算错
                rest_probe = m2.group(2).strip()
                if not (rest_probe and norm(rest_probe) in whole):
                    warns.append('%s section=%s：第%d章没有第%s节（该书实际节号 %s）'
                                 % (qid, sec, n1, parts[1], sorted(secs[n1])[:12]))
            rest = m2.group(2).strip()
            if rest and norm(rest) not in whole and not any(norm(rest) in ct for ct in chtitles):
                warns.append('%s section=%s 的小节标题在全书正文里找不到' % (qid, sec))
        elif sec and norm(sec) not in whole:
            if not any(norm(sec) in ct for ct in chtitles):
                warns.append('%s section=%s 在全书正文/章标题里都不存在' % (qid, sec))
    return errs, warns


def audit_extra(q, book, blocks, idx, ctx):
    """答案侧对账：正确项/参考答案里的硬数字，以及答案键交叉验证。"""
    win_norm = ctx.get('win_norm') or ''
    chap_norm = ctx.get('chap_norm')
    win_raw = (ctx.get('win_raw') or '').replace(' ', '')
    chap_raw = (ctx.get('chap_raw') or '').replace(' ', '')
    whole_raw = ctx.get('whole_raw') or (ctx.get('whole_norm') or '')
    issues = []
    src = answer_source(q)
    # 名解/简答本来就是跨段汇总，因此按整章核对；选择/填空必须能在锚点窗口里找到
    use_chap = q.get('type') in ('term', 'short') and chap_norm
    scope = chap_norm if use_chap else win_norm
    scope_raw = chap_raw if use_chap else win_raw
    tag = '本章' if use_chap else '锚点窗口'
    if not scope_raw:
        scope_raw = norm(' '.join(block_text(b) for b in (blocks or []))).replace(' ', '')
    dg = digits(src)
    # 数字按原文比（norm 会把 124.4 变成 1244）。
    # 只有整本书都查无才是可疑：跨段、跨页引用是名解/简答的常态，
    # 以前用 ±窗口判定，建筑概论一次就误报 4 条。
    missing = [d for d in dg if d not in scope_raw and norm(d) not in scope]
    far = [d for d in missing if (d in whole_raw or norm(d) in whole_raw)]
    gone = [d for d in missing if not (d in whole_raw or norm(d) in whole_raw)]
    for d in gone:
        issues.append('答案数字 %s 全书查不到（疑为外部知识/编造）' % d)
    if q.get('type') == 'choice' and dg:
        opts = [strip_opt(o) for o in (q.get('options') or [])]
        ai = ord(q.get('answer') or 'A') - 65
        hit = [j for j, o in enumerate(opts)
               if j != ai and any(d in (win_raw or win_norm) for d in digits(o))]
        if gone and hit:
            issues.append('答案键可疑：正确项数字 %s 全书查不到，而第 %s 项的数字在窗口内'
                          % ('/'.join(gone), ','.join(str(j + 1) for j in hit)))
    m = INTERNAL_RE.search(str(q.get('explain') or ''))
    if m:
        issues.append('解析含内部块号：%s' % m.group(0))
    return issues


def dump_line(b):
    if b.get('type') == 'table':
        rows = b.get('rows') or []
        head = rows[0] if rows else []
        body = rows[1:] if len(rows) > 1 else []
        return '【表格】%s｜表头：%s｜%d 行' % (b.get('caption') or '', ' / '.join(str(c) for c in head), len(body))
    if b.get('type') == 'figure':
        return '（图 p%s %s）' % (b.get('page'), b.get('displayCaption') or '')
    return ''


def list_books(books_dir):
    return [d for d in sorted(os.listdir(books_dir)) if os.path.exists(os.path.join(books_dir, d, 'book.json'))]


def resolve_book(books_dir, arg):
    ids = list_books(books_dir)
    if not arg:
        return arg
    if os.path.exists(os.path.join(books_dir, arg, 'book.json')):
        return arg
    key = norm(arg)
    hit = [d for d in ids if key and (norm(d).startswith(key) or key in norm(d))]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise SystemExit('找不到书稿：%s\n  books/ 下没有名字含“%s”的书；先跑 python tools/qbank.py books 看全部书名' % (arg, arg))
    raise SystemExit('书名“%s”同时匹配 %d 本，请写全：\n  %s' % (arg, len(hit), '\n  '.join(hit[:8])))
