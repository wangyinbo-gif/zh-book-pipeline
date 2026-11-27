# -*- coding: utf-8 -*-
"""把每本书的章节边界重新对齐到当前 blocks：
- 用章内首块在 blocks 里定位（id/文字/页一致）；
- 找不到时退回旧起始页；
- 重建 startBlock / endBlock（末块下标，空章为 startBlock - 1）/ blocks / chars / figures。
用法：python tools\realign_chapters.py [book_dir ...]（不给参数=全库）
"""
import json
import os
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def detect_json_format(raw):
    """B7：探测 book.json 现有的换行与缩进口径（按字节判，不用文本模式数 \\r）。

    返回 {"newline": "\\r\\n"|"\\n", "indent": int|None, "crlf": n, "lf": n, "compact": bool}
    indent=None 表示单行紧凑（json.dumps 不带 indent）。
    """
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    text = raw.decode("utf-8-sig", "replace")
    lines = text.split("\n")
    indent, compact = None, len([x for x in lines if x.strip()]) <= 1
    if not compact:
        for line in lines[1:8]:
            m = re.match(r"^([ \t]+)\S", line)
            if m:
                g = m.group(1)
                indent = "\t" if g[0] == "\t" else len(g)
                break
        if indent is None:
            indent = 2
    return {"newline": newline, "indent": indent, "crlf": crlf, "lf": lf, "compact": compact}


def dump_json_preserving(obj, fmt):
    """按探测到的口径序列化；只改内容，不改换行与缩进。"""
    ind = fmt.get("indent")
    if fmt.get("compact") or ind is None:
        s = json.dumps(obj, ensure_ascii=False)
    else:
        s = json.dumps(obj, ensure_ascii=False, indent=ind)
    nl = fmt.get("newline", "\n")
    if nl != "\n":
        s = s.replace("\n", nl)
    return s

B = str(Path(__file__).resolve().parents[1] / "books")


def key(b):
    return (b.get("id"), b.get("type"), (b.get("text") or ""), b.get("page"))


def find_from(blocks, target, start):
    tk = key(target)
    for i in range(start, len(blocks)):
        if key(blocks[i]) == tk:
            return i
    # 退化：只比文字+页
    tn = (target.get("type"), (target.get("text") or ""), target.get("page"))
    for i in range(start, len(blocks)):
        if (blocks[i].get("type"), (blocks[i].get("text") or ""), blocks[i].get("page")) == tn:
            return i
    return -1


def realign(book_dir, verbose=False):
    bj = os.path.join(book_dir, "book.json")
    with open(bj, encoding="utf-8-sig") as stream:
        book = json.load(stream)
    if not book.get("chapters"):
        return 0
    raw_before = read_bytes(bj)
    fmt = detect_json_format(raw_before)
    fallback = realign_book(book)
    payload = dump_json_preserving(book, fmt)
    with NamedTemporaryFile(mode="wb", dir=book_dir, delete=False) as stream:
        temporary = stream.name
        try:
            stream.write(payload.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()          # Windows：不先关句柄，os.replace 会被占用
            os.replace(temporary, bj)
        finally:
            if os.path.exists(temporary):
                Path(temporary).unlink(missing_ok=True)
    # 写后自证：语义等价 + 格式零漂移（B7 验收）
    raw_after = read_bytes(bj)
    fmt_after = detect_json_format(raw_after)
    if json.loads(raw_after.decode("utf-8-sig")) != book:
        raise SystemExit("写后自证失败：内容与内存对象不一致")
    for k in ("newline", "indent", "compact"):
        if fmt[k] != fmt_after[k]:
            raise SystemExit("写后自证失败：格式漂移 %s %r → %r" % (k, fmt[k], fmt_after[k]))
    if verbose:
        print("  %-24s 章%-3d 回退定位 %d ｜ 格式保持 %s indent=%s 字节 %d→%d"
              % (os.path.basename(book_dir)[:22], len(book["chapters"]), fallback,
                 "CRLF" if fmt["newline"] == "\r\n" else "LF", fmt["indent"],
                 len(raw_before), len(raw_after)))
    return fallback


def chapter_chars(chunk):
    """章字数：段落/标题正文 + 表格（caption + 单元格）。表格章的正文在 rows 里，
    只数 paragraph/heading 会把纯表格章算成 0 字，gate「目录结构·空章」会误判。"""
    total = 0
    for b in chunk:
        t = b.get("type")
        if t in ("paragraph", "heading"):
            total += len(b.get("text") or "")
        elif t == "table":
            total += len(b.get("caption") or "")
            for row in b.get("rows") or []:
                total += sum(len(cell or "") for cell in row)
    return total


def realign_book(book):
    blocks = book.get("blocks") or []
    chapters = book.get("chapters") or []
    if not chapters:
        return 0
    starts = []
    cursor = 0
    fallback = 0
    for c in chapters:
        cb = c.get("blocks") or []
        sb = -1
        if cb:
            sb = find_from(blocks, cb[0], cursor)
        if sb < 0:
            # 退回：旧 startBlock 的页
            old = c.get("startBlock") or 0
            page = c.get("startPage")
            if page is None and 0 <= old < len(blocks):
                page = blocks[old].get("page")
            if page is not None:
                sb = next((i for i in range(cursor, len(blocks)) if (blocks[i].get("page") or 0) >= page), -1)
            if sb < 0:
                sb = cursor
            fallback += 1
        starts.append(sb)
        cursor = sb
    for k, c in enumerate(chapters):
        sb = starts[k]
        end = starts[k + 1] if k + 1 < len(starts) else len(blocks)
        chunk = blocks[sb:end]
        c["startBlock"] = sb
        c["endBlock"] = end - 1
        c["blocks"] = chunk
        c["chars"] = chapter_chars(chunk)
        c["figures"] = sum(1 for b in chunk if b.get("type") == "figure")
    return fallback


def check_bounds(book):
    blocks = book["blocks"]
    chapters = book.get("chapters") or []
    for index, chapter in enumerate(chapters):
        start = chapter["startBlock"]
        expected = chapters[index + 1]["startBlock"] if index + 1 < len(chapters) else len(blocks)
        if not 0 <= start <= expected <= len(blocks) or chapter["endBlock"] != expected - 1:
            raise ValueError(f"章节边界异常：{chapter.get('id') or index + 1}")
        if chapter["blocks"] != blocks[start:expected]:
            raise ValueError(f"章内块与全局书稿不一致：{chapter.get('id') or index + 1}")


def main():
    dirs = sys.argv[1:]
    if not dirs:
        dirs = [os.path.join(B, x) for x in sorted(os.listdir(B))
                if os.path.isdir(os.path.join(B, x)) and os.path.exists(os.path.join(B, x, "book.json"))]
    for d in dirs:
        realign(d, verbose=True)


if __name__ == "__main__":
    main()
