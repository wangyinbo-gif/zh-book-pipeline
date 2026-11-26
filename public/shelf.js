/* 筑学 · 书架（分类 + 快速选择） */
(function () {
  var shelf = document.getElementById('shelf');
  var count = document.getElementById('count');
  var tabs = document.getElementById('filterTabs');
  var jump = document.getElementById('quickJump');
  var nAll = document.getElementById('nAll');
  var nText = document.getElementById('nText');
  var nExt = document.getElementById('nExt');

  /* 统一出口：有 auth-client.js 就走 zhuxueFetch（session 模式自动补 CSRF、401 跳登录），
     没有就退回原生 fetch —— open 模式与旧行为逐字一致。 */
  function api(u, o) {
    if (typeof window.zhuxueFetch === 'function') return window.zhuxueFetch(u, o);
    return window.fetch(u, o);
  }

  var allBooks = [];
  var progress = { books: {} };
  var qbanks = {};
  var cat = '全部';

  function coverColor(id) {
    var h = 0;
    for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
    return 'linear-gradient(135deg, hsl(' + h + ',32%,34%), hsl(' + ((h + 24) % 360) + ',30%,22%))';
  }
  function fmtNum(n) {
    if (!n) return '0';
    return n >= 10000 ? (n / 10000).toFixed(1) + ' 万' : String(n);
  }
  function readerHref(id) {
    if (typeof window.__READER_HREF__ === 'function') {
      var h = window.__READER_HREF__(id);
      if (h) return h;
    }
    return 'reader.html?book=' + encodeURIComponent(id);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function load() {
    shelf.innerHTML = '<div class="empty"><h2>正在读取书库…</h2></div>';
    try {
      var rs = await Promise.all([
        api('/api/books'),
        api('/api/progress'),
        api('/api/questions').catch(function () { return null; })
      ]);
      var rb = rs[0], rp = rs[1], rq = rs[2];
      allBooks = (await rb.json()).data.books || [];
      progress = (await rp.json()).data || { books: {} };
      if (rq && rq.ok) {
        var jq = await rq.json();
        ((jq.data && jq.data.items) || []).forEach(function (x) { qbanks[x.bookId] = x.total; });
      }
    } catch (e) {
      shelf.innerHTML = '<div class="empty"><h2>书库读取失败</h2><p>' + escapeHtml(e.message) + '</p></div>';
      return;
    }
    var nt = allBooks.filter(function (b) { return (b.shelfCategory || '扩展') === '教材'; }).length;
    nAll.textContent = String(allBooks.length);
    nText.textContent = String(nt);
    nExt.textContent = String(allBooks.length - nt);
    buildJump();
    render();
  }

  function buildJump() {
    var opts = ['<option value="">快速跳转…</option>'];
    ['教材', '扩展'].forEach(function (c) {
      opts.push('<optgroup label="' + c + '">');
      allBooks.filter(function (b) { return (b.shelfCategory || '扩展') === c; })
        .sort(function (a, b) { return String(a.bookTitle || a.title).localeCompare(String(b.bookTitle || b.title), 'zh'); })
        .forEach(function (b) {
          opts.push('<option value="' + escapeHtml(b.id) + '">' + escapeHtml(b.bookTitle || b.title || b.id) + (b.author ? '（' + escapeHtml(b.author) + '）' : '') + '</option>');
        });
      opts.push('</optgroup>');
    });
    jump.innerHTML = opts.join('');
  }

  function filtered() {
    return allBooks.filter(function (b) {
      if (cat !== '全部' && (b.shelfCategory || '扩展') !== cat) return false;
      return true;
    }).sort(function (a, b) {
      var ca = (a.shelfCategory || '扩展') === '教材' ? 0 : 1;
      var cb = (b.shelfCategory || '扩展') === '教材' ? 0 : 1;
      if (ca !== cb) return ca - cb;
      return String(a.bookTitle || a.title || '').localeCompare(String(b.bookTitle || b.title || ''), 'zh');
    });
  }

  function render() {
    var list = filtered();
    count.textContent = list.length === allBooks.length ? (allBooks.length ? '共 ' + allBooks.length + ' 本' : '') : list.length + ' / ' + allBooks.length + ' 本';
    if (!allBooks.length) {
      shelf.innerHTML = '<div class="empty"><h2>书架还是空的</h2><p>把 PDF 放进 <code>待转换书籍</code> 文件夹，双击 <code>一键转换书籍.bat</code>，转换完成后回来点「刷新」。</p></div>';
      return;
    }
    if (!list.length) {
      shelf.innerHTML = '<div class="empty"><h2>这个分类下还没有书</h2><p>换个分类试试。</p></div>';
      return;
    }
    shelf.innerHTML = list.map(cardHtml).join('');
  }

  function cardHtml(b) {
    var p = (progress.books || {})[b.id] || {};
    var pct = p.total ? Math.min(100, Math.round((p.page / p.total) * 100)) : 0;
    var chapters = (b.chapters || []).length;
    var isText = (b.shelfCategory || '扩展') === '教材';
    var catChip = isText ? '<span class="cat-chip is-text">教材</span>' : '<span class="cat-chip">扩展</span>';
    var qChip = qbanks[b.id] ? '<span class="qb-chip">题库 ' + qbanks[b.id] + ' 题</span>' : '';
    return [
      '<a class="book-card" href="' + readerHref(b.id) + '">',
      '  <div class="cover" style="background:' + coverColor(b.id) + '">',
      '    <span class="t">《' + escapeHtml(b.bookTitle || b.title || '') + '》</span>',
      '    <span class="a">' + escapeHtml(b.author || '') + '</span>',
      '  </div>',
      '  <div class="card-body">',
      '    <div class="card-meta">' + catChip + '<span>' + chapters + ' 章</span><span>' + fmtNum(b.stats && b.stats.chars) + ' 字</span><span>' + ((b.stats && b.stats.figures) || 0) + ' 幅插图</span>' + qChip + '</div>',
      '    <div class="progress"><i style="width:' + pct + '%"></i></div>',
      '    <div class="card-foot"><span>' + (pct ? '已读 ' + pct + '%' : '还没开始') + '</span><span class="go">' + (pct ? '继续阅读 →' : '开始阅读 →') + '</span></div>',
      '  </div>',
      '</a>'
    ].join('');
  }

  function bind() {
    tabs.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-cat]');
      if (!btn) return;
      cat = btn.dataset.cat;
      Array.prototype.forEach.call(tabs.querySelectorAll('[data-cat]'), function (x) {
        x.classList.toggle('is-active', x === btn);
      });
      render();
    });
    jump.addEventListener('change', function () {
      if (jump.value) location.href = readerHref(jump.value);
    });
    document.getElementById('refresh').addEventListener('click', function () { load(); });
  }

  bind();
  load();
})();
