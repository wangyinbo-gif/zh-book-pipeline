'use strict';
/**
 * server.js 安全回归测试（零依赖，Node 18+，Windows / Linux CI 通用）
 * 用法：node tools/test_server_security.js
 *
 * 覆盖（方案 §5 阶段四）：
 *   R1  请求体上限(413)                 R2  字段校验(400)
 *   R3  编码兜底(400)                   R4  路径穿越与空字节(403/400)
 *   R5  URL id 长度(400)                R6  AUTH_TOKEN → 统一 gate（旧 Bearer 直通已废止）
 *   R7  路径边界（P0-01 相邻前缀目录 + 符号链接）
 *   R8  未认证 GET /api/*        → 401 {"error":"unauthorized"}
 *   R9  未认证 GET /books 资产   → 401
 *   R10 未认证写（PUT/POST/DELETE）→ 401
 *   R11 错 token 登录            → 401 {"ok":false,"error":"bad_token"}（+ 失败限流 429）
 *   R12 对 token 登录建会话       → 200 {ok,csrf(hex32),expires} + Set-Cookie zhuxue_sid=<hex64>
 *   R13 过期会话（SESSION_TTL=1） → 401
 *   R14 logout 撤销会话 + 清 Cookie → 200，随后 401
 *   R15 CSRF 缺头/错头/坏 Origin|Referer → 403 {"error":"csrf"}
 *   R16 AUTH_TOKEN 空 + 非回环绑定 → 拒绝启动（退出码非 0 + 生成方法提示）
 *   R17 TRUST_PROXY=1 + X-Forwarded-Proto=https → Set-Cookie 带 Secure；未设则忽略该头
 *   R18 Host 白名单（G1 / DNS rebinding）：伪造 Host → 400 {"error":"bad_host"}；合法 Host（含端口/IPv6）
 *       放行；HOST=0.0.0.0 收网卡地址；ALLOW_EXTRA_HOSTS 自定义域名逃生门
 *   R19 匿名白名单方法矩阵（G2）：路径命中但方法不符 → 405 {"error":"method_not_allowed"}；
 *       静态/资产分发只接 GET|HEAD；open 与 session 两态同构
 *   R20 前导斜杠归一（G8）：//api/books、///api/books 归一后按 api 分类 → 401（不再 302）；
 *       静态解析仍用原路径（P0-01 零变化），别名不会命中业务 API 分支
 *   R21 AUTH_ENFORCE 组合（G10）：AUTH_ENFORCE=1 且 AUTH_TOKEN 空 → 启动即退出码 79 + 提示；
 *       配一次性随机 token 覆盖正常 AUTH_ENFORCE 路径
 *   R22 PATCH 计入写方法（G9）：带 cookie 无 csrf → 403；带 csrf → 进业务校验（404 接口不存在）
 *   G7  /review 与 /review/ 目录 URL 重写为 /review/index.html（仅静态层）
 *   另：open 模式（无 token + 回环）逐条直通＝v1.0.1 现状；日志绝不出现 token/cookie/csrf 值。
 *
 * 隔离：spawn 服务器时 BOOKROOT 指向 mkdtemp 临时书库根，不碰真实 data/。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const net = require('net');
const crypto = require('crypto');
const { spawn } = require('child_process');

const SERVER_JS = path.resolve(__dirname, '..', 'server.js');
if (!fs.existsSync(SERVER_JS)) {
  console.error('找不到 server.js：' + SERVER_JS);
  process.exit(1);
}

const TOKEN = 'sekret';            // 测试口令（断言日志里不得出现）
const JH = { 'Content-Type': 'application/json' };

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
  fs.writeFileSync(path.join(root, 'public', 'login.html'), '<html>login</html>');
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

const kids = []; // 所有被 spawn 的服务器，finally 统一回收
function startServer(port, root, extraEnv) {
  const env = { ...process.env, PORT: String(port), HOST: '127.0.0.1', BOOKROOT: root, AUTH_TOKEN: '' };
  Object.assign(env, extraEnv || {});
  const child = spawn(process.execPath, [SERVER_JS], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  child.out = '';
  child.err = '';
  child.stdout.on('data', (d) => { child.out += d; });
  child.stderr.on('data', (d) => { child.err += d; });
  kids.push(child);
  return child;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}
async function allocPorts(n) {
  const a = [];
  for (let i = 0; i < n; i++) a.push(await freePort());
  return a;
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
  else { fail++; console.log('FAIL', name, detail === undefined ? '' : String(detail).slice(0, 220)); }
}
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
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
/** 可控 header / body / cookie 的原始请求（认证用例要用 set-cookie 与自定义 Origin） */
function req2(port, method, rawPath, opts) {
  opts = opts || {};
  return new Promise((resolve, reject) => {
    const r = http.request(
      { host: '127.0.0.1', port, method, path: rawPath, headers: opts.headers || {} },
      (res) => {
        let b = '';
        res.setEncoding('utf8');
        res.on('data', (c) => (b += c));
        res.on('end', () => resolve({
          status: res.statusCode,
          headers: res.headers,
          body: b,
          cookies: res.headers['set-cookie'] || [],
          location: res.headers.location || '',
        }));
      }
    );
    r.on('error', reject);
    if (opts.body !== undefined) r.write(opts.body);
    r.end();
  });
}
function login(port, token, headers) {
  return req2(port, 'POST', '/api/auth/login', {
    headers: { ...JH, ...(headers || {}) },
    body: JSON.stringify(token === undefined ? {} : { token: token }),
  });
}
function sidOf(res) {
  const m = /zhuxue_sid=([0-9a-f]{64})/.exec(res.cookies[0] || '');
  return m ? m[1] : '';
}
function csrfOf(res) {
  try { return JSON.parse(res.body).csrf || ''; } catch (e) { return ''; }
}
function ck(sid) {
  return 'zhuxue_sid=' + sid;
}
function isUnauthorized(res) {
  return res.status === 401 && res.body.trim() === '{"error":"unauthorized"}';
}
function isCsrf(res) {
  return res.status === 403 && res.body.trim() === '{"error":"csrf"}';
}
/** R16：期望子进程自行退出（非 0），返回 {code, killed, out, err} */
function expectRefusedStart(port, root, extraEnv) {
  return new Promise((resolve) => {
    const child = startServer(port, root, extraEnv);
    const timer = setTimeout(() => {
      child.kill();
      resolve({ code: null, killed: true, out: child.out, err: child.err });
    }, 5000);
    child.on('exit', (code) => {
      clearTimeout(timer);
      resolve({ code, killed: false, out: child.out, err: child.err });
    });
  });
}

