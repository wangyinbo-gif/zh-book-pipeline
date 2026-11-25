# -*- coding: utf-8 -*-
"""校验「离线静态版」是否真的能双击就用（纯标准库，不需要 Node / 浏览器）。

用法：
    python tools/verify_offline.py                        # 校验 _dist/筑学书库-离线版
    python tools/verify_offline.py --dir D:/x/离线版
    python tools/verify_offline.py --zip _dist/筑学书库-离线版-2026-11-06.zip   # 顺带校验 zip

校验项：
  1 结构：共享前端文件、data/routes.js、data/books.js、index.html、使用说明.txt、版本.txt 齐备
  2 数量：reader-*.html 数 = 书数；practice-*.html 数 = 题库数；与 导出报告.json 一致
  3 内容：每个页面内嵌的 __OFFLINE__ 能被 JSON 解析，bookId 与路由一致，块数 > 0，题库题数与索引一致
  4 脚本：每页只加载一次 reader.js / practice.js / shelf.js（重复加载会重复执行）
  5 引用：页面里不出现 href="/、src="/（file:// 下绝对路径必然白屏或丢图）
  6 插图：书稿里每个 figure.src / cover 都能在 books/<hash>/ 下找到文件
  7 zip（可选）：条目数、顶层目录、中文文件名的 UTF-8 标记、条目数与目录一致
失败时退出码 1，并列出前若干条失败明细。
"""
import argparse
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, '_dist', '筑学书库-离线版')
REQUIRED = ['index.html', 'style.css', 'practice.css', 'shelf.js', 'reader.js', 'practice.js',
            'offline-api.js', 'data/routes.js', 'data/books.js', '使用说明.txt', '版本.txt']
SCRIPT_TAGS = {'index.html': 'shelf.js', 'reader-': 'reader.js', 'practice-': 'practice.js'}
DEC = json.JSONDecoder()


def read_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def load_js_var(text, name):
    """从 window.<name>=<json>; 里解析 JSON（内嵌时 </ 被转义成 <\\/，仍是合法 JSON）。"""
    marker = 'window.%s=' % name
    i = text.find(marker)
    if i < 0:
        raise ValueError('找不到 window.%s' % name)
    obj, _end = DEC.raw_decode(text, i + len(marker))
    return obj


def load_offline(html):
    marker = 'window.__OFFLINE__='
    i = html.find(marker)
    if i < 0:
        raise ValueError('找不到 window.__OFFLINE__')
    obj, _end = DEC.raw_decode(html, i + len(marker))
    return obj


