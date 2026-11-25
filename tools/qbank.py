# -*- coding: utf-8 -*-
"""题库制作流水线（统一入口）。

用法：python tools/qbank.py <命令> [参数]

命令：
  plan     <bookId> [--total N|--density X] [--groups N] [--prefix P] [--force]
  dump     <bookId> <chapterId>            导出某章正文（带全局 block 下标）
  status   <bookId>                        查看各章进度
  build    <bookId>                        合并草稿 -> 题库/<bookId>.json
  check    <bookId>                        结构校验
  audit    <bookId>                        题目 ↔ 原书对账
  repair   <bookId> [--dry-run]            应用修订单 _repairs_*.json 修书稿
  reanchor <旧bookId> <新bookId> <题库json> 重转后迁移锚点
  all      <bookId>                        build + check + audit 串跑
  report   <bookId>                        生成汇总报告

约定：
  - 草稿放 题库/_parts/<bookId>/<chapterId>.json（JSON 数组，不含 id）
  - 所有写盘均为「写 .tmp -> 校验 -> os.replace」原子替换
  - 失败返回非 0 退出码，便于串进脚本
"""
import argparse, difflib, glob, json, os, re, shutil, sys, datetime
import qbank_extra as QE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QBANK = os.path.join(ROOT, '题库')
BOOKS = os.path.join(ROOT, 'books')
PARTS = os.path.join(QBANK, '_parts')
PREFIX_FILE = os.path.join(QBANK, '_prefixes.json')


def find_bank(book_id, for_write=False):
    """定位题库 JSON：根目录优先，其次任意非下划线开头的子目录（归档目录）。

    返回绝对路径：找不到且 for_write=False 时返回 None；for_write=True 时返回根目录默认路径。
    """
    root = os.path.join(QBANK, book_id + '.json')
    if os.path.exists(root):
        return root
    best = None
    for dirpath, dirnames, filenames in os.walk(QBANK):
        dirnames[:] = [d for d in dirnames if not d.startswith('_')]
        if os.path.abspath(dirpath) == os.path.abspath(QBANK):
            continue
        name = book_id + '.json'
        if name in filenames:
            cand = os.path.join(dirpath, name)
            if best is None or cand.count(os.sep) < best.count(os.sep):
                best = cand
    if best:
        return best
    return root if for_write else None


def bank_sidecar(book_id, suffix):
    """题库的 .audit.md / .report.md 与题库 JSON 同目录，跟着一起归档。"""
    bank = find_bank(book_id, for_write=True)
    return os.path.join(os.path.dirname(bank), book_id + suffix)

DEFAULT_MIX = {'choice': 0.45, 'blank': 0.20, 'term': 0.21, 'short': 0.14}
TYPE_CN = {'choice': '选择题', 'blank': '填空题', 'term': '名词解释', 'short': '简答题'}


def norm(s):
    return QE.norm(s)


def read_json(p, default=None):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    with open(tmp, encoding='utf-8') as f:
        json.load(f)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def load_book(book_id):
    book_id = QE.resolve_book(BOOKS, book_id)
    p = os.path.join(BOOKS, book_id, 'book.json')
    b = read_json(p)
    if not b:
        raise SystemExit('书稿不存在或无法解析：' + p)
    return b


def chapter_ranges(book):
    chs = book.get('chapters') or []
    total = len(book.get('blocks') or [])
    out = []
    for i, c in enumerate(chs):
        start = c.get('startBlock', 0)
        end = chs[i + 1].get('startBlock') if i + 1 < len(chs) else total
        out.append({'id': c['id'], 'title': c.get('title') or c['id'],
                    'chars': c.get('chars') or 0, 'startBlock': start, 'endBlock': end})
    return out


def parts_dir(book_id):
    return os.path.join(PARTS, book_id)


def load_prefixes():
    return read_json(PREFIX_FILE, {}) or {}


def save_prefix(book_id, prefix):
    d = load_prefixes()
    if d.get(book_id) != prefix:
        # 跨书前缀撞车「直接拒绝写入」（不是警告）：给一本 1000 题的书改名要牵动 60 余个文档、
        # 7000 余处引用（2026-11-08 全盘实测），代价远高于收益。
        # 规则是「id 只需书内唯一，跨书一律以 (bookId, id) 为键」。
        dup = [k for k, v in d.items() if v == prefix and k != book_id]
        if dup:
            raise SystemExit('[ERROR] 前缀 %r 已被「%s」占用，拒绝写入。\n'
                             '  换一个前缀，或直接沿用已有 id（跨书操作一律用 (bookId, id) 作键）。\n'
                             '  注意：不要为了统一前缀而给已建库的书重编号，那会让所有引用旧题号的文档同时失联。'
                             % (prefix, '、'.join(dup)))
        d[book_id] = prefix
        atomic_write_json(PREFIX_FILE, d)


def prefix_for(book_id):
    d = load_prefixes()
    if d.get(book_id):
        return d[book_id]
    bank = read_json(find_bank(book_id))
    if bank and bank.get('questions'):
        m = re.match(r'^([a-z0-9]+)-c\d+-', bank['questions'][0].get('id', ''))
        if m:
            save_prefix(book_id, m.group(1))
            return m.group(1)
    # 兜底：生成一个未被其它书占用的前缀，绝不复用别人的号段（2026-11-08 修 qb 撞车）
    used = set(load_prefixes().values())
    cand, n = 'qb', 2
    while cand in used:
        cand = 'qb%d' % n
        n += 1
    return cand