async function main() {
  const root = mkTmpRoot();
  // 全部端口一次性随机分配（R18-R22 追加：pExtra=ALLOW_EXTRA_HOSTS、pEnf=空 token 拒绝启动、pEnfOk=随机 token 正例）
  const ports = await allocPorts(10);
  const pOpen = ports[0], pSess = ports[1], pTtl = ports[2], pProxy = ports[3];
  const pRL = ports[4], pLan = ports[5], pRefuse = ports[6];
  const pExtra = ports[7], pEnf = ports[8], pEnfOk = ports[9];
  const base = 'http://127.0.0.1:' + pOpen;
  const base2 = 'http://127.0.0.1:' + pSess;
  let openChild = null;
  try {
    /* ================= open 兼容模式（AUTH_TOKEN 空 + 回环）：R1-R7 原样 ================= */
    openChild = startServer(pOpen, root);
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

    // ---- open 模式自身契约（零变化底线 + 最小 health 体）----
    let t = await req2(pOpen, 'GET', '/api/health');
    check('open /api/health 最小体 {ok,mode}', t.status === 200 && t.body.trim() === '{"ok":true,"mode":"open"}', t.body);
    t = await req2(pOpen, 'GET', '/api/auth/status');
    check('open /api/auth status authed=true', t.status === 200 && JSON.parse(t.body).authed === true && JSON.parse(t.body).mode === 'open', t.body);
    t = await req2(pOpen, 'POST', '/api/auth/logout', { headers: JH, body: '{}' });
    check('open logout 200 无操作', t.status === 200 && t.body.trim() === '{"ok":true}', t.status + ' ' + t.body);
    t = await req2(pOpen, 'GET', '/api/books', { headers: { Accept: 'text/html' } });
    check('open 模式 HTML GET 不被 302 拦截', t.status === 200, t.status);

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
    t = await rawRequest(pOpen, 'GET', '/books/testbook/../../server.js');
    check('R4 asset 穿越拒绝', (t.status === 403 || t.status === 404) && !t.body.includes("require('http')"), t.status);
    t = await rawRequest(pOpen, 'GET', '/../../server.js');
    check('R4 静态穿越拒绝', (t.status === 403 || t.status === 404) && !t.body.includes("require('http')"), t.status);

    // ---- R7 P0-01 路径边界：相邻前缀目录 + 符号链接 ----
    // 旧实现 file.startsWith(BOOKS) 会放行 books-private/（'…/books-private' 以 '…/books' 开头）
    let sib = await rawRequest(pOpen, 'GET', '/books/../books-private/secret.txt');
    check('R7 books 相邻前缀目录不泄露', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    sib = await rawRequest(pOpen, 'GET', '/books/testbook/../../books-private/secret.txt');
    check('R7 books 多级 .. 不泄露', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    sib = await rawRequest(pOpen, 'GET', '/books/%2e%2e/books-private/secret.txt');
    check('R7 books 编码 .. 不泄露', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    sib = await rawRequest(pOpen, 'GET', '/../public-private/secret.txt');
    check('R7 public 相邻前缀目录不泄露', sib.status === 403 && !sib.body.includes('PUBLIC-SIBLING-SECRET'), sib.status);
    sib = await rawRequest(pOpen, 'GET', '/api/books/..');
    check('R7 API bookId=.. 不泄露根目录 book.json', sib.status === 404, sib.status);

    // 符号链接：库内指向库外 → 必须拒绝（Windows 无权限时跳过，CI Linux 会执行）
    let linkMade = false;
    try {
      fs.symlinkSync(path.join(root, 'books-private'),
        path.join(root, 'books', 'testbook', 'assets', 'link-out'), 'dir');
      linkMade = true;
    } catch (e) { linkMade = false; }
    if (linkMade) {
      sib = await rawRequest(pOpen, 'GET', '/books/testbook/assets/link-out/secret.txt');
      check('R7 符号链接指向库外拒绝', sib.status === 403 && !sib.body.includes('SIBLING-SECRET'), sib.status);
    } else {
      console.log('SKIP R7 符号链接（当前系统不允许创建符号链接）');
    }

    /* ================= session 模式（AUTH_TOKEN 非空）：R6 新语义 + R8-R15、R17 ================= */
    const sess = startServer(pSess, root, { AUTH_TOKEN: TOKEN });
    await waitHealth(base2);
    const origin = 'http://127.0.0.1:' + pSess;

    // ---- R6：旧「非 GET /api/* 要 Bearer」直通已按认证契约 v1 废止 ----
    t = await req2(pSess, 'POST', '/api/practice', { headers: { ...JH }, body: JSON.stringify({ bookId: 'testbook', questionId: 't-001', correct: true }) });
    check('R6 写接口无 token 401', isUnauthorized(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/api/practice', {
      headers: { ...JH, Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ bookId: 'testbook', questionId: 't-001', correct: true }),
    });
    check('R6 旧 Bearer 直通已废止(401)', isUnauthorized(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/books');
    check('R6 GET /api/books 也过 gate 401', isUnauthorized(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/health');
    check('R6 health 切到 session 模式', t.status === 200 && JSON.parse(t.body).mode === 'session', t.body);

    // ---- R8 未认证 GET API = 401 ----
    t = await req2(pSess, 'GET', '/api/books');
    check('R8 未认证 GET /api/books 401 且无书数据', isUnauthorized(t) && !t.body.includes('testbook'), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/progress');
    check('R8 未认证 GET /api/progress 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'GET', '/api/questions');
    check('R8 未认证 GET /api/questions 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'GET', '/reader.html?id=testbook', { headers: { Accept: 'text/html' } });
    check('R8 未认证 HTML 页 302 到 login', t.status === 302 && t.location.indexOf('/login.html?next=') === 0, t.status + ' ' + t.location);
    check('R8 302 next 为同源编码路径', decodeURIComponent(t.location.split('next=')[1] || '') === '/reader.html?id=testbook', t.location);
    t = await req2(pSess, 'GET', '/login.html');
    check('R8 白名单 /login.html 匿名可达 200', t.status === 200 && t.body.includes('login'), t.status + ' ' + t.body);

    // ---- R9 未认证 /books 资产 = 401 ----
    t = await req2(pSess, 'GET', '/books/testbook/assets/a.webp');
    check('R9 未认证 /books 资产 401 不泄露内容', isUnauthorized(t) && !t.body.includes('RIFF'), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/books/testbook/assets/a.webp', { headers: { Accept: '*/*' } });
    check('R9 未认证 /books 资产(Accept */*) 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'GET', '/style.css');
    check('R9 未认证静态 js/css 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'GET', '/data/progress.json');
    check('R9 未认证 /data/* 走静态 gate 401', isUnauthorized(t), t.status);

    // ---- R10 未认证写 = 401 ----
    t = await req2(pSess, 'PUT', '/api/progress', { headers: JH, body: JSON.stringify({ bookId: 'testbook', page: 1 }) });
    check('R10 未认证 PUT 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'POST', '/api/bookmarks', { headers: JH, body: JSON.stringify({ bookId: 'testbook', page: 1 }) });
    check('R10 未认证 POST 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'DELETE', '/api/bookmarks/x');
    check('R10 未认证 DELETE 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'POST', '/index.html', { headers: JH, body: '{}' });
    check('R10 未认证非 API 写 401', isUnauthorized(t), t.status);

    // ---- R11 错 token 登录 = 401 ----
    t = await login(pSess, 'wrong-' + TOKEN);
    check('R11 错 token 401 bad_token', t.status === 401 && t.body.trim() === '{"ok":false,"error":"bad_token"}', t.status + ' ' + t.body);
    check('R11 错 token 不下发会话 Cookie', !/zhuxue_sid=[0-9a-f]{64}/.test(t.cookies.join(';')), t.cookies.join('|'));
    t = await login(pSess);
    check('R11 缺 token 字段 401 bad_token', t.status === 401 && JSON.parse(t.body).error === 'bad_token', t.status + ' ' + t.body);
    t = await login(pSess, '');
    check('R11 空 token 401 bad_token', t.status === 401 && JSON.parse(t.body).error === 'bad_token', t.status + ' ' + t.body);
    t = await login(pSess, 'wrong-' + TOKEN, { Origin: origin });
    check('R11 登录入口免 CSRF（带 Origin 也能校验口令）', t.status === 401, t.status);

    // ---- R12 对 token 登录建会话 ----
    const lg = await login(pSess, TOKEN);
    const j12 = JSON.parse(lg.body);
    const sid = sidOf(lg);
    const csrf = csrfOf(lg);
    check('R12 对 token 200', lg.status === 200, lg.status);
    check('R12 body {ok,csrf hex32,expires 秒级}', j12.ok === true && /^[0-9a-f]{32}$/.test(j12.csrf) && Math.abs(j12.expires - Math.floor(Date.now() / 1000) - 28800) <= 5, lg.body);
    check('R12 Set-Cookie 逐字格式', /^zhuxue_sid=[0-9a-f]{64}; HttpOnly; SameSite=Strict; Path=\/; Max-Age=28800$/.test(lg.cookies[0] || ''), lg.cookies[0]);
    check('R12 sid=randomBytes(32) hex64', sid.length === 64, sid.length);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid) } });
    check('R12 会话 Cookie 可取书列表 200', t.status === 200 && t.body.includes('testbook'), t.status);
    t = await req2(pSess, 'GET', '/books/testbook/assets/a.webp', { headers: { Cookie: ck(sid) } });
    check('R12 会话 Cookie 可取资产 200', t.status === 200 && t.body.includes('RIFF'), t.status);
    t = await req2(pSess, 'GET', '/api/auth/status', { headers: { Cookie: ck(sid) } });
    check('R12 status 已认证附 csrf 且无书数据', t.status === 200 && JSON.parse(t.body).authed === true && JSON.parse(t.body).csrf === csrf && !t.body.includes('testbook'), t.body);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: 'zhuxue_sid=' + 'f'.repeat(64) } });
    check('R12 伪造 sid 401', isUnauthorized(t), t.status);

    // ---- R14 logout 撤销会话 + 清 Cookie ----
    t = await req2(pSess, 'POST', '/api/auth/logout', { headers: { Cookie: ck(sid) }, body: '{}' });
    check('R14 logout 200 {ok:true} 免 CSRF', t.status === 200 && t.body.trim() === '{"ok":true}', t.status + ' ' + t.body);
    check('R14 logout 清 Cookie(Max-Age=0)', /zhuxue_sid=;/.test((t.cookies[0] || '')) && /Max-Age=0/.test(t.cookies[0] || ''), t.cookies[0]);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid) } });
    check('R14 logout 后旧会话 401', isUnauthorized(t), t.status);
    const lg2 = await login(pSess, TOKEN);
    const sid2 = sidOf(lg2);
    const csrf2 = csrfOf(lg2);
    check('R14 logout 后可重新登录且 sid 不同', sid2.length === 64 && sid2 !== sid, '');

    // ---- R15 CSRF：缺头 / 错头 / 坏 Origin|Referer = 403 ----
    const PR = JSON.stringify({ bookId: 'testbook', questionId: 't-001', correct: true });
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2) }, body: PR });
    check('R15 缺 X-CSRF-Token 403', isCsrf(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': '0'.repeat(32) }, body: PR });
    check('R15 错 X-CSRF-Token 403', isCsrf(t), t.status);
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: 'http://evil.example' }, body: PR });
    check('R15 坏 Origin 403', isCsrf(t), t.status);
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Referer: 'http://evil.example/x' }, body: PR });
    check('R15 坏 Referer 403', isCsrf(t), t.status);
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: 'http://evil.example:' + pSess }, body: PR });
    check('R15 Origin 端口不符 403', isCsrf(t), t.status);
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: origin }, body: PR });
    check('R15 对 csrf + 同源 Origin 200', t.status === 200, t.status + ' ' + t.body);
    t = await req2(pSess, 'PUT', '/api/progress', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2 }, body: JSON.stringify({ bookId: 'testbook', page: 2 }) });
    check('R15 PUT 带 csrf 无 Origin 200（curl 场景放行）', t.status === 200, t.status);
    t = await req2(pSess, 'DELETE', '/api/practice/t-001?bookId=testbook', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2 } });
    check('R15 DELETE 带 csrf 200', t.status === 200, t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2) } });
    check('R15 GET 不要求 csrf 200', t.status === 200, t.status);

    // ---- 日志脱敏：token / cookie / csrf 值都不得出现在 stdout+stderr ----
    const allOut = sess.out + sess.err;
    check('日志不含口令', !allOut.includes(TOKEN), allOut.slice(0, 120));
    check('日志不含 sid', !allOut.includes(sid) && !allOut.includes(sid2), '');
    check('日志不含 csrf', !allOut.includes(csrf) && !allOut.includes(csrf2), '');

    /* ================= R13 过期会话（SESSION_TTL 注入） ================= */
    const ttlSrv = startServer(pTtl, root, { AUTH_TOKEN: TOKEN, SESSION_TTL: '1' });
    await waitHealth('http://127.0.0.1:' + pTtl);
    const lgTtl = await login(pTtl, TOKEN);
    check('R13 SESSION_TTL=1 → Max-Age=1', /Max-Age=1$/.test(lgTtl.cookies[0] || ''), lgTtl.cookies[0]);
    check('R13 SESSION_TTL=1 → expires≈now+1', Math.abs(JSON.parse(lgTtl.body).expires - Math.floor(Date.now() / 1000) - 1) <= 2, lgTtl.body);
    t = await req2(pTtl, 'GET', '/api/books', { headers: { Cookie: ck(sidOf(lgTtl)) } });
    check('R13 TTL 内会话可用 200', t.status === 200, t.status);
    await sleep(1300);
    t = await req2(pTtl, 'GET', '/api/books', { headers: { Cookie: ck(sidOf(lgTtl)) } });
    check('R13 过期会话 401', isUnauthorized(t), t.status + ' ' + t.body);

    /* ================= R17 TRUST_PROXY / X-Forwarded-Proto ================= */
    const pxSrv = startServer(pProxy, root, { AUTH_TOKEN: TOKEN, TRUST_PROXY: '1' });
    await waitHealth('http://127.0.0.1:' + pProxy);
    const p1 = await login(pProxy, TOKEN, { 'X-Forwarded-Proto': 'https' });
    check('R17 TRUST_PROXY=1 + XFP=https → Secure', /; Secure$/.test(p1.cookies[0] || ''), p1.cookies[0]);
    check('R17 Secure 仍在其余属性之后不破坏格式', /^zhuxue_sid=[0-9a-f]{64}; HttpOnly; SameSite=Strict; Path=\/; Max-Age=28800; Secure$/.test(p1.cookies[0] || ''), p1.cookies[0]);
    const p2 = await login(pProxy, TOKEN);
    check('R17 TRUST_PROXY=1 无 XFP → 无 Secure', !/Secure/.test(p2.cookies[0] || ''), p2.cookies[0]);
    const p3 = await login(pProxy, TOKEN, { 'X-Forwarded-Proto': 'http' });
    check('R17 XFP=http → 无 Secure', !/Secure/.test(p3.cookies[0] || ''), p3.cookies[0]);
    const p4 = await login(pSess, TOKEN, { 'X-Forwarded-Proto': 'https' });
    check('R17 未设 TRUST_PROXY 忽略代理头（无 Secure）', !/Secure/.test(p4.cookies[0] || ''), p4.cookies[0]);

    /* ================= 附加：登录失败限流 10 次/分钟 → 429 ================= */
    const rlSrv = startServer(pRL, root, { AUTH_TOKEN: TOKEN });
    await waitHealth('http://127.0.0.1:' + pRL);
    let codes = [];
    for (let i = 0; i < 10; i++) {
      const x = await login(pRL, 'nope-' + i);
      codes.push(x.status);
    }
    t = await login(pRL, 'nope-11');
    check('R11 失败 10 次内都 401', codes.every((c) => c === 401), codes.join(','));
    check('R11 第 11 次 429 rate_limited', t.status === 429 && t.body.trim() === '{"ok":false,"error":"rate_limited"}', t.status + ' ' + t.body);
    t = await login(pRL, TOKEN);
    check('R11 限流窗口内对 token 也 429', t.status === 429, t.status);

    /* ================= R16 非回环 + 空 token → 拒绝启动 ================= */
    const refused = await expectRefusedStart(pRefuse, root, { HOST: '0.0.0.0', AUTH_TOKEN: '' });
    check('R16 非回环无 token 退出码非 0', refused.killed === false && refused.code !== 0 && refused.code !== null, 'code=' + refused.code + ' killed=' + refused.killed);
    const refMsg = refused.err + refused.out;
    check('R16 拒绝启动提示 AUTH_TOKEN', /AUTH_TOKEN/.test(refMsg), refMsg.slice(0, 160));
    check('R16 提示给出 token 生成方法', /randomBytes/.test(refMsg) && /node/.test(refMsg), refMsg.slice(0, 220));
    check('R16 拒绝启动不监听端口', !(await fetch('http://127.0.0.1:' + pRefuse + '/api/health').then(() => true, () => false)), '');
    // 正例：非回环 + 有 token 可正常启动并强制登录
    const lanSrv = startServer(pLan, root, { AUTH_TOKEN: TOKEN, HOST: '0.0.0.0' });
    await waitHealth('http://127.0.0.1:' + pLan);
    t = await req2(pLan, 'GET', '/api/books');
    check('R16 正例 非回环+token 启动并 gate 生效', isUnauthorized(t), t.status);
    check('R16 正例 启动日志含 session 模式', /session/.test(lanSrv.out), lanSrv.out.slice(0, 200));
    check('R16 正例 启动日志不含口令', !(lanSrv.out + lanSrv.err).includes(TOKEN), '');
    /* ================= R18（G1 / P0）Host 白名单：DNS rebinding ================= */
    // gate 第 0 层：Host（剥端口）不在允许集 → 400 {"error":"bad_host"}，先于会话与 CSRF 判断。
    // 下面每条都用"同一合法会话 + 只换 Host 头"，证明拦截只能来自 Host（矩阵 G1 实测原为 200 全库 / 200 写入）。
    const BAD_HOST = '{"error":"bad_host"}';
    const isBadHost = (x) => x.status === 400 && x.body.trim() === BAD_HOST;
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2), Host: 'evil.example' } });
    check('R18 Host: evil.example → GET /api/books 400 bad_host 且无书数据', isBadHost(t) && !t.body.includes('testbook'), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: 'http://evil.example', Host: 'evil.example' }, body: PR });
    check('R18 Host: evil.example + 假同源 Origin → POST /api/practice 400（Host 校验先于 CSRF）', isBadHost(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/reader.html', { headers: { Cookie: ck(sid2), Host: 'evil.example', Accept: 'text/html' } });
    check('R18 Host: evil.example → 静态页 400（不再是 200 页面）', isBadHost(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2), Host: '127.0.0.1.evil.example' } });
    check('R18 前缀伪装 Host 127.0.0.1.evil.example 400', isBadHost(t), t.status);
    // 合法 Host 形态：浏览器对 127.0.0.1:端口 实发 Host "127.0.0.1:端口"，比对前必须剥端口
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2), Host: '127.0.0.1:' + pSess } });
    check('R18 Host: 127.0.0.1:端口 → 200', t.status === 200 && t.body.includes('testbook'), t.status);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2), Host: 'localhost:' + pSess } });
    check('R18 Host: localhost:端口 → 200', t.status === 200, t.status);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2), Host: '127.0.0.1' } });
    check('R18 Host 无端口形态 → 200', t.status === 200, t.status);
    t = await req2(pSess, 'GET', '/api/books', { headers: { Cookie: ck(sid2), Host: '[::1]:' + pSess } });
    check('R18 Host: [::1]:端口 → 200（IPv6 方括号形态不被剥端口逻辑破坏）', t.status === 200, t.status);
    // HOST=0.0.0.0：允许集含本机网卡地址（手机/平板走局域网 IP），但陌生域名仍拒
    t = await req2(pLan, 'GET', '/api/books', { headers: { Host: '127.0.0.1:' + pLan } });
    check('R18 HOST=0.0.0.0 回环 Host 未被误挡（401 而非 400）', t.status === 401, t.status);
    t = await req2(pLan, 'GET', '/api/books', { headers: { Host: 'evil.example' } });
    check('R18 HOST=0.0.0.0 陌生 Host 仍 400', isBadHost(t), t.status);
    let lanIP = '';
    {
      const nets = os.networkInterfaces();
      for (const n of Object.keys(nets)) {
        for (const ni of nets[n] || []) if (ni.family === 'IPv4' && !ni.internal) lanIP = ni.address;
      }
    }
    if (lanIP) {
      t = await req2(pLan, 'GET', '/api/books', { headers: { Host: lanIP + ':' + pLan } });
      check('R18 HOST=0.0.0.0 时网卡 IP 进入允许集（401 而非 400）', t.status === 401, lanIP + ' → ' + t.status);
    } else {
      console.log('SKIP R18 网卡 IP 用例（本机无非回环 IPv4）');
    }
    // ALLOW_EXTRA_HOSTS=逗号分隔：自定义域名/反代 Host 的显式逃生门（表项自带端口时也先剥端口）
    const exSrv = startServer(pExtra, root, { AUTH_TOKEN: TOKEN, ALLOW_EXTRA_HOSTS: 'evil.example, books.local:8080' });
    await waitHealth('http://127.0.0.1:' + pExtra);
    const sidEx = sidOf(await login(pExtra, TOKEN));
    t = await req2(pExtra, 'GET', '/api/books', { headers: { Cookie: ck(sidEx), Host: 'evil.example:' + pExtra } });
    check('R18 ALLOW_EXTRA_HOSTS 生效后该 Host 200', t.status === 200 && t.body.includes('testbook'), t.status);
    t = await req2(pExtra, 'GET', '/api/books', { headers: { Cookie: ck(sidEx), Host: 'books.local' } });
    check('R18 ALLOW_EXTRA_HOSTS 表项带端口声明仍匹配裸 Host', t.status === 200, t.status);
    t = await req2(pExtra, 'GET', '/api/books', { headers: { Cookie: ck(sidEx), Host: 'unlisted.example' } });
    check('R18 未声明 Host 仍 400（EXTRA 不是通配开关）', isBadHost(t), t.status);
    // open 模式同构执行：Host 本身是 Origin/Referer 的比对基准，两态一致才没有"先放行再校验"的余地
    t = await req2(pOpen, 'GET', '/api/books', { headers: { Host: 'evil.example' } });
    check('R18 open 模式伪造 Host 也 400（两态同构；正常 Host 下 open 行为零变化）', isBadHost(t), t.status + ' ' + t.body);

    /* ================= R19（G2 / P0）匿名白名单方法矩阵 ================= */
    // 稳定值口径（server.js gate 顺序）：
    //   Host 400 → 白名单方法不符 405 → open 直通 → 未认证 401/302 → 已认证写方法 CSRF 403 → 分发（静态非 GET|HEAD 405）
    // 于是：白名单路径 + 非法方法 → 两态一律 405（不再落到静态/业务分发）；
    //       非白名单路径 + 写方法 → session 未认证 401（鉴权优先，不向未登录者暴露资源存在性）、open 405。
    // open 模式对静态非 GET/HEAD 选 405（而不是沿用原分发结果）的理由：方法集是路由语义声明、不是鉴权开关，
    // 两态同构才不会留下"只有 open 部署才能 PUT 到静态分发层"的形态差异；GET/HEAD 的正常读路径一字不变。
    const MA = '{"error":"method_not_allowed"}';
    const isMA = (x) => x.status === 405 && x.body.trim() === MA;
    t = await req2(pSess, 'PUT', '/api/health');
    check('R19 匿名 PUT /api/health → 405（旧 200）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'DELETE', '/api/health');
    check('R19 匿名 DELETE /api/health → 405（旧 200）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pOpen, 'PUT', '/api/health');
    check('R19 open PUT /api/health → 405（与 session 同构）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/login.html', { headers: JH, body: '{}' });
    check('R19 匿名 POST /login.html → 405（旧 200）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'PUT', '/login.html', { headers: JH, body: '{}' });
    check('R19 匿名 PUT /login.html → 405（旧 200）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pOpen, 'POST', '/login.html', { headers: JH, body: '{}' });
    check('R19 open POST /login.html → 405（两态同构）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/login.html');
    check('R19 GET /login.html → 200（白名单方法仍匿名放行）', t.status === 200 && t.body.includes('login'), t.status);
    t = await req2(pSess, 'PUT', '/api/auth/status');
    check('R19 PUT /api/auth/status → 405（旧 404）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/api/auth/status', { headers: JH, body: '{}' });
    check('R19 POST /api/auth/status → 405', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/auth/login');
    check('R19 GET /api/auth/login → 405（只接 POST，旧 404）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'HEAD', '/api/health');
    check('R19 HEAD /api/health → 200 无体（白名单含 HEAD）', t.status === 200 && t.body === '', t.status + ' ' + t.body);
    // OPTIONS：旧实现"白名单命中即 return"⇒ 匿名 200；本轮按方法集判 → 405（两态同构）
    t = await req2(pSess, 'OPTIONS', '/api/health');
    check('R19 OPTIONS /api/health → 405（session；旧 200）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pOpen, 'OPTIONS', '/api/health');
    check('R19 OPTIONS /api/health → 405（open，两态同构）', isMA(t), t.status + ' ' + t.body);
    // 白名单表查找只吃"以单个 / 开头的精确键"，Object.prototype 上的键名蹭不到白名单
    t = await req2(pSess, 'GET', '/hasOwnProperty');
    check('R19 GET /hasOwnProperty 不被误判为白名单（默认拒绝 401）', isUnauthorized(t), t.status + ' ' + t.body);
    // 注：Node 的 HTTP 解析层会直接拒掉未知方法（400），不会进到 gate；
    //     方法比对用 methodOf() 大写 + anon[m] === 1，原型键名（TOSTRING 等）恒不命中。
    // 非白名单路径的写方法：断言上面注释的 gate 顺序稳定值
    t = await req2(pSess, 'POST', '/books/testbook/assets/a.webp', { headers: JH, body: '{}' });
    check('R19 session 未认证 POST /books 资产 → 401（鉴权先于静态 405）', isUnauthorized(t), t.status + ' ' + t.body);
    t = await req2(pOpen, 'POST', '/books/testbook/assets/a.webp', { headers: JH, body: '{}' });
    check('R19 open POST /books 资产 → 405（open 无鉴权可言，直接吃静态层方法限制）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pOpen, 'POST', '/index.html', { headers: JH, body: '{}' });
    check('R19 open POST /index.html → 405（静态只接 GET|HEAD）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/index.html', { headers: { Cookie: ck(sid2) }, body: '{}' });
    check('R19 session 已认证无 csrf POST 静态 → 403（CSRF 先于分发 405）', isCsrf(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'POST', '/index.html', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: origin }, body: '{}' });
    check('R19 session 已认证带 csrf POST 静态 → 405（与 open 同构）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'HEAD', '/index.html', { headers: { Cookie: ck(sid2) } });
    check('R19 HEAD 静态仍 200（现状零变化）', t.status === 200, t.status);

    /* ================= R20（G8 / P2）前导斜杠归一 ================= */
    t = await req2(pSess, 'GET', '//api/books', { headers: { Accept: 'text/html' } });
    check('R20 session 未登录 //api/books → 401（归一后按 api 分类，不再 302）', isUnauthorized(t) && !t.location, t.status + ' ' + t.location);
    t = await req2(pSess, 'GET', '///api/books', { headers: { Accept: 'text/html' } });
    check('R20 session 未登录 ///api/books → 401', isUnauthorized(t) && !t.location, t.status + ' ' + t.location);
    t = await req2(pSess, 'GET', '/api//books', { headers: { Accept: 'text/html' } });
    check('R20 session 未登录 /api//books → 401（原口径不劣化）', isUnauthorized(t), t.status + ' ' + t.location);
    t = await req2(pSess, 'GET', '/%61pi/books', { headers: { Accept: 'text/html' } });
    check('R20 编码首段 /%61pi/books 解码后仍按 api → 401', isUnauthorized(t), t.status);
    t = await req2(pOpen, 'GET', '//api/books');
    check('R20 open //api/books 仍落静态 404/403（静态解析零变化，无书数据）', (t.status === 404 || t.status === 403) && !t.body.includes('testbook'), t.status + ' ' + t.body.slice(0, 60));
    t = await req2(pSess, 'GET', '//api/books', { headers: { Cookie: ck(sid2) } });
    check('R20 已登录 //api/books → 404 无数据（别名不命中业务 API 分支）', t.status === 404 && !t.body.includes('testbook'), t.status + ' ' + t.body.slice(0, 60));
    t = await req2(pSess, 'GET', '//login.html', { headers: { Accept: 'text/html' } });
    check('R20 //login.html 归一后命中白名单，静态兜底解析到 login.html 而非 index.html', t.status === 200 && t.body.includes('login') && !t.body.includes('shelf'), t.status + ' ' + t.body.slice(0, 60));
    t = await req2(pSess, 'GET', '//reader.html', { headers: { Accept: 'text/html' } });
    check('R20 //reader.html 未登录仍拦 302 且 next 归一为 /reader.html', t.status === 302 && decodeURIComponent(t.location.split('next=')[1] || '') === '/reader.html', t.status + ' ' + t.location);
    t = await req2(pSess, 'GET', '/API/books');
    check('R20 大小写 /API/books 仍默认拒绝 401（归一不放开大小写）', isUnauthorized(t), t.status);

    /* ================= R21（G10 / P3）AUTH_ENFORCE 组合 ================= */
    const enf = await expectRefusedStart(pEnf, root, { AUTH_ENFORCE: '1', AUTH_TOKEN: '' });
    check('R21 AUTH_ENFORCE=1 空 token → 启动即退出码 79', enf.killed === false && enf.code === 79, 'code=' + enf.code + ' killed=' + enf.killed);
    const enfMsg = enf.err + enf.out;
    check('R21 退出提示点名 AUTH_ENFORCE 与必须设 AUTH_TOKEN', /AUTH_ENFORCE/.test(enfMsg) && /AUTH_TOKEN/.test(enfMsg), enfMsg.slice(0, 160));
    check('R21 提示给出 token 生成方法', /randomBytes/.test(enfMsg) && /node/.test(enfMsg), enfMsg.slice(0, 200));
    check('R21 拒绝启动不监听端口', !(await fetch('http://127.0.0.1:' + pEnf + '/api/health').then(() => true, () => false)), '');
    // 正例：AUTH_ENFORCE=1 + 一次性随机 token（覆盖正常 AUTH_ENFORCE 路径，不依赖常量 TOKEN）
    const RND = crypto.randomBytes(16).toString('hex');
    const enfSrv = startServer(pEnfOk, root, { AUTH_ENFORCE: '1', AUTH_TOKEN: RND });
    await waitHealth('http://127.0.0.1:' + pEnfOk);
    t = await req2(pEnfOk, 'GET', '/api/books');
    check('R21 正例 AUTH_ENFORCE+token 未登录 gate 401', isUnauthorized(t), t.status);
    const lgE1 = await login(pEnfOk, 'wrong-' + RND);
    check('R21 正例 错 token 仍 401 bad_token', lgE1.status === 401 && JSON.parse(lgE1.body).error === 'bad_token', lgE1.status);
    const lgE2 = await login(pEnfOk, RND);
    check('R21 正例 随机 token 登录 200 + csrf hex32', lgE2.status === 200 && /^[0-9a-f]{32}$/.test(csrfOf(lgE2)), lgE2.status + ' ' + lgE2.body);
    t = await req2(pEnfOk, 'GET', '/api/books', { headers: { Cookie: ck(sidOf(lgE2)) } });
    check('R21 正例 登录后 session 生效 200', t.status === 200 && t.body.includes('testbook'), t.status);
    check('R21 正例 启动日志不含随机口令', !(enfSrv.out + enfSrv.err).includes(RND), '');

    /* ================= R22（G9 / P3）PATCH 计入写方法 ================= */
    t = await req2(pSess, 'PATCH', '/api/practice', { headers: { Cookie: ck(sid2) }, body: PR });
    check('R22 PATCH 带 cookie 无 csrf → 403 csrf', isCsrf(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'PATCH', '/api/practice', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2 }, body: PR });
    check('R22 PATCH 带 csrf → 进业务校验（404 接口不存在，非 401/403）', t.status === 404 && /接口不存在/.test(t.body), t.status + ' ' + t.body);
    t = await req2(pSess, 'PATCH', '/api/practice', { headers: JH, body: PR });
    check('R22 未认证 PATCH → 401', isUnauthorized(t), t.status);
    t = await req2(pSess, 'PATCH', '/index.html', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: origin }, body: '{}' });
    check('R22 静态 PATCH 带 csrf → 405（静态只接 GET|HEAD）', isMA(t), t.status + ' ' + t.body);
    t = await req2(pSess, 'PATCH', '/api/health', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2 }, body: '{}' });
    check('R22 PATCH /api/health → 405（白名单方法集不含 PATCH）', isMA(t), t.status + ' ' + t.body);

    /* ================= G7 /review 与 /review/ 目录 URL 重写 ================= */
    fs.mkdirSync(path.join(root, 'public', 'review'), { recursive: true });
    fs.writeFileSync(path.join(root, 'public', 'review', 'index.html'), '<html>review-index</html>');
    t = await req2(pOpen, 'GET', '/review');
    check('G7 open /review → 200 目录索引', t.status === 200 && t.body.includes('review-index'), t.status + ' ' + t.body.slice(0, 60));
    t = await req2(pOpen, 'GET', '/review/');
    check('G7 open /review/ → 200 同一页', t.status === 200 && t.body.includes('review-index'), t.status);
    t = await req2(pSess, 'GET', '/review', { headers: { Accept: 'text/html' } });
    check('G7 session 未登录 /review → 302 next=/review（登录后不再死链）', t.status === 302 && decodeURIComponent(t.location.split('next=')[1] || '') === '/review', t.status + ' ' + t.location);
    t = await req2(pSess, 'GET', '/review', { headers: { Cookie: ck(sid2) } });
    check('G7 session 已登录 /review → 200 目录索引', t.status === 200 && t.body.includes('review-index'), t.status);
    t = await req2(pSess, 'GET', '/review/index.html', { headers: { Cookie: ck(sid2) } });
    check('G7 原直链 /review/index.html 不受影响 200', t.status === 200 && t.body.includes('review-index'), t.status);
    t = await req2(pSess, 'GET', '/review', { headers: { Cookie: ck(sid2), Host: 'evil.example' } });
    check('G7 /review 同样受 Host 白名单保护 400', isBadHost(t), t.status);
    t = await req2(pSess, 'POST', '/review', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2, Origin: origin }, body: '{}' });
    check('G7 /review 静态层写方法 405（重写不放开写面）', isMA(t), t.status + ' ' + t.body);

    /* ================= R23/R24 检索端点（v1.1.0）：鉴权矩阵与方法绑定 ================= */
    t = await req2(pSess, 'GET', '/api/search?q=' + encodeURIComponent('测试'));
    check('R23 未登录 /api/search → 401', t.status === 401, t.status + ' ' + t.body);
    t = await req2(pSess, 'GET', '/api/search?q=x', { headers: { Accept: 'text/html' } });
    check('R23 导航头打 /api 仍 401（api 永不 302，契约既定）', t.status === 401, t.status + ' ' + t.location);
    t = await req2(pSess, 'GET', '/api/search?q=' + encodeURIComponent('测试'), { headers: { Cookie: ck(sid2) } });
    let sj = null; try { sj = JSON.parse(t.body); } catch (e) {}
    t = await req2(pSess, 'GET', '/api/search', { headers: { Cookie: ck(sid2) } });
    check('R23 缺 q → 400', t.status === 400, t.status);
    t = await req2(pSess, 'GET', '/api/search?q=a&book=%ZZ', { headers: { Cookie: ck(sid2) } });
    check('R23 book 非法编码 → 400', t.status === 400, t.status);
    t = await req2(pSess, 'GET', '/api/search?q=a', { headers: { Cookie: ck(sid2), Host: 'evil.example' } });
    check('R23 检索同样受 Host 白名单 400', isBadHost(t), t.status);
    t = await req2(pSess, 'POST', '/api/search?q=a', { headers: { Cookie: ck(sid2), 'X-CSRF-Token': csrf2 } });
    check('R23 POST 检索不放行（非 200，落入 404/405）', t.status === 404 || t.status === 405, t.status);
    t = await req2(pSess, 'GET', '/api/search?q=' + encodeURIComponent('测试'), { headers: { Cookie: ck(sid2) } });
    let tj = null; try { tj = JSON.parse(t.body); } catch (e) {}
    t = await req2(pOpen, 'GET', '/api/search?q=' + encodeURIComponent('测试'));
    check('R24 open 模式检索直通 200', t.status === 200, t.status);

  } finally {
    for (const k of kids) {
      try { k.kill(); } catch (e) {}
    }
    if (openChild) { try { openChild.kill(); } catch (e) {} }
    // 夹具清理 + 复核（矩阵 §7 口径：夹具只建在系统 Temp，跑完即删并校验不存在）
    let gone = false;
    for (let i = 0; i < 10 && !gone; i++) {
      try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) {}
      try { gone = !fs.existsSync(root); } catch (e) { gone = false; }
      if (!gone) await sleep(200);
    }
    check('夹具目录已删除并复核（系统 Temp 无残留）', gone, root);
  }

  console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error('测试异常：', e); process.exit(1); });
