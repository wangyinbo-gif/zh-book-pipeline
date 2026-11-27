# -*- coding: utf-8 -*-
"""生成「离线静态版」：没有 Node 的 Windows/Mac 解压后双击 index.html 即可用。

用法：
    python tools/export_offline.py                 # → _dist/筑学书库-离线版/
    python tools/export_offline.py --dry-run       # 只统计，不写盘
    python tools/export_offline.py --copy          # 图片真实复制（默认硬链接）
    python tools/export_offline.py --no-assets     # 不复制图片（调试用）
    python tools/export_offline.py --out D:/x/离线版
    python tools/export_offline.py --zip           # 导出后顺带打 zip（含 .sha256.txt）

结构：index.html（书架）+ reader-<hash>.html（每本一个）+ practice-<hash>.html（每个题库一个）
      + review/（复习笔记）+ data/routes.js + data/books.js + offline-api.js
      + books/<hash>/assets/ + 使用说明.txt
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile

if __package__:
    from .build_review_notes import build_review_notes
else:
    from build_review_notes import build_review_notes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS = os.path.join(ROOT, 'books')
QBANK = os.path.join(ROOT, '题库')
PUBLIC = os.path.join(ROOT, 'public')
DEFAULT_OUT = os.path.join(ROOT, '_dist', '筑学书库-离线版')
SHARED = ['style.css', 'practice.css', 'shelf.js', 'reader.js', 'practice.js', 'offline-api.js', 'auth-client.js']
SKIP_DIR_PREFIX = '_'
# 这些格式已经压缩过，zip 里用 STORED，别再浪费 CPU
STORE_EXT = {'.webp', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff2', '.woff', '.ttf', '.otf', '.mp4', '.pdf'}


def sha8(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]


def read_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def embed_json(obj):
    """内嵌到 <script> 里的 JSON：转义 </ 与 U+2028/2029，避免提前闭合或语法错误。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    s = s.replace('</', '<\\/')
    s = s.replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    return s


def book_meta(book_id, book):
    chapters = book.get('chapters') or []
    return {
        'id': book_id,
        'bookTitle': book.get('bookTitle') or book.get('title') or book_id,
        'title': book.get('title') or '',
        'source': book.get('source') or '',
        'author': book.get('author') or '',
        'shelfCategory': book.get('shelfCategory') or '扩展',
        'pageRange': book.get('pageRange') or [],
        'generatedAt': book.get('generatedAt') or '',
        'chapters': [
            {'id': c.get('id'), 'title': c.get('title'), 'chars': c.get('chars') or 0,
             'figures': c.get('figures') or 0, 'startPage': c.get('startPage')}
            for c in chapters
        ],
        'stats': book.get('stats') or {},
        'cover': book.get('cover') or '',
    }


def list_books():
    out = []
    if not os.path.isdir(BOOKS):
        return out
    for name in sorted(os.listdir(BOOKS)):
        if name.startswith(SKIP_DIR_PREFIX):
            continue
        d = os.path.join(BOOKS, name)
        f = os.path.join(d, 'book.json')
        if not os.path.isdir(d) or not os.path.exists(f):
            continue
        try:
            book = json.loads(read_text(f))
        except Exception as e:
            print('[warn] 跳过无法解析的 book.json:', name, e)
            continue
        out.append((name, book, d))
    return out


def qbank_index():
    """与 server.js 同规则：递归 <=3 层、跳过 _ 目录、同 bookId 取最浅。"""
    found = {}
    if not os.path.isdir(QBANK):
        return found
    for cur, dirs, files in os.walk(QBANK):
        depth = cur[len(QBANK):].count(os.sep)
        dirs[:] = [d for d in dirs if not d.startswith(SKIP_DIR_PREFIX)] if depth < 3 else []
        if depth > 3:
            continue
        for fn in files:
            if not fn.lower().endswith('.json'):
                continue
            book_id = fn[:-5]
            if not book_id or book_id.startswith(SKIP_DIR_PREFIX):
                continue
            full = os.path.join(cur, fn)
            prev = found.get(book_id)
            if prev is None or full.count(os.sep) < prev.count(os.sep):
                found[book_id] = full
    banks = {}
    for book_id, path in found.items():
        try:
            bank = json.loads(read_text(path))
        except Exception:
            continue
        if isinstance(bank, dict) and isinstance(bank.get('questions'), list):
            banks[book_id] = bank
    return banks