def largest_remainder(weights, total):
    s = sum(weights) or 1.0
    raw = [w / s * total for w in weights]
    base = [int(x) for x in raw]
    rest = total - sum(base)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in order[:rest]:
        base[i] += 1
    return base


def distribute_types(total, mix):
    keys = [k for k in ('choice', 'blank', 'term', 'short') if mix.get(k)]
    if not keys:
        return {}
    counts = largest_remainder([mix.get(k, 0) for k in keys], total)
    return dict(zip(keys, counts))


def write_brief(book_id, plan, group, path):
    chs = [c for c in plan['chapters'] if c['id'] in group['chapters']]
    L = ['# 出题任务书 · %s · %s' % (plan['bookTitle'], group['name']), '',
         '> 由 python tools/qbank.py plan 自动生成，与 交接文档/题库规范/_spec.md 配套使用。', '',
         '## 书', '', '- bookId（目录名，命令里原样使用）：%s' % book_id, '',
         '## 章节与题量（合计 %d 题）' % group['quota'], '',
         '| chapterId | 章标题 | 字数 | 题量 | block 范围 |', '| --- | --- | --- | --- | --- |']
    for c in chs:
        L.append('| %s | %s | %d | %d | %d-%d |' % (c['id'], c['title'], c['chars'], c['quota'], c['startBlock'], c['endBlock']))
    L += ['', '## 题型配比', '', '| 题型 | 数量 |', '| --- | --- |']
    for k in ('choice', 'blank', 'term', 'short'):
        L.append('| %s（%s）| %d |' % (TYPE_CN[k], k, group['types'].get(k, 0)))
    L += ['', '## 操作步骤', '',
          '1. 读规范：交接文档/题库规范/_spec.md',
          '2. 逐章导出正文：python tools/qbank.py dump "<bookId>" <chapterId>',
          '3. 按章写草稿：题库/_parts/<bookId>/<chapterId>.json（JSON 数组，不写 id）',
          '4. 自检：python tools/qbank.py status "<bookId>"',
          '5. 汇报：status 输出 + 原文存疑清单', '',
          '## 硬性要求', '',
          '- 每题 block 必须落在本章 startBlock-endBlock 之间，page 取该 block 的页',
          '- explain ≥60 字（正确项依据 + 至少两个错项分析 + 关联考点）',
          '- 名词解释 3-5 句；简答 ≥3 个分点；填空 ____ 数 == 答案数组长度',
          '- 同一知识点只出一道；正确答案分布均衡；不出现「如图所示」', '']
    atomic_write_text(path, '\n'.join(L))


def cmd_plan(args):
    book = load_book(args.book)
    chs = chapter_ranges(book)
    skip = args.skip or '前言与序|^序$|^目\\s*录$|^索引$'
    use = [c for c in chs if not re.search(skip, c['title']) and c['chars'] >= args.min_chars]
    if not use:
        raise SystemExit('没有可用章节')

    if args.total:
        quotas = largest_remainder([c['chars'] for c in use], args.total)
    else:
        quotas = [max(args.min_per_chapter, int(round(c['chars'] / 1000.0 * args.density))) for c in use]

    plan = {'bookId': args.book, 'bookTitle': book.get('bookTitle') or args.book,
            'generatedAt': datetime.date.today().isoformat(), 'total': sum(quotas),
            'density': None if args.total else args.density,
            'prefix': args.prefix or prefix_for(args.book), 'mix': DEFAULT_MIX,
            'chapters': [], 'groups': []}
    for c, q in zip(use, quotas):
        plan['chapters'].append({'id': c['id'], 'title': c['title'], 'chars': c['chars'],
                                 'startBlock': c['startBlock'], 'endBlock': c['endBlock'],
                                 'quota': q, 'types': distribute_types(q, DEFAULT_MIX)})

    groups = [[] for _ in range(max(1, args.groups))]
    load = [0] * len(groups)
    for c in plan['chapters']:
        i = load.index(min(load))
        groups[i].append(c)
        load[i] += c['quota']
    for gi, g in enumerate(groups, 1):
        if not g:
            continue
        plan['groups'].append({'name': 'g%d' % gi, 'chapters': [c['id'] for c in g],
                               'quota': sum(c['quota'] for c in g),
                               'types': {k: sum(c['types'].get(k, 0) for c in g) for k in DEFAULT_MIX}})

    d = parts_dir(args.book)
    os.makedirs(d, exist_ok=True)
    plan_path = os.path.join(d, '_plan.json')
    if os.path.exists(plan_path) and not args.force:
        raise SystemExit('已存在计划：%s（要覆盖加 --force）' % plan_path)
    atomic_write_json(plan_path, plan)
    for g in plan['groups']:
        write_brief(args.book, plan, g, os.path.join(d, '_task_%s.md' % g['name']))
    print('计划已生成：%s' % plan_path)
    print('  总题量 %d ｜ 章 %d ｜ 组 %d' % (plan['total'], len(plan['chapters']), len(plan['groups'])))
    for g in plan['groups']:
        print('   %s: %d 题, %d 章' % (g['name'], g['quota'], len(g['chapters'])))
    return 0


