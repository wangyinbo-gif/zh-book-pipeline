'use strict';
/**
 * 筑学 · 本地书库服务（零依赖）
 * 启动：node server.js   → http://127.0.0.1:4590
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');
const os = require('os');
const crypto = require('crypto');

const ROOT = process.env.BOOKROOT ? path.resolve(process.env.BOOKROOT) : __dirname;
const PUBLIC = path.join(ROOT, 'public');
const BOOKS = path.join(ROOT, 'books');
const DATA = path.join(ROOT, 'data');
const QBANK = path.join(ROOT, '题库');
const PORT = Number(process.env.PORT || 4590);
const HOST = process.env.HOST || '127.0.0.1';
// 认证契约 v1：AUTH_TOKEN 非空 → 会话认证全路由生效（mode=session）。
// 旧的「非 GET /api/* 要 Authorization: Bearer」直通已按契约废止（测试 R6 已改为固化新行为）。
// AUTH_TOKEN 为空时：回环绑定 → open 兼容模式（全部直通，行为与 v1.0.1 一致）；
//                    非回环绑定 → 拒绝启动（见文件末尾启动守卫，测试 R16）。
const AUTH_TOKEN = process.env.AUTH_TOKEN || '';

/* ---------------- 路径边界（P0-01） ---------------- */
// 严格判断 target 是否位于 base 之内（base 自身算通过）。
// 不能用 file.startsWith(base)：books 与 books-private 这类「相邻前缀目录」会误通过。
function withinDir(base, target) {
  const rel = path.relative(base, target);
  if (rel === '') return true;
  if (path.isAbsolute(rel)) return false;
  return rel !== '..' && !rel.startsWith('..' + path.sep);
}

// 把 parts 解析到 base 下并做边界校验（含符号链接真实路径）；越界/无法判定返回 null。
function resolveInside(base, ...parts) {
  const baseAbs = path.resolve(base);
  const target = path.resolve(baseAbs, ...parts);
  if (!withinDir(baseAbs, target)) return null;
  let baseReal;
  try { baseReal = fs.realpathSync(baseAbs); } catch (e) { baseReal = baseAbs; }
  // 从 target 往上找最近的已存在祖先，取它 realpath 后拼回剩余路径段：
  // 这样既能识别目标本身是符号链接，也能识别「父目录是符号链接指向外部」。
  let probe = target;
  let rest = '';
  for (let i = 0; i < 64; i++) {
    let real = null;
    try { real = fs.realpathSync(probe); } catch (e) { real = null; }
    if (real !== null) {
      const effective = rest ? path.join(real, rest) : real;
      return withinDir(baseReal, effective) ? target : null;
    }
    const parent = path.dirname(probe);
    if (parent === probe) return null;
    rest = path.join(path.basename(probe), rest);
    probe = parent;
  }
  return null;
}

/* ---------------- 字段校验（R2/R5） ---------------- */
const MAX_ID_LEN = 120; // 真实全库 bookId 最长 86 字符（2026-11-25 实测）
const MAX_PAGE = 200000; // 单本真实最大 679 页
const MAX_SHORT = 200; // chapter 等短文本

function validId(v) {
  return (
    typeof v === 'string' &&
    v.length > 0 &&
    v.length <= MAX_ID_LEN &&
    !/[\\/\0]/.test(v)
  );
}
function numInRange(v, min, max) {
  const n = Number(v);
  return Number.isFinite(n) && n >= min && n <= max ? n : null;
}

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function sendJSON(res, code, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body)
  });
  res.end(body);
}

/* 路由段二次解码兜底：非法 % 序列返回 null，调用方回 400 */
function safeDecode(s) {
  try {
    return decodeURIComponent(s);
  } catch (e) {
    return null;
  }
}

function readJSON(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return fallback;
  }
}

