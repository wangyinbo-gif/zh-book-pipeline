# models/ · 可选 OCR 模型（竖排分支用）

本目录存放**可选**识别模型：核心流程不依赖它，缺省时自动走 RapidOCR 自带的 ch_PP-OCRv4；只有竖排（右起竖排扫描件）路线需要这里的 rec 模型。

| 文件 | 大小 | 用途 | 许可 |
|---|---|---|---|
| `ch_PP-OCRv5_rec_mobile.onnx` | 16,631,306 B | 竖排路线专用：字形直立横拼后送的**横排文字识别器**（rec-only，不含检测模型） | Apache-2.0（PaddleOCR 官方导出权重） |

- sha256：`5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5`
- 来源：PaddleOCR PP-OCRv5（server 版 84MB 未入库，需要更高精度时另行下载）
- 加载方式（RapidOCR 1.4.4，词典由 onnx metadata `character` 自带，36,775 字符，无需外部 keys 文件）：

模型放置：仓库根 `models/` 与 `tools/models/` 二者皆可——pipeline/vertical 默认自动搜索（先根后 tools），`VERTICAL_OCR_MODEL` 或 `--vmodel` 可显式覆盖。

```python
from rapidocr_onnxruntime import RapidOCR
engine = RapidOCR(rec_model_path="models/ch_PP-OCRv5_rec_mobile.onnx")
texts, _ = engine.text_rec(strip_image)
```

## 为什么入库而不在线下载

- 目标环境是单机离线使用（OCR 常在断网/离线校验下跑），首次运行下载不可靠；
- 16MB 在可接受范围，单文件、无依赖、可直接随仓库分发；
- 记录 sha256 便于三处（本地 / 公仓 / 私仓）比对一致。

## 不放进仓库的内容

- PP-OCRv5 **server** 版（84MB）：实验显示单栏 1.8 分钟级，成本不可接受，未采用；
- Tesseract `chi_tra_vert`（12.9MB）：仅作交叉校验，CER 15.9–16.6%，不进主线。