def cmd_dump(args):
    book = load_book(args.book)
    blocks = book['blocks']
    rng = {c['id']: c for c in chapter_ranges(book)}
    if args.chapter not in rng:
        raise SystemExit('章节不存在：%s；可选：%s' % (args.chapter, ', '.join(rng)))
    c = rng[args.chapter]
    d = parts_dir(args.book)
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, '_dump_%s.txt' % args.chapter)
    L = []
    for i in range(c['startBlock'], min(c['endBlock'], len(blocks))):
        b = blocks[i]
        if b.get('type') == 'paragraph':
            txt = b.get('text') or ''
        elif b.get('type') == 'heading':
            txt = '【标题】' + (b.get('text') or '')
        elif b.get('type') == 'table':
            txt = QE.dump_line(b)
        else:
            txt = '（图 p%s %s）' % (b.get('page'), b.get('displayCaption') or '')
        L.append('[%d] p%s %s %s' % (i, b.get('page'), b.get('type'), txt))
    atomic_write_text(out, '\n'.join(L))
    print('chapter=%s blocks=%d-%d -> %s (%d 块)' % (args.chapter, c['startBlock'], c['endBlock'], out, len(L)))
    return 0


def check_chapter_file(path, rng_start, rng_end, blocks):
    arr = read_json(path)
    if not isinstance(arr, list):
        return {'total': 0, 'bad': 1, 'types': {}, 'notes': ['不是 JSON 数组']}
    types, bad, notes = {}, 0, []
    for q in arr:
        t = q.get('type')
        types[t] = types.get(t, 0) + 1
        issues = []
        if t == 'choice':
            if len(q.get('options') or []) != 4:
                issues.append('选项数')
            if q.get('answer') not in ('A', 'B', 'C', 'D'):
                issues.append('答案')
        if t == 'blank':
            n = len(re.findall(r'_{2,}', q.get('stem') or ''))
            if not isinstance(q.get('answer'), list) or n != len(q['answer']):
                issues.append('空位≠答案')
        b = q.get('block')
        if not isinstance(b, int) or not (rng_start <= b < rng_end):
            issues.append('block越界')
        elif q.get('page') != blocks[b].get('page'):
            issues.append('page不符')
        if len(q.get('explain') or '') < 10:
            issues.append('解析短')
        if issues:
            bad += 1
            notes.append('%s: %s' % (str(q.get('stem', ''))[:14], ','.join(issues)))
    return {'total': len(arr), 'bad': bad, 'types': types, 'notes': notes[:5]}


def cmd_status(args):
    book = load_book(args.book)
    blocks = book['blocks']
    plan = read_json(os.path.join(parts_dir(args.book), '_plan.json')) or {}
    plan_ch = {c['id']: c for c in plan.get('chapters', [])}
    d = parts_dir(args.book)
    rows, bad_total, deliver = [], 0, 0
    for c in chapter_ranges(book):
        p = os.path.join(d, c['id'] + '.json')
        quota = plan_ch.get(c['id'], {}).get('quota', '')
        if not os.path.exists(p):
            rows.append((c['id'], c['title'][:22], quota, '—', '—', ''))
            continue
        st = check_chapter_file(p, c['startBlock'], c['endBlock'], blocks)
        deliver += st['total']
        bad_total += st['bad']
        types = ' '.join('%s%d' % (k[0].upper(), v) for k, v in sorted(st['types'].items()))
        rows.append((c['id'], c['title'][:22], quota, st['total'], types, st['bad'] or ''))
    print('=== 进度：%s ===' % (book.get('bookTitle') or args.book))
    print('%-8s %-24s %5s %5s %-16s %s' % ('chapter', '标题', '计划', '已交', '题型', '问题'))
    for r in rows:
        print('%-8s %-24s %5s %5s %-16s %s' % (str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5])))
    print('---')
    print('已交 %d 题 ｜ 问题 %d ｜ 计划 %s' % (deliver, bad_total, plan.get('total', '未规划')))
    return 0 if bad_total == 0 else 1


def _clean_internal_markers(q):
    """清理解析/题干里的内部块号标记（出题 agent 常写「原文[123]」「block 123」）。

    只处理数字形式，[美] 这类国别标注不受影响；幂等。
    """
    import re as _re

    def _clean(s):
        if not isinstance(s, str):
            return s
        s = _re.sub(r'原文\s*\[\s*\d{1,4}\s*\]\s*', '原文', s)
        s = _re.sub(r'见\s*\[\s*\d{1,4}\s*\]\s*', '见', s)
        s = _re.sub(r'\[\s*\d{1,4}\s*\]', '', s)
        s = _re.sub(r'(?<![A-Za-z])block\s*\d{1,4}', '', s, flags=_re.I)
        s = _re.sub(r'[ \t]{2,}', ' ', s)
        return s

    for f in ('stem', 'answer', 'explain'):
        v = q.get(f)
        if isinstance(v, str):
            q[f] = _clean(v)
        elif isinstance(v, list):
            q[f] = [_clean(x) if isinstance(x, str) else x for x in v]
    if isinstance(q.get('options'), list):
        q['options'] = [_clean(x) if isinstance(x, str) else x for x in q['options']]
    return q