function writeJSON(file, value) {
  ensureDir(path.dirname(file));
  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

/* ---------------- 书库扫描 ---------------- */
function bookDirs() {
  if (!fs.existsSync(BOOKS)) return [];
  return fs
    .readdirSync(BOOKS, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name)
    .filter((name) => fs.existsSync(path.join(BOOKS, name, 'book.json')));
}

function bookMeta(id) {
  const dir = path.join(BOOKS, id);
  const book = readJSON(path.join(dir, 'book.json'), null);
  if (!book) return null;
  let stat = null;
  try {
    stat = fs.statSync(path.join(dir, 'book.json'));
  } catch (e) {}
  const chapters = Array.isArray(book.chapters) ? book.chapters : [];
  return {
    id,
    bookTitle: book.bookTitle || book.title || id,
    title: book.title || '',
    source: book.source || '',
    author: book.author || '',
    shelfCategory: book.shelfCategory || '扩展',
    pageRange: book.pageRange || [],
    generatedAt: book.generatedAt || '',
    updatedAt: stat ? stat.mtimeMs : 0,
    chapters: chapters.map((c) => ({
      id: c.id,
      title: c.title,
      chars: c.chars || 0,
      figures: c.figures || 0,
      startPage: c.startPage || null
    })),
    stats: book.stats || {},
    cover: book.cover || ''
  };
}

function listBooks() {
  const list = [];
  for (const id of bookDirs()) {
    const meta = bookMeta(id);
    if (meta) list.push(meta);
  }
  list.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  return list;
}

function getBook(id) {
  const safe = path.basename(String(id || ''));
  // P0-01：safe 可能是 '..'，basename 并不能兜住 → 一律走 resolveInside 边界校验
  const file = resolveInside(BOOKS, safe, 'book.json');
  if (!file || !fs.existsSync(file)) return null;
  return readJSON(file, null);
}

/* ---------------- 进度 ---------------- */
const PROGRESS_FILE = path.join(DATA, 'progress.json');
function loadProgress() {
  return readJSON(PROGRESS_FILE, { version: 1, books: {} });
}
function saveProgress(progress) {
  writeJSON(PROGRESS_FILE, progress);
}

/* ---------------- 书签 ---------------- */
const BOOKMARKS_FILE = path.join(DATA, 'bookmarks.json');
function loadBookmarks() {
  return readJSON(BOOKMARKS_FILE, { version: 1, items: [] });
}
function saveBookmarks(db) {
  writeJSON(BOOKMARKS_FILE, db);
}

/* ---------------- 题库 ---------------- */
// 题库可以放在 题库/ 根目录，也可以放进任意非下划线开头的子目录（归档用）。
// 同一 bookId 若有多份，取层级最浅的一份，避免备份/草稿副本抢戏。
let qbankIndexCache = { at: 0, map: null };
function qbankIndex() {
  const now = Date.now();
  if (qbankIndexCache.map && now - qbankIndexCache.at < 3000) return qbankIndexCache.map;
  const found = new Map();
  const walk = (dir, depth) => {
    if (depth > 3) return;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
    for (const ent of entries) {
      if (ent.name[0] === '_') continue;
      const full = path.join(dir, ent.name);
      if (ent.isDirectory()) { walk(full, depth + 1); continue; }
      if (!ent.name.toLowerCase().endsWith('.json')) continue;
      const id = ent.name.replace(/\.json$/i, '');
      if (!id) continue;
      const prev = found.get(id);
      if (!prev || full.split(path.sep).length < prev.file.split(path.sep).length) {
        found.set(id, { id, file: full });
      }
    }
  };
  walk(QBANK, 0);
  qbankIndexCache = { at: now, map: found };
  return found;
}

function qbankFiles() {
  return [...qbankIndex().values()].sort((a, b) => a.file.localeCompare(b.file));
}

function loadBank(bookId) {
  const safe = path.basename(String(bookId || ''));
  if (!safe || safe[0] === '_') return null;
  const hit = qbankIndex().get(safe);
  if (!hit) return null;
  const file = resolveInside(QBANK, hit.file); // P0-01：索引来自磁盘遍历，仍复核边界
  if (!file) return null;
  return readJSON(file, null);
}

function questionIndex() {
  const items = [];
  for (const hit of qbankFiles()) {
    const bank = readJSON(hit.file, null);
    if (!bank || !Array.isArray(bank.questions)) continue;
    const byType = {};
    for (const q of bank.questions) byType[q.type] = (byType[q.type] || 0) + 1;
    const id = bank.bookId || hit.id;
    items.push({
      bookId: id,
      bookTitle: bank.bookTitle || id,
      total: bank.questions.length,
      byType,
      chapters: bank.chapters || []
    });
  }
  items.sort((a, b) => a.bookTitle.localeCompare(b.bookTitle, 'zh'));
  return items;
}

/* ---------------- 刷题记录 ---------------- */
const PRACTICE_FILE = path.join(DATA, 'practice.json');
function loadPractice() {
  return readJSON(PRACTICE_FILE, { version: 1, books: {} });
}
function savePractice(db) {
  writeJSON(PRACTICE_FILE, db);
}
/* ---------------- 请求体（限额） ---------------- */
const MAX_BODY_BYTES = 256 * 1024; // 256 KB，防内存 DoS

/**
 * 读取 JSON 请求体。
 * cb(body, err)：err='TOO_LARGE' 表示超限；body=null 表示非法 JSON。
 * 超限后吞掉剩余数据（req.resume）避免连接挂起。
 */
function readBody(req, cb) {
  let raw = '';
  let size = 0;
  let done = false;
  req.on('data', (c) => {
    if (done) return;
    size += c.length;
    if (size > MAX_BODY_BYTES) {
      done = true;
      req.resume();
      return cb(null, 'TOO_LARGE');
    }
    raw += c;
  });
  req.on('end', () => {
    if (done) return;
    let body = {};
    try {
      body = JSON.parse(raw || '{}');
    } catch (e) {
      return cb(null);
    }
    cb(body);
  });
}

/* ---------------- 认证（认证契约 v1，零依赖） ----------------
 * 字段名/路径/状态码逐字对齐契约，不得私改：
 *   AUTH_TOKEN 非空              → mode=session：除匿名白名单外全部过 gate
 *   AUTH_TOKEN 空 + 回环绑定     → mode=open：全部直通（= v1.0.1 现状），启动日志一行警告
 *   AUTH_TOKEN 空 + 非回环绑定   → 拒绝启动（退出码非 0）＝测试 R16
 *   AUTH_ENFORCE=1 + AUTH_TOKEN 空 → 拒绝启动（退出码 79）＝测试 R21（旧行为是"谁都登不进"的自锁）
 *   AUTH_ENFORCE=1               → 空 token 本机也强制登录（mode=session）
 *   Host 不在白名单              → 400 {"error":"bad_host"}（G1，先于匿名白名单与会话判断）
 *   匿名白名单路径但方法不符      → 405 {"error":"method_not_allowed"}（G2，open/session 同构）
 *   静态与 /books 资产            → 只接 GET|HEAD，其余 405（G2）
 *   SESSION_TTL（秒，默认 28800）→ 会话绝对过期
 *   TRUST_PROXY=1                → 信任 X-Forwarded-Proto，https 时给 Cookie 追加 Secure；
 *                                  不设则完全忽略代理头（测试 R17）
 * 会话表只在内存（Map sid → {csrf, expires}）：绝对过期 + 每小时惰性清理，服务重启即全部失效。
 * 日志绝不输出 token / cookie / csrf 的值（本段代码不做任何请求级日志）。
 */
const SESSION_TTL = (function () {
  const n = Math.floor(Number(process.env.SESSION_TTL || 28800));
  return Number.isFinite(n) && n >= 1 && n <= 31536000 ? n : 28800;
})();
const AUTH_ENFORCE = process.env.AUTH_ENFORCE === '1';
const TRUST_PROXY = process.env.TRUST_PROXY === '1';
const SESSION_COOKIE = 'zhuxue_sid';
const LOGIN_PATH = '/login.html';
// open = 现状直通（零变化底线）；session = 会话认证全路由生效
const AUTH_MODE = AUTH_TOKEN || AUTH_ENFORCE ? 'session' : 'open';
// G9：写方法集合含 PATCH（契约 v1 字面只列 POST/PUT/DELETE，实现按超集执行；本轮契约同步"含 PATCH"）。
const WRITE_METHODS = { POST: 1, PUT: 1, DELETE: 1, PATCH: 1 };
// G2：非 /api 的静态/资产分发只接 GET|HEAD，其余方法 405。
const READ_METHODS = { GET: 1, HEAD: 1 };
const SID_RE = /^[0-9a-f]{64}$/;
const CSRF_RE = /^[0-9a-f]{32}$/;

function methodOf(req) {
  return String(req.method || '').toUpperCase();
}

// 匿名白名单（仅此五条）⇒ 允许方法集（G2/P0）：
// 路径命中但方法不符 → 405 {"error":"method_not_allowed"}，不再落到静态/业务分发
// （旧实现只匹配路径，实测 PUT/DELETE /api/health、POST/PUT /login.html 匿名 200）。
// 方法集在 open 与 session 两态同构执行：白名单描述的是"该入口只提供这些语义"，
// 不是鉴权开关；open 模式放行 PUT /api/health 只会让 G2 在最常用的部署形态里继续存在。
// 其余 /api/*、/books/*、/data/*、页面与静态全部过 gate。
const ANON_PATHS = {
  '/login.html': { GET: 1, HEAD: 1 },
  '/api/auth/login': { POST: 1 },
  '/api/auth/logout': { POST: 1 },
  '/api/auth/status': { GET: 1, HEAD: 1 },
  '/api/health': { GET: 1, HEAD: 1 }
};

function isLoopbackHost(h) {
  const s = String(h || '').trim().toLowerCase();
  if (!s) return false;
  if (s === 'localhost' || s === '::1' || s === '[::1]' || s === '0:0:0:0:0:0:0:1') return true;
  if (s.indexOf('127.') === 0) return true;          // 127.0.0.0/8
  if (s.indexOf('::ffff:127.') === 0) return true;   // IPv4-mapped 回环
  return false;
}
const LOOPBACK = isLoopbackHost(HOST);

/* ---- G1（P0，DNS rebinding）：Host 白名单（启动时建表，运行期只查表） ----
 * 契约 v1 的 Origin/Referer 同源比对基准正是 Host 头本身（originMatches）。Host 不受校验时，
 * 攻击者把自己的域名 A 记录指到 127.0.0.1：浏览器视角属"同站"，SameSite=Strict 不挡 Cookie，
 * originMatches 又因 Origin 与 Host 同域而放过 ⇒ 全库可读、写接口可冒用（矩阵 G1 已实测 200）。
 * 允许集 = { localhost, 127.0.0.1, [::1] }
 *        ∪ { HOST 值 }（HOST ≠ 0.0.0.0 / :: 时）
 *        ∪ os.networkInterfaces() 全部 IPv4/IPv6 地址（HOST = 0.0.0.0 / :: 时，平板手机用局域网 IP 访问）
 *        ∪ ALLOW_EXTRA_HOSTS（逗号分隔，反代域名等自定义 Host 的显式逃生门）
 * 比对前必须剥端口：浏览器对 127.0.0.1:4590 发的 Host 就是 "127.0.0.1:4590"。
 * 不匹配 → 400 {"error":"bad_host"}，位置在 handler 最前（先于编码检查、匿名白名单、会话与 CSRF）。
 */
function hostNoPort(h) {
  const s = String(h || '').trim().toLowerCase();
  if (!s) return '';
  if (s.charAt(0) === '[') {              // IPv6 字面量：[::1] 或 [::1]:4590
    const rb = s.indexOf(']');
    return rb > 0 ? s.slice(0, rb + 1) : s;
  }
  const c = s.indexOf(':');
  if (c < 0) return s;                        // 无端口：127.0.0.1 / localhost / 域名
  if (s.indexOf(':', c + 1) >= 0) return s;   // 多个冒号 → 裸 IPv6 字面量，没有端口可剥
  return s.slice(0, c);                       // host:port → host
}

const ALLOWED_HOSTS = (function buildAllowedHosts() {
  const set = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);
  const h = String(HOST || '').trim().toLowerCase();
  if (h === '0.0.0.0' || h === '::' || h === '[::]') {
    let nets = {};
    try { nets = os.networkInterfaces() || {}; } catch (e) { nets = {}; }
    const names = Object.keys(nets);
    for (let i = 0; i < names.length; i++) {
      const list = nets[names[i]] || [];
      for (let j = 0; j < list.length; j++) {
        const a = String((list[j] && list[j].address) || '').trim().toLowerCase();
        if (!a) continue;
        if (a.indexOf(':') >= 0) { set.add(a); set.add('[' + a + ']'); } // IPv6 存双形态（带/不带方括号）
        else set.add(a);
      }
    }
  } else if (h) {
    set.add(hostNoPort(h));
  }
  const extra = String(process.env.ALLOW_EXTRA_HOSTS || '').split(',');
  for (let i = 0; i < extra.length; i++) {
    const e = hostNoPort(extra[i]);
    if (e) set.add(e);
  }
  return set;
})();

