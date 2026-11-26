# 数据说明：为什么仓库里没有书

## 不附带数据的原因

1. **版权**：扫描书正文、插图、原 PDF 都是受版权保护作品的复制件。公开仓库分发等于向公众提供下载，
   与「开源工具」是两件事。私有仓库也不改变「复制件」性质，只是降低暴露面。
2. **体积**：真实书库约 11 GB（书稿 3 GB、插图 2.2 GB、原 PDF 3.2 GB、离线包 4.1 GB），
   远超 GitHub 单文件 100 MB、仓库约 5 GB 的硬限制；走 Git LFS 也要付费且无助于版权问题。
3. **可复现性**：`book.json` 由 PDF 经流水线生成，工具 + 输入 PDF = 可重建，不必把产物入库。

## 正确用法

```bash
# 1) 准备你有权处理的 PDF（自有扫描件 / 公有领域 / 已获授权）
mkdir -p inbox
cp "your-book.pdf" inbox/

# 2) 指向书库根目录
export BOOKROOT=$PWD

# 3) 转换 + 后处理 + 质检
python tools/pipeline.py one "inbox/your-book.pdf"
python tools/pipeline.py post "books/your-book"
python tools/proofread.py "books/your-book"
python tools/pipeline.py recheck "your-book"
```

产出：`books/<书名>/book.json`（结构化书稿）、`assets/*.webp`（插图）、`校对清单.md`（人工复核清单）。

## 公版原文抽样数据集

`samples/public_domain_sampling/`（2026-11-26 首发，v1.0.9）：六本公版古籍正文抽样（45% 目标线、
完整章确定性贪心、无标点原字）+ 三本现代著作结构包。公版依据、剥离与复验规则见
[docs/PUBLIC-SAMPLING.md](PUBLIC-SAMPLING.md) 与各目录 README。除该抽样外，本仓库仍不含任何书稿正文。

## 示例样书

`samples/demo-book/` 是手工编写的 3 页样书（文字与示意图均为自制），演示 `book.json` 结构、
阅读器渲染与校对流程，不涉及任何真实书籍内容。用法见 [samples/README.md](../samples/README.md)。

## 合规提示

- 只处理你有权处理的文档：自有扫描、公有领域作品（作者逝世超过保护期等）、明确授权的资料。
- 不要把转换产物（尤其整书正文/插图）公开分发；工具本身不限制用途，责任在使用者。
- 若用于教学/研究引用，遵循合理使用范围，注明来源。