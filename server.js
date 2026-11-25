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

const ROOT = process.env.BOOKROOT ? path.resolve(process.env.BOOKROOT) : __dirname;
const PUBLIC = path.join(ROOT, 'public');
const BOOKS = path.join(ROOT, 'books');
const DATA = path.join(ROOT, 'data');
const QBANK = path.join(ROOT, '题库');
const PORT = Number(process.env.PORT || 4590);
const HOST = process.env.HOST || '127.0.0.1';
// R6：设置 AUTH_TOKEN 后，非 GET /api/* 写接口要求 Authorization: Bearer <token>。
// 默认不启用（本机/局域网现有前端不带 token，行为与 v1.0.1 一致）。
const AUTH_TOKEN = process.env.AUTH_TOKEN || '';

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
  const file = path.join(BOOKS, safe, 'book.json');
  if (!file.startsWith(BOOKS) || !fs.existsSync(file)) return null;
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
  if (!hit || !hit.file.startsWith(QBANK)) return null;
  return readJSON(hit.file, null);
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

  if (pathname === '/api/health') {
    return sendJSON(res, 200, { ok: true, data: { books: bookDirs().length } });
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
  const parsed = url.parse(req.url, true);
  let pathname;
  try {
    pathname = decodeURIComponent(parsed.pathname || '/');
  } catch (e) {
    // 非法 % 编码：不抛给顶层，直接 400
    return sendJSON(res, 400, { ok: false, error: '请求路径编码非法' });
  }
  if (pathname.startsWith('/api/')) {
    // R4：解码后仍含空字节一律拒
    if (pathname.indexOf('\0') !== -1) {
      return sendJSON(res, 400, { ok: false, error: '路径含非法字符' });
    }
    // R6：启用 AUTH_TOKEN 后写接口必须带 Bearer
    if (AUTH_TOKEN && req.method !== 'GET' && req.headers.authorization !== 'Bearer ' + AUTH_TOKEN) {
      return sendJSON(res, 401, { ok: false, error: '未授权' });
    }
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
  // 书籍资源
  const mAsset = pathname.match(/^\/books\/([^/]+)\/(.+)$/);
  if (mAsset) {
    const file = path.join(BOOKS, path.basename(mAsset[1]), path.normalize(mAsset[2]));
    if (file.startsWith(BOOKS)) return serveFile(res, file);
    return res.writeHead(403).end('Forbidden');
  }
  const rel = pathname === '/' ? '/index.html' : pathname;
  const file = path.join(PUBLIC, path.normalize(rel));
  if (!file.startsWith(PUBLIC)) {
    res.writeHead(403).end('Forbidden');
    return;
  }
  serveFile(res, file);
});

ensureDir(DATA);
ensureDir(BOOKS);
server.listen(PORT, HOST, () => {
  console.log('┌───────────────────────────────────────────┐');
  console.log('│  筑学 · 本地书库                          │');
  console.log('│  电脑访问：http://127.0.0.1:' + PORT + '                │');
  console.log('└───────────────────────────────────────────┘');
  if (HOST !== '127.0.0.1') {
    const nets = os.networkInterfaces();
    for (const name of Object.keys(nets)) {
      for (const ni of nets[name] || []) {
        if (ni.family === 'IPv4' && !ni.internal) {
          console.log('  平板/手机访问：http://' + ni.address + ':' + PORT + '  （同一 Wi-Fi）');
        }
      }
    }
    if (!AUTH_TOKEN) {
      console.log('  ⚠ 局域网模式未启用认证：同 Wi-Fi 设备可读写进度/书签/作答。');
      console.log('    启用：set AUTH_TOKEN=<口令>（写接口需 Authorization: Bearer；前端暂未带 token，启用后前端写入会被 401 拒绝，属预期）。');
    }
  }
  console.log('书库目录：' + BOOKS);
});
