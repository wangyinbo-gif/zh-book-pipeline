# zh-book-pipeline · 中文扫描书 → 结构化可读书稿

一套把**扫描版中文书籍（PDF）**变成**结构化、可质检、可阅读**电子书稿的完整工具链：
渲染 → OCR/文字层 → 块级结构（标题/正文/图/表/注/图注）→ 章节重建 → 三态质检 → 本地阅读器/离线包/题库。
零 Web 框架（阅读器与服务器是无依赖 Node + 原生 JS），OCR 全 CPU 可跑，单人单机可复现全流程。

> 仓库包含**工具、文档与公版原文抽样数据集**（`samples/public_domain_sampling/`）。
> 真实书库数据（扫描页、插图、整书正文）不随仓库分发，原因见 [docs/DATASET.md](docs/DATASET.md)。

## 技术原理（一图流）

```
输入 PDF ──► ① 渲染        pypdfium2 / PyMuPDF，Matrix(3,3)≈216dpi 灰度位图
          ──► ② 取文        双通道二选一（准入判据决定，见下）
               文字层通道    内嵌层七判据审查（A乱码/B空层/C体量/D bigram/E缺文/F对基线序/G流序自查）
                            ——通不过必须走 OCR；历史教训：文字层直取曾造成倒置+丢字 304 处
               OCR 通道      RapidOCR（PP-OCR det+rec，onnxruntime，CPU）
                 横排        检测框行序 + 置信度
                 竖排        独立分支：列槽投影分列 → 字列切单字 → 字形直立横拼 rec（不用检测框）
                            + 页级去倾斜：±3°/0.25° 投影方差找角、2× 谐波回退、转正后结构校验
                            （pitch/slots 落回全书中位带才采用；变体缓存键可整体回退）
          ──► ③ 结构化      行→栏→块：6 种块型 heading/paragraph/figure/table/note/caption；
                            图区=墨迹几何+版式信号判定，裁切 assets/*.webp；图注照录原页印刷文字并一一配对，
                            保留「页码-原页-图注-章节」四元组追溯
          ──► ④ 章节重建    目录页识别/卷端三形态/卷序连续护栏/锚点校正；章→节→页码映射，跨页续段（pages 字段）
          ──► ⑤ 质检        gate 12 项门槛（程序）+ textcheck/coverage（机器对照）+ 人工目视 = 三态 doctrine
          ──► ⑥ 消费        book.json 单一事实源 → 本地阅读器（书架/阅读/练习三页）→ 离线静态包 → 题库
```

关键设计判断（都来自实盘翻车与修复，非拍脑袋）：

- **「程序绿灯 ≠ 内容干净」**：曾实证 47–65% 乱码页在 12 项 gate 全绿下通过——所以坚持三态，只有人工目视算深审。
- **文字层不可默认信任**：乱序、丢字、伪码位是常态；`textlayer_admission.py` 七判据机判「该不该用层」，其中 G 流序自查（块流序 vs 视觉序）专治「书的数据本来就来自这层」的同源盲区。
- **页级异常先量化再修**：`skew_census.py` 全库倾斜普查 4,962 强结构页，异常率 0.44%——把「疑似系统病」证伪成「个别页事故」，避免全库回炉。
- **修复必须可归因可回滚**：`lost_diff.py`（OCR 行 vs 书稿全语料逐行包含）、`pollute_scan.py`（广告/书眉夹带）逐页证据；写回优先并入既有块（不动 startBlock），改前双备份。
- **缓存即责任**：OCR 结果带参数版本键（PARAM_VERSION + 去倾斜变体位），删变体文件即整体回退；改参数必须递增版本，防假命中。


## 流水线与用法

```bash
pip install pymupdf pypdfium2 opencv-python-headless rapidocr-onnxruntime pillow numpy
export BOOKROOT=/path/to/library    # 其下 books/ 题库/ data/ inbox/

python tools/pipeline.py one "inbox/some-book.pdf"           # 一本全链：下表 8 阶段
python tools/pipeline.py one "inbox/vertical-scan.pdf" --vertical --ear 书名,书名末三字
python tools/pipeline.py post "books/some-book"              # 只重跑后处理（不重新识别）
python tools/pipeline.py recheck "books/some-book"           # gate+textcheck+coverage → 质检.json
python -m unittest discover -s tools                         # 工具回归（150 项，零数据依赖）

node server.js                                               # http://127.0.0.1:4590 见「部署与安全」
```

| 阶段 | 脚本 | 干什么 |
| --- | --- | --- |
| convert | `pdf2book.py` | 渲染 + OCR/文字层 → book.json + assets |
| bookfix | `bookfix.py` | 清书名尾标、去目录页、重建章节 |
| postfix | `postfix_book.py` | 跨页/跨插图续段合并、图例并入图注 |
| readability | `pipeline.py` | 删广告页与误检图、合并断段、补图注 |
| toc / dims | `pipeline.py` | 「第N篇」并入「第N章」；插图补宽高 |
| proofread | `proofread.py` | 校对清单.md（必看/参考分级）+ proofread.json |
| preview | `pipeline.py` | 翻页预览 preview.html |
| recheck | `pipeline.py recheck` | 三态第①②层复跑，写 质检.json 与报告「复查」节 |

