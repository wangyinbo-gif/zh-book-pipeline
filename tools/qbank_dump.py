# -*- coding: utf-8 -*-
"""题库出题辅助：导出任意书任意章的正文块（带全局 block 下标）。

用法：
  python tools/qbank_dump.py "<bookId>"                  # 只列章节表（id/标题/块范围/字数）
  python tools/qbank_dump.py "<bookId>" ch-1             # 导出某章
  python tools/qbank_dump.py "<bookId>" all              # 导出全书（每章一个文件）
  python tools/qbank_dump.py "<bookId>" ch-1 --limit 8000  # 只导出前 8000 字符（预览用）

输出：题库/_parts/<bookId>/_dump_<chapterId>.txt
每行形如：[137] p19 paragraph 正文……
方括号里 = book.json 的 blocks 全局下标，直接填进题目的 block 字段。

说明：这是通用版，不动题库/_dump.py（那本是《建筑概论》专用）。
"""
from paths import ROOT
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_book(book_id):
    p = os.path.join(ROOT, 'books', book_id, 'book.json')
    if not os.path.exists(p):
        raise SystemExit('书稿不存在：%s\n（bookId 必须是 books\\ 下的目录名）' % p)
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def chapter_ranges(book):
    total = len(book['blocks'])
    chs = book['chapters']
    out = []
    for i, c in enumerate(chs):
        start = c['startBlock']
        end = chs[i + 1]['startBlock'] if i + 1 < len(chs) else total
        out.append((c['id'], c.get('title') or '', start, end))
    return out


def line_of(i, b):
    if b['type'] == 'paragraph':
        txt = b.get('text') or ''
    elif b['type'] == 'heading':
        txt = '【标题】' + (b.get('text') or '')
    else:
        cap = b.get('caption') or ''
        txt = '（图 p%s%s）' % (b.get('page'), (' ' + cap) if cap else '')
    return '[%d] p%s %s %s' % (i, b.get('page'), b['type'], txt)


def dump(book, cid, start, end, outdir, budget):
    blocks = book['blocks']
    lines, used, truncated = [], 0, False
    for i in range(start, end):
        s = line_of(i, blocks[i])
        if budget and used + len(s) > budget:
            truncated = True
            break
        lines.append(s)
        used += len(s)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, '_dump_%s.txt' % cid)
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return out, len(lines), used, truncated


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    book_id = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else None
    budget = 0
    if '--limit' in sys.argv:
        budget = int(sys.argv[sys.argv.index('--limit') + 1])

    book = load_book(book_id)
    ranges = chapter_ranges(book)
    outdir = os.path.join(ROOT, '题库', '_parts', book_id)

    if not target:
        print('《%s》 %s' % (book.get('bookTitle') or book_id, book_id))
        print('%-8s %-40s %-16s %s' % ('id', 'title', 'blocks', 'chars'))
        for cid, title, s, e in ranges:
            chars = sum(len(book['blocks'][i].get('text') or '') for i in range(s, e))
            print('%-8s %-40s %-16s %d' % (cid, title[:38], '%d-%d' % (s, e), chars))
        print('总计 %d 块 / %d 章' % (len(book['blocks']), len(ranges)))
        return

    todo = ranges if target == 'all' else [r for r in ranges if r[0] == target]
    if not todo:
        raise SystemExit('没有章节 %r；可用：%s' % (target, ', '.join(r[0] for r in ranges)))
    for cid, title, s, e in todo:
        out, n, used, truncated = dump(book, cid, s, e, outdir, budget)
        print('%-8s %-30s blocks %d-%d | %d 块 %d 字符%s | %s'
              % (cid, title[:28], s, e, n, used, ' (已截断)' if truncated else '', out))


if __name__ == '__main__':
    main()