def check_dir(base, fails, notes):
    def fail(msg):
        fails.append(msg)

    for fn in REQUIRED:
        if not os.path.exists(os.path.join(base, fn)):
            fail('缺少文件: ' + fn)
    if not os.path.isdir(os.path.join(base, 'books')):
        fail('缺少 books/ 目录（插图会全部丢）')

    routes = load_js_var(read_text(os.path.join(base, 'data', 'routes.js')), '__BOOK_ROUTES__')
    qindex = load_js_var(read_text(os.path.join(base, 'data', 'routes.js')), '__QBANK_INDEX__')
    books = load_js_var(read_text(os.path.join(base, 'data', 'books.js')), '__BOOKS__')
    notes.append('路由: %d 本书 / %d 套题库；书架元数据: %d 条' % (len(routes), len(qindex), len(books)))
    if len(books) != len(routes):
        fail('books.js(%d) 与 routes.js(%d) 数量不一致' % (len(books), len(routes)))

    files = set(os.listdir(base))
    readers = sorted(f for f in files if re.match(r'^reader-[0-9a-f]{8}\.html$', f))
    practices = sorted(f for f in files if re.match(r'^practice-[0-9a-f]{8}\.html$', f))
    want_readers = [r['reader'] for r in routes.values()]
    want_practices = [r['practice'] for r in routes.values() if r.get('practice')]
    for f in want_readers:
        if f not in files:
            fail('路由指向的阅读页不存在: ' + f)
    for f in want_practices:
        if f not in files:
            fail('路由指向的刷题页不存在: ' + f)
    if len(readers) != len(want_readers):
        fail('磁盘上 reader 页 %d 个，路由 %d 个' % (len(readers), len(want_readers)))
    if len(practices) != len(want_practices):
        fail('磁盘上 practice 页 %d 个，题库 %d 套' % (len(practices), len(want_practices)))
    notes.append('页面: %d 个阅读页 / %d 个刷题页' % (len(readers), len(practices)))

    # index.html
    try:
        idx = read_text(os.path.join(base, 'index.html'))
        if idx.count('src="shelf.js"') != 1:
            fail('index.html 里 shelf.js 出现 %d 次（应为 1）' % idx.count('src="shelf.js"'))
        for need in ('data/routes.js', 'data/books.js', 'offline-api.js'):
            if need not in idx:
                fail('index.html 未引用 ' + need)
    except OSError as e:
        fail('index.html 读取失败: %s' % e)

    # 每个阅读页
    hash_to_book = {r['hash']: bid for bid, r in routes.items()}
    page_ok = 0
    for f in readers:
        try:
            html = read_text(os.path.join(base, f))
        except OSError as e:
            fail('%s 读取失败: %s' % (f, e))
            continue
        if html.count('src="reader.js"') != 1:
            fail('%s 里 reader.js 出现 %d 次（应为 1，重复加载会重复执行）' % (f, html.count('src="reader.js"')))
        if 'src="offline-api.js"' not in html:
            fail('%s 未引用 offline-api.js' % f)
        if re.search(r'(href|src)="/', html):
            fail('%s 含绝对路径 href="/ 或 src="/' % f)
        try:
            data = load_offline(html)
        except Exception as e:
            fail('%s 内嵌 JSON 解析失败: %s' % (f, e))
            continue
        bid = data.get('bookId')
        book = data.get('book') or {}
        if not bid or bid not in routes:
            fail('%s 的 bookId 不在路由表里: %r' % (f, bid))
            continue
        if routes[bid]['reader'] != f:
            fail('%s 与路由不符（路由给的是 %s）' % (f, routes[bid]['reader']))
            continue
        blocks = book.get('blocks') or []
        if not blocks:
            fail('%s 的书稿 blocks 为空' % f)
            continue
        h = routes[bid]['hash']
        for b in blocks:
            if b.get('type') in ('figure', 'cover') and b.get('src'):
                p = os.path.join(base, 'books', h, b['src'].replace('/', os.sep))
                if not os.path.exists(p):
                    fail('%s 缺插图: books/%s/%s' % (f, h, b['src']))
                    break
        cov = book.get('cover')
        if cov and not os.path.exists(os.path.join(base, 'books', h, cov.replace('/', os.sep))):
            fail('%s 缺封面图: books/%s/%s' % (f, h, cov))
        page_ok += 1
    notes.append('阅读页自检通过: %d/%d' % (page_ok, len(readers)))

    # 每个刷题页
    by_title = {}
    for meta in qindex:
        key = (meta.get('bookTitle') or '', meta.get('author') or '')
        by_title[key] = meta.get('total')
    pr_ok = 0
    for f in practices:
        html = read_text(os.path.join(base, f))
        if html.count('src="practice.js"') != 1:
            fail('%s 里 practice.js 出现 %d 次' % (f, html.count('src="practice.js"')))
        if re.search(r'(href|src)="/', html):
            fail('%s 含绝对路径' % f)
        try:
            data = load_offline(html)
        except Exception as e:
            fail('%s 内嵌 JSON 解析失败: %s' % (f, e))
            continue
        bank = data.get('bank') or {}
        qs = bank.get('questions') or []
        if not qs:
            fail('%s 的题库为空' % f)
            continue
        expect = by_title.get((bank.get('bookTitle') or '', bank.get('author') or ''))
        if expect is not None and expect != len(qs):
            fail('%s 题数 %d 与索引 %d 不一致' % (f, len(qs), expect))
        pr_ok += 1
    notes.append('刷题页自检通过: %d/%d' % (pr_ok, len(practices)))
    notes.append('题库总题数: %d' % sum((m.get('total') or 0) for m in qindex))

    # 报告与文本一致性
    rp = os.path.join(base, '导出报告.json')
    if os.path.exists(rp):
        try:
            rep = json.loads(read_text(rp))
            if rep.get('books') != len(readers):
                fail('导出报告 books=%s 与实际 %d 不符' % (rep.get('books'), len(readers)))
            if rep.get('qbanks') != len(practices):
                fail('导出报告 qbanks=%s 与实际 %d 不符' % (rep.get('qbanks'), len(practices)))
        except Exception as e:
            fail('导出报告.json 解析失败: %s' % e)
    vt = os.path.join(base, '版本.txt')
    if os.path.exists(vt):
        txt = read_text(vt)
        if ('书籍: %d 本' % len(readers)) not in txt:
            fail('版本.txt 里的书籍数与实际 %d 不符' % len(readers))
    return readers, practices