function hostAllowed(req) {
  const h = hostNoPort(req.headers.host);
  return h !== '' && ALLOWED_HOSTS.has(h);
}

/* ---- 会话表 ---- */
const sessions = new Map();
let lastSweepAt = Date.now();

function nowSec() {
  return Math.floor(Date.now() / 1000);
}
function sweepSessions() {
  const t = nowSec();
  for (const pair of sessions) if (pair[1].expires <= t) sessions.delete(pair[0]);
  lastSweepAt = Date.now();
}
function maybeSweep() {
  if (Date.now() - lastSweepAt > 3600000) sweepSessions(); // 每小时惰性清理
}
// unref：定时器不拖住进程退出（测试/正常关闭都不受影响）
const sweepTimer = setInterval(sweepSessions, 3600000);
if (sweepTimer && typeof sweepTimer.unref === 'function') sweepTimer.unref();

function randomHex(bytes) {
  return crypto.randomBytes(bytes).toString('hex');
}
function createSession() {
  const sid = randomHex(32); // hex64
  const csrf = randomHex(16); // hex32
  const rec = { csrf: csrf, expires: nowSec() + SESSION_TTL };
  sessions.set(sid, rec);
  if (sessions.size > 10000) sweepSessions();
  return { sid: sid, csrf: rec.csrf, expires: rec.expires };
}
function cookieValue(req, name) {
  const raw = req.headers.cookie;
  if (!raw || typeof raw !== 'string') return '';
  const parts = raw.split(';');
  for (let i = 0; i < parts.length; i++) {
    const kv = parts[i];
    const eq = kv.indexOf('=');
    if (eq < 0) continue;
    if (kv.slice(0, eq).trim() === name) return kv.slice(eq + 1).trim();
  }
  return '';
}
function getSession(req) {
  maybeSweep();
  const sid = cookieValue(req, SESSION_COOKIE);
  if (!sid || !SID_RE.test(sid)) return null;
  const rec = sessions.get(sid);
  if (!rec) return null;
  if (rec.expires <= nowSec()) {
    sessions.delete(sid); // 绝对过期：不滑动续期
    return null;
  }
  return rec;
}