def cmd_build(args):
    book = load_book(args.book)
    blocks = book['blocks']
    chs = book.get('chapters') or []
    ranges = {c['id']: (c['startBlock'], chs[i + 1]['startBlock'] if i + 1 < len(chs) else len(blocks))
              for i, c in enumerate(chs)}
    prefix = prefix_for(args.book)
    d = parts_dir(args.book)
    questions, missing, by_chapter = [], [], {}
    for i, c in enumerate(chs):
        p = os.path.join(d, c['id'] + '.json')
        if not os.path.exists(p):
            missing.append(c['id'])
            continue
        arr = read_json(p)
        if not isinstance(arr, list):
            raise SystemExit('%s 不是 JSON 数组' % p)
        for qi, q in enumerate(arr, 1):
            q = dict(q)
            _clean_internal_markers(q)
            q['id'] = '%s-c%d-%03d' % (prefix, i, qi)
            q['chapter'] = c['id']
            questions.append(q)
        by_chapter[c['id']] = len(arr)

    problems = []
    for q in questions:
        b, cid = q.get('block'), q.get('chapter')
        if not isinstance(b, int) or not (0 <= b < len(blocks)):
            problems.append('%s block 非法: %r' % (q.get('id'), b))
        elif cid in ranges and not (ranges[cid][0] <= b < ranges[cid][1]):
            problems.append('%s block %d 不在 %s 范围 %s' % (q.get('id'), b, cid, ranges[cid]))
    by_type = {}
    for q in questions:
        by_type[q['type']] = by_type.get(q['type'], 0) + 1
    out = {'version': 1, 'bookId': args.book, 'bookTitle': book.get('bookTitle') or args.book,
           'author': book.get('author') or '', 'generatedAt': datetime.date.today().isoformat(),
           'source': 'books/%s/book.json' % args.book, 'generator': 'qbank.py',
           'chapters': [{'id': c['id'], 'title': c.get('title') or c['id']} for c in chs],
           'stats': {'total': len(questions), 'byType': by_type, 'byChapter': by_chapter},
           'questions': questions}
    existing = read_json(find_bank(args.book)) or {}
    old_qs = existing.get('questions') or []
    if old_qs and len(old_qs) > len(questions) and not getattr(args, 'force', False):
        raise SystemExit(
            '拒绝构建：现有题库 %d 题，而 ch-*.json 只能构建 %d 题，继续会用更少的题覆盖整库。\n'
            '请先把新题合并进 ch-*.json，或加 --force 确认覆盖。' % (len(old_qs), len(questions)))
    dst = find_bank(args.book, for_write=True)
    atomic_write_json(dst, out)
    save_prefix(args.book, prefix)
    print('合并完成：%s' % dst)
    print('  题数 %d ｜ 题型 %s ｜ 缺章 %s' % (len(questions), by_type, missing or '无'))
    if problems:
        print('  锚点问题 %d 条（前 10）：' % len(problems))
        for x in problems[:10]:
            print('   ', x)
    return 0 if not problems else 2


def cmd_check(args):
    book = load_book(args.book)
    blocks = book['blocks']
    chs = book.get('chapters') or []
    ranges = {c['id']: (c['startBlock'], chs[i + 1]['startBlock'] if i + 1 < len(chs) else len(blocks))
              for i, c in enumerate(chs)}
    bank = read_json(find_bank(args.book))
    if not bank:
        raise SystemExit('题库不存在，先跑 build')
    qs = bank.get('questions') or []
    errs, warns, seen_stem, seen_id = [], [], {}, {}
    for q in qs:
        qid = q.get('id', '?')
        seen_id[qid] = seen_id.get(qid, 0) + 1
        t = q.get('type')
        if t not in ('choice', 'blank', 'term', 'short'):
            errs.append('%s 题型非法: %r' % (qid, t))
        for f in ('stem', 'section', 'page', 'block', 'answer', 'explain', 'difficulty', 'tags'):
            if q.get(f) in (None, '', []):
                errs.append('%s 缺字段 %s' % (qid, f))
        b = q.get('block')
        if isinstance(b, int):
            if not (0 <= b < len(blocks)):
                errs.append('%s block 越界' % qid)
            else:
                if q.get('page') != blocks[b].get('page'):
                    warns.append('%s page=%s 与 block 实际 page=%s 不一致' % (qid, q.get('page'), blocks[b].get('page')))
                cid = q.get('chapter')
                if cid in ranges and not (ranges[cid][0] <= b < ranges[cid][1]):
                    errs.append('%s block 不在 %s 范围' % (qid, cid))
        if t == 'choice':
            if len(q.get('options') or []) != 4:
                errs.append('%s 选项数 %d' % (qid, len(q.get('options') or [])))
            if q.get('answer') not in ('A', 'B', 'C', 'D'):
                errs.append('%s 答案非法: %r' % (qid, q.get('answer')))
        elif t == 'blank':
            ans = q.get('answer')
            n = len(re.findall(r'_{2,}', str(q.get('stem') or '')))
            if not isinstance(ans, list) or n != len(ans):
                errs.append('%s 空位与答案数不符' % qid)
        elif t in ('term', 'short'):
            if not isinstance(q.get('answer'), str) or len(q.get('answer') or '') < 5:
                errs.append('%s 参考答案过短' % qid)
        if len(str(q.get('explain') or '')) < 10:
            errs.append('%s 解析过短' % qid)
        k = norm(q.get('stem'))
        if k in seen_stem:
            errs.append('%s 题干与 %s 重复' % (qid, seen_stem[k]))
        else:
            seen_stem[k] = qid
    e2, w2 = QE.check_extra(qs, book, chs, blocks)
    errs += e2
    warns += w2

    for k, v in seen_id.items():
        if v > 1:
            errs.append('重复 id: %s x%d' % (k, v))
    by_type, answers = {}, {}
    for q in qs:
        by_type[q.get('type')] = by_type.get(q.get('type'), 0) + 1
        if q.get('type') == 'choice':
            answers[q.get('answer')] = answers.get(q.get('answer'), 0) + 1
    print('=== 题库校验：%s ===' % args.book)
    print('题数 %d ｜ 题型 %s ｜ 答案分布 %s' % (len(qs), by_type, answers))
    print('错误 %d 条，警告 %d 条' % (len(errs), len(warns)))
    for e in errs[:40]:
        print('  [ERR]', e)
    for w in warns[:20]:
        print('  [WARN]', w)
    if not errs:
        print('校验通过')
    return 0 if not errs else 1


