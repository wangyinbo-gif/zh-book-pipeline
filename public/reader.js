/* 筑学 · 阅读器（翻页 + 章节目录 + 进度） */
(function () {
  const params = new URLSearchParams(location.search);
  const bookId = params.get('book');
  const els = {
    bookTitle: document.getElementById('bookTitle'),
    bookSub: document.getElementById('bookSub'),
    toc: document.getElementById('toc'),
    tocList: document.getElementById('tocList'),
    chapterSelect: document.getElementById('chapterSelect'),
    stage: document.getElementById('stage'),
    body: document.getElementById('bookBody'),
    prev: document.getElementById('prevBtn'),
    next: document.getElementById('nextBtn'),
    counter: document.getElementById('counter'),
    bar: document.getElementById('barFill'),
    modeBtn: document.getElementById('modeBtn'),
    qbBtn: document.getElementById('qbBtn'),
    tocBtn: document.getElementById('tocBtn'),
    pageInput: document.getElementById('pageInput'),
    jumpBtn: document.getElementById('jumpBtn'),
    bmAddBtn: document.getElementById('bmAddBtn'),
    bmListBtn: document.getElementById('bmListBtn'),
    bmClose: document.getElementById('bmClose'),
    bmPanel: document.getElementById('bookmarks'),
    bmList: document.getElementById('bmList'),
    bmTip: document.getElementById('bmTip')
  };

  if (!bookId) {
    document.body.innerHTML = '<div class="loading">没有指定书名。请从书架进入。</div>';
    return;
  }

  const MAXH = 996;
  const PAGE_W = 820;
  const PAGE_H = 1160;

  let book = null;
  let blocks = [];
  let chapters = [];
  let chapterFirstBlock = [];
  let chapterPage = [];
  let pages = [];        // {blocks: [], sheet: DOM, chapter: idx}
  let current = 0;
  // 默认双页；用户手动切过就记住（localStorage）
  const savedDouble = localStorage.getItem('zhuxue-double-page');
  // 平板竖屏默认单页；用户手动切过就记住
  let double = savedDouble ? savedDouble !== '0' : window.innerWidth >= 1000;
  let saveTimer = null;
  let bookmarks = [];

  /* ---------------- 数据 ---------------- */
  async function load() {
    els.bookTitle.textContent = '加载中…';
    const [rb, rp, rbm, rq] = await Promise.all([
      fetch('/api/books/' + encodeURIComponent(bookId)),
      fetch('/api/progress'),
      fetch('/api/bookmarks?bookId=' + encodeURIComponent(bookId)),
      fetch('/api/questions').catch(() => null)
    ]);
    if (!rb.ok) throw new Error('找不到这本书');
    const jb = await rb.json();
    book = jb.data;
    const jp = await rp.json();
    const saved = (jp.data.books || {})[bookId] || null;
    try {
      const jbm = await rbm.json();
      bookmarks = (jbm.data && jbm.data.items) || [];
    } catch (e) {
      bookmarks = [];
    }

    // 有题库就在顶栏露出「刷题」入口
    try {
      if (rq && rq.ok && els.qbBtn) {
        const jq = await rq.json();
        const hit = (jq.data.items || []).find((x) => x.bookId === bookId);
        if (hit) {
          const qbHref = typeof window.__PRACTICE_HREF__ === 'function'
            ? window.__PRACTICE_HREF__(bookId)
            : window.__PRACTICE_HREF__;
          els.qbBtn.href = qbHref || ('practice.html?book=' + encodeURIComponent(bookId));
          els.qbBtn.textContent = '刷题 ' + hit.total;
          els.qbBtn.classList.remove('hidden');
        }
      }
    } catch (e) {}

    blocks = Array.isArray(book.blocks) ? book.blocks : [];
    chapters = Array.isArray(book.chapters) && book.chapters.length
      ? book.chapters
      : [{ id: 'ch-1', title: book.title || book.bookTitle || '正文', chars: 0, figures: 0 }];
    chapterFirstBlock = chapters.map((c) => {
      if (typeof c.startBlock === 'number') return c.startBlock;
      const t = String(c.title || '').trim();
      const idx = blocks.findIndex((b) => b.type === 'heading' && String(b.text || '').trim() === t);
      return idx >= 0 ? idx : 0;
    });

    renderChrome();
    if (window.innerWidth <= 1100) els.toc.classList.add('hidden');
    build();
    // 支持从题库跳转：?page=N（1 起）或 ?block=N（正文块下标，更精确）
    const qBlock = parseInt(params.get('block'), 10);
    const qPage = parseInt(params.get('page'), 10);
    const info = window.__readerInfo || {};
    if (Number.isFinite(qBlock) && info.blockPage && info.blockPage[qBlock] != null) {
      show(info.blockPage[qBlock]);
    } else if (Number.isFinite(qPage) && qPage > 0 && qPage <= pages.length) {
      show(qPage - 1);
    } else if (saved && saved.page > 0 && saved.page < pages.length) {
      show(saved.page);
    } else {
      show(0);
    }
    els.pageInput.max = pages.length;
    els.pageInput.placeholder = '1–' + pages.length;
    renderBookmarks();
    markBookmarks();
  }

  function renderChrome() {
    els.bookTitle.textContent = '《' + (book.bookTitle || book.title || bookId) + '》' + (book.author ? '\n' + book.author : '');
    els.bookSub.textContent = (book.pageRange ? `原书 p.${book.pageRange[0]}–${book.pageRange[1]} · ` : '') +
      `${(book.stats && book.stats.chars) || 0} 字 · ${chapters.length} 章`;
    els.chapterSelect.innerHTML = chapters.map((c, i) =>
      `<option value="${i}">${escapeHtml(c.title)}</option>`).join('');
    els.tocList.innerHTML = chapters.map((c, i) =>
      `<button class="toc-item" data-ch="${i}">${escapeHtml(c.title)}<small>${c.chars || 0} 字 · ${c.figures || 0} 图</small></button>`).join('');
    els.tocList.querySelectorAll('[data-ch]').forEach((btn) => {
      btn.addEventListener('click', () => jumpChapter(Number(btn.dataset.ch)));
    });
    els.chapterSelect.addEventListener('change', () => jumpChapter(Number(els.chapterSelect.value)));
    els.modeBtn.addEventListener('click', () => {
      double = !double;
      localStorage.setItem('zhuxue-double-page', double ? '1' : '0');
      els.modeBtn.textContent = double ? '双页' : '单页';
      show(current, true);
    });
    els.tocBtn.addEventListener('click', () => els.toc.classList.toggle('hidden'));
    els.jumpBtn.addEventListener('click', () => {
      const n = parseInt(els.pageInput.value, 10);
      if (!n) return toast('请输入页码');
      goToPage(n);
    });
    els.pageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        els.jumpBtn.click();
      }
    });
    els.bmAddBtn.addEventListener('click', toggleBookmark);
    els.bmListBtn.addEventListener('click', () => {
      els.bmPanel.classList.toggle('hidden');
      if (!els.bmPanel.classList.contains('hidden')) renderBookmarks();
    });
    els.bmClose.addEventListener('click', () => els.bmPanel.classList.add('hidden'));
    els.prev.addEventListener('click', () => (double ? show(current - 2) : show(current - 1)));
    els.next.addEventListener('click', () => (double ? show(current + 2) : show(current + 1)));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); double ? show(current - 2) : show(current - 1); }
      if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); double ? show(current + 2) : show(current + 1); }
    });
    let touchX = 0, touchY = 0, touching = false, suppressClickUntil = 0;
    els.stage.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      touchX = e.touches[0].clientX; touchY = e.touches[0].clientY; touching = true;
    }, { passive: true });
    els.stage.addEventListener('touchend', (e) => {
      if (!touching) return;
      touching = false;
      const t = e.changedTouches[0];
      const dx = t.clientX - touchX, dy = t.clientY - touchY;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.3) {
        suppressClickUntil = Date.now() + 350;
        if (dx < 0) els.next.click(); else els.prev.click();
        toast(dx < 0 ? '下一页（左右滑动可翻页）' : '上一页');
      }
    }, { passive: true });
    els.stage.addEventListener('click', (e) => {
      if (Date.now() < suppressClickUntil) return;
      if (e.target.closest('.nav')) return;
      const r = els.stage.getBoundingClientRect();
      if (e.clientX - r.left < r.width * 0.2) { double ? show(current - 2) : show(current - 1); }
      else if (e.clientX - r.left > r.width * 0.8) { double ? show(current + 2) : show(current + 1); }
    });
    window.addEventListener('resize', fit);
  }

  /* ---------------- 分页排版 ---------------- */
  function build() {
    const flow = document.createElement('div');
    flow.style.cssText = 'position:absolute;left:-99999px;top:0;width:660px;';
    document.body.appendChild(flow);

    let cur = null;
    let curIndex = -1;
    const blockPage = [];

    function newPage() {
      const d = document.createElement('div');
      d.style.cssText = 'width:660px;';
      flow.appendChild(d);
      pages.push({ blocks: [], sheet: null, chapter: 0 });
      cur = d;
      curIndex = pages.length - 1;
    }
    function over() { return cur.scrollHeight > MAXH; }
    function para(text) { const p = document.createElement('p'); p.appendChild(document.createTextNode(text)); p.style.cssText = 'font-size:18px;line-height:2;text-align:justify;margin:0 0 1em;text-indent:2em;font-family:"Songti SC","Noto Serif SC",Georgia,serif;'; return p; }
    function heading(text) { const h = document.createElement('h2'); h.textContent = text; h.style.cssText = 'font-size:20px;margin:0 0 16px;font-weight:600;font-family:"Songti SC",serif;'; return h; }
    function figure(b) {
      const fig = document.createElement('figure');
      fig.style.cssText = 'margin:20px 0 24px;text-align:center;';
      const img = document.createElement('img');
      const assetDir = (typeof window.__ASSET_DIR__ === 'function')
        ? window.__ASSET_DIR__(bookId)
        : (window.__ASSET_BASE__ || '/books/') + encodeURIComponent(bookId) + '/';
      img.src = assetDir + b.src;
      img.loading = 'lazy';
      img.decoding = 'async';
      if (b.w && b.h) {
        const scale = Math.min(1, 660 / b.w, 620 / b.h);
        img.style.width = Math.round(b.w * scale) + 'px';
        img.style.height = Math.round(b.h * scale) + 'px';
        img.setAttribute('width', b.w);
        img.setAttribute('height', b.h);
      }
      img.style.maxWidth = '100%';
      img.style.objectFit = 'contain';
      fig.appendChild(img);
      const cap = document.createElement('figcaption');
      cap.style.cssText = 'font-size:12.5px;color:#555;margin-top:9px;';
      cap.appendChild(document.createTextNode(b.displayCaption || ''));
      if (b.originalNumber) {
        const o = document.createElement('span');
        o.style.cssText = 'color:#a0a0a0;margin-left:8px;font-size:11px;';
        o.textContent = '原书 ' + b.originalNumber;
        cap.appendChild(o);
      }
      fig.appendChild(cap);
      return fig;
    }
    function tableBlock(b) {
      const wrap = document.createElement('div');
      wrap.className = 'table-block';
      if (b.caption) {
        const cap = document.createElement('div');
        cap.className = 'table-caption';
        cap.textContent = b.caption;
        wrap.appendChild(cap);
      }
      const table = document.createElement('table');
      (b.rows || []).forEach((row, ri) => {
        const tr = document.createElement('tr');
        (row || []).forEach((cell) => {
          const td = document.createElement(ri === 0 ? 'th' : 'td');
          td.textContent = cell == null ? '' : String(cell);
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      wrap.appendChild(table);
      return wrap;
    }
    function findFit(text) {
      let lo = 1, hi = text.length, best = 0;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const test = para(text.slice(0, mid));
        cur.appendChild(test);
        const bad = over();
        cur.removeChild(test);
        if (!bad) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      return best;
    }
    function flowParagraph(text) {
      let rest = text;
      let guard = 0;
      while (rest.length && guard++ < 200) {
        const el = para(rest);
        cur.appendChild(el);
        if (!over()) return;
        cur.removeChild(el);
        const fit = findFit(rest);
        if (fit <= 0) {
          if (cur.childNodes.length === 0) { cur.appendChild(para(rest)); return; }
          newPage(); continue;
        }
        cur.appendChild(para(rest.slice(0, fit)));
        rest = rest.slice(fit);
        newPage();
      }
    }

    /* 表格分页：超过一页的表要拆行续排，否则被 overflow:hidden 裁掉 */
    function flowTable(b) {
      const rows = (b.rows || []).filter((r) => r && r.length);
      if (!rows.length) return;
      const head = rows[0];
      const body = rows.slice(1);
      function startTable() {
        const wrap = document.createElement('div');
        wrap.className = 'table-block';
        if (b.caption) {
          const c = document.createElement('div');
          c.className = 'table-caption';
          c.textContent = b.caption;
          wrap.appendChild(c);
        }
        const t = document.createElement('table');
        const hr = document.createElement('tr');
        (head || []).forEach((cell) => {
          const th = document.createElement('th');
          th.textContent = cell == null ? '' : String(cell);
          hr.appendChild(th);
        });
        t.appendChild(hr);
        wrap.appendChild(t);
        cur.appendChild(wrap);
        return t;
      }
      let t = startTable();
      let i = 0;
      let guard = 0;
      while (i < body.length && guard++ < 4000) {
        const row = document.createElement('tr');
        (body[i] || []).forEach((cell) => {
          const td = document.createElement('td');
          td.textContent = cell == null ? '' : String(cell);
          row.appendChild(td);
        });
        t.appendChild(row);
        if (over()) {
          t.removeChild(row);
          const freshPage = cur.childNodes.length <= 1 && t.rows.length <= 1;
          if (freshPage) { t.appendChild(row); i++; continue; }   // 一整页只放表头都超，只能硬塞，避免死循环
          newPage();
          t = startTable();
          continue;
        }
        i++;
      }
    }

    newPage();
    blocks.forEach((b, i) => {
      blockPage[i] = curIndex;
      if (b.type === 'paragraph') { flowParagraph(b.text); return; }
      if (b.type === 'table') { flowTable(b); return; }
      const el = b.type === 'heading' ? heading(b.text) : b.type === 'figure' ? figure(b) : b.type === 'table' ? tableBlock(b) : para(b.text || '');
      cur.appendChild(el);
      if (over()) {
        cur.removeChild(el);
        newPage();
        blockPage[i] = curIndex;
        cur.appendChild(el);
      }
    });

    // 章节起始页
    window.__readerInfo = { blocks: blocks.length, chapters: chapters.map(c => c.title), chapterFirstBlock: chapterFirstBlock.slice(), blockPage: blockPage.slice() };
    chapterPage = chapters.map((c, i) => {
      const bi = chapterFirstBlock[i] || 0;
      const p = blockPage[bi];
      return p === undefined ? 0 : p;
    });
    window.__readerInfo.chapterPage = chapterPage.slice();
    // 每页所属章节
    pages.forEach((p, pi) => {
      let ch = 0;
      for (let i = 0; i < chapterPage.length; i++) if (chapterPage[i] <= pi) ch = i;
      p.chapter = ch;
    });

    // 生成纸张
    const inners = Array.prototype.slice.call(flow.children);
    inners.forEach((inner, i) => {
      const sheet = document.createElement('div');
      sheet.className = 'page-sheet';
      const running = document.createElement('div');
      running.className = 'running';
      const rl = document.createElement('span');
      rl.textContent = '《' + (book.bookTitle || '筑学') + '》';
      const rr = document.createElement('span');
      rr.textContent = chapters[pages[i].chapter] ? chapters[pages[i].chapter].title : '';
      running.appendChild(rl); running.appendChild(rr);
      const holder = document.createElement('div');
      holder.className = 'inner';
      holder.appendChild(inner);
      const pageno = document.createElement('div');
      pageno.className = 'pageno';
      pageno.textContent = '— ' + (i + 1) + ' —';
      sheet.appendChild(running); sheet.appendChild(holder); sheet.appendChild(pageno);
      els.body.appendChild(sheet);
      pages[i].sheet = sheet;
    });
    flow.remove();
    updateToc();
  }

  /* ---------------- 翻页显示 ---------------- */
  function visiblePages(n) {
    if (!double) return [n];
    const a = (n % 2 === 0) ? n : n - 1;
    return (a + 1 < pages.length) ? [a, a + 1] : [a];
  }
  function show(n, keep) {
    if (!pages.length) return;
    n = Math.max(0, Math.min(pages.length - 1, n));
    current = n;
    pages.forEach((p) => { if (p.sheet) { p.sheet.classList.remove('is-active', 'left', 'right'); p.sheet.style.display = 'none'; } });
    const vis = visiblePages(n);
    vis.forEach((i, k) => {
      const p = pages[i];
      if (!p || !p.sheet) return;
      p.sheet.classList.add('is-active');
      if (double) p.sheet.classList.add(k === 0 ? 'left' : 'right');
      p.sheet.style.display = 'block';
    });
    els.body.classList.toggle('double', double);
    fit();
    const labels = vis.map((i) => '第 ' + (i + 1) + ' 页');
    els.counter.textContent = (double ? labels.join(' – ') : labels[0]) + ' / 共 ' + pages.length + ' 页';
    els.bar.style.width = (pages.length > 1 ? (n / (pages.length - 1)) * 100 : 0) + '%';
    els.prev.disabled = n === 0;
    els.next.disabled = n >= pages.length - 1;
    updateToc();
    if (!keep) scheduleSave();
  }
  function reflowMode() {
    return window.innerWidth <= 600 ||
      (window.innerWidth <= 1200 && window.innerWidth > window.innerHeight);
  }
  function fit() {
    const frame = document.getElementById('frame');
    if (reflowMode()) {
      els.stage.style.overflowY = 'auto';
      els.body.style.transform = '';
      els.body.style.width = '';
      frame.style.width = '';
      frame.style.height = '';
      return;
    }
    els.stage.style.overflowY = 'hidden';
    const h = els.stage.clientHeight - 12;
    const w = els.stage.clientWidth - 76;
    const baseW = double ? PAGE_W * 2 : PAGE_W;
    let k = Math.min(h / PAGE_H, w / baseW, 1);
    if (!(k > 0)) k = 1;
    els.body.style.width = baseW + 'px';
    els.body.style.transform = 'scale(' + k + ')';
    els.body.style.transformOrigin = 'top left';
    frame.style.width = (baseW * k) + 'px';
    frame.style.height = (PAGE_H * k) + 'px';
  }
  function updateToc() {
    const ch = pages[current] ? pages[current].chapter : 0;
    els.tocList.querySelectorAll('[data-ch]').forEach((b) => b.classList.toggle('is-active', Number(b.dataset.ch) === ch));
    if (els.chapterSelect.value !== String(ch)) els.chapterSelect.value = String(ch);
  }
  function jumpChapter(i) {
    const p = chapterPage[i] || 0;
    show(p);
    if (window.innerWidth <= 900) els.toc.classList.add('hidden');
  }

  /* ---------------- 快速翻页 / 书签 ---------------- */
  let toastTimer = null;
  function toast(msg) {
    let t = document.querySelector('.toast');
    if (!t) {
      t = document.createElement('div');
      t.className = 'toast';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 1400);
  }

  function goToPage(n) {
    if (!pages.length) return;
    const idx = Math.round(Number(n)) - 1;
    if (!(idx >= 0 && idx < pages.length)) {
      toast('页码超出范围（1–' + pages.length + '）');
      return;
    }
    show(idx);
    els.pageInput.value = '';
  }

  function pageChapterTitle(i) {
    const p = pages[i];
    if (!p) return '';
    const c = chapters[p.chapter];
    return c ? c.title : '';
  }

  function excerptForPage(i) {
    const p = pages[i];
    if (!p || !p.sheet) return '';
    const inner = p.sheet.querySelector('.inner');
    const text = (inner ? inner.textContent : p.sheet.textContent) || '';
    return text.replace(/\s+/g, ' ').trim().slice(0, 70);
  }

  async function toggleBookmark() {
    if (!pages.length) return;
    const page = current + 1;
    const existing = bookmarks.find((b) => b.page === page);
    try {
      if (existing) {
        await fetch('/api/bookmarks/' + encodeURIComponent(existing.id), { method: 'DELETE' });
        bookmarks = bookmarks.filter((b) => b.id !== existing.id);
        toast('已取消第 ' + page + ' 页书签');
      } else {
        const res = await fetch('/api/bookmarks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bookId, page, chapter: pageChapterTitle(current), excerpt: excerptForPage(current) })
        });
        const j = await res.json();
        const item = j.data && j.data.item;
        if (item) {
          bookmarks = bookmarks.filter((b) => b.id !== item.id);
          bookmarks.unshift(item);
          toast('已收藏第 ' + page + ' 页');
        }
      }
      renderBookmarks();
      markBookmarks();
    } catch (e) {
      toast('书签操作失败');
    }
  }

  async function deleteBookmark(id) {
    try {
      await fetch('/api/bookmarks/' + encodeURIComponent(id), { method: 'DELETE' });
      bookmarks = bookmarks.filter((b) => b.id !== id);
      renderBookmarks();
      markBookmarks();
      toast('已删除书签');
    } catch (e) {
      toast('删除失败');
    }
  }

  function renderBookmarks() {
    if (!els.bmList) return;
    if (!bookmarks.length) {
      els.bmList.innerHTML = '<div class="bm-empty">还没有书签。<br>翻到想记住的页，点上方「书签」收藏。</div>';
      return;
    }
    els.bmList.innerHTML = bookmarks.map((b) => `
      <div class="bm-item" data-page="${b.page}">
        <span class="bm-line"><span class="bm-page">第 ${b.page} 页</span><span class="bm-ch">${escapeHtml(b.chapter || '')}</span></span>
        <span class="bm-ex">${escapeHtml(b.excerpt || '')}</span>
        <button class="bm-del" data-del="${b.id}">删除</button>
      </div>`).join('');
    els.bmList.querySelectorAll('.bm-item').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('[data-del]')) return;
        show(Math.max(0, Number(el.dataset.page) - 1));
        if (window.innerWidth <= 1100) els.bmPanel.classList.add('hidden');
      });
    });
    els.bmList.querySelectorAll('[data-del]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteBookmark(btn.dataset.del);
      });
    });
  }

  function markBookmarks() {
    const marked = new Set(bookmarks.map((b) => Number(b.page)));
    pages.forEach((p, i) => {
      if (!p.sheet) return;
      const pageno = p.sheet.querySelector('.pageno');
      if (!pageno) return;
      const old = pageno.querySelector('.star');
      if (old) old.remove();
      if (marked.has(i + 1)) {
        const s = document.createElement('span');
        s.className = 'star';
        s.textContent = '★';
        pageno.appendChild(s);
      }
    });
  }

  /* ---------------- 进度保存 ---------------- */
  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const ch = pages[current] ? pages[current].chapter : 0;
      fetch('/api/progress', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bookId, chapterId: chapters[ch] ? chapters[ch].id : null, page: current, total: pages.length })
      }).catch(() => {});
    }, 600);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  els.modeBtn.textContent = double ? '双页' : '单页';
  load().catch((e) => {
    document.body.innerHTML = '<div class="loading">加载失败：' + escapeHtml(e.message) + '</div>';
  });
})();