/* ---- Cookie ---- */
function proxyWantsHttps(req) {
  if (!TRUST_PROXY) return false; // 未声明信任代理 → 完全忽略 X-Forwarded-*
  const v = String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim().toLowerCase();
  return v === 'https';
}
// 契约逐字：zhuxue_sid=<hex64>; HttpOnly; SameSite=Strict; Path=/; Max-Age=<ttl>[; Secure]
function sessionCookie(req, sid, maxAge) {
  let c = SESSION_COOKIE + '=' + sid + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=' + maxAge;
  if (proxyWantsHttps(req)) c += '; Secure';
  return c;
}

/* ---- 口令 / CSRF / Origin 比较 ---- */
// 恒定时间比较；长度不等直接 false（只泄露长度，属标准做法）
function secretEquals(a, b) {
  const A = Buffer.from(String(a == null ? '' : a), 'latin1');
  const B = Buffer.from(String(b == null ? '' : b), 'latin1');
  if (A.length !== B.length) return false;
  return crypto.timingSafeEqual(A, B);
}
function csrfMatches(req, rec) {
  const got = req.headers['x-csrf-token'];
  if (typeof got !== 'string' || !CSRF_RE.test(got)) return false;
  return secretEquals(got, rec.csrf);
}
// Origin/Referer 存在时必须与 Host 同 host:port；都不存在（curl / 同源脚本）放过。
function originMatches(req) {
  const src = req.headers.origin || req.headers.referer || '';
  if (!src) return true;
  const host = String(req.headers.host || '').toLowerCase();
  let u = null;
  try {
    u = new URL(String(src));
  } catch (e) {
    return false; // 无法解析的 Origin/Referer 一律视为非法
  }
  return !!host && u.host.toLowerCase() === host;
}

/* ---- 登录失败限流：同 IP 10 次/分钟，超 → 429 ---- */
const RL_MAX = 10;
const RL_WINDOW_MS = 60000;
const loginFails = new Map(); // ip -> { n, at }
function clientIp(req) {
  // 限流键只用连接层地址，不吃可伪造的 X-Forwarded-For
  return String((req.socket && req.socket.remoteAddress) || 'unknown');
}
function rlAllows(ip) {
  const rec = loginFails.get(ip);
  if (!rec) return true;
  if (Date.now() - rec.at > RL_WINDOW_MS) {
    loginFails.delete(ip);
    return true;
  }
  return rec.n < RL_MAX;
}
function rlFail(ip) {
  if (loginFails.size > 5000) {
    const t = Date.now();
    for (const pair of loginFails) if (t - pair[1].at > RL_WINDOW_MS) loginFails.delete(pair[0]);
  }
  const rec = loginFails.get(ip);
  if (!rec) loginFails.set(ip, { n: 1, at: Date.now() });
  else rec.n += 1;
}
function rlReset(ip) {
  loginFails.delete(ip);
}

/* ---- 认证三入口（匿名可达 + 免 CSRF） ---- */
function handleLogin(req, res) {
  const ip = clientIp(req);
  if (!rlAllows(ip)) return sendJSON(res, 429, { ok: false, error: 'rate_limited' });
  readBody(req, function (body, err) {
    if (err === 'TOO_LARGE') return sendJSON(res, 413, { ok: false, error: '请求体过大' });
    const got = body && typeof body.token === 'string' ? body.token : '';
    // open 模式没有可校验的口令（AUTH_TOKEN 为空）→ 一律 bad_token；不影响全部直通的 gate 行为
    if (!AUTH_TOKEN || !got || !secretEquals(got, AUTH_TOKEN)) {
      rlFail(ip);
      return sendJSON(res, 401, { ok: false, error: 'bad_token' });
    }
    rlReset(ip);
    const s = createSession();
    const payload = JSON.stringify({ ok: true, csrf: s.csrf, expires: s.expires });
    res.writeHead(200, {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'Content-Length': Buffer.byteLength(payload),
      'Set-Cookie': sessionCookie(req, s.sid, SESSION_TTL)
    });
    res.end(payload);
  });
}