def qbank_meta(bank):
    by_type = {}
    for q in bank.get('questions') or []:
        t = q.get('type')
        by_type[t] = by_type.get(t, 0) + 1
    return {
        'bookId': bank.get('bookId') or '',
        'bookTitle': bank.get('bookTitle') or bank.get('bookId') or '',
        'author': bank.get('author') or '',
        'total': len(bank.get('questions') or []),
        'byType': by_type,
        'chapters': bank.get('chapters') or [],
    }


def to_relative(html):
    html = html.replace('href="/"', 'href="index.html"')
    html = re.sub(r'(href|src)="/([^"]*)"', r'\1="\2"', html)
    return html


def replace_once(html, marker, block):
    """用 block 替换 marker 本身（不是插在前面），否则原 <script> 会留下重复加载。"""
    idx = html.index(marker)
    return html[:idx] + block + html[idx + len(marker):]


def render_index(template):
    template = template.replace(
        '加书方法：把 PDF 放进 <code>待转换书籍</code> 文件夹 → 双击 <code>一键转换书籍.bat</code> → 回来点「刷新」',
        '离线版：书稿、题库、插图已打包在本文件夹；双击 index.html 即可阅读。书稿更新后重新运行 tools/export_offline.py 再整体发送。')
    block = (
        '<script src="data/routes.js"></script>\n'
        '<script src="data/books.js"></script>\n'
        '<script src="offline-api.js"></script>\n'
        '<script src="shelf.js"></script>'
    )
    return replace_once(to_relative(template), '<script src="shelf.js"></script>', block)


def render_reader(template, book_id, book):
    data = {'page': 'reader', 'bookId': book_id, 'book': book}
    block = (
        '<script src="data/routes.js"></script>\n'
        '<script>window.__OFFLINE__=' + embed_json(data) + ';</script>\n'
        '<script src="offline-api.js"></script>\n'
        '<script src="reader.js"></script>'
    )
    return replace_once(to_relative(template), '<script src="reader.js"></script>', block)


def practice_book_blob(book_id, banks):
    """给刷题页内嵌正文块，供「出处核验」用。

    practice.js 的 verifySources() 会 fetch('/api/books/<id>')。离线包里有 44 本书、
    44 个 reader 页，但只有 9 个 practice 页——把书全塞进每个刷题页会让包暴涨。
    所以这里只内嵌「有题库的那些书」的 blocks（问答用到的书就这几本），
    放在 window.__SRC_BOOKS__ 里，由 offline-api.js 的 /api/books/<id> 分支返回。
    """
    if not book_id:
        return None
    bp = os.path.join(BOOKS, book_id, 'book.json')
    if not os.path.isfile(bp):
        return None
    book = json.loads(read_text(bp))
    blocks = book.get('blocks')
    if not isinstance(blocks, list):
        return None
    return {
        'bookId': book_id,
        'title': book.get('title') or book_id,
        'blocks': [{'type': b.get('type'), 'text': b.get('text') or ''} for b in blocks],
    }


def render_practice(template, book_id, bank, book_blob=None):
    data = {'page': 'practice', 'bookId': book_id, 'bank': bank}
    block = (
        '<script src="data/routes.js"></script>\n'
        '<script>window.__OFFLINE__=' + embed_json(data) + ';</script>\n'
        + ('<script>window.__SRC_BOOKS__=' + embed_json({book_id: book_blob}) + ';</script>\n'
           if book_blob else '')
        + '<script src="offline-api.js"></script>\n'
        '<script src="practice.js"></script>'
    )
    return replace_once(to_relative(template), '<script src="practice.js"></script>', block)


def bank_label(b):
    """题库显示名：同名书（两本《公共建筑设计原理》）靠作者区分。"""
    title = b.get('bookTitle') or b.get('bookId') or ''
    author = (b.get('author') or '').strip()
    return '%s（%s）' % (title, author) if author else title