def check_zip(zip_path, base, readers, practices, fails, notes):
    if not os.path.exists(zip_path):
        fails.append('zip 不存在: ' + zip_path)
        return
    top = os.path.basename(base.rstrip('\\/')) + '/'
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        notes.append('zip: %d 条目, %.2f GB, 压缩率 %.2f' %
                     (len(names), os.path.getsize(zip_path) / 1073741824.0,
                      os.path.getsize(zip_path) / float(sum(i.file_size for i in infos) or 1)))
        bad_top = [n for n in names if not (n.startswith(top) or n == top)]
        if bad_top:
            fails.append('zip 里有 %d 个条目不在顶层目录 %s 下，例如 %s' % (len(bad_top), top, bad_top[0]))
        for need in ['index.html', 'data/routes.js', 'offline-api.js', '使用说明.txt', '版本.txt']:
            if top + need not in names:
                fails.append('zip 缺少 %s%s' % (top, need))
        # 目录文件数 vs 条目数
        disk = 0
        for _dp, _dns, fns in os.walk(base):
            disk += len(fns)
        if disk != len(names):
            fails.append('zip 条目 %d 个，目录实际文件 %d 个（打包后有改动，需要重打）' % (len(names), disk))
        # 中文文件名必须带 UTF-8 标记，否则 Windows 资源管理器解压会乱码
        cn = [i for i in infos if any(ord(c) > 127 for c in i.filename)]
        noflag = [i.filename for i in cn if not (i.flag_bits & 0x800)]
        if noflag:
            fails.append('%d 个中文名条目缺少 UTF-8 标记，解压可能乱码，例如 %s' % (len(noflag), noflag[0]))
        else:
            notes.append('中文名条目 %d 个，均已带 UTF-8 标记' % len(cn))
        # 只有 webp 用 STORED，文本应被压缩
        stored_txt = [i.filename for i in infos
                      if i.filename.lower().endswith(('.html', '.js', '.css', '.txt', '.json'))
                      and i.compress_type == zipfile.ZIP_STORED]
        if stored_txt:
            fails.append('%d 个文本条目未压缩（体积会变大），例如 %s' % (len(stored_txt), stored_txt[0]))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='校验离线静态版')
    ap.add_argument('--dir', default=DEFAULT_DIR)
    ap.add_argument('--zip', default=None, help='额外校验 zip（不给则自动找 _dist/ 下同名日期包）')
    args = ap.parse_args()
    base = os.path.abspath(args.dir)
    if not os.path.isdir(base):
        raise SystemExit('目录不存在: ' + base)
    fails, notes = [], []
    readers, practices = check_dir(base, fails, notes)
    zp = args.zip
    if zp is None:
        cand = sorted(f for f in os.listdir(os.path.dirname(base))
                      if f.startswith(os.path.basename(base) + '-') and f.endswith('.zip'))
        if cand:
            zp = os.path.join(os.path.dirname(base), cand[-1])
    if zp:
        check_zip(zp, base, readers, practices, fails, notes)

    print('== 离线包校验: %s' % base)
    for n in notes:
        print('   ' + n)
    print('== PASS %d 项检查 / FAIL %d' % (len(notes), len(fails)))
    for f in fails[:30]:
        print('  [FAIL] ' + f)
    if fails:
        print('!! 有 %d 项失败' % len(fails))
        return 1
    print('OK 离线包结构与内容自检通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