def cmd_audit(args):
    book = load_book(args.book)
    blocks = book['blocks']
    bank = read_json(find_bank(args.book))
    if not bank:
        raise SystemExit('题库不存在，先跑 build')
    W = args.window

    def segments(stem):
        out = []
        for q in re.findall(r'[“「]([^”」]{2,80})[”」]', stem or ''):
            for part in re.split(r'[、，。；：！？…|]+', re.sub(r'_{2,}', '|', q)):
                p = norm(part)
                if len(p) >= 4:
                    out.append(p)
        return out

    _chs = book.get("chapters") or []
    _rng = {c["id"]: (c.get("startBlock", 0),
                    _chs[j + 1].get("startBlock") if j + 1 < len(_chs) else len(blocks))
            for j, c in enumerate(_chs)}
    _ccache = {}
    _whole_raw = [" ".join(QE.block_text(x) for x in blocks)]
    _whole_norm = [QE.norm(" ".join(QE.block_text(x) for x in blocks))]

    def chap_ctx(cid):
        if cid not in _ccache:
            s, e = _rng.get(cid, (0, len(blocks)))
            raw = " ".join(QE.block_text(x) for x in blocks[s:e])
            _ccache[cid] = (QE.norm(raw), raw)
        return _ccache[cid]

    flags = []
    for q in bank.get('questions', []):
        i = q.get('block')
        if not isinstance(i, int) or not (0 <= i < len(blocks)):
            flags.append((q.get('id'), q.get('stem', ''), ['锚点缺失'], i, q.get('page')))
            continue
        win = norm(' '.join((blocks[j].get('text') or '') for j in range(max(0, i - W), min(len(blocks), i + W + 1))))
        _cw, _craw = chap_ctx(q.get("chapter"))
        _wnorm = QE.norm(_craw)
        _wraw = " ".join(QE.block_text(blocks[j]) for j in range(max(0, i - W), min(len(blocks), i + W + 1)))
        issues = []
        # 判定口径：能在本章或锚点窗口里找到就不算错；整本书都查无才可疑
        for s in segments(q.get('stem') or ''):
            if s not in win and s not in _wnorm and s not in _whole_norm[0]:
                issues.append('引文全书查不到：' + s[:16])
        for n in re.findall(r'\d{2,}', q.get('stem') or ''):
            if n not in _whole_raw[0] and QE.norm(n) not in _whole_norm[0]:
                issues.append('题干数字全书查不到：' + n)
        if q.get('type') == 'blank':
            ans = q.get('answer')
            for a in (ans if isinstance(ans, list) else [ans]):
                na = norm(a)
                if len(na) >= 2 and na not in _wnorm and na not in _whole_norm[0]:
                    issues.append('填空答案全书查不到：' + str(a)[:12])
        issues = issues + QE.audit_extra(q, book, blocks, i, {
            "win_norm": win, "win_raw": _wraw, "chap_norm": _cw, "chap_raw": _craw,
            "whole_raw": _whole_raw[0]})
        if issues:
            flags.append((q.get('id'), q.get('stem', ''), issues, i, q.get('page')))

    out_path = bank_sidecar(args.book, '.audit.md')
    L = ['# 题库审核报告 · %s' % args.book, '', '- 生成时间：%s' % datetime.date.today().isoformat(),
         '- 题库：%s（%d 题）' % (os.path.relpath(find_bank(args.book), ROOT).replace(os.sep, '/'), len(bank.get('questions', []))),
         '- 规则：引文逐字 / 空白答案 / 数字 / 答案侧数字与答案键 / 内部标记泄漏', '']
    if not flags:
        L.append('**结论：未发现与原文不一致的题目。**')
    else:
        L.append('**结论：%d 题需要人工确认。**' % len(flags))
        L += ['', '| 题号 | 题干（截断） | 疑点 | block | 页 |', '| --- | --- | --- | --- | --- |']
        for qid, stem, issues, blk, page in flags:
            L.append('| %s | %s | %s | %s | %s |' % (qid, str(stem).replace('|', '/')[:34], '；'.join(issues)[:60], blk, page))
        L += ['', '> 两类原因：① 题库按通行本写了正确写法，原书是 OCR 错字；② 题目本身引用有误。']
    atomic_write_text(out_path, '\n'.join(L) + '\n')
    print('=== 题目↔原文 对账：%s ===' % args.book)
    print('题数 %d ｜ 可疑 %d' % (len(bank.get('questions', [])), len(flags)))
    for qid, stem, issues, blk, page in flags:
        print('  %-14s %-30s %s' % (qid, str(stem)[:30], '；'.join(issues)[:70]))
    print('报告：%s' % out_path)
    return 0 if not flags else 1


