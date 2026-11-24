/* 筑学 · 刷题 */
(function () {
  var params = new URLSearchParams(location.search);
  var bookId = params.get('book');
  var els = {
    title: document.getElementById('qbTitle'),
    chapterSel: document.getElementById('chapterSel'),
    typeSel: document.getElementById('typeSel'),
    modeSel: document.getElementById('modeSel'),
    wrongBtn: document.getElementById('wrongBtn'),
    wrongCount: document.getElementById('wrongCount'),
    resetBtn: document.getElementById('resetBtn'),
    qBarFill: document.getElementById('qBarFill'),
    qMeta: document.getElementById('qMeta'),
    qType: document.getElementById('qType'),
    qCard: document.getElementById('qCard'),
    qChapter: document.getElementById('qChapter'),
    qDiff: document.getElementById('qDiff'),
    qStem: document.getElementById('qStem'),
    qBody: document.getElementById('qBody'),
    qFeedback: document.getElementById('qFeedback'),
    qActions: document.getElementById('qActions'),
    prevBtn: document.getElementById('prevBtn'),
    nextBtn: document.getElementById('nextBtn'),
    qCounter: document.getElementById('qCounter'),
    srcSummary: document.getElementById('srcSummary'),
    wrongPanel: document.getElementById('wrongPanel'),
    wrongList: document.getElementById('wrongList'),
    wpClose: document.getElementById('wpClose')
  };

  var bank = null;
  var questions = [];
  var chapters = [];
  var answerMap = {};
  var view = [];
  var idx = 0;
  var submitted = false;
  var selected = '';
  var blankInputs = [];
  var checkMap = {};        // qid -> {v:'g'|'a'|'r', r:题干与原文段的 2-gram 重合率}
  var checkState = 'idle';  // idle | loading | done | failed
  var onlySuspect = false;
  var SRC_KEY = 'zhuxue-src-check:';

  var TYPE_NAME = { choice: '选择题', blank: '填空题', term: '名词解释', short: '简答题' };
  var TYPE_ORDER = ['choice', 'blank', 'term', 'short'];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function norm(s) {
    return String(s == null ? '' : s)
      .replace(/\s+/g, '')
      .replace(/[，。、；：！？（）()「」“”‘’"'’,.;:!?\-—…]/g, '')
      .toLowerCase();
  }
  function toast(msg) {
    var t = document.querySelector('.toast');
    if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove('show'); }, 1600);
  }

  function load() {
    if (!bookId) {
      document.body.innerHTML = '<div class="loading">没有指定书。请从书架或阅读器进入。</div>';
      return;
    }
    els.title.textContent = '加载中…';
    Promise.all([
      fetch('/api/questions/' + encodeURIComponent(bookId)),
      fetch('/api/practice')
    ]).then(function (rs) {
      if (!rs[0].ok) throw new Error('这本书还没有题库');
      return Promise.all([rs[0].json(), rs[1].json().catch(function () { return { data: {} }; })]);
    }).then(function (js2) {
      bank = js2[0].data;
      questions = bank.questions || [];
      chapters = bank.chapters || [];
      var db = js2[1].data || {};
      var rec = (db.books || {})[bookId];
      answerMap = (rec && rec.answered) || {};
      if (!questions.length) throw new Error('题库是空的');
      els.title.textContent = '《' + (bank.bookTitle || bank.bookId || bookId) + '》· ' + questions.length + ' 题';
      buildFilters();
      bindEvents();
      rebuild(true);
      verifySources();
    }).catch(function (e) {
      document.body.innerHTML = '<div class="loading">加载失败：' + esc(e.message) + '</div>';
    });
  }

  function buildFilters() {
    var usedChapters = {};
    questions.forEach(function (q) { usedChapters[q.chapter] = (usedChapters[q.chapter] || 0) + 1; });
    var chOpts = ['<option value="">全部章节</option>'];
    chapters.forEach(function (c) {
      if (!usedChapters[c.id]) return;
      chOpts.push('<option value="' + esc(c.id) + '">' + esc(c.title) + '（' + usedChapters[c.id] + '）</option>');
    });
    els.chapterSel.innerHTML = chOpts.join('');
    var tOpts = ['<option value="">全部题型</option>'];
    TYPE_ORDER.forEach(function (t) {
      var n = questions.filter(function (q) { return q.type === t; }).length;
      if (n) tOpts.push('<option value="' + t + '">' + TYPE_NAME[t] + '（' + n + '）</option>');
    });
    els.typeSel.innerHTML = tOpts.join('');
  }

  function bindEvents() {
    els.chapterSel.addEventListener('change', function () { rebuild(true); });
    els.typeSel.addEventListener('change', function () { rebuild(true); });
    els.modeSel.addEventListener('change', function () { rebuild(true); });
    els.resetBtn.addEventListener('click', function () {
      if (!confirm('清空这本书的全部作答记录？')) return;
      var qids = Object.keys(answerMap);
      qids.forEach(function (qid) {
        fetch('/api/practice/' + encodeURIComponent(qid) + '?bookId=' + encodeURIComponent(bookId), { method: 'DELETE' }).catch(function () {});
      });
      answerMap = {};
      renderWrong();
      rebuild(true);
    });
    els.wrongBtn.addEventListener('click', function () { els.wrongPanel.classList.toggle('hidden'); renderWrong(); });
    els.wpClose.addEventListener('click', function () { els.wrongPanel.classList.add('hidden'); });
    els.prevBtn.addEventListener('click', function () { go(idx - 1); });
    els.nextBtn.addEventListener('click', function () { go(idx + 1); });
    document.addEventListener('keydown', function (e) {
      if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) {
        if (e.key === 'Enter' && e.target.classList.contains('blank-input')) { e.preventDefault(); submitCurrent(); }
        return;
      }
      if (e.key === 'ArrowLeft') { e.preventDefault(); go(idx - 1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); go(idx + 1); }
      if (e.key === 'Enter') { e.preventDefault(); if (submitted) go(idx + 1); else submitCurrent(); }
      if (!submitted && /^[1-4]$/.test(e.key)) {
        var q = view[idx];
        if (q && q.type === 'choice') selectOption('ABCD'[Number(e.key) - 1]);
      }
    });
  }

  function rebuild(resetIndex) {
    var ch = els.chapterSel.value;
    var ty = els.typeSel.value;
    var mode = els.modeSel.value;
    view = questions.filter(function (q) {
      if (onlySuspect && !(checkMap[q.id] && checkMap[q.id].v === 'r')) return false;
      if (ch && q.chapter !== ch) return false;
      if (ty && q.type !== ty) return false;
      var rec = answerMap[q.id];
      if (mode === 'todo' && rec) return false;
      if (mode === 'wrong' && !(rec && !rec.correct)) return false;
      return true;
    });
    if (resetIndex) idx = 0;
    if (idx >= view.length) idx = Math.max(0, view.length - 1);
    render();
  }

  function go(n) {
    if (!view.length) return;
    idx = Math.max(0, Math.min(view.length - 1, n));
    render();
  }

  function render() {
    submitted = false;
    selected = '';
    blankInputs = [];
    els.qFeedback.classList.add('hidden');
    els.qFeedback.innerHTML = '';
    els.qBody.innerHTML = '';
    els.qActions.innerHTML = '';
    updateStats();
    renderSummary();

    if (!view.length) {
      els.qMeta.textContent = '没有符合条件的题目';
      els.qStem.textContent = '换个筛选条件试试。';
      els.qType.textContent = '';
      els.qChapter.textContent = '';
      els.qDiff.textContent = '';
      els.qCounter.textContent = '';
      return;
    }

    var q = view[idx];
    var chTitle = '';
    chapters.forEach(function (c) { if (c.id === q.chapter) chTitle = c.title; });
    els.qType.textContent = TYPE_NAME[q.type] || q.type;
    els.qChapter.textContent = chTitle + (q.section ? ' · ' + q.section : '');
    els.qDiff.textContent = ['', '难度 1', '难度 2', '难度 3'][q.difficulty || 0] || '';
    els.qStem.textContent = q.stem;
    els.qCounter.textContent = (idx + 1) + ' / ' + view.length;
    renderSourceBadge(q);

    var prev = answerMap[q.id];
    if (q.type === 'choice') renderChoice(q, prev);
    else if (q.type === 'blank') renderBlank(q, prev);
    else renderOpen(q, prev);
    if (prev) showFeedback(q, !!prev.correct, prev.chosen);
    renderActions(q, prev);
    updateStats();
  }

  function renderChoice(q, prev) {
    var box = document.createElement('div');
    box.className = 'q-options';
    (q.options || []).forEach(function (opt, i) {
      var letter = 'ABCD'[i];
      var b = document.createElement('button');
      b.className = 'q-option';
      b.dataset.key = letter;
      b.innerHTML = '<span class="opt-key">' + letter + '</span><span class="opt-text">' + esc(String(opt).replace(/^[A-Da-d][.、．]\s*/, '')) + '</span>';
      b.addEventListener('click', function () { if (!submitted) selectOption(letter); });
      box.appendChild(b);
    });
    els.qBody.appendChild(box);
    if (prev) {
      selected = String(prev.chosen || '');
      markChoice(selected, q.answer);
    }
  }

  function selectOption(letter) {
    selected = letter;
    markChoice(letter, null);
  }
  function markChoice(chosen, answer) {
    Array.prototype.forEach.call(els.qBody.querySelectorAll('.q-option'), function (b) {
      b.classList.toggle('is-selected', b.dataset.key === chosen);
      b.classList.remove('is-right', 'is-wrong');
      if (answer) {
        if (b.dataset.key === answer) b.classList.add('is-right');
        else if (b.dataset.key === chosen) b.classList.add('is-wrong');
      }
    });
  }

  function blankCount(q) {
    var m = String(q.stem || '').match(/_{2,}/g);
    var n = m ? m.length : 0;
    return n || (Array.isArray(q.answer) ? q.answer.length : 1);
  }

  function renderBlank(q, prev) {
    var n = blankCount(q);
    var box = document.createElement('div');
    box.className = 'q-blanks';
    for (var i = 0; i < n; i++) {
      var row = document.createElement('div');
      row.className = 'blank-row';
      row.innerHTML = '<span class="blank-no">第 ' + (i + 1) + ' 空</span>';
      var inp = document.createElement('input');
      inp.className = 'blank-input';
      inp.type = 'text';
      inp.autocomplete = 'off';
      inp.placeholder = '填这里';
      if (prev && Array.isArray(prev.chosen)) inp.value = prev.chosen[i] || '';
      row.appendChild(inp);
      blankInputs.push(inp);
      box.appendChild(row);
    }
    els.qBody.appendChild(box);
  }

  function renderOpen(q, prev) {
    var box = document.createElement('textarea');
    box.className = 'q-notes';
    box.rows = 4;
    box.placeholder = '可以先在这里写自己的答案，再点「看答案」对照（不判分，自评）';
    if (prev && typeof prev.chosen === 'string') box.value = prev.chosen;
    els.qBody.appendChild(box);
  }

  function renderActions(q, prev) {
    els.qActions.innerHTML = '';
    var open = q.type === 'term' || q.type === 'short';
    if (!prev) {
      var b = document.createElement('button');
      b.className = 'btn primary';
      b.textContent = open ? '看答案 · 自评' : '提交答案';
      b.addEventListener('click', function () { open ? revealAndGrade(q) : submitCurrent(); });
      els.qActions.appendChild(b);
    } else {
      var w = document.createElement('a');
      w.className = 'btn';
      w.href = sourceUrl(q);
      w.target = '_blank';
      w.rel = 'noopener';
      w.textContent = '看原文';
      els.qActions.appendChild(w);
      var n = document.createElement('button');
      n.className = 'btn primary';
      n.textContent = idx + 1 < view.length ? '下一题 →' : '已是最后一题';
      n.disabled = idx + 1 >= view.length;
      n.addEventListener('click', function () { go(idx + 1); });
      els.qActions.appendChild(n);
    }
  }

  function sourceUrl(q) {
    var u = readerHref(q);
    if (checkMap[q.id]) {
      var st = { g: 'green', a: 'amber', r: 'red' }[checkMap[q.id].v];
      if (st) u += (u.indexOf('?') >= 0 ? '&' : '?') + 'check=' + st;
    }
    return u;
  }

  function readerHref(q) {
    if (typeof window.__READER_HREF__ === 'function') {
      var h = window.__READER_HREF__(bookId, q.page || 1, q.block);
      if (h) return h;
    }
    var u = 'reader.html?book=' + encodeURIComponent(bookId) + '&page=' + (q.page || 1);
    if (q.block != null) u += '&block=' + q.block;
    return u;
  }

  /* ---------------- 出处核验：题干 ↔ 原文段（与离线脚本 _b_expected.py 同一套规则） ---------------- */
  function srcNorm(s) {
    return String(s == null ? '' : s)
      .replace(/\s+/g, '')
      .replace(/[，。、；：！？（）()「」“”‘’"'’,.;:!?\-—…]/g, '')
      .toLowerCase();
  }
  function srcBigrams(s) {
    var o = {};
    for (var i = 0; i + 1 < s.length; i++) o[s.substr(i, 2)] = 1;
    return o;
  }
  function srcLcs(a, b) {
    var max = Math.min(8, a.length);
    for (var L = max; L >= 4; L--) {
      for (var i = 0; i + L <= a.length; i++) {
        if (b.indexOf(a.substr(i, L)) >= 0) return L;
      }
    }
    return 0;
  }
  function blockWindow(blocks, b) {
    var s = '';
    for (var k = Math.max(0, b - 2); k <= Math.min(blocks.length - 1, b + 2); k++) s += blocks[k].text || '';
    return s;
  }
  function judgeSource(q, blocks, heads) {
    var stem = srcNorm(q.stem || '');
    var b = q.block;
    if (!stem || typeof b !== 'number' || b < 0 || b >= blocks.length) return { v: 'r', r: 0 };
    var win = srcNorm(blockWindow(blocks, b));
    if (!win) return { v: 'r', r: 0 };
    var sg = srcBigrams(stem);
    var wg = srcBigrams(win);
    var hit = 0, total = 0, k;
    for (k in sg) { if (sg.hasOwnProperty(k)) { total++; if (wg[k]) hit++; } }
    var rw = total ? hit / total : 0;
    var term = stem.length >= 2 && (win.indexOf(stem) >= 0 || heads.indexOf(stem) >= 0);
    var l = srcLcs(stem, win);
    var v = (term || rw >= 0.30 || l >= 6) ? 'g' : (rw >= 0.15 || l >= 4) ? 'a' : 'r';
    return { v: v, r: Math.round(rw * 100) / 100 };
  }

  function verifySources() {
    if (!questions.length) return;
    var key = SRC_KEY + bookId;
    try {
      var cached = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (cached && cached.n === questions.length && cached.map) {
        checkMap = cached.map;
        checkState = 'done';
        applyChecks();
        return;
      }
    } catch (e) {}
    checkState = 'loading';
    renderSummary();
    fetch('/api/books/' + encodeURIComponent(bookId)).then(function (r) {
      if (!r.ok) throw new Error('no book');
      return r.json();
    }).then(function (j) {
      var blocks = (j.data && j.data.blocks) || [];
      if (!blocks.length) throw new Error('empty book');
      var heads = '';
      blocks.forEach(function (b) { if (b.type === 'heading') heads += b.text || ''; });
      heads = srcNorm(heads);
      var map = {};
      questions.forEach(function (q) { map[q.id] = judgeSource(q, blocks, heads); });
      checkMap = map;
      checkState = 'done';
      try {
        sessionStorage.setItem(key, JSON.stringify({ n: questions.length, v: 1, map: map }));
      } catch (e) {}
      applyChecks();
    }).catch(function () {
      checkState = 'failed';
      renderSummary();
      if (view.length) renderSourceBadge(view[idx]);
    });
  }

  function applyChecks() {
    renderSummary();
    if (onlySuspect) { rebuild(false); return; }
    if (view.length) renderSourceBadge(view[idx]);
  }

  function renderSummary() {
    if (!els.srcSummary) return;
    if (checkState === 'idle') { els.srcSummary.classList.add('hidden'); return; }
    els.srcSummary.classList.remove('hidden');
    if (checkState === 'loading') {
      els.srcSummary.innerHTML = '<span class="q-src-tip">出处核验中…</span>';
      return;
    }
    if (checkState === 'failed') {
      els.srcSummary.innerHTML = '<span class="q-src-tip">核验不可用：没找到这本书的正文，徽标只做跳转</span>';
      return;
    }
    var g = 0, a = 0, r = 0;
    questions.forEach(function (q) {
      var c = checkMap[q.id];
      if (!c) return;
      if (c.v === 'g') g++; else if (c.v === 'a') a++; else r++;
    });
    var html = '<span class="q-src-tip">出处核验：</span>' +
      '<span class="q-src-chip ok">已核 ' + g + '</span>' +
      '<span class="q-src-chip warn">弱 ' + a + '</span>';
    if (r) {
      html += '<button type="button" class="q-src-chip bad' + (onlySuspect ? ' is-active' : '') +
        '" id="srcSuspectBtn">可疑 ' + r + (onlySuspect ? '（取消筛选）' : '（点此只看可疑）') + '</button>';
    } else {
      html += '<span class="q-src-chip bad">可疑 0</span>';
    }
    els.srcSummary.innerHTML = html;
    var sb = document.getElementById('srcSuspectBtn');
    if (sb) sb.addEventListener('click', function () {
      if (checkState !== 'done') { toast('核验还没完成'); return; }
      onlySuspect = !onlySuspect;
      rebuild(true);
    });
  }

  /* 出处徽标：题干上方常驻；核验完按档位上色，答题前就能点去原文 */
  function renderSourceBadge(q) {
    var old = els.qCard.querySelector('.q-source-btn');
    if (old) old.parentNode.removeChild(old);
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'q-source-btn';
    var hasPage = q.page != null && q.page !== '';
    var hasBlock = q.block != null && q.block !== '';
    if (!hasPage && !hasBlock) {
      btn.textContent = '出处 · 未标注';
      btn.disabled = true;
      btn.title = '这道题没有记录页码/段号';
      els.qDiff.insertAdjacentElement('afterend', btn);
      return;
    }
    var parts = [];
    if (hasPage) parts.push('第 ' + q.page + ' 页');
    if (hasBlock) parts.push('段 ' + q.block);
    var loc = parts.join(' · ');
    var ck = checkMap[q.id];
    var prefix = '出处 · ';
    if (ck) {
      if (ck.v === 'g') { prefix = '出处已核 · '; btn.classList.add('is-green'); }
      else if (ck.v === 'a') { prefix = '弱匹配 · '; btn.classList.add('is-amber'); }
      else { prefix = '出处可疑 · '; btn.classList.add('is-red'); }
      btn.title = '题干与该段关键词重合 ' + Math.round((ck.r || 0) * 100) + '%' +
        (ck.v === 'g' ? '（对得上）' : ck.v === 'a' ? '（大致对上）' : '（对不上，可能页码或段号偏移）');
    } else {
      btn.classList.add('is-pending');
      btn.title = checkState === 'loading' ? '出处核验中…' :
        (checkState === 'failed' ? '核验不可用，点开看原文' : '跳到原文出处');
    }
    btn.textContent = prefix + loc;
    btn.addEventListener('click', function () {
      window.open(sourceUrl(q), '_blank', 'noopener');
    });
    els.qDiff.insertAdjacentElement('afterend', btn);
  }

  function submitCurrent() {
    var q = view[idx];
    if (!q || submitted) return;
    if (q.type === 'choice') {
      if (!selected) { toast('先选一个选项'); return; }
      var ok = selected === q.answer;
      finish(q, ok, selected);
    } else if (q.type === 'blank') {
      var answers = Array.isArray(q.answer) ? q.answer : [q.answer];
      var chosen = blankInputs.map(function (i) { return i.value; });
      var allOk = answers.every(function (a, i) {
        var ua = norm(chosen[i]);
        var aa = norm(a);
        return !!ua && (ua === aa || (aa.length >= 2 && ua.indexOf(aa) >= 0));
      });
      finish(q, allOk, chosen);
    }
  }

  function revealAndGrade(q) {
    var notes = els.qBody.querySelector('.q-notes');
    var chosen = notes ? notes.value : '';
    els.qFeedback.classList.remove('hidden');
    els.qFeedback.innerHTML = '<div class="fb-title">参考答案</div><div class="fb-answer">' + esc(answerText(q)) + '</div>' +
      '<div class="fb-explain"><b>解析：</b>' + esc(q.explain || '') + '</div>' +
      '<div class="fb-grade"><span>对照一下，你答对了吗？</span>' +
      '<button class="btn" data-grade="1">答对了</button><button class="btn" data-grade="0">没答对</button></div>';
    els.qFeedback.querySelectorAll('[data-grade]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        finish(q, btn.dataset.grade === '1', chosen);
      });
    });
    els.qActions.innerHTML = '';
  }

  function finish(q, ok, chosen) {
    submitted = true;
    answerMap[q.id] = { correct: !!ok, chosen: chosen, at: Date.now() };
    fetch('/api/practice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bookId: bookId, questionId: q.id, type: q.type, correct: !!ok, chosen: chosen })
    }).catch(function () {});
    if (q.type === 'choice' && ok !== undefined) markChoice(String(chosen || ''), q.answer);
    showFeedback(q, !!ok, chosen);
    renderActions(q, answerMap[q.id]);
    renderWrong();
    updateStats();
  }

  function answerText(q) {
    if (q.type === 'choice') {
      var i = 'ABCD'.indexOf(String(q.answer || ''));
      var opt = (q.options || [])[i] || '';
      return String(q.answer) + '. ' + String(opt).replace(/^[A-Da-d][.、．]\s*/, '');
    }
    if (q.type === 'blank') return (Array.isArray(q.answer) ? q.answer : [q.answer]).map(function (a, i) { return '第' + (i + 1) + '空：' + a; }).join('　');
    return q.answer || '';
  }

  function showFeedback(q, ok, chosen) {
    var head = ok ? '✓ 答对了' : '✗ 答错了';
    var chosenLine = '';
    if (q.type === 'choice' && chosen) {
      chosenLine = '<div class="fb-chosen">你的选择：' + esc(chosen) + '</div>';
    }
    els.qFeedback.classList.remove('hidden');
    els.qFeedback.innerHTML = '<div class="fb-line ' + (ok ? 'ok' : 'bad') + '">' + head + '</div>' +
      chosenLine +
      '<div class="fb-answer"><b>答案：</b>' + esc(answerText(q)) + '</div>' +
      '<div class="fb-explain"><b>解析：</b>' + esc(q.explain || '') + '</div>';
  }

  function updateStats() {
    var done = view.filter(function (q) { return answerMap[q.id]; }).length;
    var pct = view.length ? Math.round((done / view.length) * 100) : 0;
    els.qBarFill.style.width = pct + '%';
    var right = view.filter(function (q) { return (answerMap[q.id] || {}).correct; }).length;
    els.qMeta.textContent = '本组 ' + view.length + ' 题 · 已做 ' + done + ' · 正确 ' + right + (done ? '（' + Math.round((right / done) * 100) + '%）' : '');
    els.prevBtn.disabled = idx <= 0;
    els.nextBtn.disabled = idx >= view.length - 1;
  }

  function renderWrong() {
    var wrong = questions.filter(function (q) { return answerMap[q.id] && !answerMap[q.id].correct; });
    els.wrongCount.textContent = String(wrong.length);
    if (!wrong.length) {
      els.wrongList.innerHTML = '<div class="wp-empty">还没有错题。做错的题会自动进来。</div>';
      return;
    }
    els.wrongList.innerHTML = wrong.map(function (q) {
      var chTitle = '';
      chapters.forEach(function (c) { if (c.id === q.chapter) chTitle = c.title; });
      return '<div class="wp-item" data-qid="' + esc(q.id) + '">' +
        '<span class="wp-type">' + esc(TYPE_NAME[q.type] || q.type) + '</span>' +
        '<span class="wp-stem">' + esc(String(q.stem).slice(0, 46)) + '</span>' +
        '<span class="wp-ch">' + esc(chTitle) + '</span></div>';
    }).join('');
    els.wrongList.querySelectorAll('[data-qid]').forEach(function (el) {
      el.addEventListener('click', function () {
        var qid = el.dataset.qid;
        var target = view.findIndex(function (q) { return q.id === qid; });
        if (target < 0) {
          els.chapterSel.value = '';
          els.typeSel.value = '';
          els.modeSel.value = 'all';
          rebuild(false);
          target = view.findIndex(function (q) { return q.id === qid; });
        }
        if (target >= 0) { idx = target; els.wrongPanel.classList.add('hidden'); render(); }
      });
    });
  }

  load();
})();
