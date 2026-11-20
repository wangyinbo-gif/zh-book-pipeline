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

const ROOT = __dirname;
const PUBLIC = path.join(ROOT, 'public');
const BOOKS = path.join(ROOT, 'books');
const DATA = path.join(ROOT, 'data');
const QBANK = path.join(ROOT, '题库');
const PORT = Number(process.env.PORT || 4590);
const HOST = process.env.HOST || '127.0.0.1';

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
function readBody(req, cb) {
  let raw = '';
  req.on('data', (c) => (raw += c));
  req.on('end', () => {
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
    const book = getBook(decodeURIComponent(mBook[1]));
    if (!book) return sendJSON(res, 404, { ok: false, error: '书不存在' });
    return sendJSON(res, 200, { ok: true, data: book });
  }
  if (pathname === '/api/progress' && req.method === 'GET') {
    return sendJSON(res, 200, { ok: true, data: loadProgress() });
  }
  if (pathname === '/api/progress' && req.method === 'PUT') {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      let body = {};
      try {
        body = JSON.parse(raw || '{}');
      } catch (e) {
        return sendJSON(res, 400, { ok: false, error: '请求体不是合法 JSON' });
      }
      const progress = loadProgress();
      const { bookId, chapterId, page, total } = body;
      if (!bookId) return sendJSON(res, 400, { ok: false, error: '缺少 bookId' });
      progress.books[bookId] = {
        chapterId: chapterId || null,
        page: Number(page) || 0,
        total: Number(total) || 0,
        updatedAt: Date.now()
      };
      saveProgress(progress);
      return sendJSON(res, 200, { ok: true, data: progress.books[bookId] });
    });
    return;
  }
  if (pathname === '/api/bookmarks' && req.method === 'GET') {
    const bookId = String((query && query.bookId) || '');
    const db = loadBookmarks();
    const items = db.items
      .filter((x) => !bookId || x.bookId === bookId)
      .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    return sendJSON(res, 200, { ok: true, data: { items } });
  }
  if (pathname === '/api/bookmarks' && req.method === 'POST') {
    readBody(req, (body) => {
      if (!body) return sendJSON(res, 400, { ok: false, error: '请求体不是合法 JSON' });
      const bookId = String(body.bookId || '');
      const page = Number(body.page) || 0;
      if (!bookId || page <= 0) return sendJSON(res, 400, { ok: false, error: '缺少 bookId 或 page' });
      const db = loadBookmarks();
      const dup = db.items.find((x) => x.bookId === bookId && x.page === page);
      if (dup) return sendJSON(res, 200, { ok: true, data: { item: dup, existed: true } });
      const item = {
        id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
        bookId,
        page,
        chapter: String(body.chapter || ''),
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
    const id = decodeURIComponent(mBm[1]);
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
    const bank = loadBank(decodeURIComponent(mQ[1]));
    if (!bank) return sendJSON(res, 404, { ok: false, error: '题库不存在' });
    return sendJSON(res, 200, { ok: true, data: bank });
  }
  if (pathname === '/api/practice' && req.method === 'GET') {
    return sendJSON(res, 200, { ok: true, data: loadPractice() });
  }
  if (pathname === '/api/practice' && req.method === 'POST') {
    readBody(req, (body) => {
      if (!body) return sendJSON(res, 400, { ok: false, error: '请求体不是合法 JSON' });
      const bookId = String(body.bookId || '');
      const questionId = String(body.questionId || '');
      if (!bookId || !questionId) {
        return sendJSON(res, 400, { ok: false, error: '缺少 bookId 或 questionId' });
      }
      const db = loadPractice();
      db.books[bookId] = db.books[bookId] || { answered: {} };
      db.books[bookId].answered[questionId] = {
        correct: !!body.correct,
        chosen: body.chosen == null ? '' : String(body.chosen).slice(0, 200),
        type: String(body.type || ''),
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
    const qid = decodeURIComponent(mP[1]);
    const onlyBook = String((query && query.bookId) || '');
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
  const pathname = decodeURIComponent(parsed.pathname || '/');
  if (pathname.startsWith('/api/')) {
    try {
      return api(req, res, pathname, parsed.query || {});
    } catch (e) {
      console.error('[api]', e);
      return sendJSON(res, 500, { ok: false, error: e.message || '服务器错误' });
    }
  }
  // 书籍资源
  const mAsset = pathname.match(/^\/books\/([^/]+)\/(.+)$/);
  if (mAsset) {
    const file = path.join(BOOKS, path.basename(mAsset[1]), mAsset[2]);
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
  }
  console.log('书库目录：' + BOOKS);
});