## 功能一览

- **竖排古籍**：`vertical_ocr.py` / `vertical_book.py`——投影分列、直立横拼识别、页级去倾斜、卷端定章、页级诊断闸；细节 [docs/VERTICAL-OCR.md](docs/VERTICAL-OCR.md)。
- **本地阅读器**：零依赖 Node 服务器 + 原生 JS 三页（书架/阅读器/练习），翻页、书签、进度、图注悬浮。
- **全文/图注检索**（v1.1.0）：`GET /api/search?q=…&mode=text|figures&book=&limit=`——无预建索引，服务器惰性语料缓存 + 运行时全扫（55 本 1,492 万字冷跑 <1s、热跑 ~30ms）；多词 AND 分词、命中片段带书/章/页/块坐标，书架发起、阅读器 `?block=&term=` 深链定位并高亮。受统一 gate 保护（不在匿名白名单，R23 断言锁定）。
- **离线包**：`export_offline.py` 把任意书打成纯静态目录，断网可读。
- **题库**：`qbank*.py` 生成/校验/导出（本书房实盘 9 套 5,594 题），练习页直接消费。
- **图版工具链**：`figgap.py`（缺口报告）、`figure_audit.py`（原页 vs 书稿逐图对照）、`figure_asset_boxes.py`（旧资产回配定位，注意 scale=3.0/4.0 坐标系换算）、`review_batch.py`（批量目视辅助）。
- **数据修复工具**：`textlayer_admission.py`（文字层准入）、`skew_census.py`（倾斜普查）、`lost_diff.py`（丢行归因）、`pollute_scan.py`（夹带扫描）。
- **批处理护栏（G1–G6）**：`pipeline.py batch` 默认**只预演零写入**（要 `--apply` 才动盘）；单实例互斥锁（活锁 exit=3、陈旧锁需显式接管）；已在库书稿本批禁写（四键定位，重转须 `--force` 或写进批准清单）；run-id + 计划/结束/中止三件留痕；写盘前内容指纹校验；`restore_raw()` 硬闸（`rawrisk --all` 现算「现行正文 / book_raw」比，不在 0.90–1.15 判高危并拒绝回滚，除非 `--force --allow-restore-raw`；回滚前强制快照）。回归见 `tools/test_batch_guard.py`。
- **统计与发布**：`project_stats.py`（README 锚点统计块 + project_stats.json + 发布快照）、`check_stats.py`（漂移即红）、`export_public_sample.py` 方法学见 [docs/PUBLIC-SAMPLING.md](docs/PUBLIC-SAMPLING.md)。
- **端到端自检**：`e2e_check.py` + 自渲染公版夹具（横 6 页/竖 4 页），五阶段退出码定位，CI 与本地同路径。

## 三态质检

1. **程序绿灯**：`gate` 12 项门槛 + textcheck/proofread/coverage（机器可判定）
2. **逐页机器对照**：OCR 原文 vs 书稿，按页覆盖率与偏移，抓整页丢文/整段串行
3. **逐页人工目视**：只有第③层算深审；「28 本程序绿」≠「28 本可展示」

命令与验收见 [docs/QUALITY.md](docs/QUALITY.md)。

## 部署与安全

- **模式 A 本机**（默认）：绑 127.0.0.1；不设 `AUTH_TOKEN` 则完全开放（open 模式行为逐字节不变）。
- **模式 B 局域网/反代**：必须设 `AUTH_TOKEN`（否则拒绝启动），`HOST=0.0.0.0`；HTTPS 交给反代或 VPN。
- **认证契约 v1**（v1.0.7 起）：登录页 → HttpOnly 会话 Cookie（8h 绝对过期，重启失效）→ 全路由同一 gate：
  Host 白名单 400（防 DNS rebinding）→ 匿名路径方法绑定 405 → open 直通 → 未认证 API 401 / 导航 302 → CSRF 403 → 分发。
  前端 `auth-client.js` 统一 fetch 出口（open 模式透明直通，离线包零改动）。
- **已知限制**：会话存内存（单进程约束）；`TRUST_PROXY=1` 时限流退化为按代理 IP；自定义域名需 `ALLOW_EXTRA_HOSTS`。
- 全路由审计表 [docs/ROUTE-SECURITY-MATRIX.md](docs/ROUTE-SECURITY-MATRIX.md)；威胁模型与部署细节 [docs/SECURITY.md](docs/SECURITY.md)。

## 公版抽样数据集（v1.0.9）

`samples/public_domain_sampling/`：六本公版古籍正文抽样（45% 目标线、完整章确定性贪心、无标点原字）+ 三本现代著作结构包。
剥离规则（现代标点/注释/译文/校记/书影/出版信息六项）与出口复验（全样本零非汉字）见 [docs/PUBLIC-SAMPLING.md](docs/PUBLIC-SAMPLING.md)。


