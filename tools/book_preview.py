# -*- coding: utf-8 -*-
"""筑学 · 书稿包 → 翻页阅读器（白底黑字，单页/双页可切换）"""
import json


def build_preview(book, out_path):
    blocks_json = json.dumps(book["blocks"], ensure_ascii=False).replace("</", "<\\/")
    css = """
    *{box-sizing:border-box}
    html,body{margin:0;height:100%}
    body{background:#e8e8e8;font-family:"Songti SC","Noto Serif SC","Source Han Serif SC",Georgia,"Microsoft YaHei",serif;display:flex;flex-direction:column;overflow:hidden}
    .topbar{width:100%;display:flex;align-items:center;gap:12px;padding:9px 20px;font-size:12px;color:#666;background:#f4f4f4;border-bottom:1px solid #ddd}
    .topbar b{color:#333;font-weight:600;letter-spacing:.08em}
    .topbar .spacer{flex:1}
    .mode{border:1px solid #ccc;background:#fff;color:#444;border-radius:15px;padding:4px 12px;font-size:12px;cursor:pointer;font-family:inherit}
    .mode:hover{background:#f7f7f7}
    .stage{flex:1;display:flex;align-items:center;justify-content:center;width:100%;padding:14px;position:relative;min-height:0}
    #bookBody{display:flex;position:relative;width:820px;height:1160px;transform-origin:top left}
    #bookBody.double{width:1640px}
    .sheet{width:820px;height:1160px;background:#fff;position:relative;flex:0 0 auto;display:none;color:#111;padding:72px 80px 92px}
    .sheet.is-active{display:block}
    .sheet.left{box-shadow:inset -22px 0 26px -30px rgba(0,0,0,.42)}
    .sheet.right{box-shadow:inset 22px 0 26px -30px rgba(0,0,0,.42)}
    .sheet.blank{box-shadow:inset -22px 0 26px -30px rgba(0,0,0,.42)}
    .frame{position:relative}
    .spine{position:absolute;top:0;bottom:0;left:50%;width:2px;background:linear-gradient(90deg,rgba(0,0,0,.05),rgba(0,0,0,.14),rgba(0,0,0,.05));pointer-events:none;display:none}
    .frame.double .spine{display:block}
    .sheet .running{position:absolute;top:34px;left:80px;right:80px;display:flex;justify-content:space-between;font-size:11px;color:#b0b0b0;letter-spacing:.06em}
    .sheet .content{width:660px;height:996px;overflow:hidden}
    .sheet .pageno{position:absolute;bottom:40px;left:0;right:0;text-align:center;font-size:12px;color:#888;letter-spacing:.12em}
    h1{font-size:30px;margin:0 0 12px;font-weight:600;letter-spacing:.02em}
    h2{font-size:20px;margin:0 0 16px;font-weight:600}
    p{font-size:18px;line-height:2;text-align:justify;margin:0 0 1em;text-indent:2em;color:#111}
    figure{margin:20px 0 24px;text-align:center}
    figure img{max-width:100%;max-height:620px;object-fit:contain;background:#fff}
    figcaption{font-size:12.5px;color:#555;margin-top:9px;line-height:1.7}
    figcaption .orig{color:#a0a0a0;margin-left:8px;font-size:11px}
    .titlepage{height:100%;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center}
    .titlepage .bk{font-size:13px;letter-spacing:.34em;color:#999}
    .titlepage h1{font-size:34px;line-height:1.5;margin:22px 0 26px;max-width:560px}
    .titlepage .meta{font-size:12px;color:#999;line-height:2.1}
    .nav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:44px;border-radius:50%;border:1px solid #ccc;background:#fff;color:#444;cursor:pointer;display:grid;place-items:center;box-shadow:0 2px 10px rgba(0,0,0,.08);z-index:5;font-size:20px}
    .nav:hover{background:#f7f7f7}
    .nav.prev{left:20px}
    .nav.next{right:20px}
    .nav:disabled{opacity:.25;cursor:default}
    .bottom{width:100%;padding:9px 0 13px;display:flex;flex-direction:column;align-items:center;gap:6px;background:#f4f4f4;border-top:1px solid #ddd}
    .counter{font-size:12px;color:#666;letter-spacing:.06em}
    .bar{width:280px;height:4px;background:#dcdcdc;border-radius:2px;overflow:hidden}
    .bar>i{display:block;height:100%;width:0;background:#444;transition:width .2s}
    .flow{position:absolute;left:-99999px;top:0;width:660px}
    .flow .content{width:660px;height:auto;overflow:visible}
    @media (max-width:900px){
      #bookBody{width:96vw;height:auto}
      .sheet{width:96vw;height:auto;min-height:70vh;padding:26px 22px 60px}
      .sheet .content{width:auto;height:auto;overflow:visible}
      .nav.prev{left:4px}.nav.next{right:4px}
    }
    """
    js = r"""
(function(){
  var blocks = JSON.parse(document.getElementById('blocks').textContent);
  var MAXH = 996;
  var stage = document.getElementById('stage');
  var flow = document.getElementById('flow');
  var body = document.getElementById('bookBody');
  var frame = document.getElementById('frame');
  var counter = document.getElementById('counter');
  var barFill = document.getElementById('barFill');
  var prevBtn = document.getElementById('prevBtn');
  var nextBtn = document.getElementById('nextBtn');
  var modeBtn = document.getElementById('modeBtn');
  var sheets = [];
  var cur = null;

  function newInner(){ var d = document.createElement('div'); d.className = 'content'; flow.appendChild(d); return d; }
  function newPage(){ cur = newInner(); return cur; }
  function makePara(text){ var p = document.createElement('p'); p.appendChild(document.createTextNode(text)); return p; }
  function makeBlock(b){
    if (b.type === 'heading'){ var h = document.createElement('h2'); h.textContent = b.text; return h; }
    if (b.type === 'paragraph'){ return makePara(b.text); }
    if (b.type === 'figure'){
      var fig = document.createElement('figure');
      var img = document.createElement('img'); img.src = b.src; img.alt = b.displayCaption || '';
      fig.appendChild(img);
      var cap = document.createElement('figcaption');
      cap.appendChild(document.createTextNode(b.displayCaption || ''));
      if (b.originalNumber){ var o = document.createElement('span'); o.className = 'orig'; o.textContent = '原书 ' + b.originalNumber; cap.appendChild(o); }
      fig.appendChild(cap);
      return fig;
    }
    var d = document.createElement('div'); d.textContent = b.text || ''; return d;
  }
  function over(){ return cur.scrollHeight > MAXH; }
  function findFit(text){
    var lo = 1, hi = text.length, best = 0;
    while (lo <= hi){
      var mid = (lo + hi) >> 1;
      var test = makePara(text.slice(0, mid));
      cur.appendChild(test);
      var bad = over();
      cur.removeChild(test);
      if (!bad){ best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return best;
  }
  function flowParagraph(text){
    var rest = text;
    while (rest.length){
      var el = makePara(rest);
      cur.appendChild(el);
      if (!over()) return;
      cur.removeChild(el);
      var fit = findFit(rest);
      if (fit <= 0){
        if (cur.childNodes.length === 0){ cur.appendChild(makePara(rest)); return; }
        newPage(); continue;
      }
      cur.appendChild(makePara(rest.slice(0, fit)));
      rest = rest.slice(fit);
      newPage();
    }
  }

  function build(){
    newPage();
    for (var i = 0; i < blocks.length; i++){
      var b = blocks[i];
      if (b.type === 'paragraph'){ flowParagraph(b.text); continue; }
      var el = makeBlock(b);
      cur.appendChild(el);
      if (over()){ cur.removeChild(el); newPage(); cur.appendChild(el); }
    }

    var inners = Array.prototype.slice.call(flow.children);
    var bookTitle = document.getElementById('bookTitle').textContent;
    var chapterTitle = document.getElementById('chapterTitle').textContent;
    inners.forEach(function(inner, idx){
      var sheet = document.createElement('div');
      sheet.className = 'sheet';
      var running = document.createElement('div'); running.className = 'running';
      var rl = document.createElement('span'); rl.textContent = bookTitle;
      var rr = document.createElement('span'); rr.textContent = chapterTitle;
      running.appendChild(rl); running.appendChild(rr);
      var holder = document.createElement('div'); holder.className = 'content'; holder.appendChild(inner);
      var pageno = document.createElement('div'); pageno.className = 'pageno'; pageno.textContent = '— ' + (idx + 1) + ' —';
      sheet.appendChild(running); sheet.appendChild(holder); sheet.appendChild(pageno);
      body.appendChild(sheet); sheets.push(sheet);
    });

    var titleSheet = document.createElement('div');
    titleSheet.className = 'sheet';
    titleSheet.innerHTML = '<div class="titlepage"><div class="bk">' + bookTitle + '</div><h1 id="tpTitle"></h1><div class="meta" id="tpMeta"></div></div>';
    titleSheet.querySelector('#tpTitle').textContent = chapterTitle;
    titleSheet.querySelector('#tpMeta').textContent = document.getElementById('metaLine').textContent;
    body.insertBefore(titleSheet, body.firstChild);
    sheets.unshift(titleSheet);

    var blank = document.createElement('div');
    blank.className = 'sheet blank';
    body.insertBefore(blank, body.firstChild);
    sheets.unshift(blank);   // sheets[0] = 空白页（与扉页组成第一跨页）

    prepare();
  }

  var N = 0, double = window.innerWidth >= 1500, anchor = 0;

  function pagesFor(n){
    if (!double) return [n];
    if (n <= 1) return [0, 1];               // 空白页 + 扉页
    var a = (n % 2 === 0) ? n : n - 1;       // 内容页从 sheets[2] 开始，跨页锚点为偶数
    var b = a + 1;
    return (b < N) ? [a, b] : [a];
  }
  function spreadAnchor(n){
    if (!double) return n;
    if (n <= 1) return 0;
    return (n % 2 === 0) ? n : n - 1;
  }
  function prepare(){
    N = sheets.length;
    fit();
    show(0);
  }
  function show(n){
    anchor = Math.max(0, Math.min(N - 1, spreadAnchor(n)));
    var vis = pagesFor(anchor);
    sheets.forEach(function(s){ s.classList.remove('is-active', 'left', 'right'); s.style.display = 'none'; });
    var shown = vis.filter(function(i){ return i !== null && sheets[i]; });
    shown.forEach(function(i, k){
      var s = sheets[i];
      s.classList.add('is-active');
      if (double) s.classList.add(k === 0 ? 'left' : 'right');
      s.style.display = 'block';
    });
    body.classList.toggle('double', double);
    frame.classList.toggle('double', double);
    fit();
    var label;
    function pageLabel(i){ return i <= 1 ? '扉页' : ('第 ' + (i - 1) + ' 页'); }
    if (!double || shown.length === 1){
      label = pageLabel(shown[0]) + ' / 共 ' + (N - 2) + ' 页';
    } else {
      label = (shown[0] === 0 ? '扉页' : (pageLabel(shown[0]) + ' – ' + pageLabel(shown[1]))) + '  / 共 ' + (N - 2) + ' 页';
    }
    counter.textContent = label;
    var total = Math.max(1, N - 1);
    barFill.style.width = Math.min(100, anchor / total * 100) + '%';
    prevBtn.disabled = anchor === 0;
    nextBtn.disabled = double ? (anchor === 0 ? N <= 2 : (anchor + 2 > N - 1)) : (anchor >= N - 1);
  }
  function fit(){
    var stageH = stage.clientHeight - 22;
    var stageW = stage.clientWidth - (double ? 70 : 150);
    var baseW = double ? 1640 : 820;
    var k = Math.min(stageH / 1160, stageW / baseW, 1);
    if (!(k > 0)) k = 1;
    body.style.width = baseW + 'px';
    frame.style.width = (baseW * k) + 'px';
    frame.style.height = (1160 * k) + 'px';
    body.style.transform = 'scale(' + k + ')';
  }

  function next(){
    if (!double) return show(anchor + 1);
    show(anchor === 0 ? 2 : Math.min(N - 1, anchor + 2));
  }
  function prev(){
    if (!double) return show(anchor - 1);
    show(anchor <= 2 ? 0 : anchor - 2);
  }

  prevBtn.addEventListener('click', prev);
  nextBtn.addEventListener('click', next);
  modeBtn.addEventListener('click', function(){
    double = !double;
    modeBtn.textContent = double ? '双页' : '单页';
    show(anchor);
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'ArrowLeft' || e.key === 'PageUp'){ prev(); e.preventDefault(); }
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' '){ next(); e.preventDefault(); }
  });
  stage.addEventListener('click', function(e){
    if (e.target.closest('.nav') || e.target.closest('.mode')) return;
    var r = stage.getBoundingClientRect();
    if (e.clientX - r.left < r.width * 0.25) prev();
    else if (e.clientX - r.left > r.width * 0.75) next();
  });
  window.addEventListener('resize', function(){ fit(); });

  var srcs = [];
  blocks.forEach(function(b){ if (b.type === 'figure') srcs.push(b.src); });
  Promise.all(srcs.map(function(s){
    return new Promise(function(res){ var im = new Image(); im.onload = res; im.onerror = res; im.src = s; });
  })).then(build);
})();
"""
    meta_line = (f"原书 p.{book['pageRange'][0]}–{book['pageRange'][1]} · 正文 {book['stats']['chars']} 字 · "
                 f"插图 {book['stats']['figures']} 幅 · 图注配对 {book['stats']['paired']}")
    html = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{book['title']} · 翻页阅读</title><style>{css}</style></head><body>"
        "<div class=\"topbar\"><span><b id=\"bookTitle\">" + book["bookTitle"] + "</b>"
        " · 统一排版样章</span><span class=\"spacer\"></span>"
        "<button class=\"mode\" id=\"modeBtn\">双页</button>"
        "<span>← → 翻页</span></div>"
        "<div class=\"stage\" id=\"stage\">"
        "<button class=\"nav prev\" id=\"prevBtn\" aria-label=\"上一页\">‹</button>"
        "<div class=\"frame\" id=\"frame\"><div id=\"bookBody\"></div><div class=\"spine\"></div></div>"
        "<button class=\"nav next\" id=\"nextBtn\" aria-label=\"下一页\">›</button>"
        "</div>"
        "<div class=\"bottom\"><div class=\"counter\" id=\"counter\"></div>"
        "<div class=\"bar\"><i id=\"barFill\"></i></div></div>"
        f"<div id=\"chapterTitle\" style=\"display:none\">{book['title']}</div>"
        f"<div id=\"metaLine\" style=\"display:none\">{meta_line}</div>"
        "<div class=\"flow\" id=\"flow\"></div>"
        "<script id=\"blocks\" type=\"application/json\">" + blocks_json + "</script>"
        "<script>" + js + "</script>"
        "</body></html>"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