def make_readme(book_count, banks, exported_at, total_files, total_bytes):
    bank_lines = '\n'.join('  · %s — %d 题' % (bank_label(b), b['total']) for b in banks) or '  · （本包没有题库）'
    return '''筑学书库 · 离线版
导出时间：%s
本包体积：%s（%d 个文件）

怎么用
1. 解压整个文件夹（不要只把 index.html 单独拖出来，书架、书、图都靠同目录的其它文件）。
2. 双击 index.html，浏览器打开书架。
3. 点一本书开始读；有题库的书，顶栏会出现「刷题」按钮。
4. 在书架筛选栏点「复习笔记」，可按书目阅读整理好的笔记。

需要装什么
- 什么都不用装：不需要 Node、不需要 Python、不需要联网。
- Windows：用 Edge 或 Chrome 打开（双击 index.html 默认就是它们）。
- Mac：双击后可能被 Safari 接管；建议右键「打开方式 → Google Chrome」。
  Safari 对本地文件的存储限制较多，阅读没问题，但进度可能存不住（页面会提示）。

关于进度
- 阅读进度、书签、刷题记录只存在你这台电脑的浏览器里：换电脑、换浏览器、清浏览器数据都会丢。
- 页面底部如果出现黄色提示条，说明浏览器禁止本地存储，本次阅读照常，只是进度不保存。

包里有什么
- 书架 %d 本：完整书稿 + 插图（按页排好，可翻页、跳章、搜正文、加书签）。
- 题库 %d 套：
%s
- books/<8位编号>/assets 是插图，不要删、不要改名，整个文件夹是一个整体。

小提示
- 第一次解压/打开时杀毒软件扫描可能较慢（本包约 %s），等一会儿就好。
- 书稿或题库更新后，整包会重新导出一次；旧包可以整个删掉。
''' % (exported_at, human(total_bytes), total_files, book_count, len(banks), bank_lines, human(total_bytes))


def make_version(book_count, banks, exported_at, total_files, total_bytes):
    banks_txt = ', '.join('%s %d 题' % (bank_label(b), b['total']) for b in banks) or '无'
    return ('筑学书库-离线版\n'
            '导出时间: %s\n'
            '书籍: %d 本\n'
            '题库: %d 套 -> %s\n'
            '文件: %d 个，%s\n'
            '说明: 解压后双击 index.html 即可使用；不需要 Node，Windows/macOS 通用。\n'
            % (exported_at, book_count, len(banks), banks_txt, total_files, human(total_bytes)))


def human(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit)
        n /= 1024.0


def dir_stats(root):
    total, count = 0, 0
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
                count += 1
            except OSError:
                pass
    return count, total