function handleLogout(req, res) {
  const sid = cookieValue(req, SESSION_COOKIE);
  if (sid && SID_RE.test(sid)) sessions.delete(sid); // open 模式无会话 → 等效无操作，仍 200
  const payload = '{"ok":true}';
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(payload),
    'Set-Cookie': sessionCookie(req, '', 0)
  });
  res.end(payload);
}

function handleStatus(req, res) {
  const rec = AUTH_MODE === 'session' ? getSession(req) : null;
  const out = { authed: AUTH_MODE === 'open' ? true : !!rec, mode: AUTH_MODE };
  if (rec) out.csrf = rec.csrf; // 已认证才下发 csrf；任何情况下都不含书数据
  return sendJSON(res, 200, out);
}

// 返回 true 表示已由认证层应答
function authApi(req, res, pathname) {
  const m = String(req.method || '').toUpperCase();
  if (pathname === '/api/auth/login' && m === 'POST') {
    handleLogin(req, res);
    return true;
  }
  if (pathname === '/api/auth/logout' && m === 'POST') {
    handleLogout(req, res);
    return true;
  }
  if (pathname === '/api/auth/status' && m === 'GET') {
    handleStatus(req, res);
    return true;
  }
  return false;
}

/* ---- gate ---- */
function isNavigation(req) {
  const m = String(req.method || '').toUpperCase();
  return (m === 'GET' || m === 'HEAD') && /text\/html/i.test(String(req.headers.accept || ''));
}
// next 只取同源路径：必须以单个 / 开头（挡掉 //host、/\host），并拒绝控制字符与超长值；
// 输出前一律 encodeURIComponent，登录页用 URLSearchParams 取回。
function safeNext(pathname, search) {
  let cand = String(pathname || '') + String(search || '');
  if (cand === LOGIN_PATH || cand.indexOf(LOGIN_PATH) === 0) cand = '';
  if (cand.length > 2048) cand = '';
  if (cand && !(cand.charAt(0) === '/' && cand.charAt(1) !== '/' && cand.charAt(1) !== '\\')) cand = '';
  if (cand && /[\\\u0000-\u001f\u007f]/.test(cand)) cand = '';
  return cand;
}
function redirectLogin(req, res, pathname, search) {
  const next = safeNext(pathname, search);
  const loc = LOGIN_PATH + (next ? '?next=' + encodeURIComponent(next) : '');
  res.writeHead(302, { Location: loc, 'Cache-Control': 'no-store' });
  res.end();
}
/* ---- G8：前导斜杠归一（只在 gate 口径里用，一处算一次） ----
 * url.parse 会原样保留 origin-form 的 "//api/books"（pathname 仍带双斜杠），于是
 * "白名单查找 / api 分类 / 跳转分支"三条口径在同一资源的不同写法下抖动：
 * 实测 //api/books 未认证走页面分支 → 302，而契约要求未认证 /api/* → 401。
 * 归一规则：把开头连续的 / 与 \ 折成恰好一个 "/"，其余部分一字不改（不折叠内部斜杠、不解析点段）。
 * 重要：分发与静态文件解析仍用原 pathname（P0-01 现状零变化），所以别名 //api/books
 * 归一后被判为 api → 401/直通，但不会命中业务 API 分支，静态兜底仍 404，零数据泄露。
 */
function normPath(p) {
  const s = String(p || '');
  let i = 0;
  while (i < s.length && (s.charAt(i) === '/' || s.charAt(i) === '\\')) i++;
  if (i === 0) return '/' + s; // "*" 等非斜杠开头的请求目标：挂到根下，判定方向不变
  return i >= s.length ? '/' : '/' + s.slice(i);
}

// gate 顺序（稳定契约，测试 R19/R20 按此断言）：
//   0) Host 白名单（G1）已在 handler 最前执行，先于本函数
//   1) 白名单路径（G8 归一后查找）：方法在白名单集合内 → 放行；否则 405，不再落到静态/业务分发
//   2) open / AUTH 关闭：其余一律直通（现状行为）
//   3) session 未认证：归一后 /api/ 前缀 → 401 {"error":"unauthorized"}；HTML/GET 页 → 302 ?next=归一路径；其余 → 401
//   4) session 已认证 + 写方法（含 PATCH）：X-CSRF-Token + Origin/Referer 不符 → 403 {"error":"csrf"}
// 返回 false 表示已应答，调用方必须立即 return。
function gate(req, res, pathname, search, np) {
  const m = methodOf(req);
  const anon = ANON_PATHS[np]; // np 必以 "/" 开头，取不到 Object.prototype 上的键
  if (anon) {
    if (anon[m] === 1) return true;
    sendJSON(res, 405, { error: 'method_not_allowed' });
    return false;
  }
  if (AUTH_MODE !== 'session') return true; // open / AUTH 关闭：其余全部直通（现状行为）
  const rec = getSession(req);
  if (!rec) {
    if (isNavigation(req) && np.indexOf('/api/') !== 0) {
      redirectLogin(req, res, np, search); // G8：next 用归一路径，别名不再变成 next=%2F 的错位跳转
      return false;
    }
    sendJSON(res, 401, { error: 'unauthorized' });
    return false;
  }
  if (WRITE_METHODS[m]) {
    if (!csrfMatches(req, rec) || !originMatches(req)) {
      sendJSON(res, 403, { error: 'csrf' });
      return false;
    }
  }
  return true;
}

