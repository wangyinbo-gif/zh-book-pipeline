# zh-book-pipeline · 中文扫描书 → 结构化可读书稿

一套把**扫描版中文书籍（PDF）**变成**结构化、可质检、可阅读**电子书稿的工具链：
渲染 → OCR/文字层 → 块级结构（标题/正文/图/表/注/图注）→ 章节重建 → 三态质检 → 阅读器/离线包/题库。

> 本仓库只包含**工具与文档**。真实书库数据（正文、插图、原 PDF）不随仓库分发，原因见
> [docs/DATASET.md](docs/DATASET.md)。

## 它解决什么问题

扫描书直接 OCR 得到的是「一页一坨文字」，读不了、也校对不了。本项目在 OCR 之上做**结构化清洗**：

- 把页面文字拆成 **6 种块**：`heading` / `paragraph` / `figure` / `table` / `note` / `caption`
- 图版单独裁切为 `assets/*.webp`，图注与图**一一配对**（图注照录原页印刷文字，不拟题）——保留「页码-原页-图注-章节」四元组追溯
- 按原书目录重建 **章 / 节 / 页码映射**，并给出章首末块，支持跨页续段（`pages` 字段）
- 每本书写出 `book.json`（单一事实源）与 `stats`（页/字/章/图/配注数，工具重算，不手写）

实盘规模（2026-11-24 现算）：**44 本 / 17,513 页 / 12,525,558 字 / 632 章 / 14,869 图（配注 13,619）**，全部通过程序门禁；题库 9 套 / 5,594 题（`qbank.py check` 0 错 0 警）。

## 三态质检

1. **程序绿灯**：`gate` / `textcheck` / `proofread` / `coverage` / `structure` 全过（机器可判定）
2. **逐页机器对照**：OCR 文本 vs 书稿文本，按页算覆盖率与偏移，抓「整页丢文 / 整段串行」
3. **逐页人工目视**：只有第 ③ 层算「深审」；程序绿不等于内容对

细节与验收命令见 [docs/QUALITY.md](docs/QUALITY.md)。

## 目录

```
tools/                 全部流水线脚本（核心入口 pipeline.py）
  pipeline.py          convert / bookfix / postfix / readability / toc / dims / proofread / preview
  pdf2book.py          PDF → book.json + assets（OCR 与文字层两条路）
  bookfix.py           清书名、去目录页、重建章节
  postfix_book.py      跨页与跨插图续段合并、图例并入图注
  proofread.py         生成校对清单（必看/参考分级）
  build_tables.py      表格结构重建
  review_batch.py      批量人工复核辅助（裁图、对照表）
  figgap.py            图版缺口报告（OCR 图注/正文引用/页内图块 + PDF 视觉证据）
  figure_audit.py      图版审计：原页检出图 vs 书稿裁图，逐图覆盖对照
  figure_asset_boxes.py 旧资产在原页的定位（多尺度对位 + 低置信留证）
  layout_parse.py      版式解析：栏位判定 / 行归栏 / 组装不变量
  reflow_book.py       实验性重排验收（只读 OCR + book.json，输出不写回 books/）
  export_offline.py    导出离线静态版
  qbank*.py            题库生成/校验/导出
  test_*.py            诊断/复核工具回归测试（unittest，临时目录）
public/                零依赖 Web 阅读器与练习页（Node server.js，端口 4590）
server.js              本地书库服务（零第三方依赖）
samples/demo-book/     自制 3 页示例样书（无版权内容）
docs/                  架构、质检、硬规矩、数据说明
```

## 快速开始

环境：Python 3.10+（开发用 3.12）、Node.js（仅阅读器需要）。

```bash
pip install pymupdf pypdfium2 rapidocr-onnxruntime pillow numpy

# 书库根目录：其下有 books/、题库/、data/、inbox/
export BOOKROOT=/path/to/library        # Windows: set BOOKROOT=D:\library

# 转换一本（PDF 放 inbox/）
python tools/pipeline.py one "inbox/some-book.pdf"

# 已有书稿重跑后处理与质检
python tools/pipeline.py post "books/some-book"
python tools/proofread.py "books/some-book"

# 工具回归自检（只读，不写书稿）
python -m unittest discover -s tools

# 阅读器（先放一本书到 books/ 下，或放示例样书）
mkdir -p books && cp -r samples/demo-book books/demo-book
node server.js        # http://127.0.0.1:4590
```

`tools/paths.py` 定义路径约定：`BOOKROOT` 环境变量优先，未设置时取仓库根目录。

## 数据说明

- 仓库**不含任何真实书籍正文、插图或 PDF**；`samples/demo-book` 为自制样书。
- 工具设计上假设你**有权处理**输入文档（自有扫描件、公有领域作品、已获授权内容）。
- 请遵守输入文档的版权与许可；本项目不对使用者的输入内容承担责任。

## 状态与限制

- 主要针对**中文竖排/横排扫描书**调校，OCR 依赖 RapidOCR（onnxruntime，CPU 可跑）。
- 版面极端复杂（多栏、表格套图、双页跨版）需要人工介入；工具提供裁图/对照/回退辅助，不保证全自动。候选召回与诊断各自独立验证，不自动删块。
- 仓库内脚本是实盘工具，部分为单书一次性脚本（文件名含书名或页面范围），按需修改后使用。
- 对外展示只使用已深审的书（28 本仅程序绿灯的书未做逐页人工目视，不作展示依据）。

## License

MIT，见 [LICENSE](LICENSE)。