def sha256_file(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()


def make_zip(src_dir, zip_path=None):
    """把离线文件夹打成 zip（Win/Mac 通用）。

    - 包内保留顶层文件夹名，解压不会把三万个文件倒进下载目录；
    - 图片等已压缩格式用 STORED（不再压，快且不涨体积），文本用 DEFLATE；
    - 中文文件名依赖 zipfile 自动写入的 UTF-8 标记（0x800），Win10+/macOS 均能正确显示；
    - 先写 .tmp → 回读校验条目数/字节数/CRC → 旧包改名保留 → 原子替换。
    """
    src_dir = os.path.abspath(src_dir)
    prefix = os.path.basename(src_dir) + '/'
    if zip_path is None:
        zip_path = os.path.join(os.path.dirname(src_dir),
                                '%s-%s.zip' % (os.path.basename(src_dir), time.strftime('%Y-%m-%d')))
    tmp = zip_path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)

    files = []
    total = 0
    for dp, dns, fns in os.walk(src_dir):
        dns.sort()
        for fn in sorted(fns):
            ap = os.path.join(dp, fn)
            if fn.endswith('.tmp'):
                continue
            files.append((ap, prefix + os.path.relpath(ap, src_dir).replace(os.sep, '/')))
            total += os.path.getsize(ap)

    print('zip: %d files, %s -> %s' % (len(files), human(total), zip_path), flush=True)
    t0, done, usize = time.time(), 0, 0
    with zipfile.ZipFile(tmp, 'w', allowZip64=True) as z:
        for ap, arc in files:
            ct = zipfile.ZIP_STORED if os.path.splitext(ap)[1].lower() in STORE_EXT else zipfile.ZIP_DEFLATED
            zi = zipfile.ZipInfo.from_file(ap, arc)
            zi.compress_type = ct
            size = zi.file_size
            with open(ap, 'rb') as fh, z.open(zi, 'w') as out:
                shutil.copyfileobj(fh, out, 1 << 20)
            done += 1
            usize += size
            if done % 2000 == 0 or done == len(files):
                print('  %d/%d  %s  %.0fs' % (done, len(files), human(usize), time.time() - t0), flush=True)

    print('zip: 回读校验（%s）…' % human(os.path.getsize(tmp)), flush=True)
    with zipfile.ZipFile(tmp) as z:
        infos = z.infolist()
        zusize = sum(i.file_size for i in infos)
        bad = z.testzip()
    if len(infos) != done or zusize != usize or bad is not None:
        os.remove(tmp)
        raise SystemExit('zip 校验失败：条目 %d/%d，字节 %d/%d，坏条目 %s（原包未动）'
                         % (len(infos), done, zusize, usize, bad))

    if os.path.exists(zip_path):
        keep = zip_path[:-4] + '.prev.zip'
        if os.path.exists(keep):
            os.remove(keep)
        os.replace(zip_path, keep)
        print('旧 zip 已保留:', os.path.basename(keep))
    os.replace(tmp, zip_path)

    sha = sha256_file(zip_path)
    with open(zip_path + '.sha256.txt', 'w', encoding='utf-8', newline='\n') as f:
        f.write('%s  %s\n' % (sha, os.path.basename(zip_path)))
    report = {'zip': zip_path, 'zipBytes': os.path.getsize(zip_path), 'zipHuman': human(os.path.getsize(zip_path)),
              'entries': len(infos), 'rawBytes': usize, 'sha256': sha,
              'ratio': round(os.path.getsize(zip_path) / float(usize), 3) if usize else 0,
              'elapsedSec': round(time.time() - t0, 1)}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print('SHA256', sha)
    return report