/* ---------------- 路由 ---------------- */
function api(req, res, pathname, query) {
  if (pathname === '/api/books' && req.method === 'GET') {
    return sendJSON(res, 200, { ok: true, data: { books: listBooks() } });
  }
  const mBook = pathname.match(/^\/api\/books\/([^/]+)$/);
  if (mBook && req.method === 'GET') {
    const bArg = safeDecode(mBook[1]);
    if (bArg === null) return sendJSON(res, 400, { ok: false, error: '路径编码非法' });
    if (bArg.length > MAX_ID_LEN) return sendJSON(res, 400, { ok: false, error: 'bookId 过长' });
    const book = getBook(bArg);
    if (!book) return sendJSON(res, 404, { ok: false, error: '书不存在' });
    return sendJSON(res, 200, { ok: true, data: book });
  }
  if (pathname === '/api/progress' && req.method === 'GET') {
    return sendJSON(res, 200, { ok: true, data: loadProgress() });
  }
  if (pathname === '/api/progress' && req.method === 'PUT') {
    readBody(req, (body, err) => {
      if (err === 'TOO_LARGE') return sendJSON(res, 413, { ok: false, error: '请求体过大' });
      if (!body) return sendJSON(res, 400, { ok: false, error: '请求体不是合法 JSON' });
      const progress = loadProgress();
      const { bookId, chapterId, page, total } = body;
      if (!validId(bookId)) return sendJSON(res, 400, { ok: false, error: 'bookId 非法' });
      if (chapterId != null && (typeof chapterId !== 'string' || chapterId.length > MAX_SHORT)) {
        return sendJSON(res, 400, { ok: false, error: 'chapterId 非法' });
      }
      const p = numInRange(page == null ? 0 : page, 0, MAX_PAGE);
      const t = numInRange(total == null ? 0 : total, 0, MAX_PAGE);
      if (p === null || t === null) return sendJSON(res, 400, { ok: false, error: 'page/total 超出范围' });
      progress.books[bookId] = {
        chapterId: chapterId || null,
        page: p,
        total: t,
        updatedAt: Date.now()
      };
      saveProgress(progress);
      return sendJSON(res, 200, { ok: true, data: progress.books[bookId] });
    });
    return;
  }
  if (pathname === '/api/bookmarks' && req.method === 'GET') {
    const bookId = String((query && query.bookId) || '');
    if (bookId.length > MAX_ID_LEN || /[\\/\0]/.test(bookId)) {
      return sendJSON(res, 400, { ok: false, error: 'bookId 非法' });
    }
    const db = loadBookmarks();
    const items = db.items
      .filter((x) => !bookId || x.bookId === bookId)
      .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    return sendJSON(res, 200, { ok: true, data: { items } });
  }
  if (pathname === '/api/bookmarks' && req.method === 'POST') {
    readBody(req, (body, err) => {
      if (err === 'TOO_LARGE') return sendJSON(res, 413, { ok: false, error: '请求体过大' });
      if (!body) return sendJSON(res, 400, { ok: false, error: '请求体不是合法 JSON' });
      if (!validId(body.bookId)) return sendJSON(res, 400, { ok: false, error: 'bookId 非法' });
      const bookId = String(body.bookId);
      const page = numInRange(body.page, 1, MAX_PAGE);
      if (page === null) return sendJSON(res, 400, { ok: false, error: 'page 超出范围' });
      const chapter = body.chapter == null ? '' : String(body.chapter);
      if (chapter.length > MAX_SHORT) return sendJSON(res, 400, { ok: false, error: 'chapter 过长' });
      const db = loadBookmarks();
      const dup = db.items.find((x) => x.bookId === bookId && x.page === page);
      if (dup) return sendJSON(res, 200, { ok: true, data: { item: dup, existed: true } });
      const item = {
        id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
        bookId,
        page,
        chapter,
        excerpt: String(body.excerpt || '').slice(0, 120),
        createdAt: Date.now()
      };
      db.items.push(item);
      if (db.items.length > 2000) db.items = db.items.slice(-2000);
      saveBookmarks(db);
      return sendJSON(res, 200, { ok: true, data: { item } });
    });
    return;
  }
  const mBm = pathname.match(/^\/api\/bookmarks\/([^/]+)$/);
  if (mBm && req.method === 'DELETE') {
    const id = safeDecode(mBm[1]);
    if (id === null) return sendJSON(res, 400, { ok: false, error: '路径编码非法' });
    if (id.length > MAX_ID_LEN) return sendJSON(res, 400, { ok: false, error: 'id 过长' });
    const db = loadBookmarks();
    const before = db.items.length;
    db.items = db.items.filter((x) => x.id !== id);
    if (db.items.length === before) return sendJSON(res, 404, { ok: false, error: '书签不存在' });
    saveBookmarks(db);
    return sendJSON(res, 200, { ok: true, data: { deleted: id } });
  }
  /* ---------- 题库 ---------- */
  if (pathname === '/api/questions' && req.method === 'GET') {
    return sendJSON(res, 200, { ok: true, data: { items: questionIndex() } });
  }
  const mQ = pathname.match(/^\/api\/questions\/([^/]+)$/);
  if (mQ && req.method === 'GET') {
    const qArg = safeDecode(mQ[1]);
    if (qArg === null) return sendJSON(res, 400, { ok: false, error: '路径编码非法' });
    if (qArg.length > MAX_ID_LEN) return sendJSON(res, 400, { ok: false, error: 'bookId 过长' });
    const bank = loadBank(qArg);
    if (!bank) return sendJSON(res, 404, { ok: false, error: '题库不存在' });
    return sendJSON(res, 200, { ok: true, data: bank });
  }
  if (pathname === '/api/practice' && req.method === 'GET') {
    return sendJSON(res, 200, { ok: true, data: loadPractice() });
  }
  if (pathname === '/api/practice' && req.method === 'POST') {
    readBody(req, (body, err) => {
      if (err === 'TOO_LARGE') return sendJSON(res, 413, { ok: false, error: '请求体过大' });
      if (!body) return sendJSON(res, 400, { ok: false, error: '请求体不是合法 JSON' });
      if (!validId(body.bookId) || !validId(body.questionId)) {
        return sendJSON(res, 400, { ok: false, error: 'bookId/questionId 非法' });
      }
      const bookId = String(body.bookId);
      const questionId = String(body.questionId);
      const type = body.type == null ? '' : String(body.type);
      if (type.length > MAX_SHORT) return sendJSON(res, 400, { ok: false, error: 'type 过长' });
      const db = loadPractice();
      db.books[bookId] = db.books[bookId] || { answered: {} };
      db.books[bookId].answered[questionId] = {
        correct: !!body.correct,
        chosen: body.chosen == null ? '' : String(body.chosen).slice(0, 200),
        type,
        at: Date.now()
      };
      db.books[bookId].updatedAt = Date.now();
      savePractice(db);
      return sendJSON(res, 200, { ok: true, data: db.books[bookId].answered[questionId] });
    });
    return;
  }
  const mP = pathname.match(/^\/api\/practice\/([^/]+)$/);
  if (mP && req.method === 'DELETE') {
    const qid = safeDecode(mP[1]);
    if (qid === null) return sendJSON(res, 400, { ok: false, error: '路径编码非法' });
    if (qid.length > MAX_ID_LEN) return sendJSON(res, 400, { ok: false, error: 'questionId 过长' });
    const onlyBook = String((query && query.bookId) || '');
    if (onlyBook.length > MAX_ID_LEN || /[\\/\0]/.test(onlyBook)) {
      return sendJSON(res, 400, { ok: false, error: 'bookId 非法' });
    }
    const db = loadPractice();
    const targets = onlyBook ? [onlyBook] : Object.keys(db.books);
    let hit = 0;
    for (const b of targets) {
      if (db.books[b] && db.books[b].answered && db.books[b].answered[qid]) {
        delete db.books[b].answered[qid];
        hit++;
      }
    }
    if (!hit) return sendJSON(res, 404, { ok: false, error: '记录不存在' });
    savePractice(db);
    return sendJSON(res, 200, { ok: true, data: { deleted: qid } });
  }

  // 匿名白名单：保持最小体（认证契约 v1）——只报模式，不回任何书数据
  if (pathname === '/api/health') {
    return sendJSON(res, 200, { ok: true, mode: AUTH_MODE });
  }
  return sendJSON(res, 404, { ok: false, error: '接口不存在' });
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webp': 'image/webp',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2'
};

