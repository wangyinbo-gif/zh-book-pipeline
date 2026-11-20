# 示例样书（自制，无版权内容）

`demo-book/` 是手工编写的 3 页样书，用来演示 `book.json` 结构、`public/` 阅读器渲染与
`tools/proofread.py` 的扫描流程。内容与图片均为本仓库自制，不含任何真实书籍正文。

```bash
# 用阅读器看样书：把样书放进 books/ 目录（目录名随意）
mkdir -p books && cp -r samples/demo-book books/demo-book
node server.js          # http://127.0.0.1:4590
```

真实书库数据不随仓库分发，见根目录 README「数据说明」。
