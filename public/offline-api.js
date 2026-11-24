/* 筑学 · 离线接口层
 * 在 file:// 下替代 server.js 的 /api/*：数据来自页面内嵌的 window.__OFFLINE__，
 * 进度/书签/作答存 localStorage（不可用时降级为内存并提示）。
 * 由 export_offline.py 生成；服务器模式不加载本文件。
 */
(function () {
  'use strict';
  var DATA = window.__OFFLINE__ || {};
  var ROUTES = window.__BOOK_ROUTES__ || {};
  var QB_INDEX = window.__QBANK_INDEX__ || [];
  var SRC_BOOKS = window.__SRC_BOOKS__ || {};   // 刷题页内嵌的正文块（供出处核验）
  var KEYS = {
    progress: 'zhuxue-offline-progress',
    bookmarks: 'zhuxue-offline-bookmarks',
    practice: 'zhuxue-offline-practice'
  };

  /* ---------- 存储（不可用时降级） ---------- */
  function memoryStore() {
    var m = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null; },
      setItem: function (k, v) { m[k] = String(v); },
      removeItem: function (k) { delete m[k]; }
    };
  }
  var storage;
  var storageOk = false;
  try {
    var probe = '__zhuxue_probe__';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    storage = window.localStorage;
    storageOk = true;
  } catch (e) {
    storage = memoryStore();
  }
  window.__OFFLINE_STORAGE_OK__ = storageOk;

  function readJSON(key, fallback) {
    try {
      var raw = storage.getItem(key);
      if (!raw) return fallback;
      var v = JSON.parse(raw);
      return v == null ? fallback : v;
    } catch (e) { return fallback; }
  }
  function writeJSON(key, val) {
    try { storage.setItem(key, JSON.stringify(val)); return true; }
    catch (e) { return false; }
  }

  /* ---------- 返回体（与 server.js 同构） ---------- */
  function reply(data, status) {
    status = status || 200;
    var body = { ok: status >= 200 && status < 300, data: data };
    return Promise.resolve({
      ok: body.ok, status: status,
      json: function () { return Promise.resolve(body); },
      text: function () { return Promise.resolve(JSON.stringify(body)); }
    });
  }
  function fail(error, status) {
    status = status || 404;
    var body = { ok: false, error: error };
    return Promise.resolve({
      ok: false, status: status,
      json: function () { return Promise.resolve(body); },
      text: function () { return Promise.resolve(JSON.stringify(body)); }
    });
  }

  /* ---------- 导航钩子（供 shelf/reader/practice 复用） ---------- */
  function routeOf(id) { return ROUTES[id] || null; }
  window.__ASSET_DIR__ = function (id) {
    var r = routeOf(id);
    return 'books/' + (r && r.hash ? r.hash : 'unknown') + '/';
  };
  window.__READER_HREF__ = function (id, page, block) {
    var r = routeOf(id);
    var base = (r && r.reader) ? r.reader : 'reader.html';
    var q = ['book=' + encodeURIComponent(id)];
    if (page != null) q.push('page=' + encodeURIComponent(page));
    if (block != null) q.push('block=' + encodeURIComponent(block));
    return base + '?' + q.join('&');
  };
  window.__PRACTICE_HREF__ = function (id) {
    var r = routeOf(id);
    return (r && r.practice) ? (r.practice + '?book=' + encodeURIComponent(id)) : '';
  };

  /* ---------- 进度 ---------- */
  function loadProgress() {
    var db = readJSON(KEYS.progress, null);
    if (!db || typeof db !== 'object' || !db.books) db = { version: 1, books: {} };
    return db;
  }
  function loadBookmarks() {
    var db = readJSON(KEYS.bookmarks, null);
    if (!db || !Array.isArray(db.items)) db = { version: 1, items: [] };
    return db;
  }
  function loadPractice() {
    var db = readJSON(KEYS.practice, null);
    if (!db || typeof db !== 'object' || !db.books) db = { version: 1, books: {} };
    return db;
  }

  /* ---------- 路由 ---------- */
  function route(method, pathname, query, body) {
    method = (method || 'GET').toUpperCase();
    var m;

    if (pathname === '/api/books' && method === 'GET') {
      return reply({ books: DATA.books || window.__BOOKS__ || [] });
    }
    m = pathname.match(/^\/api\/books\/([^/]+)$/);
    if (m && method === 'GET') {
      if (DATA.book && decodeURIComponent(m[1]) === (DATA.bookId || (DATA.book && DATA.book.id))) {
        return reply(DATA.book);
      }
      // 刷题页的出处核验要正文块：practice.html 里内嵌了本题库所属书的 blocks
      var sb = SRC_BOOKS[decodeURIComponent(m[1])];
      if (sb && sb.blocks) {
        return reply({ data: { id: sb.bookId, title: sb.title, blocks: sb.blocks } });
      }
      return fail('书不存在');
    }
    if (pathname === '/api/progress' && method === 'GET') {
      return reply(loadProgress());
    }
    if (pathname === '/api/progress' && method === 'PUT') {
      var db = loadProgress();
      var b = body || {};
      if (!b.bookId) return fail('缺少 bookId', 400);
      db.books[b.bookId] = {
        chapterId: b.chapterId || null,
        page: Number(b.page) || 0,
        total: Number(b.total) || 0,
        updatedAt: Date.now()
      };
      writeJSON(KEYS.progress, db);
      return reply(db.books[b.bookId]);
    }
    if (pathname === '/api/bookmarks' && method === 'GET') {
      var bookId = String(query.bookId || '');
      var items = loadBookmarks().items
        .filter(function (x) { return !bookId || x.bookId === bookId; })
        .sort(function (a, b) { return (b.createdAt || 0) - (a.createdAt || 0); });
      return reply({ items: items });
    }
    if (pathname === '/api/bookmarks' && method === 'POST') {
      var bm = body || {};
      var page = Number(bm.page) || 0;
      if (!bm.bookId || page <= 0) return fail('缺少 bookId 或 page', 400);
      var bdb = loadBookmarks();
      var dup = bdb.items.find(function (x) { return x.bookId === bm.bookId && x.page === page; });
      if (dup) return reply({ item: dup, existed: true });
      var item = {
        id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
        bookId: String(bm.bookId),
        page: page,
        chapter: String(bm.chapter || ''),
        excerpt: String(bm.excerpt || '').slice(0, 120),
        createdAt: Date.now()
      };
      bdb.items.push(item);
      if (bdb.items.length > 2000) bdb.items = bdb.items.slice(-2000);
      writeJSON(KEYS.bookmarks, bdb);
      return reply({ item: item });
    }
    m = pathname.match(/^\/api\/bookmarks\/([^/]+)$/);
    if (m && method === 'DELETE') {
      var bid = decodeURIComponent(m[1]);
      var ddb = loadBookmarks();
      var before = ddb.items.length;
      ddb.items = ddb.items.filter(function (x) { return x.id !== bid; });
      if (ddb.items.length === before) return fail('书签不存在');
      writeJSON(KEYS.bookmarks, ddb);
      return reply({ deleted: bid });
    }
    if (pathname === '/api/questions' && method === 'GET') {
      return reply({ items: QB_INDEX });
    }
    m = pathname.match(/^\/api\/questions\/([^/]+)$/);
    if (m && method === 'GET') {
      if (DATA.bank) return reply(DATA.bank);
      return fail('题库不存在');
    }
    if (pathname === '/api/practice' && method === 'GET') {
      return reply(loadPractice());
    }
    if (pathname === '/api/practice' && method === 'POST') {
      var p = body || {};
      if (!p.bookId || !p.questionId) return fail('缺少 bookId 或 questionId', 400);
      var pdb = loadPractice();
      pdb.books[p.bookId] = pdb.books[p.bookId] || { answered: {} };
      pdb.books[p.bookId].answered[p.questionId] = {
        correct: !!p.correct,
        chosen: p.chosen == null ? '' : String(p.chosen).slice(0, 200),
        type: String(p.type || ''),
        at: Date.now()
      };
      pdb.books[p.bookId].updatedAt = Date.now();
      writeJSON(KEYS.practice, pdb);
      return reply(pdb.books[p.bookId].answered[p.questionId]);
    }
    m = pathname.match(/^\/api\/practice\/([^/]+)$/);
    if (m && method === 'DELETE') {
      var qid = decodeURIComponent(m[1]);
      var onlyBook = String(query.bookId || '');
      var pdb2 = loadPractice();
      var targets = onlyBook ? [onlyBook] : Object.keys(pdb2.books);
      var hit = 0;
      targets.forEach(function (b) {
        var rec = pdb2.books[b];
        if (rec && rec.answered && rec.answered[qid]) { delete rec.answered[qid]; hit++; }
      });
      if (!hit) return fail('记录不存在');
      writeJSON(KEYS.practice, pdb2);
      return reply({ deleted: qid });
    }
    if (pathname === '/api/health' && method === 'GET') {
      return reply({ books: (DATA.books || window.__BOOKS__ || []).length });
    }
    return fail('接口不存在');
  }

  /* ---------- fetch 接管 ---------- */
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = (init && init.method) || (input && input.method) || 'GET';
    if (url.indexOf('/api/') !== 0) {
      if (origFetch) return origFetch.call(window, input, init);
      return fail('离线模式：仅支持 /api/ 接口', 0);
    }
    var qi = url.indexOf('?');
    var pathname = qi < 0 ? url : url.slice(0, qi);
    var query = {};
    if (qi >= 0) {
      url.slice(qi + 1).split('&').forEach(function (kv) {
        if (!kv) return;
        var i = kv.indexOf('=');
        var k = i < 0 ? kv : kv.slice(0, i);
        var v = i < 0 ? '' : kv.slice(i + 1);
        query[decodeURIComponent(k)] = decodeURIComponent(v);
      });
    }
    var body = null;
    if (init && typeof init.body === 'string' && init.body) {
      try { body = JSON.parse(init.body); } catch (e) { body = null; }
    }
    try {
      return route(method, pathname, query, body);
    } catch (e) {
      return fail('离线接口异常：' + (e && e.message ? e.message : e), 500);
    }
  };

  /* ---------- 存储不可用提示 ---------- */
  if (!storageOk) {
    var showWarn = function () {
      var d = document.createElement('div');
      d.textContent = '当前浏览器不允许本地存储：阅读进度、书签、刷题记录本次有效，关闭后会丢失。建议改用 Chrome / Edge 打开。';
      d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:9999;background:#8a6d3b;color:#fff;font:13px/1.5 sans-serif;padding:8px 14px;text-align:center;';
      document.body.appendChild(d);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', showWarn);
    else showWarn();
  }
})();