def cmd_repair(args):
    book = load_book(args.book)
    blocks = book['blocks']
    d = parts_dir(args.book)
    files = sorted(glob.glob(os.path.join(d, '_repairs_*.json')))
    if not files:
        raise SystemExit('没有修订单：%s' % os.path.join(d, '_repairs_*.json'))
    ok, fail, unfix = [], [], []
    for fp in files:
        data = read_json(fp) or {}
        src = os.path.basename(fp)
        for it in data.get('fixes', []):
            idx, old, new = it.get('block'), it.get('old') or '', it.get('new') or ''
            if not isinstance(idx, int) or not (0 <= idx < len(blocks)):
                fail.append((src, idx, old[:20], 'block 越界'))
                continue
            t = blocks[idx].get('text') or ''
            if not old or old not in t:
                fail.append((src, idx, old[:24], 'old 不在该块'))
                continue
            blocks[idx]['text'] = t.replace(old, new, 1)
            ok.append((src, idx, old[:24], new[:24]))
        for u in data.get('unfixable', []):
            unfix.append((src, u.get('block'), (u.get('note') or '')[:40]))
    repl = {}
    for fp in files:
        for it in (read_json(fp) or {}).get('fixes', []):
            if it.get('old'):
                repl[it['old']] = it.get('new') or ''
    synced = 0
    for ch in book.get('chapters', []):
        for b in ch.get('blocks') or []:
            t = b.get('text')
            if not isinstance(t, str):
                continue
            for old, new in repl.items():
                if old in t:
                    b['text'] = t.replace(old, new, 1)
                    t = b['text']
                    synced += 1
    print('=== 应用修订单：%s ===' % args.book)
    print('成功 %d ｜ 失败 %d ｜ 无法自动修复 %d ｜ 章内副本同步 %d' % (len(ok), len(fail), len(unfix), synced))
    if fail:
        print('失败明细（已生效的条目也会出现在这里）：')
        for x in fail[:20]:
            print('  [%s] block=%s %r -> %s' % x)
    if args.dry_run:
        print('（dry-run，未写入）')
        return 0
    if not ok:
        print('没有可应用的修改')
        return 1
    bp = os.path.join(BOOKS, args.book, 'book.json')
    bak = bp + '.bak_ocrrepair'
    if not os.path.exists(bak):
        shutil.copy2(bp, bak)
    atomic_write_json(bp, book)
    print('已写入：%s' % bp)
    return 0


def cmd_reanchor(args):
    ob = load_book(args.old)
    nb = load_book(args.new)
    bank = read_json(args.bank)
    if not bank:
        raise SystemExit('题库不存在：%s' % args.bank)
    obl, nbl = ob['blocks'], nb['blocks']
    by_page = {}
    for i, b in enumerate(nbl):
        by_page.setdefault(b.get('page'), []).append((i, norm(b.get('text') or '')))

    def best(cands, target):
        bi, bs = None, 0.0
        for idx, txt in cands:
            r = difflib.SequenceMatcher(None, target, txt).ratio()
            if r > bs:
                bi, bs = idx, r
        return bi, bs

    allc = [(i, norm(b.get('text') or '')) for i, b in enumerate(nbl)]
    changed, low, failed = 0, [], []
    for q in bank.get('questions', []):
        oi = q.get('block')
        if not isinstance(oi, int) or not (0 <= oi < len(obl)):
            failed.append((q.get('id'), '旧 block 非法'))
            continue
        target = norm(obl[oi].get('text') or '')
        if len(target) < 10:
            target = norm(''.join((obl[j].get('text') or '') for j in range(max(0, oi - 1), min(len(obl), oi + 2))))
        cands = by_page.get(obl[oi].get('page')) or []
        ni, sc = best(cands, target) if cands else (None, 0.0)
        if ni is None or sc < 0.45:
            ni2, sc2 = best(allc, target)
            if sc2 > sc:
                ni, sc = ni2, sc2
        if ni is None:
            failed.append((q.get('id'), '无匹配'))
            continue
        if sc < 0.6:
            low.append((q.get('id'), q.get('page'), round(sc, 2)))
        if ni != oi:
            changed += 1
        q['block'] = ni
        q['page'] = nbl[ni].get('page')
    print('题目 %d ｜ block 变化 %d ｜ 低置信 %d ｜ 失败 %d' % (len(bank.get('questions', [])), changed, len(low), len(failed)))
    if low:
        print('低置信（前 15）：', low[:15])
    if failed:
        print('失败：', failed[:10])
    if args.dry_run:
        print('（dry-run，未写入）')
        return 0
    bak = args.bank + '.bak_reanchor'
    if not os.path.exists(bak):
        shutil.copy2(args.bank, bak)
    atomic_write_json(args.bank, bank)
    print('已写入：%s' % args.bank)
    return 0


def cmd_all(args):
    rc = cmd_build(args)
    if rc != 0:
        return rc
    rc = cmd_check(args)
    if rc != 0:
        return rc
    return cmd_audit(args)