function serveFile(res, file) {
  fs.readFile(file, (err, buf) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Not found');
      return;
    }
    const ext = path.extname(file).toLowerCase();
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Cache-Control': ext === '.html' ? 'no-cache' : 'public, max-age=600'
    });
    res.end(buf);
  });
}

const server = http.createServer((req, res) => {
  // G1（P0）：Host 白名单最先——它同时是 Origin/Referer 同源比对的基准，所以必须
  // 先于编码检查、匿名白名单、会话判断与 CSRF（否则 rebinding 还能打 /api/auth/login 口令爆破）。
  if (!hostAllowed(req)) return sendJSON(res, 400, { error: 'bad_host' });
  const parsed = url.parse(req.url, true);
  let pathname;
  try {
    pathname = decodeURIComponent(parsed.pathname || '/');
  } catch (e) {
    // 非法 % 编码：不抛给顶层，直接 400
    return sendJSON(res, 400, { ok: false, error: '请求路径编码非法' });
  }
  const qSearch = parsed.search || '';
  // G8：gate 的白名单查找 / api 分类 / 跳转分支统一用归一路径；分发仍用 pathname
  const np = normPath(pathname);
  if (pathname.startsWith('/api/')) {
    // R4：解码后仍含空字节一律拒
    if (pathname.indexOf('\0') !== -1) {
      return sendJSON(res, 400, { ok: false, error: '路径含非法字符' });
    }
    // 认证三入口（匿名可达、免 CSRF）：/api/auth/login|logout|status
    if (authApi(req, res, pathname)) return;
    // 统一 gate + CSRF（替代旧 R6 的「非 GET 要 Bearer」直通）
    if (!gate(req, res, pathname, qSearch, np)) return;
    try {
      return api(req, res, pathname, parsed.query || {});
    } catch (e) {
      console.error('[api]', e);
      return sendJSON(res, 500, { ok: false, error: '服务器错误' });
    }
  }
  if (pathname.indexOf('\0') !== -1) {
    return sendJSON(res, 400, { ok: false, error: '路径含非法字符' });
  }
  // 页面 / 静态 / /books 资产同样过 gate（open 模式直通，行为与现状一致）
  if (!gate(req, res, pathname, qSearch, np)) return;
  // G2（P0）：静态层（非 /api 路由，含 /books 资产）统一只接 GET|HEAD，其余 405。
  // 位置在 gate 之后，所以未认证仍是 401/302（鉴权优先，不向未登录者泄露资源存在性），
  // 已认证带 CSRF 也是 405（页面与资产本来就不可写）；open 模式同样 405 ⇒ 两态同构。
  if (!READ_METHODS[methodOf(req)]) {
    return sendJSON(res, 405, { error: 'method_not_allowed' });
  }
  // 书籍资源
  const mAsset = pathname.match(/^\/books\/([^/]+)\/(.+)$/);
  if (mAsset) {
    // P0-01：bookId 段可能是 '..'，mAsset[2] 也可能含 ../ → 边界校验后再服务
    const file = resolveInside(BOOKS, path.basename(mAsset[1]), mAsset[2]);
    if (!file) return res.writeHead(403).end('Forbidden');
    return serveFile(res, file);
  }
  // 静态资源：rel 必须以相对形式交给 resolveInside（绝对路径段会重置到盘根）
  const rel0 = pathname === '/' ? 'index.html' : pathname.replace(/^[\\/]+/, '');
  // G7：复习笔记目录 URL 重写（仅静态层一处小改）。rel0 已剥掉前导斜杠，
  // 常量替换不引入任何用户可控片段 ⇒ 不影响 P0-01 边界，也不新增 gate 可达面。
  const rel = rel0 === 'review' || rel0 === 'review/' ? 'review/index.html' : rel0;
  const file = resolveInside(PUBLIC, rel);
  if (!file) {
    res.writeHead(403).end('Forbidden');
    return;
  }
  serveFile(res, file);
});