## 测试与自动化（CI）

- **smoke.yml**（每次 push）：语法 / 工具回归 unittest（150 项，含批处理护栏 G1–G6 与统计校验 C1–C4 回归）/ 示例样书结构 / 服务端安全 R1-R24（171 断言，Node 原生测试）。
- **e2e.yml**（workflow_dispatch + 相关路径 push + 每周五）：真实扫描 PDF 全链——`tests/e2e/gen_fixtures.py` 现场渲染公版夹具（无二进制入库、零版权），五阶段断言（渲染/OCR/结构/gate/schema），失败退出码 20-24 定位阶段。
- **依赖可复现**：CI 安装 `pip install --no-deps -r requirements-ci.txt`（从本机 .venv 全闭包精确锁定，opencv 以同版本 headless 顶替防 libGL）；OCR 模型 SHA-256 清单 `tools/models/MODELS.sha256`；`run_info.py` 为每次转换落盘 commit/依赖/模型/输入哈希。
- **统计守护**：README 数字全部机器生成（锚点块 + STATS-DATA 机读行），`check_stats.py` 不一致即 exit 1；发布快照 `project_stats.py --snapshot <tag>` 拒覆盖。

## 目录结构

```
tools/            全部流水线/质检/修复/统计脚本（test_*.py 150 项回归随仓）
public/           阅读器前端（index/reader/practice + login + auth-client，零构建）
server.js         本地书库服务器（零第三方依赖，安全回归 R1-R24）
samples/          demo-book（自制 3 页样书）+ public_domain_sampling（公版抽样数据集）
tests/e2e/        端到端夹具生成器 + 期望值（PDF 现场渲染不入库）
docs/             QUALITY / SECURITY / ROUTE-SECURITY-MATRIX / VERTICAL-OCR / PUBLIC-SAMPLING / DATASET / ARCHITECTURE
models/           竖排识别模型（PP-OCRv5 rec mobile，入库分发，SHA 见 tools/models/MODELS.sha256）
```

> 实盘规模数字由 `tools/project_stats.py` 现算写入下方锚点块，`tools/check_stats.py` 做一致性闸门（README 块、逐书 `stats.chars` 漂移、质检时效、文档现状段与失效指针五类校验）；生成物（书单/质检汇总/统计快照）一律由工具刷新，不手改。

<!-- BEGIN PROJECT-STATS (auto-generated by tools/project_stats.py; 勿手改本块) -->
实盘规模（现算 2026-11-27）：**55 本 / 22,192 页 / 14,917,501 字 / 796 章 / 16,786 图（配注 15,425）**，全部通过程序门禁（gate 55/55）；题库 9 套 / 5,594 题（qbank.py check 0 错 0 警）。
<!-- STATS-DATA {"books": 55, "chapters": 796, "characters": 14917501, "figures": 16786, "gate_pass": 55, "gate_total": 55, "pages": 22192, "paired_figures": 15425, "question_sets": 9, "questions": 5594, "rule_version": "V1.2-chapters-from-stats.chapters"} -->
<!-- END PROJECT-STATS -->

## 版本

发布走 git tag + GitHub Release（完整历史见 [Releases](../../releases)）。

| tag | 日期 | 摘要 |
| --- | --- | --- |
| `v1.1.0` | 2026-11-26 | 全文/图注检索上线：`/api/search`（惰性语料缓存+运行时扫描，热 ~30ms）+ 书架检索条/结果面板 + 阅读器 term 高亮深链；路由矩阵补行；安全断言 R1-R24（171 项） |
| `v1.0.9` | 2026-11-26 | 数据发布：公版原文抽样数据集入库（九书修复轮终态之后；6 本正文样本 + 3 本结构包；净化终扫零红线） |
| `v1.0.8` | 2026-11-26 | 工程批：认证前端六件补录入仓（修 v1.0.7 缺件）+ resolve_model_path 四级解析 + 统计自动化（632/636 口径考古定案）+ 真实 PDF e2e（自渲染夹具 + requirements-ci 锁定 + MODELS.sha256）+ 审计四工具 |
| `v1.0.7` | 2026-11-26 | 安全批：统一会话认证（契约 v1）+ CSRF + Host 白名单 + 方法绑定 + AUTH_ENFORCE 防自锁；server.js 557→996；安全回归 164 断言 |
| `v1.0.6` | 2026-11-26 | 竖排投产：`--vertical` 入口 + 页级去倾斜（投影方差找角/谐波回退/变体缓存）+ 竖排页级闸；单测 30 |
| `v1.0.5` | 2026-11-25 | 安全 P0-01：路径边界 path.relative + realpath，四路由统一 resolveInside |
| `v1.0.4` | 2026-11-25 | 古籍卷系三形态识别 + 熔段图注提取 + gate「卷序连续」护栏 |
| 更早 | — | v1.0.0-1.0.3：主链路与阅读器成型、gate 体系、题库与离线包 |

## License

MIT（见 [LICENSE](LICENSE)）。`samples/public_domain_sampling` 的原文本身属公版；工具与文档版权如上。