def cmd_support(args):
    """答案依据筛查：把全库筛成一份「需人工复核」的短清单。

    判据（均经注入错误 + 已知问题反测标定）：
      1. 数字全书查不到        强可疑，注入检出率 100%、正常题库误报 0
      2. 填空答案词未命中锚点   命中率 < 80% 报警
      3. 名解/简答 4-gram 覆盖率 < 0.15
      4. 锚点优势倍数 < 3       锚点窗口覆盖率 / 随机块平均覆盖率

    这是「筛查器」不是「判定器」：它筛出可疑，最终仍需人回原书看。
    已知局限：语义相邻的锚点偏移（如薄板共振 vs 穿孔板共振）文本统计分不开。
    """
    book = load_book(args.book)
    blocks = book.get('blocks') or []
    bank = read_json(find_bank(args.book))
    if not bank:
        raise SystemExit('题库不存在，先跑 build')
    span = max(0, int(args.window))
    rnd_n = max(0, int(args.random))
    chs = book.get('chapters') or []
    rng_map = {c['id']: (c.get('startBlock', 0),
                         chs[j + 1].get('startBlock') if j + 1 < len(chs) else len(blocks))
               for j, c in enumerate(chs)}

    def btext(b):
        return (b.get('text') or '') + ' ' + (b.get('caption') or '')

    def han(s):
        return ''.join(ch for ch in (s or '') if '一' <= ch <= '鿿')

    def win_raw(i):
        return ' '.join(btext(blocks[j]) for j in range(max(0, i - span), min(len(blocks), i + span + 1)))

    full_raw = ' '.join(btext(b) for b in blocks)
    full_han = han(full_raw)

    def cov4(txt, wraw):
        a = han(txt)
        if len(a) < 4:
            return None
        wn = han(wraw)
        hit = tot = 0
        for k in range(0, len(a) - 3, 2):
            tot += 1
            if a[k:k + 4] in wn:
                hit += 1
        return hit / tot if tot else None

    def ans_text(q):
        a = q.get('answer')
        return a if isinstance(a, str) else ' '.join(str(x) for x in (a or []))

    import random as _random
    rng = _random.Random(20261107)
    flags = []
    for q in bank.get('questions') or []:
        i = q.get('block')
        if not isinstance(i, int) or not (0 <= i < len(blocks)):
            flags.append((q.get('id'), q.get('chapter'), q.get('type'), '锚点越界', q.get('page'), ''))
            continue
        at = ans_text(q)
        wraw = win_raw(i)
        why, det = [], ''
        nums = re.findall(r'\d+(?:\.\d+)?', at)
        miss = [n for n in nums if n not in full_raw]
        if miss:
            why.append('数字全书查不到')
            det = ','.join(miss[:4])
        qt = q.get('type')
        if qt == 'blank':
            arr = q.get('answer') if isinstance(q.get('answer'), list) else [q.get('answer')]
            items = [str(x).strip() for x in arr if str(x).strip()]
            if items:
                hit = 0
                for a in items:
                    ha = han(a)
                    if (ha and ha in han(wraw)) or (not ha and a in wraw):
                        hit += 1
                if hit / len(items) < 0.8:
                    why.append('填空答案词未命中')
                    det = det or ('命中 %d/%d' % (hit, len(items)))
        elif qt in ('term', 'short'):
            c = cov4(at, wraw)
            if c is not None and c < 0.15:
                why.append('名解/简答覆盖率低')
                det = det or ('cov=%.2f' % c)
        # 锚点优势倍数只对「长答案」有意义；填空题答案多是词、选择题答案是字母，4-gram 不适用
        c0 = cov4(at, wraw) if qt in ('term', 'short') else None
        if c0 is not None and rnd_n:
            vals = []
            for _ in range(rnd_n):
                cj = cov4(at, win_raw(rng.randrange(len(blocks))))
                if cj is not None:
                    vals.append(cj)
            if vals:
                mean = sum(vals) / len(vals)
                ratio = c0 / max(mean, 0.01)
                if ratio < 3:
                    why.append('锚点优势低')
                    det = det or ('锚点×%.1f' % ratio)
        if why:
            flags.append((q.get('id'), q.get('chapter'), qt, '；'.join(why), q.get('page'), det))

    outp = bank_sidecar(args.book, '.support.md')
    total = len(bank.get('questions') or [])
    import time as _time
    L = ['# 答案依据筛查 · %s' % (book.get('bookTitle') or args.book), '',
         '- 生成时间：%s' % _time.strftime('%Y-%m-%d %H:%M'),
         '- **快照时效**：本清单是生成那一刻的状态。题号可能随后续迁章/改名失效（如 qb-c2-049 → qb-c1-070）；'
         '拿题号去成品库解析前，请先核对现行 id。',
         '- 题库：%d 题' % total,
         '- 窗口：锚点 ±%d 块' % span,
         '- 随机对照：每题 %d 个随机块' % rnd_n,
         '- 判据：数字全书查不到 / 填空答案词未命中 / 名解简答覆盖率低 / 锚点优势低', '',
         '**结论：%s**' % ('未发现可疑题。' if not flags else '%d 题需人工复核（占 %.1f%%）。' % (len(flags), 100.0 * len(flags) / max(1, total))), '']
    if flags:
        L += ['| 题号 | 章 | 题型 | 报警 | 页 | 细节 |', '| --- | --- | --- | --- | --- | --- |']
        for f in flags:
            L.append('| %s | %s | %s | %s | %s | %s |' % (f[0], f[1] or '', TYPE_CN.get(f[2], f[2] or ''), f[3], f[4] if f[4] is not None else '', f[5]))
        L += ['', '> 这是**筛查清单**不是错误判定：请逐条回原书看。误报常见原因——答案综合了多个块、答案词序被重组、单字答案词。',
              '> 已知盲区：语义相邻的两个段落（如薄板共振 vs 穿孔板共振）文本统计分不开。']
    open(outp, 'w', encoding='utf-8', newline='\n').write('\n'.join(L) + '\n')
    print('=== 答案依据筛查：%s ===' % (book.get('bookTitle') or args.book))
    print('题数 %d ｜ 可疑 %d' % (total, len(flags)))
    for f in flags[:args.show]:
        print('  %s | %s | %s' % (f[0], f[3], f[5]))
    print('报告：%s' % outp)
    return 0