ensureDir(DATA);
ensureDir(BOOKS);

/* ---------------- 启动守卫（认证契约 v1 ＝ 测试 R16） ----------------
 * AUTH_TOKEN 为空且绑定非回环地址 → 拒绝启动：非回环等于把全库与写入口交给同网段任何设备。
 * 退出码非 0（78 = EX_CONFIG），并打印口令生成方法。
 */
if (!AUTH_TOKEN && !LOOPBACK) {
  console.error('[拒绝启动] HOST=' + HOST + ' 不是回环地址，且 AUTH_TOKEN 未设置。');
  console.error('  认证契约 v1：非回环绑定必须启用会话认证（AUTH_TOKEN 非空 → mode=session）。');
  console.error('  生成一次性口令（32 位十六进制）：');
  console.error('    node -e "console.log(require(\'crypto\').randomBytes(16).toString(\'hex\'))"');
  console.error('  Windows: set AUTH_TOKEN=<口令> && node server.js');
  console.error('  Linux/Mac: AUTH_TOKEN=<口令> node server.js');
  console.error('  只在本机使用可保持不设口令：HOST=127.0.0.1（open 兼容模式，全部直通）。');
  process.exit(78);
}

/* ---------------- 启动守卫 2（G10/P3 → 本轮收口）：AUTH_ENFORCE=1 且 AUTH_TOKEN 空 ----------------
 * 该组合下 mode=session 却没有任何可用口令（handleLogin 恒判 bad_token）＝"强制登录但谁都登不进"的自锁态。
 * 契约给了两个候选：降级 open（＝静默失去鉴权，更危险，否决）或启动即拒绝。这里选后者：
 * 退出码 79（EX_ERROR）+ 明确提示必须设 AUTH_TOKEN，杜绝"看起来起来了、实际没人能用"。
 * 位置在守卫 1 之后：HOST=0.0.0.0 + 空 token 仍先按 R16 报 78（保持既有语义与测试）。
 */
if (AUTH_ENFORCE && !AUTH_TOKEN) {
  console.error('[拒绝启动] AUTH_ENFORCE=1 但 AUTH_TOKEN 为空：已强制登录却没有任何可用口令，谁都登不进。');
  console.error('  必须设置 AUTH_TOKEN 后再启动（AUTH_ENFORCE=1 时不提供 open 直通）。');
  console.error('  生成一次性口令（32 位十六进制）：');
  console.error('    node -e "console.log(require(\'crypto\').randomBytes(16).toString(\'hex\'))"');
  console.error('  Windows: set AUTH_TOKEN=<口令> && node server.js');
  console.error('  Linux/Mac: AUTH_TOKEN=<口令> node server.js');
  process.exit(79);
}

server.listen(PORT, HOST, () => {
  console.log('┌───────────────────────────────────────────┐');
  console.log('│  筑学 · 本地书库                          │');
  console.log('│  电脑访问：http://127.0.0.1:' + PORT + '                │');
  console.log('└───────────────────────────────────────────┘');
  if (!LOOPBACK) {
    const nets = os.networkInterfaces();
    for (const name of Object.keys(nets)) {
      for (const ni of nets[name] || []) {
        if (ni.family === 'IPv4' && !ni.internal) {
          console.log('  平板/手机访问：http://' + ni.address + ':' + PORT + '  （同一 Wi-Fi）');
        }
      }
    }
  }
  if (AUTH_MODE === 'session') {
    console.log('  认证：mode=session（AUTH_TOKEN 已启用）。未登录：/api/* → 401，页面 → 302 /login.html。');
    console.log('  会话：Cookie zhuxue_sid（HttpOnly; SameSite=Strict）绝对过期 ' + SESSION_TTL + ' 秒；服务重启即全部失效，需重新登录。');
    // G10：AUTH_ENFORCE=1 + 空 token 已在启动守卫 2 里退出（79），此处不再打"谁都登不进"的告警
    if (TRUST_PROXY) {
      console.log('  ⚠ TRUST_PROXY=1：已信任 X-Forwarded-Proto（仅当服务前置可信反代时才可开启）。');
    }
  } else {
    console.log('  ⚠ 未设置 AUTH_TOKEN 且绑定回环地址：open 兼容模式（全部直通，与 v1.0.1 行为一致）。');
  }
  console.log('  Host 白名单（G1）：' + ALLOWED_HOSTS.size + ' 项（localhost/127.0.0.1/[::1]'
    + (HOST === '0.0.0.0' || HOST === '::' ? ' + 本机网卡全部地址' : ' + HOST=' + HOST)
    + (process.env.ALLOW_EXTRA_HOSTS ? ' + ALLOW_EXTRA_HOSTS' : '')
    + '），不匹配的 Host → 400 bad_host。');
  console.log('书库目录：' + BOOKS);
});
