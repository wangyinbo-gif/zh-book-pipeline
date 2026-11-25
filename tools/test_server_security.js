'use strict';
/**
 * server.js 安全回归测试（零依赖，Node 18+）
 * 用法：node tools/test_server_security.js
 * 覆盖：R1 请求体上限(413) / R2 字段校验(400) / R3 编码兜底(400) /
 *       R4 路径穿越与空字节(403/400) / R5 URL id 长度(400) / R6 AUTH_TOKEN(401) /
 *       R7 路径边界（P0-01 相邻前缀目录 + 符号链接）/ 正常流无回归
 * 隔离：spawn 服务器时 BOOKROOT 指向 mkdtemp 临时书库根，不碰真实 data/。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const SERVER_JS = path.resolve(__dirname, '..', 'server.js');
if (!fs.existsSync(SERVER_JS)) {
  console.error('找不到 server.js：' + SERVER_JS);
  process.exit(1);
}

function mkTmpRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'zhsec-'));
  fs.mkdirSync(path.join(root, 'books', 'testbook', 'assets'), { recursive: true });
  fs.mkdirSync(path.join(root, 'public'), { recursive: true });
  fs.mkdirSync(path.join(root, '题库'), { recursive: true });
  fs.mkdirSync(path.join(root, 'data'), { recursive: true });
  fs.writeFileSync(path.join(root, 'books', 'testbook', 'book.json'), JSON.stringify({
    bookTitle: '测试书', title: '测试书', chapters: [{ id: 'c1', title: '第一章' }],
    stats: { pages: 1 }, blocks: [{ type: 'paragraph', page: 1, text: '正文。' }],
  }));
  fs.writeFileSync(path.join(root, 'books', 'testbook', 'assets', 'a.webp'), 'RIFF-test');
  fs.writeFileSync(path.join(root, 'public', 'index.html'), '<html>shelf</html>');
  fs.writeFileSync(path.join(root, '题库', 'testbook.json'), JSON.stringify({
    bookId: 'testbook', bookTitle: '测试书', chapters: [], questions: [{ id: 't-001', type: 'choice' }],
  }));
  // P0-01 夹具：与 books / public 共享字符串前缀的相邻目录（旧 startsWith 判断会误通过）
  fs.mkdirSync(path.join(root, 'books-private'), { recursive: true });
  fs.writeFileSync(path.join(root, 'books-private', 'secret.txt'), 'SIBLING-SECRET');
  fs.mkdirSync(path.join(root, 'public-private'), { recursive: true });
  fs.writeFileSync(path.join(root, 'public-private', 'secret.txt'), 'PUBLIC-SIBLING-SECRET');
  fs.writeFileSync(path.join(root, 'server.js'), "require('http')"); // 根目录哨兵
  return root;
}

function startServer(port, root, extraEnv) {
  const env = { ...process.env, PORT: String(port), HOST: '127.0.0.1', BOOKROOT: root, AUTH_TOKEN: '' };
  Object.assign(env, extraEnv || {});
  const child = spawn(process.execPath, [SERVER_JS], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.resume();
  child.stderr.resume();
  return child;
}

async function waitHealth(base, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(base + '/api/health');
      if (r.status === 200) return;
    } catch (e) {}
    await new Promise((res) => setTimeout(res, 100));
  }
  throw new Error('服务器未在 6 秒内就绪：' + base);
}

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('PASS', name); }
  else { fail++; console.log('FAIL', name, detail || ''); }
}

function json(method, url, body, headers) {
  return fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...(headers || {}) },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}
function rawRequest(port, method, rawPath) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path: rawPath }, (res) => {
      let b = '';
      res.on('data', (c) => (b += c));
      res.on('end', () => resolve({ status: res.statusCode, body: b }));
    });
    req.on('error', reject);
    req.end();
  });
}

async function main() {
  const root = mkTmpRoot();
  const port = 14500 + (process.pid % 400);
  const base = `http://127.0.0.1:${port}`;
  const child = startServer(port, root);
  const port2 = port + 1;
  const base2 = `http://127.0.0.1:${port2}`;
  let child2 = null;
  try {
    await waitHealth(base);

    // ---- 正常流回归 ----
    let r = await json('PUT', base + '/api/progress', { bookId: 'testbook', page: 3, total: 10 });
    check('正常 PUT progress 200', r.status === 200, r.status);
    r = await fetch(base + '/api/books/testbook');
    check('正常 GET book 200', r.status === 200, r.status);
    r = await fetch(base + '/');
    check('正常 GET / 200', r.status === 200, r.status);
    r = await fetch(base + '/books/testbook/assets/a.webp');
    check('正常 GET asset 200', r.status === 200, r.status);
    r = await json('POST', base + '/api/practice', { bookId: 'testbook', questionId: 't-001', correct: true, chosen: 'A', type: 'choice' });
    check('正常 POST practice 200', r.status === 200, r.status);

    // ---- R1 ----
    r = await json('PUT', base + '/api/progress', { bookId: 'testbook', pad: 'A'.repeat(300000) });
    check('R1 300KB body 413', r.status === 413, r.status);
    r = await json('PUT', base + '/api/progress', { bookId: 'testbook', page: 1 });
    check('R1 超限后服务仍正常 200', r.status === 200, r.status);
    r = await json('PUT', base + '/api/progress', '{bad json');
    check('R1 坏 JSON 400', r.status === 400, r.status);

    // ---- R2 ----
    r = await json('PUT', base + '/api/progress', { bookId: 'a/b', page: 1 });
    check('R2 bookId 含斜杠 400', r.status === 400, r.status);
    r = await json('PUT', base + '/api/progress', { bookId: 'x'.repeat(121), page: 1 });
    check('R2 bookId 121 字符 400', r.status === 400, r.status);
    r = await json('PUT', base + '/api/progress', { bookId: 'testbook', page: 'abc' });
    check('R2 page 非数字 400', r.status === 400, r.status);
    r = await json('PUT', base + '/api/progress', { bookId: 'testbook', page: -1 });
    check('R2 page 负数 400', r.status === 400, r.status);
    r = await json('POST', base + '/api/bookmarks', { bookId: 'testbook', page: 5, chapter: 'y'.repeat(201) });
    check('R2 chapter 201 字符 400', r.status === 400, r.status);
    r = await json('POST', base + '/api/bookmarks', { bookId: 'testbook', page: 'x' });
    check('R2 书签 page 非数字 400', r.status === 400, r.status);
    r = await json('POST', base + '/api/practice', { bookId: 'testbook' });
    check('R2 practice 缺 questionId 400', r.status === 400, r.status);
    r = await json('POST', base + '/api/practice', { bookId: 'testbook', questionId: 'q'.repeat(121) });
    check('R2 questionId 过长 400', r.status === 400, r.status);
    // 校验失败不写盘：坏 bookId 不应出现在 progress 文件
    const prog = JSON.parse(fs.readFileSync(path.join(root, 'data', 'progress.json'), 'utf8'));
    check('R2 校验失败未写盘', !('a/b' in prog.books) && !('x'.repeat(121) in prog.books));

    // ---- R3 / R5 ----
    r = await fetch(base + '/api/books/%zz');
    check('R3 非法编码 %zz 400', r.status === 400, r.status);
    r = await fetch(base + '/api/books/%25zz');
    check('R3 双重编码 %25zz 400', r.status === 400, r.status);
    r = await fetch(base + '/api/books/' + 'z'.repeat(121));
    check('R5 URL id 121 字符 400', r.status === 400, r.status);

    // ---- R4 ----
    r = await fetch(base + '/api/books/a%00b');
    check('R4 空字节 400', r.status === 400, r.status);
    let t = await rawRequest(port, 'GET', '/books/testbook/../../server.js');
    check('R4 asset 穿越拒绝', (t.status === 403 || t.status === 404) && !t.body.includes("require('http')"), t.status);
    t = await rawRequest(port, 'GET', '/../../server.js');
    check('R4 静态穿越拒绝', (t.status === 403 || t.status === 404) && !t.body.includes("require('http')"), t.status);

    // ---- R7 P0-01 路径边界：相邻前缀目录 + 符号链接 ----
    // 旧实现 file.startsWith(BOOKS) 会放行 books-private/（'…/books-private' 以 '…/books' 开头）
    let sib = await rawRequest(port, 'GET', '/books/../books-private/secret.txt');
    check('R7 books 相邻前缀目录不泄露', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    sib = await rawRequest(port, 'GET', '/books/testbook/../../books-private/secret.txt');
    check('R7 books 多级 .. 不泄露', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    sib = await rawRequest(port, 'GET', '/books/%2e%2e/books-private/secret.txt');
    check('R7 books 编码 .. 不泄露', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    sib = await rawRequest(port, 'GET', '/../public-private/secret.txt');
    check('R7 public 相邻前缀目录不泄露', sib.status === 403 && !sib.body.includes('PUBLIC-SIBLING-SECRET'), sib.status);
    sib = await rawRequest(port, 'GET', '/api/books/..');
    check('R7 API bookId=.. 不泄露根目录 book.json', sib.status === 404, sib.status);

    // 符号链接：库内指向库外 → 必须拒绝（Windows 无权限时跳过，CI Linux 会执行）
    let linkMade = false;
    try {
      fs.symlinkSync(path.join(root, 'books-private'),
        path.join(root, 'books', 'testbook', 'assets', 'link-out'), 'dir');
      linkMade = true;
    } catch (e) { linkMade = false; }
    if (linkMade) {
      sib = await rawRequest(port, 'GET', '/books/testbook/assets/link-out/secret.txt');
      check('R7 符号链接指向库外拒绝', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    } else {
      console.log('SKIP R7 符号链接（当前系统不允许创建符号链接）');
    }

    // ---- R6 AUTH_TOKEN ----
    child2 = startServer(port2, root, { AUTH_TOKEN: 'sekret' });
    await waitHealth(base2);
    r = await json('POST', base2 + '/api/practice', { bookId: 'testbook', questionId: 't-001', correct: true });
    check('R6 写接口无 token 401', r.status === 401, r.status);
    r = await json('POST', base2 + '/api/practice',
      { bookId: 'testbook', questionId: 't-001', correct: true },
      { Authorization: 'Bearer sekret' });
    check('R6 写接口带 token 200', r.status === 200, r.status);
    r = await fetch(base2 + '/api/books');
    check('R6 GET 不受 token 影响 200', r.status === 200, r.status);
  } finally {
    child.kill();
    if (child2) child2.kill();
    fs.rmSync(root, { recursive: true, force: true });
  }

  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error('测试异常：', e); process.exit(1); });
