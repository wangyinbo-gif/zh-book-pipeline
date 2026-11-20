# 架构与数据模型

## 流水线

```
PDF ──convert──> pages/*_ocr2.json + assets/*.webp + book_raw.json
                      │
                      ├─bookfix────> 书名清理、目录页剔除、章节重建
                      ├─postfix────> 跨页/跨插图续段合并、图注归一
                      ├─readability> 广告页/误检图清理、断段合并
                      ├─toc────────> 章标题归并（第N篇并入第N章）
                      ├─dims───────> 插图宽高（阅读器排版预留）
                      ├─proofread──> 校对清单.md + proofread.json
                      └─preview────> preview.html（翻页预览）

book.json ──recheck/coverage/structure/realign──> 质检报告
book.json + 题库 ──export_offline──> _dist/（离线静态版，双击可用）
```

每个阶段把进度写进书目录的 `.pipeline.json`；改 OCR 参数（`--scale`）会重跑 convert 之后的阶段。
`book_raw.json` 是 OCR 后的原始快照，重做后处理时从它重建，保证可重复。

## 目录约定

```
<BOOKROOT>/
  books/<书名>/
    book.json            单一事实源（indent=1，UTF-8）
    assets/*.webp        插图（按 figure 块顺序命名）
    pages/*_ocr2.json    逐页 OCR 结果（含文字行与图块坐标）
    .pipeline.json       阶段进度
    book.json.bak_*      写入前快照（≤3 份）
    校对清单.md / proofread.json / 报告
  题库/<书名>.json        练习题库
  data/                  本地服务运行时数据
  inbox/                 待转换 PDF（约定目录，可改）
```

## book.json

```jsonc
{
  "bookId": "demo-book",              // 稳定 id
  "title": "书名",
  "author": "作者",
  "pages": 3,
  "blocks": [
    {"id": "b1", "type": "heading", "level": 1, "text": "第1章 示例", "page": 1},
    {"id": "b2", "type": "paragraph", "text": "正文…", "page": 1, "pages": [1, 2]},
    {"id": "b5", "type": "figure", "page": 1, "asset": "assets/demo-fig-1.webp",
     "width": 640, "height": 400,
     "caption": "图1-1 印刷图注照录", "captionDisplay": true},
    {"id": "b8", "type": "table", "page": 2, "rows": [["列1", "列2"]], "caption": "表1-1 …"},
    {"id": "b9", "type": "note", "text": "脚注文字", "page": 2}
  ],
  "chapters": [
    {"id": "ch1", "title": "第1章 示例", "startPage": 1,
     "startBlock": "b1", "endBlock": "b5", "blocks": 5, "chars": 123, "figures": 1}
  ],
  "stats": {"pages": 3, "chars": 456, "paragraphs": 4, "figures": 1, "tables": 1, "captions": 1}
}
```

### 块类型

| type | 用途 | 关键字段 |
|---|---|---|
| `heading` | 章/节标题（来自原书标题行） | `level`、`text`、`page` |
| `paragraph` | 正文段落（跨页续段合并后仍是一个块） | `text`、`page`、`pages`（跨页时） |
| `figure` | 图版 | `asset`、`width`、`height`、`caption`、`captionDisplay` |
| `table` | 表格 | `rows`（二维数组）、`caption` |
| `note` | 脚注/表注 | `text`、`page`（紧跟所在段落之后） |
| `caption` | 独立图注块（仅在图注无法并入 figure 时使用） | `text`、`page` |

`caption` 也可作为 figure 的字段；`captionDisplay=false` 表示该图原页无印刷图注（豁免计数，**不得滥用**）。

### 章节

`chapters[]` 用 `startBlock` / `endBlock`（块 id）锚定，`blocks` / `chars` / `figures` 由工具重算。
章标题一律照录原书（如「第N章 章名」），英文原题照录为紧随其后的独立段落块。

## 坐标与渲染

- OCR 坐标系固定 **1800×2558**；PDF 渲染用 `fitz.Matrix(3, 3)`。
- 从原页裁图时，OCR 坐标必须按 `(page.rect.width/1800, page.rect.height/2558)` 缩放到 PDF 点坐标，
  否则 PyMuPDF 会报 `Invalid bandwriter header dimensions`。
- 禁止从「缩放后的预览图」量坐标；一律用原分辨率渲染图。

## 阅读器

`server.js` 是零依赖 Node 服务：扫描 `books/*/book.json` 成书架，按 `bookId` 提供书稿与 assets，
前端在 `public/`（`reader` / `practice` / `shelf`）。`export_offline.py` 用同一份数据生成可离线双击的静态版。