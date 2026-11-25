# -*- coding: utf-8 -*-
"""Render the review catalog and book notes for the bookshelf and offline export."""

import json
import re
from html import escape
from itertools import groupby
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "复习资料"
STYLE = ROOT / "public" / "review" / "notes.css"


def inline(text):
    safe = escape(text)
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    return re.sub(r"\x60([^\x60]+)\x60", r"<code>\1</code>", safe)


def line_indent(line):
    return len(line) - len(line.lstrip(" "))


def line_kind(line):
    if line.startswith("|") and line.endswith("|"):
        return "table"
    body = line.lstrip(" ")
    if re.match(r"\d+\. ", body):
        return "ordered"
    if body.startswith("- "):
        return "bullets"
    return "text"


def render_list(lines, ordered=False):
    """Render one list, keeping indented sub-items at any depth."""
    tag = "ol" if ordered else "ul"
    items = [(line_indent(line), line.lstrip(" ").split(" ", 1)[1]) for line in lines]
    # 缩进量归一成连续的层号：2 空格、4 空格、6 空格…都行，跳级也不出错
    rank = {d: k for k, d in enumerate(sorted({d for d, _ in items}))}
    items = [(rank[d], text) for d, text in items]

    def build(start, depth):
        out, i = [], start
        while i < len(items) and items[i][0] >= depth:
            text = items[i][1]
            j = i + 1
            inner = ""
            if j < len(items) and items[j][0] > depth:
                sub, j = build(j, depth + 1)
                inner = f"<ul>{sub}</ul>"
            out.append(f"<li>{inline(text)}{inner}</li>")
            i = j
        return "".join(out), i

    return f"<{tag}>{build(0, 0)[0]}</{tag}>"


def render_table(lines):
    rows = [[cell.strip() for cell in line[1:-1].split("|")] for line in lines]
    if len(rows) < 2 or len({len(row) for row in rows}) != 1 or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]
    ):
        raise ValueError("复习笔记表格需包含表头和等列数的分隔行。")
    header = "".join(f'<th scope="col">{inline(cell)}</th>' for cell in rows[0])
    data = "".join(
        "<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>"
        for row in rows[2:]
    )
    return (
        '<div class="review-table" tabindex="0" role="region" aria-label="复习要点对照表">'
        f"<table><thead><tr>{header}</tr></thead><tbody>{data}</tbody></table></div>"
    )


def render_note(markdown):
    """Render the headings, tables, lists and prose used in the note sources."""
    title, _, markdown = markdown.partition("\n")
    if not title.startswith("# "):
        raise ValueError("复习笔记必须以一级标题开始。")
    content = []
    outline = []
    for block in re.split(r"\n\s*\n", markdown.strip()):
        for kind, grouped in groupby(block.splitlines(), line_kind):
            lines = list(grouped)
            if kind == "table":
                content.append(render_table(lines))
            elif kind in {"ordered", "bullets"}:
                content.append(render_list(lines, ordered=kind == "ordered"))
            else:
                for line in lines:
                    heading = re.fullmatch(r"(#{2,3}) (.+)", line)
                    if heading:
                        level = len(heading[1])
                        label = inline(heading[2])
                        anchor = f"part-{len(outline) + 1}"
                        outline.append((anchor, label, level))
                        content.append(f'<h{level} id="{anchor}">{label}</h{level}>')
                    else:
                        content.append(f"<p>{inline(line)}</p>")
    return title[2:], outline, "\n".join(content)


def page(title, content, note=False):
    back = '<a href="index.html">笔记目录</a>' if note else ''
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{escape(title)} · 筑学书库</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="../style.css">
  <link rel="stylesheet" href="notes.css">
</head>
<body class="review-page">
  <nav class="review-nav" aria-label="书库导航"><a href="../index.html">返回书架</a>{back}</nav>
  <main class="review-main">
    {content}
  </main>
</body>
</html>
"""


def build_review_notes(output, *, routes=None):
    """Build one directory. With offline routes, link to exported book readers."""
    if not SOURCE.is_dir():
        return 0  # 复习笔记库（复习资料/）不存在：跳过，不影响离线包主体
    entries = json.loads((SOURCE / "目录.json").read_text(encoding="utf-8-sig"))
    rendered = {}
    cards = []
    for entry in entries:
        slug = entry["slug"]
        filename = entry["file"]
        book_id = entry["bookId"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in rendered:
            raise ValueError(f"复习笔记编号重复或非法：{slug}")
        if "/" in filename or "\\" in filename or not filename.endswith(".md"):
            raise ValueError(f"复习笔记应使用资料目录内的 Markdown 文件名：{filename}")
        if routes is None and not (ROOT / "books" / book_id / "book.json").is_file():
            raise ValueError(f"找不到复习笔记对应的原书：{book_id}")
        title, outline, content = render_note((SOURCE / filename).read_text(encoding="utf-8-sig"))
        if routes is None:
            reader = "../reader.html?book=" + quote(book_id, safe="")
        else:
            route = routes.get(book_id)
            reader = "../" + route["reader"] if route else None
        reader_link = f'<a class="btn" href="{escape(reader)}">打开原书</a>' if reader else ""
        headings = "".join(
            f'<li class="lv{level}"><a href="#{key}">{label}</a></li>'
            for key, label, level in outline
        )
        article = (
            '<article class="review-article">'
            f'<header class="review-heading"><h1>{escape(title)}</h1>{reader_link}</header>'
            f'<nav class="review-outline" aria-label="本篇目录"><ul>{headings}</ul></nav>'
            f'{content}</article>'
        )
        rendered[slug] = page(title, article, note=True)
        cards.append(
            f'<a class="book-card review-card" href="{slug}.html">'
            '<div class="card-body">'
            f'<span class="review-category">{escape(entry["category"])}</span>'
            f'<h2>《{escape(entry["title"])}》</h2>'
            f'<p>{escape(entry["edition"])}</p><p>{escape(entry["scope"])}</p>'
            '<span class="review-open">阅读笔记 →</span></div></a>'
        )
    index = page("复习笔记", '<h1>复习笔记</h1><div class="review-grid">' + "\n".join(cards) + '</div>')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if output.resolve() != STYLE.parent:
        (output / STYLE.name).write_bytes(STYLE.read_bytes())
    for slug, html in rendered.items():
        (output / f"{slug}.html").write_text(html, encoding="utf-8", newline="\n")
    (output / "index.html").write_text(index, encoding="utf-8", newline="\n")
    return len(rendered)


if __name__ == "__main__":
    print(f"Generated {build_review_notes(STYLE.parent)} review notes.")