def build(out_dir, copy=False, dry_run=False, no_assets=False):
    t0 = time.time()
    books = list_books()
    banks = qbank_index()
    if dry_run:
        asset_bytes = 0
        asset_files = 0
        for name, book, d in books:
            ad = os.path.join(d, 'assets')
            if os.path.isdir(ad):
                for fn in os.listdir(ad):
                    fp = os.path.join(ad, fn)
                    if os.path.isfile(fp):
                        asset_bytes += os.path.getsize(fp)
                        asset_files += 1
        report = {'books': len(books), 'qbanks': len(banks), 'assetFiles': asset_files,
                  'assetBytes': asset_bytes, 'assetHuman': human(asset_bytes), 'dryRun': True}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return report

    tmp = out_dir + '.tmp-%d' % os.getpid()
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    routes = {}
    bank_metas = []
    for name, book, d in books:
        h = sha8(name)
        routes[name] = {'hash': h, 'reader': 'reader-%s.html' % h,
                        'practice': ('practice-%s.html' % h) if name in banks else None}
        if name in banks:
            bank_metas.append(qbank_meta(banks[name]))
    books_meta = [book_meta(name, book) for name, book, d in books]

    try:
        os.makedirs(os.path.join(tmp, 'data'), exist_ok=True)
        os.makedirs(os.path.join(tmp, 'books'), exist_ok=True)

        write_text(os.path.join(tmp, 'data', 'routes.js'),
                   'window.__BOOK_ROUTES__=' + embed_json(routes) + ';\n'
                   'window.__QBANK_INDEX__=' + embed_json(bank_metas) + ';\n')
        write_text(os.path.join(tmp, 'data', 'books.js'),
                   'window.__BOOKS__=' + embed_json(books_meta) + ';\n')

        for fn in SHARED:
            src = os.path.join(PUBLIC, fn)
            if not os.path.exists(src):
                raise SystemExit('缺少前端文件: ' + src)
            shutil.copy2(src, os.path.join(tmp, fn))

        write_text(os.path.join(tmp, 'index.html'), render_index(read_text(os.path.join(PUBLIC, 'index.html'))))
        review_count = build_review_notes(os.path.join(tmp, 'review'), routes=routes)

        asset_files = 0
        asset_bytes = 0
        linked = 0
        for name, book, d in books:
            h = routes[name]['hash']
            write_text(os.path.join(tmp, routes[name]['reader']),
                       render_reader(read_text(os.path.join(PUBLIC, 'reader.html')), name, book))
            if name in banks:
                blob = practice_book_blob(name, banks)
                write_text(os.path.join(tmp, routes[name]['practice']),
                           render_practice(read_text(os.path.join(PUBLIC, 'practice.html')), name,
                                           banks[name], blob))
            if no_assets:
                continue
            src_assets = os.path.join(d, 'assets')
            if not os.path.isdir(src_assets):
                continue
            dst_assets = os.path.join(tmp, 'books', h, 'assets')
            os.makedirs(dst_assets, exist_ok=True)
            for fn in sorted(os.listdir(src_assets)):
                sp = os.path.join(src_assets, fn)
                if not os.path.isfile(sp):
                    continue
                dp = os.path.join(dst_assets, fn)
                size = os.path.getsize(sp)
                if not copy and hasattr(os, 'link'):
                    try:
                        os.link(sp, dp)
                        linked += 1
                        asset_files += 1
                        asset_bytes += size
                        continue
                    except OSError:
                        pass
                shutil.copy2(sp, dp)
                asset_files += 1
                asset_bytes += size

        exported_at = time.strftime('%Y-%m-%d %H:%M')
        bank_metas.sort(key=lambda b: b['bookTitle'])
        total_files, total_bytes = dir_stats(tmp)
        write_text(os.path.join(tmp, '使用说明.txt'),
                   make_readme(len(books), bank_metas, exported_at, total_files, total_bytes))
        write_text(os.path.join(tmp, '版本.txt'),
                   make_version(len(books), bank_metas, exported_at, total_files, total_bytes))

        report = {
            'out': out_dir,
            'exportedAt': exported_at,
            'books': len(books),
            'qbanks': len(banks),
            'reviewNotes': review_count,
            'readerPages': len(books),
            'practicePages': len(banks),
            'bankTotals': [{'bookTitle': b['bookTitle'], 'author': b.get('author') or '',
                            'total': b['total']} for b in bank_metas],
            'assetFiles': asset_files,
            'assetBytes': asset_bytes,
            'assetHuman': human(asset_bytes),
            'hardlinked': linked,
            'files': total_files,
            'bytes': total_bytes,
            'human': human(total_bytes),
            'elapsedSec': round(time.time() - t0, 1),
        }
        write_text(os.path.join(tmp, '导出报告.json'), json.dumps(report, ensure_ascii=False, indent=1))

        if os.path.isdir(out_dir):
            prev = out_dir + '-prev'
            if os.path.isdir(prev):
                shutil.rmtree(prev)
            os.rename(out_dir, prev)
        os.rename(tmp, out_dir)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return report
    except Exception:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        raise


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='导出离线静态版（免 Node）')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出目录（默认 _dist/筑学书库-离线版）')
    ap.add_argument('--copy', action='store_true', help='图片真实复制（默认硬链接）')
    ap.add_argument('--dry-run', action='store_true', help='只统计，不写盘')
    ap.add_argument('--no-assets', action='store_true', help='不复制图片（调试用）')
    ap.add_argument('--zip', action='store_true', help='导出后顺带打 zip（同目录，含 .sha256.txt）')
    ap.add_argument('--release-preflight', action='store_true', help='检查历史卡点后退出，不生成离线包')
    args = ap.parse_args()
    if args.release_preflight or not args.dry_run:
        from repair_preflight import release_holds
        try:
            held = release_holds()
        except (OSError, ValueError, KeyError, TypeError) as e:
            ap.exit(2, 'STOP 发布台账无法校验：%s\n' % e)
        if held:
            detail = '\n'.join(f'- {name}：{status}' for name, status in held.items())
            ap.exit(2, 'STOP 离线发布存在尚未核清的历史书稿：\n' + detail + '\n')
        if args.release_preflight:
            print('PASS 离线发布历史阻断台账')
            return
    report = build(args.out, copy=args.copy, dry_run=args.dry_run, no_assets=args.no_assets)
    if args.zip and not args.dry_run:
        make_zip(report['out'])


if __name__ == '__main__':
    main()