def cmd_report(args):
    book = load_book(args.book)
    d = parts_dir(args.book)
    plan = read_json(os.path.join(d, '_plan.json')) or {}
    bank = read_json(find_bank(args.book)) or {}
    audit = bank_sidecar(args.book, '.audit.md')
    audit_txt = open(audit, encoding='utf-8').read() if os.path.exists(audit) else '（未生成）'
    L = ['# 题库报告 · %s' % (book.get('bookTitle') or args.book), '',
         '- bookId：%s' % args.book, '- 题数：%d' % len(bank.get('questions', [])),
         '- 题型：%s' % (bank.get('stats', {}).get('byType') or {}),
         '- 计划：%s' % (plan.get('total', '未规划')),
         '- 书稿：%d 页 / %d 章 / %s 字' % ((book.get('stats') or {}).get('pages', 0),
                                            len(book.get('chapters') or []),
                                            (book.get('stats') or {}).get('chars', 0)), '',
         '## 分章', '', '| chapter | 标题 | 题数 |', '| --- | --- | --- |']
    byc = (bank.get('stats') or {}).get('byChapter') or {}
    for c in book.get('chapters') or []:
        L.append('| %s | %s | %d |' % (c['id'], c.get('title') or '', byc.get(c['id'], 0)))
    L += ['', '## 审核', '', audit_txt]
    nt = QE.count_tables(book)
    if nt:
        L.insert(4, '- 表格块：%d 个（表格文字 %d 字，已计入书稿字数）' % (nt, QE.table_chars(book)))
    out = bank_sidecar(args.book, '.report.md')
    atomic_write_text(out, '\n'.join(L))
    print('报告：%s' % out)
    return 0


def cmd_books(args):
    ids = QE.list_books(BOOKS)
    print('books/ 下共 %d 本（书名可只打能唯一定位的片段）：' % len(ids))
    for d in ids:
        print('  ' + d)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog='qbank.py', description='题库制作流水线')
    sub = p.add_subparsers(dest='cmd', required=True)

    q = sub.add_parser('plan', help='规划题量、生成任务书')
    q.add_argument('book'); q.add_argument('--total', type=int); q.add_argument('--density', type=float, default=1.0)
    q.add_argument('--groups', type=int, default=5); q.add_argument('--prefix'); q.add_argument('--min-chars', type=int, default=1000)
    q.add_argument('--min-per-chapter', type=int, default=5); q.add_argument('--skip'); q.add_argument('--force', action='store_true')
    q.set_defaults(func=cmd_plan)

    q = sub.add_parser('dump', help='导出某章正文'); q.add_argument('book'); q.add_argument('chapter'); q.set_defaults(func=cmd_dump)
    q = sub.add_parser('status', help='查看进度'); q.add_argument('book'); q.set_defaults(func=cmd_status)

    for name, fn in (('build', cmd_build), ('check', cmd_check), ('audit', cmd_audit)):
        q = sub.add_parser(name)
        q.add_argument('book')
        if name == 'build':
            q.add_argument('--force', action='store_true', help='题库已存在且题数更多时仍覆盖')
        if name == 'audit':
            q.add_argument('--window', type=int, default=6)
        q.set_defaults(func=fn)

    q = sub.add_parser('repair', help='应用修订单'); q.add_argument('book'); q.add_argument('--dry-run', action='store_true'); q.set_defaults(func=cmd_repair)
    q = sub.add_parser('reanchor', help='重转后迁移锚点'); q.add_argument('old'); q.add_argument('new'); q.add_argument('bank'); q.add_argument('--dry-run', action='store_true'); q.set_defaults(func=cmd_reanchor)
    q = sub.add_parser('all', help='build+check+audit'); q.add_argument('book'); q.set_defaults(func=cmd_all)
    q = sub.add_parser('support', help='答案依据筛查（输出需人工复核的短清单）')
    q.add_argument('book')
    q.add_argument('--window', type=int, default=2)
    q.add_argument('--random', type=int, default=5, help='每题随机对照块数')
    q.add_argument('--show', type=int, default=10, help='终端打印前 N 条')
    q.set_defaults(func=cmd_support)
    q = sub.add_parser('report', help='生成汇总报告'); q.add_argument('book'); q.set_defaults(func=cmd_report)
    q = sub.add_parser('books', help='列出可用书名')
    q.set_defaults(func=cmd_books)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    for attr in ('book', 'old', 'new', 'bankdir', 'bookdir'):
        v = getattr(args, attr, None)
        if isinstance(v, str) and v and attr != 'bank':
            setattr(args, attr, QE.resolve_book(BOOKS, v))
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
