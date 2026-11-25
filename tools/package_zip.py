# -*- coding: utf-8 -*-
"""打包可分发的「筑学书库」压缩包（零依赖，只用标准库）。

用法：
    python tools/package_zip.py                 # 默认先试桌面，写不进就自动落到 _dist/
    python tools/package_zip.py --out D:/x.zip  # 指定输出
    python tools/package_zip.py --dry-run       # 只统计，不写文件
    python tools/package_zip.py --no-sha        # 不写 .sha256.txt

历史教训（2026-11-05）：
1. 旧版把 题库/建筑概论_资料、题库/外国建筑史_资料 写死在清单里 —— 新加的第三本题库
   会被**静默漏打包**。现在自动扫描 题库/*_资料 与根目录的 *.存疑.md 指针。
2. 旧版用 ZipFile(out, 'w') 直接覆盖目标：一旦中途出错（或被杀），旧包已经被截断，
   实际造成过一次「桌面压缩包整个消失」。现在改为「写 .tmp → 校验条目数 → 旧包改名保留一份 → 替换」。
3. python.exe 直接写桌面会被 Windows「受控文件夹访问」拒绝（PermissionError）。
   现在自动退回工作区 _dist/，并打印一条 PowerShell 复制命令给你手工搬过去。
"""
import argparse
import datetime
import hashlib
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = '筑学书库完整版/'
STORE_EXT = {'.webp', '.png', '.jpg', '.jpeg', '.gif', '.mp4', '.pdf', '.zip', '.woff2', '.ttf', '.otf'}
SKIP_NAMES = {'Thumbs.db', '.DS_Store', 'desktop.ini'}
# 目录名以 _ 开头的（_parts/_backups/_render/_shots/_book_backups 等）一律不进包
SKIP_DIR_PREFIX = '_'


def bank_dirs():
    """自动发现 题库/*_资料（新增书不会漏）。"""
    qb = os.path.join(ROOT, '题库')
    out = []
    if os.path.isdir(qb):
        for d in sorted(os.listdir(qb)):
            if d.endswith('_资料') and not d.startswith(SKIP_DIR_PREFIX):
                out.append('题库/' + d)
    return out


def bank_pointers():
    qb = os.path.join(ROOT, '题库')
    out = []
    if os.path.isdir(qb):
        for f in sorted(os.listdir(qb)):
            if os.path.isfile(os.path.join(qb, f)) and not f.startswith(SKIP_DIR_PREFIX):
                out.append('题库/' + f)
    return out


def collect():
    items = []

    def add_dir(relroot):
        base = os.path.join(ROOT, relroot.replace('/', os.sep))
        if not os.path.isdir(base):
            return
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d != '__pycache__' and not d.startswith(SKIP_DIR_PREFIX)]
            for f in fns:
                if f in SKIP_NAMES or f.endswith('.tmp') or f.endswith('.prev.zip'):
                    continue
                ap = os.path.join(dp, f)
                rel = os.path.relpath(ap, ROOT).replace('\\', '/')
                items.append((ap, rel))

    for d in ('books', 'public', 'tools', '交接文档') + tuple(bank_dirs()):
        add_dir(d)
    for rel in ('server.js', 'pipeline.json',
                '打开书库.bat', '平板访问.bat', '一键转换书籍.bat', '待转换书籍/说明.txt'):
        ap = os.path.join(ROOT, rel.replace('/', os.sep))
        if os.path.exists(ap):
            items.append((ap, rel))
    for rel in bank_pointers():
        if rel.startswith('题库/') and ('_资料' in rel or rel.endswith('.存疑.md')):
            ap = os.path.join(ROOT, rel.replace('/', os.sep))
            if os.path.isfile(ap):
                items.append((ap, rel))
    # 去重（同一 rel 只留一次）
    seen, uniq = set(), []
    for ap, rel in items:
        if rel in seen:
            continue
        seen.add(rel)
        uniq.append((ap, rel))
    return uniq


def sha256(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()


def writable(path):
    probe = path + '.probe'
    try:
        with open(probe, 'wb') as f:
            f.write(b'x')
        os.remove(probe)
        return True
    except Exception:
        try:
            os.remove(probe)
        except Exception:
            pass
        return False


def build(items, dest):
    """写到 dest.tmp，校验后再替换；已存在的旧包改名保留。"""
    tmp = dest + '.tmp'
    t0, done, written = time.time(), 0, 0
    total = sum(os.path.getsize(a) for a, _ in items)
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for ap, rel in items:
            ext = os.path.splitext(ap)[1].lower()
            ct = zipfile.ZIP_STORED if ext in STORE_EXT else zipfile.ZIP_DEFLATED
            try:
                z.write(ap, PREFIX + rel, compress_type=ct)
            except Exception as e:
                print('SKIP', rel, e)
            done += 1
            written += os.path.getsize(ap)
            if done % 2000 == 0 or done == len(items):
                print('progress %d/%d  %.1f/%.1f MB  %.0fs' %
                      (done, len(items), written / 1048576, total / 1048576, time.time() - t0), flush=True)
    with zipfile.ZipFile(tmp) as z:
        n = len(z.infolist())
    if n != done:
        os.remove(tmp)
        raise SystemExit('校验失败：写入 %d 条但压缩包内有 %d 条，保留原包不动' % (done, n))
    if os.path.exists(dest):
        keep = dest + '.prev.zip'
        if os.path.exists(keep):
            os.remove(keep)
        os.replace(dest, keep)
        print('旧包已保留为：%s' % os.path.basename(keep))
    os.replace(tmp, dest)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-sha', action='store_true')
    args = ap.parse_args()

    name = '筑学书库完整版-%s.zip' % datetime.date.today().isoformat()
    items = collect()
    banks = sorted({rel.split('/')[1] for _, rel in items if rel.startswith('题库/') and rel.endswith('.json')})
    print('files=%d  total=%.1f MB' % (len(items), sum(os.path.getsize(a) for a, _ in items) / 1048576))
    print('题库: %d 本 -> %s' % (len(banks), ', '.join(os.path.splitext(os.path.basename(b))[0] for b in banks) or '（无！）'))
    if args.dry_run:
        return 0

    dest = args.out or os.path.join(os.path.expanduser('~'), 'Desktop', name)
    if not writable(os.path.dirname(dest) or '.'):
        fb_dir = os.path.join(ROOT, '_dist')
        os.makedirs(fb_dir, exist_ok=True)
        fb = os.path.join(fb_dir, name)
        print('!! 目标目录不可写（多半是 Windows「受控文件夹访问」拦了 python.exe）')
        print('   改写到：%s' % fb)
        print('   写完后请手工复制到桌面（PowerShell 不受该限制）：')
        print("     Copy-Item -LiteralPath '%s' -Destination '%s' -Force" % (fb, dest))
        dest = fb
    out = build(items, dest)
    print('DONE %s  %.1f MB  %.0fs' % (out, os.path.getsize(out) / 1048576, time.time()))
    if not args.no_sha:
        sp = out + '.sha256.txt'
        h = sha256(out)
        with open(sp, 'w', encoding='utf-8') as f:
            f.write('%s  %s\n' % (h, out))
        print('SHA256 %s\nSHA256 文件：%s' % (h, sp))
    return 0


if __name__ == '__main__':
    sys.exit(main())
